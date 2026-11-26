"""Saytni bron qilgan foydalanuvchining o'z akkaunti bilan ochish ("💳 Saytda to'lash" tugmasi).

Sayt login tokenini faqat sessionStorage da saqlaydi — har bir yangi tabda u bo'sh, shuning uchun
oddiy havola har safar login so'raydi. Boshqa domendan saytning sessionStorage iga yozib bo'lmaydi,
shu sababli sayt ilova manzili orqali ochiladi (proksi): /sitepay/... sahifasi bot tokenini
sessionStorage ga yozadi va to'lov sahifasiga o'tadi, keyingi barcha so'rovlar (sahifa, API, websocket)
eticket.uzrailpass.uz ga uzatiladi.

Saytning to'lov sahifasi (/pages/payment) buyurtmani API dan olmaydi: sayt ichida bron qilinganda websocket
xabaridan sessionStorage ga "paymentId" va "paymentTypesSnapshot" ({orderId, confirmData}) yozib qo'yiladi.
Bot bronni saytdan tashqarida qilgani uchun ularni buyurtma holatidan (get-snapshot) o'zimiz yozamiz —
aks holda sahifa bo'sh qoladi. (/pages/current-order esa to'lovdan keyingi chipta sahifasi.)

Saytning API va websocket manzillari nisbiy (host: "", wsurl: "/ws"), shuning uchun sayt kodi
o'zgartirilmasdan ishlaydi.

Havola imzolangan va muddatli (faqat bron egasiga yuboriladi) — sayt shu bronni qilgan foydalanuvchining
akkaunti bilan ochiladi; proksi faqat shu havola orqali o'rnatilgan cookie bilan ishlaydi — begona odam
proksidan foydalana olmaydi.
"""
import asyncio
import hashlib
import hmac
import json
import logging
import time
from typing import Callable

import aiohttp
from aiohttp import web

from site_auth import SiteSession
from uzrail import BASE_URL, UzRailClient, UzRailError, decode_jwt

COOKIE = "uzbot_sp"
COOKIE_TTL = 3 * 3600  # sekund: proksi cookie si shuncha amal qiladi
LINK_GRACE = 15 * 60  # sekund: havola bron muddatidan keyin ham shuncha amal qiladi
CLOSED = {"CANCELLED": "Bron bekor qilingan yoki to'lov muddati tugagan — to'lab bo'lmaydi.",
          "FAILED": "Bron saytda amalga oshmagan — to'lab bo'lmaydi."}
# Brauzerdan saytga uzatilmaydigan sarlavhalar
SKIP_REQUEST = {"host", "origin", "referer", "cookie", "content-length", "connection", "accept-encoding",
                "upgrade", "sec-websocket-key", "sec-websocket-version", "sec-websocket-extensions"}
SKIP_RESPONSE = {"content-encoding", "content-length", "transfer-encoding", "connection", "set-cookie",
                 "strict-transport-security", "keep-alive"}
log = logging.getLogger(__name__)


def _sign(secret: str, message: str) -> str:
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()[:32]


def pay_link(base_url: str, secret: str, order_id: str, expires_at: float | None) -> str:
    """Bron egasi uchun imzolangan havola: saytni uning akkaunti bilan shu bron sahifasida ochadi."""
    exp = int((expires_at or time.time()) + LINK_GRACE)
    return f"{base_url.rstrip('/')}/sitepay/{order_id}?e={exp}&s={_sign(secret, f'pay:{order_id}:{exp}')}"


def _cookie_value(secret: str) -> str:
    exp = int(time.time() + COOKIE_TTL)
    return f"{exp}.{_sign(secret, f'proxy:{exp}')}"


def _cookie_ok(secret: str, value: str | None) -> bool:
    exp, _, sig = (value or "").partition(".")
    return exp.isdigit() and int(exp) > time.time() and hmac.compare_digest(sig, _sign(secret, f"proxy:{exp}"))


def is_site_path(path: str) -> bool:
    """Saytning API manzillari (/api/v1/..., /api/v3/...) — ilovaning o'z /api/... manzillari emas."""
    return path.startswith(("/api/v1/", "/api/v2/", "/api/v3/"))


