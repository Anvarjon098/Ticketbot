"""Bron to'lov muddati taymeri: bot xabaridagi qolgan vaqtni har 10 soniyada yangilab turadi.

Sayt to'lanmagan bronni 12 daqiqa saqlaydi (aniq vaqt — payment-end-time). Muddat tugagach saytdagi holat
tekshiriladi: to'lanmagan bo'lsa bron "muddati tugagan" deb belgilanadi. Bot qayta ishga tushsa, davom
etayotgan taymerlar resume_all() bilan tiklanadi (xabarlar users.json da saqlanadi).
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
log = logging.getLogger(__name__)


def left_text(expires_at: float) -> str:
    left = max(0, int(expires_at - time.time()))
    return f"{left // 60:02d}:{left % 60:02d}"


def booking_message(record: dict) -> str:
    """Bron xabari matni (taymer qatorisiz)."""
    return "✅ Bron qilindi!\n" + booking.booking_text(record)


class BookingTimers:
    def __init__(self, bot: Bot, users: UserStore, client: UzRailClient, session: SiteSession):
        self.bot, self.users, self.client, self.session = bot, users, client, session
        self._tasks: dict[tuple[int, int], asyncio.Task] = {}

    def start(self, chat_id: int, message_id: int, order_id: str, remember: bool = True):
        """Shu xabarda taymerni ishga tushiradi."""
        if remember:
            self.users.add_booking_message(order_id, chat_id, message_id)
        key = (chat_id, message_id)
        if key not in self._tasks or self._tasks[key].done():
            self._tasks[key] = asyncio.create_task(self._run(chat_id, message_id, order_id))

    def resume_all(self):
        """Bot qayta ishga tushganda: muddati tugamagan bronlar taymerlarini tiklaydi."""
        for uid in self.users.ids():
            for b in self.users.bookings(uid):
                if b.get("expires_at") and not b.get("status"):
                    for chat_id, message_id in b.get("messages", []):
                        self.start(chat_id, message_id, b["order_id"], remember=False)

    async def _edit(self, chat_id: int, message_id: int, text: str, markup=None):
        try:
            await self.bot.edit_message_text(text, chat_id=chat_id, message_id=message_id,
                                             parse_mode=None, reply_markup=markup)
        except TelegramBadRequest as e:
            if "not modified" not in str(e):
                log.warning("Taymer xabarini yangilab bo'lmadi (%s/%s): %s", chat_id, message_id, e)

    async def _run(self, chat_id: int, message_id: int, order_id: str):
        try:
            while True:
                found = self.users.find_booking(order_id)
                if not found:
                    return
                record = found[1]
                if record.get("status"):  # bekor qilingan / amalga oshmagan — xabarni boshqa joy yangilagan
                    return
                left = record["expires_at"] - time.time()
                if left <= 0:
                    break
                until = datetime.fromtimestamp(record["expires_at"]).strftime("%H:%M")
                await self._edit(chat_id, message_id,
                                 f"{booking_message(record)}\n\n⏳ To'lov uchun qoldi: {left_text(record['expires_at'])} ({until} gacha)",
                                 booking.cancel_markup(order_id))
                await asyncio.sleep(min(TICK, left))
            await self._finish(chat_id, message_id, order_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Taymer xatosi: %s", order_id)

    async def _finish(self, chat_id: int, message_id: int, order_id: str):
        """Muddat tugadi: saytdagi holatni tekshirib, xabarni yakunlaydi."""
        await asyncio.sleep(5)  # sayt holatni yangilashi uchun
        found = self.users.find_booking(order_id)
        if not found or found[1].get("status") in ("cancelled", "failed"):
            return
        owner_id, record = found
        try:
            lifecycle = (await self.client.order_snapshot(await self.session.get_token(), order_id)).get("lifecycle")
        except UzRailError:
            lifecycle = None
        base = booking_message(record).split("\n\n")[0]
        if lifecycle and any(s in lifecycle for s in ("PAID", "COMPLET", "SUCCE")):
            self.users.set_status(owner_id, order_id, "paid")
            text = f"✅ TO'LANGAN\n{base}"
        elif lifecycle in (None, "CANCELLED", "EXPIRED", "FAILED"):
            if lifecycle != "CANCELLED" or record.get("status") != "cancelled":
                self.users.set_status(owner_id, order_id, "expired")
            text = f"⌛ BRON MUDDATI TUGADI — to'lanmadi, joylar bo'shatildi.\n{base}"
        else:  # noma'lum holat — taxmin qilmaymiz
            text = f"ℹ️ To'lov muddati tugadi. Saytdagi holat: {lifecycle}\n{base}"
        log.info("Bron %s muddati tugadi, saytdagi holat: %s", order_id, lifecycle)
        await self._edit(chat_id, message_id, text)
