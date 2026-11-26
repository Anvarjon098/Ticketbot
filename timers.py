"""Bron xabarlari va to'lov taymeri.

Sayt to'lanmagan bronni 12 daqiqa saqlaydi (aniq vaqt — payment-end-time). Har bir bron uchun bitta vazifa
uning bot xabarini har 10 soniyada yangilab turadi, vaqti-vaqti bilan saytdan to'langanini tekshiradi va muddat
tugaganda holatni yakunlaydi.

Har bir foydalanuvchi o'z sayt akkaunti bilan bron qiladi va o'zi to'laydi: xabar ostidagi «💳 Saytda to'lash»
tugmasi saytni uning akkaunti bilan ochadi. To'lov o'tgach chipta (PDF) avtomatik yuboriladi.
Bot qayta ishga tushsa, resume_all() taymerlarni tiklaydi.
"""
import asyncio
import logging
import time
from datetime import datetime

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile

import booking
import site_proxy
from site_auth import SessionStore
from users import UserStore
from uzrail import UzRailClient, UzRailError

TICK = 10  # sekund
PAY_CHECK_EVERY = 30  # sekund: saytdan "to'landimi" tekshiruvi
PDF_TRIES, PDF_RETRY_DELAY = 3, 3  # chipta PDF i to'lovdan so'ng darhol tayyor bo'lmasligi mumkin
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
    def __init__(self, bot: Bot, users: UserStore, client: UzRailClient, sessions: SessionStore, owner_id: int):
        self.bot, self.users, self.client, self.sessions, self.owner_id = bot, users, client, sessions, owner_id
        self._tasks: dict[str, asyncio.Task] = {}
        self.pay_base: str | None = None  # ilova manzili — "Saytda to'lash" tugmasi uchun (main() da)

    def session_for(self, order_id: str):
        """Bron qilgan foydalanuvchining sayt akkaunti (to'lov, holat, PDF shu akkaunt bilan)."""
        found = self.users.find_booking(order_id)
        return self.sessions.get(found[0] if found else self.owner_id)

    # ---------- xabar ko'rinishi ----------

    def pay_url(self, record: dict) -> str | None:
        if not self.pay_base:
            return None
        return site_proxy.pay_link(self.pay_base, self.bot.token, record["order_id"], record.get("expires_at"))

    def view(self, record: dict, chat_id: int, headline: str = "✅ Bron qilindi!"):
        """Faol bron xabari (matn, tugmalar): «Saytda to'lash» va «Bronni bekor qilish»."""
        until = datetime.fromtimestamp(record["expires_at"]).strftime("%H:%M") if record.get("expires_at") else ""
        tail = f"\n\n⏳ To'lov uchun qoldi: {left_text(record['expires_at'])} ({until} gacha)" if until else ""
        pay_url = self.pay_url(record)
        text = booking.booking_text(record, pay_button=bool(pay_url))
        markup = booking.cancel_markup(record["order_id"])
        if pay_url:
            markup = booking.with_pay_button(markup, pay_url)
        return f"{headline}\n{text}{tail}", markup

    def final_text(self, record: dict, chat_id: int) -> str | None:
        """Yakunlangan bron xabari (bekor qilingan / to'langan / muddati tugagan) yoki None."""
        status, base = record.get("status"), booking.booking_text(record, with_pay=False)
        heads = {"cancelled": "❌ BRON BEKOR QILINDI", "failed": "⚠️ SAYTDA AMALGA OSHMAGAN",
                 "paid": "✅ TO'LANGAN", "expired": "⌛ BRON MUDDATI TUGADI — to'lanmadi, joylar bo'shatildi."}
        return f"{heads[status]}\n{base}" if status in heads else None

    # ---------- yuborish ----------

    async def announce(self, user_id: int, record: dict, headline: str = "✅ Bron qilindi!", name: str | None = None):
        """Yangi bronni saqlaydi va foydalanuvchiga xabar beradi (taymer bilan). Bot egasiga — qisqa bildirishnoma."""
        if name:
            self.users.update(user_id, name=name)
        self.users.add_booking(user_id, record)
        text, markup = self.view(record, user_id, headline)
        try:
            sent = await self.bot.send_message(user_id, text, parse_mode=None, reply_markup=markup)
            self.start(user_id, sent.message_id, record["order_id"])
        except Exception:
            log.exception("Bron xabari yuborilmadi: %s", user_id)
        if user_id != self.owner_id:
            who = self.users.display_name(user_id) or name or user_id
            try:
                await self.bot.send_message(
                    self.owner_id, f"ℹ️ {who} bron qildi (o'z akkauntidan, o'zi to'laydi):\n"
                                   f"{booking.booking_text(record, with_pay=False)}", parse_mode=None)
            except Exception:
                log.exception("Bot egasiga bildirishnoma yuborilmadi")
        return record

    async def send_ticket(self, order_id: str) -> bool:
        """To'langan chiptani (PDF) bron egasiga yuboradi. False — sayt PDF ni hali bermadi."""
        found = self.users.find_booking(order_id)
        if not found:
            return False
        uid, record = found
        pdf = None
        for attempt in range(PDF_TRIES):
            try:
                pdf = await self.client.ticket_pdf(await self.sessions.get(uid).get_token(), order_id)
            except UzRailError as e:
                log.warning("Chipta PDF olinmadi %s: %s", order_id, e)
            if pdf:
                break
            if attempt + 1 < PDF_TRIES:
                await asyncio.sleep(PDF_RETRY_DELAY)
        if not pdf:
            return False
        await self.bot.send_document(uid, BufferedInputFile(pdf, filename=f"chipta-{order_id}.pdf"),
                                     caption="🎫 Chiptangiz\n" + booking.booking_text(record, with_pay=False),
                                     parse_mode=None)
        self.users.update_booking(uid, order_id, ticket_sent=time.time())
        return True

    async def _paid(self, uid: int, order_id: str):
        """To'lov o'tdi: holat, xabarlar va chipta (PDF)."""
        self.users.set_status(uid, order_id, "paid")
        await self.finalize(order_id)
        try:
            if not await self.send_ticket(order_id):
                await self.bot.send_message(uid, f"✅ To'lov o'tdi ({order_id}). Chiptani saytdagi "
                                                 f"«Buyurtmalarim» bo'limidan yuklab olishingiz mumkin:\n"
                                                 f"{booking.BASE_URL}/uz/cabinet/orders", parse_mode=None)
        except Exception:
            log.exception("Chipta yuborilmadi: %s", order_id)

    async def finalize(self, order_id: str):
        """Holat o'rnatilgandan keyin (bekor / to'landi / muddati tugadi) barcha xabarlarni yakunlaydi."""
        found = self.users.find_booking(order_id)
        if not found:
            return
        record = found[1]
        for chat_id, message_id in record.get("messages", []):
            text = self.final_text(record, chat_id)
            if text:
                await self._edit(chat_id, message_id, text)

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
        task = self._tasks.get(order_id)
        if not task or task.done():
            self._tasks[order_id] = asyncio.create_task(self._run(order_id))

    def resume_all(self):
        """Bot qayta ishga tushganda: yakunlanmagan bronlar taymerlarini tiklaydi."""
        for uid in self.users.ids():
            for b in self.users.bookings(uid):
                if b.get("expires_at") and not b.get("status") and b.get("messages"):
                    self._tasks[b["order_id"]] = asyncio.create_task(self._run(b["order_id"]))

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
            order = await self.client.order_snapshot(await self.session_for(order_id).get_token(), order_id)
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
                        await self._paid(uid, order_id)
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
        lifecycle = await self.lifecycle(order_id)
        log.info("Bron %s muddati tugadi, saytdagi holat: %s", order_id, lifecycle)
        if is_paid(lifecycle):
            await self._paid(uid, order_id)
            return
        elif lifecycle in (None, "CANCELLED", "EXPIRED", "FAILED"):
            self.users.set_status(uid, order_id, "expired")
        else:  # noma'lum holat — taxmin qilmaymiz
            for chat_id, message_id in record.get("messages", []):
                await self._edit(chat_id, message_id, f"ℹ️ To'lov muddati tugadi. Saytdagi holat: {lifecycle}\n"
                                                      f"{booking.booking_text(record, with_pay=False)}")
            return
        await self.finalize(order_id)
