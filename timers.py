"""Bron xabarlari va to'lov taymeri.

Sayt to'lanmagan bronni 12 daqiqa saqlaydi (aniq vaqt — payment-end-time). Har bir bron uchun bitta vazifa
uning barcha bot xabarlarini (foydalanuvchida va bot egasida) har 10 soniyada yangilab turadi, vaqti-vaqti
bilan saytdan to'langanini tekshiradi va muddat tugaganda holatni yakunlaydi.

Boshqa foydalanuvchining oddiy broni (record["auto_ticket"]): u «💳 Saytda to'lash» tugmasi bilan saytni bot
egasining akkaunti orqali ochib, o'z kartasidan to'laydi; to'lov saytda o'tgach chipta (PDF) avtomatik yuboriladi.

Kuzatuv orqali bron xizmat haqi bilan (record["fee_due"]): chipta pulini foydalanuvchi o'zi saytda to'laydi,
xizmat haqini esa bot egasining kartasiga o'tkazib «Xizmat haqini o'tkazdim» ni bosadi (record["fee_reported"]).

Eski bronlar (record["approval"] = "pending") bot egasining tasdig'ini kutadi:
foydalanuvchiga bot egasining karta raqami (ilova → Profil) va chipta summasi yuboriladi; pul kelgach egasi
o'z akkauntidan saytda to'laydi va "✅ Tasdiqlash" ni bosadi — chipta (PDF) foydalanuvchiga yuboriladi
(approval = "approved"). "❌ Rad etish" — bekor qiladi. Foydalanuvchiga to'lov havolasi va bekor qilish
tugmasi ko'rsatilmaydi. Bot qayta ishga tushsa, resume_all() taymerlarni tiklaydi.
"""
import asyncio
import logging
import time
from datetime import datetime

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup

import booking
import site_proxy
from site_auth import SiteSession
from users import UserStore
from uzrail import UzRailClient, UzRailError

TICK = 10  # sekund
PAY_CHECK_EVERY = 30  # sekund: saytdan "to'landimi" tekshiruvi
PDF_TRIES, PDF_RETRY_DELAY = 3, 3  # chipta PDF i to'lovdan so'ng darhol tayyor bo'lmasligi mumkin
AUTO_TRIES, AUTO_RETRY_DELAY = 10, 30  # avtomatik yuborish: PDF tayyor bo'lmasa shuncha marta qayta urinadi
SEND_TRIES, SEND_RETRY_DELAY = 3, 5  # bron xabari: internet bir lahza uzilsa qayta urinamiz
FINISH_TRIES, FINISH_RETRY_DELAY = 10, 30  # muddat tugagach sayt javob bermasa — shuncha marta qayta so'raladi
RESUME_MAX_AGE = 24 * 3600  # qayta ishga tushganda: muddati shundan oldin tugagan bronlar tiklanmaydi
log = logging.getLogger(__name__)


def left_text(expires_at: float) -> str:
    left = max(0, int(expires_at - time.time()))
    return f"{left // 60:02d}:{left % 60:02d}"


def booking_message(record: dict) -> str:
    """Bron xabari matni (taymer qatorisiz)."""
    return "✅ Bron qilindi!\n" + booking.booking_text(record)


def is_paid(lifecycle: str | None) -> bool:
    """Saytdagi holat to'langanmi. Ma'lum to'lanmagan holat — RESERVED; to'langanining aniq nomi noma'lum,
    shuning uchun faqat aniq so'zlar (PAID / COMPLETED) — "RESERVATION_SUCCEEDED" kabilar to'lov emas."""
    return bool(lifecycle) and "UNPAID" not in lifecycle and any(s in lifecycle for s in ("PAID", "COMPLET"))


def order_state(order: dict) -> str | None:
    """Buyurtmaning saytdagi holati (lifecycle). Chiptalari to'langan bo'lsa — "PAID": sayt kodidagi chipta
    holatlari RESERVED / PAID / CONFIRMED / FAILED, buyurtma lifecycle ining to'langan nomi esa ma'lum emas."""
    lifecycle = order.get("lifecycle")
    if lifecycle not in ("CANCELLED", "FAILED") and any(
            item.get("status") in ("PAID", "CONFIRMED") for item in order.get("items") or []):
        return "PAID"
    return lifecycle


