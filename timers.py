"""Bron xabarlari va to'lov taymeri.

Sayt to'lanmagan bronni 12 daqiqa saqlaydi (aniq vaqt — payment-end-time). Har bir bron uchun bitta vazifa
uning barcha bot xabarlarini (foydalanuvchida va bot egasida) har 10 soniyada yangilab turadi, vaqti-vaqti
bilan saytdan to'langanini tekshiradi va muddat tugaganda holatni yakunlaydi.

Boshqa foydalanuvchining broni bot egasining tasdig'ini kutadi (record["approval"]): egasi
"✅ Tasdiqlash" bosib saytda to'laydi yoki "❌ Rad etish" bilan bekor qiladi. Foydalanuvchiga to'lov
havolasi va bekor qilish tugmasi ko'rsatilmaydi. Bot qayta ishga tushsa, resume_all() taymerlarni tiklaydi.
"""
import asyncio
import logging
import time
from datetime import datetime

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest

import booking
from site_auth import SiteSession
from users import UserStore
from uzrail import UzRailClient, UzRailError

TICK = 10  # sekund
PAY_CHECK_EVERY = 30  # sekund: saytdan "to'landimi" tekshiruvi
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


class BookingTimers:
    def __init__(self, bot: Bot, users: UserStore, client: UzRailClient, session: SiteSession, owner_id: int):
        self.bot, self.users, self.client, self.session, self.owner_id = bot, users, client, session, owner_id
        self._tasks: dict[str, asyncio.Task] = {}

    # ---------- xabar ko'rinishi ----------

    def _for_user(self, record: dict, chat_id: int) -> bool:
        """Foydalanuvchi ko'rinishi: bron egasi tasdig'ini kutadi va xabar bot egasiga emas."""
        return bool(record.get("approval")) and chat_id != self.owner_id

    def view(self, record: dict, chat_id: int, headline: str = "✅ Bron qilindi!"):
        """Faol bron xabari (matn, tugmalar) — kim ko'rayotganiga qarab."""
        until = datetime.fromtimestamp(record["expires_at"]).strftime("%H:%M") if record.get("expires_at") else ""
        left = f"{left_text(record['expires_at'])} ({until} gacha)" if until else ""
        approval = record.get("approval")
        if self._for_user(record, chat_id):
            head = ("✅ Bot egasi tasdiqladi — to'lov kutilmoqda" if approval == "approved"
                    else "📨 Bron qilindi — bot egasi tasdiqlab to'lashini kuting")
            tail = f"\n\n⏳ Bron saqlanadi: {left}" if left else ""
            return f"{head}\n{booking.booking_text(record, with_pay=False)}{tail}", None
        tail = f"\n\n⏳ To'lov uchun qoldi: {left}" if left else ""
        if approval == "pending":
            head = f"🔔 TASDIQLASH KERAK — {record.get('by') or 'foydalanuvchi'} bron qildi.\nTasdiqlab, saytda to'lang:"
            return f"{head}\n{booking.booking_text(record)}{tail}", booking.approval_markup(record["order_id"])
        if approval == "approved":
            head = f"✅ Tasdiqlangan ({record.get('by') or 'foydalanuvchi'}) — saytda to'lang:"
            return f"{head}\n{booking.booking_text(record)}{tail}", booking.cancel_markup(record["order_id"])
        return f"{headline}\n{booking.booking_text(record)}{tail}", booking.cancel_markup(record["order_id"])

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
            head = "✅ TO'LANDI! Bot egasi chiptangizni to'ladi." if user else "✅ TO'LANGAN"
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
            record = {**record, "approval": "pending", "by": f"{profile.get('name') or user_id}{phone}"}
        self.users.add_booking(user_id, record)
        for chat_id in dict.fromkeys([user_id, self.owner_id]):
            text, markup = self.view(record, chat_id, headline)
            try:
                sent = await self.bot.send_message(chat_id, text, parse_mode=None, reply_markup=markup)
                self.start(chat_id, sent.message_id, record["order_id"])
            except Exception:
                log.exception("Bron xabari yuborilmadi: %s", chat_id)
        return record

    async def _ping_user(self, record_owner: int, text: str):
        """Foydalanuvchiga qisqa bildirishnoma (xabarni tahrirlash ovozsiz bo'ladi)."""
        if record_owner != self.owner_id:
            try:
                await self.bot.send_message(record_owner, text, parse_mode=None)
            except Exception:
                log.exception("Foydalanuvchiga xabar yuborilmadi: %s", record_owner)

    async def approve(self, order_id: str) -> dict | None:
        found = self.users.find_booking(order_id)
        if not found:
            return None
        uid, record = found
        self.users.update_booking(uid, order_id, approval="approved")
        await self.refresh(order_id)
        await self._ping_user(uid, f"✅ Bot egasi broningizni tasdiqladi, to'lov amalga oshirilmoqda.\n"
                                   f"{record['from_name']} → {record['to_name']}, {record['departure']}, "
                                   f"vagon {record['car_number']}, joylar: {', '.join(map(str, record['seats']))}")
        return record

    async def finalize(self, order_id: str):
        """Holat o'rnatilgandan keyin (bekor/rad/to'landi/muddati tugadi) barcha xabarlarni yakunlaydi."""
        found = self.users.find_booking(order_id)
        if not found:
            return
        uid, record = found
        for chat_id, message_id in record.get("messages", []):
            text = self.final_text(record, chat_id)
            if text:
                await self._edit(chat_id, message_id, text)
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

    async def _lifecycle(self, order_id: str) -> str | None:
        try:
            return (await self.client.order_snapshot(await self.session.get_token(), order_id)).get("lifecycle")
        except UzRailError:
            return None

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
                    lifecycle = await self._lifecycle(order_id)
                    if lifecycle not in (None, "RESERVED"):
                        log.info("Bron %s saytdagi holati: %s", order_id, lifecycle)  # to'lov holati nomini bilish uchun
                    if is_paid(lifecycle):
                        self.users.set_status(uid, order_id, "paid")
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
        lifecycle = await self._lifecycle(order_id)
        log.info("Bron %s muddati tugadi, saytdagi holat: %s", order_id, lifecycle)
        if is_paid(lifecycle):
            self.users.set_status(uid, order_id, "paid")
        elif lifecycle in (None, "CANCELLED", "EXPIRED", "FAILED"):
            self.users.set_status(uid, order_id, "expired")
        else:  # noma'lum holat — taxmin qilmaymiz
            for chat_id, message_id in record.get("messages", []):
                await self._edit(chat_id, message_id, f"ℹ️ To'lov muddati tugadi. Saytdagi holat: {lifecycle}\n"
                                                      f"{booking.booking_text(record, with_pay=False)}")
            return
        await self.finalize(order_id)