class SiteProxy:
    def __init__(self, secret: str, session_for: Callable[[str], SiteSession], client: UzRailClient):
        """session_for(order_id) — shu bronni qilgan foydalanuvchining sayt akkaunti."""
        self.secret, self.session_for, self.client = secret, session_for, client
        self._http: aiohttp.ClientSession | None = None

    def http(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            # cookie lar brauzerda saqlanadi — bu yerda saqlanmaydi
            self._http = aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar(), auto_decompress=True,
                                               timeout=aiohttp.ClientTimeout(total=60))
        return self._http

    async def close(self):
        if self._http:
            await self._http.close()

    async def start_page(self, request: web.Request) -> web.Response:
        """Imzoli havola: token va buyurtmani sessionStorage ga yozib, to'lov sahifasini ochadi."""
        order_id = request.match_info["order_id"]
        exp = request.query.get("e", "")
        sig = request.query.get("s", "")
        if not (exp.isdigit() and int(exp) > time.time()
                and hmac.compare_digest(sig, _sign(self.secret, f"pay:{order_id}:{exp}"))):
            return web.Response(text="Havola eskirgan — botdagi bron xabaridan qayta oching.", status=403)
        try:
            token = await self.session_for(order_id).get_token()
            order = await self.client.order_snapshot(token, order_id)
        except UzRailError as e:
            return web.Response(text=str(e), status=503)
        if order.get("lifecycle") in CLOSED:
            return web.Response(text=CLOSED[order["lifecycle"]], status=410)
        claims = decode_jwt(token)
        storage = {"token": token, "userId": str(claims.get("id", "")), "username": claims.get("sub", ""),
                   "orderId": order_id, "paymentId": order.get("paymentId") or f"{order_id}_PAYMENT",
                   "paymentTypesSnapshot": json.dumps({"orderId": order_id, "confirmData": order})}
        script = "".join(f"sessionStorage.setItem({json.dumps(k)},{json.dumps(v)});" for k, v in storage.items())
        script = script.replace("</", "<\\/")  # buyurtma matnidagi "</script>" sahifani buzmasin
        target = json.dumps("/uz/pages/payment")
        html = (f"<!doctype html><meta charset=utf-8><title>To'lov</title>"
                f"<script>{script}location.replace({target});</script>")
        resp = web.Response(text=html, content_type="text/html",
                            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
        resp.set_cookie(COOKIE, _cookie_value(self.secret), max_age=COOKIE_TTL, path="/",
                        secure=True, httponly=True, samesite="Lax")
        return resp

    async def handle(self, request: web.Request) -> web.StreamResponse:
        """Boshqa barcha so'rovlar — saytga uzatiladi (faqat imzoli havola orqali kirilgan bo'lsa)."""
        if not _cookie_ok(self.secret, request.cookies.get(COOKIE)):
            raise web.HTTPNotFound()
        headers = {k: v for k, v in request.headers.items() if k.lower() not in SKIP_REQUEST}
        headers["Origin"] = BASE_URL
        headers["Referer"] = BASE_URL + "/uz/"
        cookies = "; ".join(f"{k}={v}" for k, v in request.cookies.items() if k != COOKIE)
        if cookies:
            headers["Cookie"] = cookies
        url = BASE_URL + request.rel_url.path_qs
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await self._websocket(request, url.replace("https://", "wss://", 1), headers)
        body = await request.read() if request.can_read_body else None
        try:
            async with self.http().request(request.method, url, headers=headers, data=body,
                                           allow_redirects=False) as upstream:
                content = await upstream.read()
                out = {k: v for k, v in upstream.headers.items() if k.lower() not in SKIP_RESPONSE}
                if "Location" in out:
                    out["Location"] = out["Location"].replace(BASE_URL, "")
                resp = web.Response(status=upstream.status, body=content, headers=out)
                for raw in upstream.headers.getall("Set-Cookie", []):
                    # Domain=eticket.uzrailpass.uz bo'lsa brauzer cookie ni rad etadi — olib tashlaymiz
                    parts = [p for p in raw.split(";") if not p.strip().lower().startswith("domain=")]
                    resp.headers.add("Set-Cookie", ";".join(parts))
                return resp
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.warning("Sayt proksisi xatosi (%s): %s", request.rel_url.path, e)
            return web.Response(text="Sayt javob bermadi.", status=504)

    async def _websocket(self, request: web.Request, url: str, headers: dict) -> web.WebSocketResponse:
        """Saytning websocket i (to'lov holati xabarlari) — ikki tomonga uzatiladi."""
        browser = web.WebSocketResponse()
        await browser.prepare(request)
        try:
            async with self.http().ws_connect(url, headers=headers) as site:
                async def to_site():
                    async for msg in browser:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await site.send_str(msg.data)
                        elif msg.type == aiohttp.WSMsgType.BINARY:
                            await site.send_bytes(msg.data)
                    await site.close()

                async def to_browser():
                    async for msg in site:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await browser.send_str(msg.data)
                        elif msg.type == aiohttp.WSMsgType.BINARY:
                            await browser.send_bytes(msg.data)
                    await browser.close()

                tasks = [asyncio.create_task(to_site()), asyncio.create_task(to_browser())]
                _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.warning("Sayt websocket proksisi xatosi: %s", e)
        await browser.close()
        return browser
