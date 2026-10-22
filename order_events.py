"""Saytning WebSocket kanali (wss://eticket.uzrailpass.uz/ws): buyurtma natijalari shu yerdan keladi.

Bron so'roviga sayt darhol buyurtma raqamini qaytaradi, lekin bron haqiqatan bo'ldimi — keyinroq shu kanal
orqali xabar keladi: {"status": "Success"|"Error"|"Info", "state": "RESERVATION_SUCCEEDED"|"ORDER_FAILED"|...,
"message": {"uz", "ru", "en"}, "data": {"orderId", "lifecycle", ...}}.
Kanalga foydalanuvchi id si bilan obuna bo'linadi: {"group": <id>, "subscribe": 1}.
"""
import asyncio
import json
import logging
import re

import aiohttp

from uzrail import BASE_URL

WS_URL = BASE_URL.replace("https://", "wss://") + "/ws"
log = logging.getLogger(__name__)

# Saytning o'zi bronni shu holatlarda muvaffaqiyatli deb hisoblaydi. Boshqa "Success" xabarlari oraliq
# (masalan, buyurtma yaratildi) — ularga ishonib "bron bo'ldi" deyish mumkin emas.
SUCCESS_STATES = {"RESERVATION_SUCCEEDED", "PAYMENT_ENTITY_CREATED", "PAYMENT_STARTED", "ORDER_COMPLETED_SUCCESSFULLY"}


# Express tizimi xatolari uchun tushunarli izoh: kod -> (o'zbekcha, ruscha)
EXPRESS_HINTS = {
    "US97": ("yo'lovchi ma'lumoti sotuv tizimiga noto'g'ri formatda yetdi (ism-familiyadagi belgilar)",
             "данные пассажира дошли до системы продажи в неверном формате (символы в ФИО)"),
}
_LATIN_TO_CYR = str.maketrans("ABCEHKMOPTXY", "АВСЕНКМОРТХУ")


def message_text(event: dict, lang: str = "uz") -> str:
    """Sayt xabaridan o'qiladigan matn: texnik "orderItemId=...: " qismi olib tashlanadi.

    Express tizimi matnni lotin va kirill harflari aralash yuboradi ("ПEPBЫM") — kirillga keltiramiz.
    """
    msg = event.get("message") or {}
    text = (msg.get(lang) or msg.get("ru") or msg.get("uz") or "") if isinstance(msg, dict) else str(msg)
    text = re.sub(r"^orderItemId=\S+:\s*", "", text).strip()
    if re.search(r"[А-Яа-я]", text):
        text = re.sub(r"\b(?:(?=\w*[А-Яа-я])\w+|[ABCEHKMOPTXY]+)\b", lambda m: m.group(0).translate(_LATIN_TO_CYR), text)
    code = re.match(r"([A-Z]{2}\d+):", text)
    if code and code.group(1) in EXPRESS_HINTS:
        hint = EXPRESS_HINTS[code.group(1)][1 if lang == "ru" else 0]
        text = f"{hint} ({text})"
    return text


class OrderEvents:
    """async with OrderEvents(user_id) as events: ... await events.wait(order_id)"""

    def __init__(self, user_id: str):
        self.user_id = user_id
        self._session: aiohttp.ClientSession | None = None
        self._ws = None

    async def __aenter__(self):
        try:
            self._session = aiohttp.ClientSession()
            self._ws = await self._session.ws_connect(
                WS_URL, heartbeat=20, timeout=aiohttp.ClientWSTimeout(ws_close=10),
                headers={"Origin": BASE_URL, "User-Agent": "Mozilla/5.0"})
            await self._ws.send_json({"group": self.user_id, "subscribe": 1})
        except Exception as e:  # kanal ishlamasa — natija buyurtma holati orqali tekshiriladi
            log.warning("WebSocket ulanmadi: %s", e)
            await self._close()
        return self

    async def __aexit__(self, *exc):
        if self._ws is not None:
            try:
                await self._ws.send_json({"group": self.user_id, "remove": 1})
            except Exception:
                pass
        await self._close()

    async def _close(self):
        if self._ws is not None:
            await self._ws.close()
        if self._session is not None:
            await self._session.close()
        self._ws = self._session = None

    async def wait(self, order_id: str, timeout: float = 90) -> dict | None:
        """Shu buyurtma bo'yicha birinchi yakuniy xabarni kutadi (boshqa buyurtmalarniki o'tkazib yuboriladi)."""
        if self._ws is None:
            return None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while (left := deadline - loop.time()) > 0:
            try:
                msg = await self._ws.receive(timeout=left)
            except asyncio.TimeoutError:
                return None
            if msg.type != aiohttp.WSMsgType.TEXT:
                if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.ERROR):
                    return None
                continue
            try:
                event = json.loads(msg.data)
            except ValueError:
                continue
            data = event.get("data")
            event_order = data.get("orderId") if isinstance(data, dict) else data
            if event_order != order_id:
                continue
            log.info("Buyurtma %s: %s %s", order_id, event.get("status"), event.get("state"))
            if event.get("status") in ("Error", "Info") or event.get("state") in SUCCESS_STATES:
                return event
        return None