class BookingTimers:
    def __init__(self, bot: Bot, users: UserStore, client: UzRailClient, session: SiteSession, owner_id: int):
        self.bot, self.users, self.client, self.session, self.owner_id = bot, users, client, session, owner_id
        self._tasks: dict[str, asyncio.Task] = {}
        self.pay_base: str | None = None  # ilova manzili — bot egasining "Saytda to'lash" tugmasi uchun (main() da)

    # ---------- xabar ko'rinishi ----------

    def _for_user(self, record: dict, chat_id: int) -> bool:
        """Foydalanuvchi ko'rinishi: bron egasi tasdig'ini kutadi va xabar bot egasiga emas."""
        return bool(record.get("approval")) and chat_id != self.owner_id

    def card(self) -> dict | None:
        """Bot egasining kartasi (ilova → Profil): {"number", "holder"} yoki None."""
        owner = self.users.get(self.owner_id)
        if not owner.get("card_number"):
            return None
        return {"number": owner["card_number"], "holder": owner.get("card_holder", "")}

    @staticmethod
    def awaiting_delivery(record: dict) -> bool:
        """Saytda to'langan, lekin bot egasi hali tasdiqlab chiptani yubormagan."""
        return record.get("status") == "paid" and record.get("approval") == "pending"

    def view(self, record: dict, chat_id: int, headline: str = "✅ Bron qilindi!"):
        """Faol bron xabari (matn, tugmalar) — kim ko'rayotganiga qarab."""
        until = datetime.fromtimestamp(record["expires_at"]).strftime("%H:%M") if record.get("expires_at") else ""
        left = f"{left_text(record['expires_at'])} ({until} gacha)" if until else ""
        approval = record.get("approval")
        card = self.card()
        if self._for_user(record, chat_id):
            reported = record.get("transfer_reported")
            if approval == "approved":  # eski bronlar: tasdiq to'lovdan oldin bo'lgan
                head = "✅ Bot egasi tasdiqladi — to'lov kutilmoqda"
            elif reported:
                head = "📨 To'lov haqida bot egasiga xabar berildi — u tekshirib, chiptani to'laydi"
            elif card:
                head = "📨 Bron qilindi — chipta pulini bot egasining kartasiga o'tkazing"
            else:
                head = "📨 Bron qilindi — bot egasi tasdiqlab to'lashini kuting"
            pay = f"\n\n{booking.card_text(record, card)}" if card and approval == "pending" else ""
            tail = f"\n\n⏳ Bron saqlanadi: {left} — pulni shu vaqtgacha o'tkazing" if left and pay and not reported else (
                f"\n\n⏳ Bron saqlanadi: {left}" if left else "")
            markup = booking.transfer_markup(record["order_id"]) if pay and not reported else None
            return f"{head}\n{booking.booking_text(record, with_pay=False)}{pay}{tail}", markup
        tail = f"\n\n⏳ To'lov uchun qoldi: {left}" if left else ""
        # saytni bot egasining akkaunti bilan ochadigan tugma (ilova manzili bo'lsa), aks holda oddiy havola.
        # Bot egasi to'laydigan (approval) bronlarda u faqat egasiga ko'rsatiladi.
        pay_url = (site_proxy.pay_link(self.pay_base, self.bot.token, record["order_id"], record.get("expires_at"))
                   if self.pay_base and (chat_id == self.owner_id or not approval) else None)
        text = booking.booking_text(record, pay_button=bool(pay_url))

        def markup(base):
            return booking.with_pay_button(base, pay_url) if pay_url else base

        if approval == "pending":
            who = record.get("by") or "foydalanuvchi"
            then = ("To'lov saytda o'tgach shu yerda «✅ Tasdiqlash» tugmasi chiqadi — bossangiz, chipta (PDF) "
                    "egasiga yuboriladi.")
            step = (f"Unga karta raqamingiz va summa yuborildi. Pul kelgach «💳 Saytda to'lang».\n{then}" if card else
                    "⚠️ Karta raqamingiz kiritilmagan (Ilova → Profil) — foydalanuvchiga yuborilmadi.\n"
                    f"«💳 Saytda to'lang».\n{then}")
            if record.get("transfer_reported"):
                at = datetime.fromtimestamp(record["transfer_reported"]).strftime("%H:%M")
                step = (f"💸 {at} da pulni o'tkazganini bildirdi — kartangizni tekshirib, «💳 Saytda to'lang».\n"
                        f"{then}")
            head = f"🔔 {who} bron qildi.\n{step}"
            return f"{head}\n{text}{tail}", markup(booking.approval_markup(record["order_id"]))
        if approval == "approved":
            head = f"✅ Tasdiqlangan ({record.get('by') or 'foydalanuvchi'}) — saytda to'lang:"
            return f"{head}\n{text}{tail}", markup(booking.cancel_markup(record["order_id"]))
        buttons = booking.cancel_markup(record["order_id"])
        if record.get("by") and chat_id == self.owner_id:  # boshqa foydalanuvchi o'zi to'laydi
            headline = (f"{headline}\n🔔 {record['by']} bron qildi — chipta pulini o'zi saytda to'laydi, "
                        "chipta (PDF) unga avtomatik yuboriladi.")
            if record.get("fee_due"):
                headline += "\n" + self.fee_status(record)
        elif record.get("auto_ticket"):
            text += "\nTo'lov o'tgach chipta (PDF) shu yerga avtomatik yuboriladi."
            if booking.fee_due(record):
                text += "\n\n" + booking.fee_text(record, card)
                if card:
                    buttons = InlineKeyboardMarkup(inline_keyboard=[
                        *booking.transfer_markup(record["order_id"], booking.FEE_BUTTON).inline_keyboard,
                        *buttons.inline_keyboard])
            elif record.get("fee_reported"):
                text += "\n💼 Xizmat haqi o'tkazilgani haqida bot egasiga xabar berildi."
        return f"{headline}\n{text}{tail}", markup(buttons)

    def fee_status(self, record: dict) -> str:
        """Bot egasiga: xizmat haqi holati."""
        fee = booking.som(record.get("service_fee") or 0)
        if record.get("fee_reported"):
            at = datetime.fromtimestamp(record["fee_reported"]).strftime("%H:%M")
            return f"💸 Xizmat haqini ({fee}) {at} da kartangizga o'tkazganini bildirdi — tekshiring."
        if not self.card():
            return f"⚠️ Xizmat haqi {fee}: karta raqamingiz kiritilmagan (Ilova → Profil) — foydalanuvchiga ko'rsatilmadi."
        return f"💼 Xizmat haqi {fee} kartangizga o'tkazilishi kutilmoqda."

    def final_text(self, record: dict, chat_id: int) -> str | None:
        """Yakunlangan bron xabari (bekor qilingan / to'langan / muddati tugagan) yoki None."""
        status, base = record.get("status"), booking.booking_text(record, with_pay=False)
        user = self._for_user(record, chat_id)
        if status == "cancelled" and record.get("approval") == "rejected":
            head = "❌ Bot egasi bronni rad etdi — bron bekor qilindi." if user else "❌ RAD ETILDI — bron bekor qilindi."
        elif status == "cancelled":
            head = "❌ BRON BEKOR QILINDI"
        elif status == "failed":
            head = "⚠️ SAYTDA AMALGA OSHMAGAN"
        elif status == "paid":
            sent = record.get("approval") == "approved"
            if user:
                head = ("✅ TO'LANDI! Chipta yuborildi." if sent
                        else "✅ To'landi — bot egasi tasdiqlagach chipta shu yerga yuboriladi.")
            elif self.awaiting_delivery(record):
                head = (f"✅ TO'LANGAN — «✅ Tasdiqlash» ni bosing, chipta egasiga "
                        f"({record.get('by') or 'foydalanuvchi'}) yuboriladi.")
            elif record.get("auto_ticket"):
                to = f" ({record['by']})" if record.get("by") and chat_id == self.owner_id else ""
                head = (f"✅ TO'LANDI! Chipta (PDF) yuborildi{to}." if record.get("ticket_sent")
                        else f"✅ TO'LANDI — chipta (PDF) tayyorlanmoqda{to}...")
                if record.get("fee_due") and to:
                    head += "\n" + self.fee_status(record)
                elif booking.fee_due(record):
                    return f"{head}\n{base}\n\n{booking.fee_text(record, self.card())}"
            else:
                head = "✅ TO'LANGAN — chipta egasiga yuborildi." if sent else "✅ TO'LANGAN"
        elif status == "expired":
            head = ("⌛ Bot egasi muddatida to'lamadi — bron bekor bo'ldi, joylar bo'shatildi." if user
                    else "⌛ BRON MUDDATI TUGADI — to'lanmadi, joylar bo'shatildi.")
        else:
            return None
        return f"{head}\n{base}"

    # ---------- yuborish ----------

    async def announce(self, user_id: int, record: dict, headline: str = "✅ Bron qilindi!", name: str | None = None):
        """Yangi bronni saqlaydi va foydalanuvchi hamda bot egasiga xabar beradi (taymer bilan).

        Bron bot egasiniki bo'lmasa — egasining tasdig'ini kutadi (approval="pending").
        """
        if name:
            self.users.update(user_id, name=name)
        if user_id != self.owner_id:
            profile = self.users.get(user_id)
            phone = f" (+{profile['phone']})" if profile.get("phone") else ""
            record = {**record, "by": f"{self.users.display_name(user_id) or user_id}{phone}"}
            if record.get("service_fee"):  # xizmat haqi bot egasining kartasiga o'tkaziladi
                record["fee_due"] = True
        if not record.get("approval"):  # to'lov saytda o'tgach chipta (PDF) avtomatik yuboriladi
            record = {**record, "auto_ticket": True}
        self.users.add_booking(user_id, record)
        # Taymer xabarlardan qat'i nazar ishlaydi: xabar yuborilmasa ham to'lov va muddat kuzatiladi
        self.ensure_timer(record["order_id"])
        for chat_id in dict.fromkeys([user_id, self.owner_id]):
            text, markup = self.view(record, chat_id, headline)
            for attempt in range(SEND_TRIES):
                try:
                    sent = await self.bot.send_message(chat_id, text, parse_mode=None, reply_markup=markup)
                    self.start(chat_id, sent.message_id, record["order_id"])
                    break
                except Exception:
                    if attempt + 1 == SEND_TRIES:
                        log.exception("Bron xabari yuborilmadi: %s", chat_id)
                    else:
                        log.warning("Bron xabari yuborilmadi (%s), qayta urinish...", chat_id)
                        await asyncio.sleep(SEND_RETRY_DELAY)
        return record

    async def _ping_user(self, record_owner: int, text: str):
        """Foydalanuvchiga qisqa bildirishnoma (xabarni tahrirlash ovozsiz bo'ladi)."""
        if record_owner != self.owner_id:
            try:
                await self.bot.send_message(record_owner, text, parse_mode=None)
            except Exception:
                log.exception("Foydalanuvchiga xabar yuborilmadi: %s", record_owner)

    async def send_card(self, order_id: str) -> bool:
        """Foydalanuvchiga bot egasining karta raqami va summani alohida xabar (bildirishnoma) qilib yuboradi."""
        found, card = self.users.find_booking(order_id), self.card()
        if not found or not card:
            return False
        uid, record = found
        markup = None if record.get("transfer_reported") else booking.transfer_markup(order_id)
        await self.bot.send_message(uid, f"{booking.card_text(record, card)}\n\nBuyurtma: {order_id}",
                                    parse_mode=None, reply_markup=markup)
        return True

    async def report_transfer(self, order_id: str, user_id: int) -> bool:
        """Foydalanuvchi "To'lov qildim" ni bosdi: bot egasiga xabar (Saytda to'lash tugmasi bilan).

        False — bron topilmadi, unga tegishli emas, yakunlangan yoki allaqachon xabar berilgan.
        """
        found = self.users.find_booking(order_id)
        if not found:
            return False
        uid, record = found
        if uid != user_id:
            return False
        if booking.fee_due(record):  # xizmat haqi o'tkazildi
            self.users.update_booking(uid, order_id, fee_reported=time.time())
            record = self.users.find_booking(order_id)[1]
            try:
                await self.bot.send_message(
                    self.owner_id, f"💸 {record.get('by') or 'Foydalanuvchi'} xizmat haqini "
                                   f"({booking.som(record.get('service_fee') or 0)}) kartangizga o'tkazganini "
                                   f"bildirdi.\nBuyurtma: {order_id}\nKartangizni tekshiring.", parse_mode=None)
            except Exception:
                log.exception("Bot egasiga xizmat haqi xabari yuborilmadi: %s", order_id)
            if record.get("status"):
                await self.finalize_messages(order_id)
            else:
                await self.refresh(order_id)
            return True
        if record.get("approval") != "pending" or record.get("status") or record.get("transfer_reported"):
            return False
        self.users.update_booking(uid, order_id, transfer_reported=time.time())
        record = self.users.find_booking(order_id)[1]
        pay_url = (site_proxy.pay_link(self.pay_base, self.bot.token, order_id, record.get("expires_at"))
                   if self.pay_base else None)
        markup = booking.with_pay_button(InlineKeyboardMarkup(inline_keyboard=[]), pay_url) if pay_url else None
        price = booking.som(booking.total_price(record))
        try:
            await self.bot.send_message(
                self.owner_id, f"💸 {record.get('by') or 'Foydalanuvchi'} chipta pulini (≈ {price}) kartangizga "
                               f"o'tkazganini bildirdi.\nBuyurtma: {order_id}\n"
                               "Kartangizni tekshirib, saytda to'lang — so'ng «✅ Tasdiqlash» tugmasi chiqadi.",
                parse_mode=None, reply_markup=markup)
        except Exception:
            log.exception("Bot egasiga to'lov xabari yuborilmadi: %s", order_id)
        await self.refresh(order_id)
        return True

    async def deliver(self, order_id: str) -> bool:
        """To'langan chiptani (PDF) egasiga yuboradi va bronni yakunlaydi.

        False — sayt PDF ni bermadi (to'lovdan so'ng biroz kechikishi mumkin): hech narsa yuborilmaydi, bron
        o'zgarmaydi — "Tasdiqlash" ni keyinroq qayta bosish mumkin. Telegram yoki sayt xatosi chaqiruvchiga o'tadi.
        """
        found = self.users.find_booking(order_id)
        if not found:
            return False
        uid, record = found
        pdf = None
        for attempt in range(PDF_TRIES):
            pdf = await self.client.ticket_pdf(await self.session.get_token(), order_id)
            if pdf:
                break
            if attempt + 1 < PDF_TRIES:
                await asyncio.sleep(PDF_RETRY_DELAY)
        if not pdf:
            return False
        caption = "🎫 Chiptangiz\n" + booking.booking_text(record, with_pay=False)
        if uid != self.owner_id and booking.fee_due(record):
            caption += "\n\n" + booking.fee_text(record, self.card())
        await self.bot.send_document(uid, BufferedInputFile(pdf, filename=f"chipta-{order_id}.pdf"),
                                     caption=caption[:1024], parse_mode=None)
        if record.get("approval"):
            self.users.update_booking(uid, order_id, approval="approved", status="paid", status_at=time.time())
        else:
            self.users.update_booking(uid, order_id, ticket_sent=time.time(), status="paid", status_at=time.time())
        await self.finalize(order_id)
        return True

    @staticmethod
    def needs_auto_delivery(record: dict) -> bool:
        return bool(record.get("auto_ticket")) and record.get("status") == "paid" and not record.get("ticket_sent")

    def ensure_delivery(self, order_id: str):
        """To'langan auto_ticket bron: chipta (PDF) fonda yuboriladi (bir vaqtda bitta vazifa)."""
        key = "pdf:" + order_id
        task = self._tasks.get(key)
        if not task or task.done():
            self._tasks[key] = asyncio.create_task(self._auto_deliver(order_id))

    async def _auto_deliver(self, order_id: str):
        for attempt in range(AUTO_TRIES):
            found = self.users.find_booking(order_id)
            if not found or not self.needs_auto_delivery(found[1]):
                return
            try:
                if await self.deliver(order_id):
                    return
                log.info("Bron %s: chipta PDF hali tayyor emas", order_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Chipta avtomatik yuborilmadi: %s", order_id)
            if attempt + 1 < AUTO_TRIES:
                await asyncio.sleep(AUTO_RETRY_DELAY)
        found = self.users.find_booking(order_id)
        if found and self.needs_auto_delivery(found[1]):
            try:
                await self.bot.send_message(
                    self.owner_id, f"⚠️ To'langan chipta (PDF) avtomatik yuborilmadi: {order_id} "
                                   f"({found[1].get('by') or 'sizniki'}). Saytdan yuklab, egasiga yuboring.",
                    parse_mode=None)
            except Exception:
                log.exception("Bot egasiga xabar yuborilmadi: %s", order_id)

    async def finalize_messages(self, order_id: str):
        """Yakunlangan bronning bot xabarlarini qayta yozadi (bildirishnomalarsiz)."""
        found = self.users.find_booking(order_id)
        if not found:
            return
        uid, record = found
        for chat_id, message_id in record.get("messages", []):
            text = self.final_text(record, chat_id)
            if text:  # to'langan, lekin chipta hali yuborilmagan — bot egasida "Tasdiqlash" tugmasi qoladi
                markup = (booking.approval_markup(order_id, paid=True)
                          if chat_id == self.owner_id and self.awaiting_delivery(record) else None)
                if chat_id == uid and chat_id != self.owner_id and booking.fee_due(record) and self.card():
                    markup = booking.transfer_markup(order_id, booking.FEE_BUTTON)
                await self._edit(chat_id, message_id, text, markup)

    async def finalize(self, order_id: str):
        """Holat o'rnatilgandan keyin (bekor/rad/to'landi/muddati tugadi) barcha xabarlarni yakunlaydi."""
        found = self.users.find_booking(order_id)
        if not found:
            return
        uid, record = found
        await self.finalize_messages(order_id)
        if self.awaiting_delivery(record):  # tahrirlash ovozsiz — bot egasiga yangi xabar (tugmasi bilan)
            try:
                sent = await self.bot.send_message(
                    self.owner_id, f"💰 To'lov o'tdi: {record['order_id']} ({record.get('by') or 'foydalanuvchi'}).\n"
                                   "«✅ Tasdiqlash» ni bosing — chipta (PDF) egasiga yuboriladi.",
                    parse_mode=None, reply_markup=booking.approval_markup(order_id, paid=True))
                self.users.add_booking_message(order_id, self.owner_id, sent.message_id)
            except Exception:
                log.exception("Bot egasiga to'lov xabari yuborilmadi: %s", order_id)
        if self.needs_auto_delivery(record):
            self.ensure_delivery(order_id)
        if record.get("approval"):
            note = self.final_text(record, uid)
            if note:
                await self._ping_user(uid, note.split("\n")[0])

    async def refresh(self, order_id: str):
        found = self.users.find_booking(order_id)
        if not found or found[1].get("status"):
            return
        record = found[1]
        for chat_id, message_id in record.get("messages", []):
            text, markup = self.view(record, chat_id)
            await self._edit(chat_id, message_id, text, markup)

    # ---------- taymer ----------

    def start(self, chat_id: int, message_id: int, order_id: str, remember: bool = True):
        """Xabarni bronga bog'laydi va bron taymerini (bitta vazifa) ishga tushiradi."""
        if remember:
            self.users.add_booking_message(order_id, chat_id, message_id)
        self.ensure_timer(order_id)

    def ensure_timer(self, order_id: str):
        """Bron taymeri (bitta vazifa) ishlamayotgan bo'lsa ishga tushiradi."""
        task = self._tasks.get(order_id)
        if not task or task.done():
            self._tasks[order_id] = asyncio.create_task(self._run(order_id))

    def resume_all(self):
        """Bot qayta ishga tushganda: yakunlanmagan bronlar taymerlarini tiklaydi (xabari yuborilmaganlarini ham)."""
        for uid in self.users.ids():
            for b in self.users.bookings(uid):
                if (b.get("expires_at") and not b.get("status")
                        and b["expires_at"] > time.time() - RESUME_MAX_AGE):
                    self.ensure_timer(b["order_id"])
                elif self.needs_auto_delivery(b) and (b.get("status_at") or 0) > time.time() - RESUME_MAX_AGE:
                    self.ensure_delivery(b["order_id"])

    async def _edit(self, chat_id: int, message_id: int, text: str, markup=None):
        try:
            await self.bot.edit_message_text(text, chat_id=chat_id, message_id=message_id,
                                             parse_mode=None, reply_markup=markup)
        except TelegramBadRequest as e:
            if "not modified" not in str(e):
                log.warning("Bron xabarini yangilab bo'lmadi (%s/%s): %s", chat_id, message_id, e)

    async def lifecycle(self, order_id: str) -> str | None:
        """Saytdagi holat (order_state): lifecycle yoki chiptalar to'langan bo'lsa "PAID"."""
        try:
            order = await self.client.order_snapshot(await self.session.get_token(), order_id)
        except UzRailError:
            return None
        state = order_state(order)
        if state == "PAID" and not is_paid(order.get("lifecycle")):  # to'langan lifecycle nomini bilish uchun
            log.info("Bron %s chiptalari to'langan, lifecycle: %s", order_id, order.get("lifecycle"))
        return state

    async def _run(self, order_id: str):
        last_check = time.time()
        try:
            while True:
                found = self.users.find_booking(order_id)
                if not found:
                    return
                uid, record = found
                if record.get("status"):  # bekor qilingan / to'langan — xabarlarni boshqa joy yakunlagan
                    return
                left = record["expires_at"] - time.time()
                if left <= 0:
                    break
                if time.time() - last_check >= PAY_CHECK_EVERY:
                    last_check = time.time()
                    lifecycle = await self.lifecycle(order_id)
                    if lifecycle not in (None, "RESERVED"):
                        log.info("Bron %s saytdagi holati: %s", order_id, lifecycle)  # to'lov holati nomini bilish uchun
                    if is_paid(lifecycle):
                        self.users.set_status(uid, order_id, "paid")
                        await self.finalize(order_id)
                        return
                    if lifecycle in ("CANCELLED", "FAILED"):  # saytning o'zida bekor qilingan
                        self.users.set_status(uid, order_id, "cancelled" if lifecycle == "CANCELLED" else "failed")
                        await self.finalize(order_id)
                        return
                await self.refresh(order_id)
                await asyncio.sleep(min(TICK, left))
            await self._finish(order_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Taymer xatosi: %s", order_id)

    async def _finish(self, order_id: str):
        """Muddat tugadi: saytdagi holatni tekshirib, barcha xabarlarni yakunlaydi."""
        await asyncio.sleep(5)  # sayt holatni yangilashi uchun
        found = self.users.find_booking(order_id)
        if not found or found[1].get("status"):
            return
        uid, record = found
        # Sayt javob bermasa (None) — taxmin qilmaymiz: oxirgi daqiqada to'langan bo'lishi mumkin
        lifecycle = None
        for attempt in range(FINISH_TRIES):
            lifecycle = await self.lifecycle(order_id)
            if lifecycle is not None:
                break
            if attempt + 1 < FINISH_TRIES:
                log.warning("Bron %s holati olinmadi, %s s dan keyin qayta so'raladi", order_id, FINISH_RETRY_DELAY)
                await asyncio.sleep(FINISH_RETRY_DELAY)
        log.info("Bron %s muddati tugadi, saytdagi holat: %s", order_id, lifecycle)
        found = self.users.find_booking(order_id)  # kutish paytida boshqa joyda yakunlangan bo'lishi mumkin
        if not found or found[1].get("status"):
            return
        if is_paid(lifecycle):
            self.users.set_status(uid, order_id, "paid")
        elif lifecycle in (None, "CANCELLED", "EXPIRED", "FAILED"):  # None — ~5 daqiqa davomida sayt javob bermadi
            self.users.set_status(uid, order_id, "expired")
        else:  # noma'lum holat — taxmin qilmaymiz
            for chat_id, message_id in record.get("messages", []):
                await self._edit(chat_id, message_id, f"ℹ️ To'lov muddati tugadi. Saytdagi holat: {lifecycle}\n"
                                                      f"{booking.booking_text(record, with_pay=False)}")
            return
        await self.finalize(order_id)
