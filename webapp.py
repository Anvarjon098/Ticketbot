"""Telegram Mini App (ilova): mahalliy veb-server (API + sahifa) va cloudflared HTTPS tunneli.

Har bir API so'rovi Telegram imzosi (initData) bilan tekshiriladi, shuning uchun ilovani faqat
botdan ochgan va ruxsati bor foydalanuvchi ishlata oladi.
"""
import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from aiogram import Bot
from aiogram.utils.web_app import safe_parse_webapp_init_data
from aiohttp import web

import booking
import car_schemas
from monitor import Monitor, MonitorManager
from passengers import validate_fields
from categories import ORDER as CATEGORY_CODES
from categories import describe, normalize as normalize_category, train_categories
from site_auth import SiteSession, normalize_phone
from users import UserStore
from uzrail import UzRailClient, UzRailError

STATIC_DIR = Path(__file__).with_name("webapp")
CLOUDFLARED = Path(__file__).parent / "bin" / "cloudflared.exe"
INIT_DATA_MAX_AGE = 24 * 3600
log = logging.getLogger(__name__)


@dataclass
class AppContext:
    bot: Bot
    client: UzRailClient
    session: SiteSession
    users: UserStore
    monitors: MonitorManager
    owner_id: int
    allowed_ids: set[int]
    poll_interval: float
    client_ru: UzRailClient | None = None  # reyslar ro'yxatini ruscha nomlar bilan olish uchun
    timers: object | None = None  # timers.BookingTimers — bot xabaridagi to'lov taymeri


CTX = web.AppKey("ctx", AppContext)

# Ilova xabarlari: kalit -> (o'zbekcha, ruscha)
MESSAGES = {
    "open_via_bot": ("Ilovani Telegram bot orqali oching.", "Откройте приложение через Telegram-бота."),
    "session_expired": ("Sessiya eskirgan — ilovani qayta oching.", "Сессия устарела — откройте приложение заново."),
    "not_allowed": ("Sizga bu botdan foydalanishga ruxsat berilmagan.", "У вас нет доступа к этому боту."),
    "server_error": ("Serverda kutilmagan xato.", "Непредвиденная ошибка сервера."),
    "bad_request": ("Noto'g'ri so'rov.", "Некорректный запрос."),
    "bad_date": ("Sana noto'g'ri.", "Неверная дата."),
    "past_date": ("O'tgan sanani tanlab bo'lmaydi.", "Нельзя выбрать прошедшую дату."),
    "bad_station": ("Stansiya noto'g'ri.", "Неверная станция."),
    "bad_train": ("Poyezd ma'lumoti noto'g'ri.", "Неверные данные поезда."),
    "sorbon": ("Bu reys (Sorbon tizimi) hozircha qo'llab-quvvatlanmaydi.",
               "Этот рейс (система Sorbon) пока не поддерживается."),
    "pick_passenger": ("Kamida bitta yo'lovchini tanlang.", "Выберите хотя бы одного пассажира."),
    "need_site": ("Avval bot egasi Profil bo'limida saytga kirishi kerak.",
                  "Сначала владелец бота должен войти на сайт в разделе «Профиль»."),
    "sold_out_now": ("Afsuski, joylar band bo'lib qoldi. Kuzatuvga qo'yishingiz mumkin.",
                     "К сожалению, места уже заняты. Можно поставить поезд на отслеживание."),
    "pick_seats": ("Vagon sxemasida joylarni tanlang.", "Выберите места на схеме вагона."),
    "seats_mismatch": ("{n} ta joy tanlang — har bir yo'lovchiga bittadan.",
                       "Выберите {n} мест(а) — по одному на каждого пассажира."),
    "seats_taken": ("Tanlangan joylardan biri band bo'lib qoldi. Sxema yangilandi — boshqa joy tanlang.",
                    "Одно из выбранных мест уже заняли. Схема обновлена — выберите другое место."),
    "monitor_not_found": ("Kuzatuv topilmadi.", "Отслеживание не найдено."),
    "owner_only": ("Faqat bot egasi uchun.", "Только для владельца бота."),
    "bad_phone": ("Telefon raqam noto'g'ri. Masalan: +998901234567", "Неверный номер телефона. Пример: +998901234567"),
    "booking_not_found": ("Bron topilmadi.", "Бронь не найдена."),
    "already_cancelled": ("Bu bron allaqachon bekor qilingan.", "Эта бронь уже отменена."),
    "booking_active": ("Faol yoki to'langan bronni o'chirib bo'lmaydi — avval bekor qiling.",
                       "Активную или оплаченную бронь удалить нельзя — сначала отмените её."),
    "cancel_failed": ("Bekor qilib bo'lmadi: {error}. To'langan chiptani saytning o'zida qaytarish kerak.",
                      "Не удалось отменить: {error}. Оплаченный билет возвращается на самом сайте."),
}


def lang_of(request: web.Request) -> str:
    return "ru" if request.headers.get("X-Lang") == "ru" else "uz"


def tr(request: web.Request, key: str, **fmt) -> str:
    return MESSAGES[key][1 if lang_of(request) == "ru" else 0].format(**fmt)


class ApiError(Exception):
    """key — MESSAGES dagi kalit (tarjima qilinadi) yoki tayyor matn."""

    def __init__(self, key: str, status: int = 400):
        super().__init__(key)
        self.key = key
        self.status = status


def _error(message: str, status: int) -> web.Response:
    return web.json_response({"error": message}, status=status)


@web.middleware
async def api_middleware(request: web.Request, handler):
    if not request.path.startswith("/api/"):
        return await handler(request)
    ctx = request.app[CTX]
    try:
        init = safe_parse_webapp_init_data(ctx.bot.token, request.headers.get("X-Telegram-Init-Data", ""))
    except ValueError:
        return _error(tr(request, "open_via_bot"), 401)
    if time.time() - init.auth_date.timestamp() > INIT_DATA_MAX_AGE:
        return _error(tr(request, "session_expired"), 401)
    if init.user.id not in ctx.allowed_ids:
        return _error(tr(request, "not_allowed"), 403)
    request["user_id"] = init.user.id
    try:
        return await handler(request)
    except ApiError as e:
        return _error(tr(request, e.key) if e.key in MESSAGES else e.key, e.status)
    except web.HTTPException:
        raise  # masalan, noma'lum manzil — 404
    except UzRailError as e:
        # 502 emas: Cloudflare tunneli 502 javobini o'z sahifasiga almashtiradi va xato matni ilovaga yetmaydi
        return _error(e.text(lang_of(request)), 422)
    except Exception:
        log.exception("API xatosi: %s", request.path)
        return _error(tr(request, "server_error"), 500)


# ---------- yordamchilar ----------

def client_for(request: web.Request) -> UzRailClient:
    ctx = request.app[CTX]
    return ctx.client_ru if lang_of(request) == "ru" and ctx.client_ru else ctx.client


async def body(request: web.Request) -> dict:
    try:
        data = await request.json()
    except Exception:
        raise ApiError("bad_request")
    if not isinstance(data, dict):
        raise ApiError("bad_request")
    return data


def trip_params(data: dict) -> tuple[str, str, str, dict]:
    """So'rovdan sana, stansiya kodlari va poyezd ma'lumotini oladi."""
    trip_date, from_code, to_code, train = (data.get(k) for k in ("date", "from_code", "to_code", "train"))
    try:
        date.fromisoformat(str(trip_date))
    except ValueError:
        raise ApiError("bad_date")
    if not (isinstance(from_code, str) and from_code.isdigit() and isinstance(to_code, str) and to_code.isdigit()):
        raise ApiError("bad_station")
    if not isinstance(train, dict) or not all(k in train for k in ("number", "departureDate", "arrivalDate", "subRoute")):
        raise ApiError("bad_train")
    if train.get("trainId"):
        raise ApiError("sorbon")
    return trip_date, from_code, to_code, train


def chosen_passengers(ctx: AppContext, user_id: int, passports) -> list[dict]:
    saved = ctx.users.passengers(user_id)
    chosen = [p for p in saved if p["passport"] in set(passports or [])]
    if not chosen:
        raise ApiError("pick_passenger")
    return chosen


def chosen_categories(data: dict) -> list[str] | None:
    """So'rovdagi kategoriya kodlari (bo'sh — istalgan kategoriya)."""
    raw = data.get("categories")
    if not isinstance(raw, list):
        return None
    codes = [normalize_category(c) for c in raw if isinstance(c, str) and 0 < len(c) <= 4]
    return sorted(set(codes), key=lambda c: CATEGORY_CODES.index(c) if c in CATEGORY_CODES else 99) or None


def require_site_account(ctx: AppContext):
    if not ctx.session.account:
        raise ApiError("need_site")


def monitor_info(m: Monitor) -> dict:
    return {"id": m.id, "title": m.title(), "active": m.active, "checks": m.checks, "order_id": m.order_id,
            "departure": m.train["departureDate"], "train_number": m.train["number"],
            "from_name": m.from_name, "to_name": m.to_name, "strict": m.strict,
            "passengers": len(m.passengers), "categories": m.categories or [],
            "categories_uz": describe(m.categories, "uz", m.train), "categories_ru": describe(m.categories, "ru", m.train)}


# ---------- API ----------

async def me(request: web.Request):
    ctx, uid = request.app[CTX], request["user_id"]
    user = ctx.users.get(uid)
    is_owner = uid == ctx.owner_id
    return web.json_response({
        "phone": user.get("phone"),
        "is_owner": is_owner,
        "site_linked": bool(ctx.session.account),
        "site_status": ctx.session.status(lang_of(request)) if is_owner else None,
        "passengers": ctx.users.passengers(uid),
    })


async def add_passenger(request: web.Request):
    ctx, uid = request.app[CTX], request["user_id"]
    data = await body(request)
    try:
        passenger = validate_fields(data, lang_of(request))
    except ValueError as e:
        raise ApiError(str(e))
    replaces = data.get("replaces")  # tahrirlashda pasport o'zgargan bo'lsa — eski yozuvni olib tashlaymiz
    if replaces and replaces != passenger["passport"] and not data.get("is_self"):
        ctx.users.remove_passenger(uid, replaces)
    ctx.users.add_passenger(uid, passenger, is_self=bool(data.get("is_self")))
    return web.json_response({"passengers": ctx.users.passengers(uid)})


async def delete_passenger(request: web.Request):
    ctx, uid = request.app[CTX], request["user_id"]
    ctx.users.remove_passenger(uid, request.match_info["passport"])
    return web.json_response({"passengers": ctx.users.passengers(uid)})


async def tickets(request: web.Request):
    ctx, uid = request.app[CTX], request["user_id"]
    own = [m for m in ctx.monitors.monitors.values() if m.chat_id == uid]
    own.sort(key=lambda m: m.created, reverse=True)
    return web.json_response({"bookings": ctx.users.bookings(uid), "monitors": [monitor_info(m) for m in own]})


async def delete_monitor(request: web.Request):
    ctx, uid = request.app[CTX], request["user_id"]
    m = ctx.monitors.monitors.get(request.match_info["id"])
    if not m or uid not in (m.chat_id, ctx.owner_id):
        raise ApiError("monitor_not_found", 404)
    ctx.monitors.remove(m.id)
    return web.json_response({"ok": True})


async def stations(request: web.Request):
    query = request.query.get("q", "").strip()
    if len(query) < 2:
        return web.json_response({"stations": []})
    return web.json_response({"stations": (await request.app[CTX].client.search_stations(query))[:10]})


async def trains(request: web.Request):
    trip_date, from_code, to_code = (request.query.get(k, "") for k in ("date", "from_code", "to_code"))
    try:
        if date.fromisoformat(trip_date) < date.today():
            raise ApiError("past_date")
    except ValueError:
        raise ApiError("bad_date")
    # Poyezd turi, stansiya va vagon nomlari tanlangan tilda keladi
    found = await client_for(request).list_trains(trip_date, from_code, to_code)
    parse = lambda t: datetime.strptime(t["departureDate"], "%d.%m.%Y %H:%M")
    found = sorted((t for t in found if parse(t) > datetime.now()), key=parse)
    for t in found:
        cars = t.get("cars") or []
        t["free"] = sum(c.get("freeSeats", 0) for c in cars)
        prices = [x["tariff"] for c in cars for x in c.get("tariffs") or [] if x.get("tariff")]
        t["min_price"] = min(prices) if prices else None
        t["categories"] = train_categories(t)
    return web.json_response({"trains": found})


async def places(request: web.Request):
    ctx = request.app[CTX]
    trip_date, from_code, to_code, train = trip_params(await body(request))
    data = await ctx.client.train_places(trip_date, from_code, to_code, train)
    # joyi bor kategoriyalar (aniq son bilan) + shu poyezd turidagi joyi tugagan kategoriyalar (kuzatish uchun)
    # cars — vagonlar va ularning bo'sh joylari: ilovada saytdagidek vagon sxemasida ko'rsatiladi
    return web.json_response({"categories": train_categories(train, data or {}), "cars": car_schemas.cars(data)})


async def book(request: web.Request):
    ctx, uid = request.app[CTX], request["user_id"]
    require_site_account(ctx)
    data = await body(request)
    trip_date, from_code, to_code, train = trip_params(data)
    chosen = chosen_passengers(ctx, uid, data.get("passports"))
    seats = data.get("seats")
    if not (isinstance(data.get("car_key"), str) and isinstance(seats, list)
            and all(isinstance(s, int) for s in seats)):
        raise ApiError("pick_seats")
    if len(set(seats)) != len(chosen):
        raise ApiError(tr(request, "seats_mismatch", n=len(chosen)))
    # Foydalanuvchi sxemada tanlagan joylar — shu orada band bo'lmaganini yangi ma'lumot bilan tekshiramiz
    places_now = await ctx.client.train_places(trip_date, from_code, to_code, train)
    choice = car_schemas.pick(places_now, data["car_key"], seats, data.get("tier"))
    if not choice:
        raise ApiError("seats_taken", 409)
    order_id = await booking.reserve(ctx.client, ctx.session, train, choice, chosen)
    record = booking.booking_record(order_id, train, choice, chosen,
                                    str(data.get("from_name") or ""), str(data.get("to_name") or ""))
    record["expires_at"] = await booking.payment_deadline(ctx.client, ctx.session, order_id)
    ctx.users.add_booking(uid, record)
    text = "✅ Bron qilindi!\n" + booking.booking_text(record)
    for chat_id in {uid, ctx.owner_id}:
        try:
            sent = await ctx.bot.send_message(chat_id, text, parse_mode=None, reply_markup=booking.cancel_markup(order_id))
            if ctx.timers:  # bot xabarida ham to'lov taymeri
                ctx.timers.start(chat_id, sent.message_id, order_id)
        except Exception:
            log.exception("Xabar yuborilmadi: %s", chat_id)
    return web.json_response({"booking": record})


async def cancel_booking(request: web.Request):
    """To'lanmagan bronni saytda bekor qiladi va ro'yxatda "bekor qilingan" deb belgilaydi."""
    ctx, uid = request.app[CTX], request["user_id"]
    order_id = request.match_info["order_id"]
    found = ctx.users.find_booking(order_id)
    if not found or uid not in (found[0], ctx.owner_id):
        raise ApiError("booking_not_found", 404)
    owner_uid, record = found
    if record.get("status") in ("cancelled", "failed"):
        raise ApiError("already_cancelled", 409)
    require_site_account(ctx)
    try:
        await booking.cancel(ctx.client, ctx.session, order_id)
    except booking.OrderNotActive as e:
        ctx.users.set_status(owner_uid, order_id, "failed")
        return web.json_response({"bookings": ctx.users.bookings(uid), "notice": e.text(lang_of(request))})
    except UzRailError as e:
        raise ApiError(tr(request, "cancel_failed", error=e.text(lang_of(request))), 422)
    ctx.users.mark_cancelled(owner_uid, order_id)
    text = (f"❌ Bron bekor qilindi (ilova orqali): {order_id}\n"
            f"{record['from_name']} → {record['to_name']}, {record['departure']}")
    for chat_id in {owner_uid, ctx.owner_id}:
        try:
            await ctx.bot.send_message(chat_id, text, parse_mode=None)
        except Exception:
            log.exception("Xabar yuborilmadi: %s", chat_id)
    return web.json_response({"bookings": ctx.users.bookings(uid)})


async def delete_booking(request: web.Request):
    """Bekor qilingan / amalga oshmagan / muddati tugagan bronni ro'yxatdan o'chiradi (saytga tegmaydi)."""
    ctx, uid = request.app[CTX], request["user_id"]
    order_id = request.match_info["order_id"]
    found = ctx.users.find_booking(order_id)
    if not found or uid not in (found[0], ctx.owner_id):
        raise ApiError("booking_not_found", 404)
    if not booking.is_inactive(found[1]):
        raise ApiError("booking_active", 409)
    ctx.users.remove_bookings(found[0], {order_id})
    return web.json_response({"bookings": ctx.users.bookings(uid)})


async def clear_bookings(request: web.Request):
    """Barcha bekor qilingan / amalga oshmagan / muddati tugagan bronlarni ro'yxatdan o'chiradi."""
    ctx, uid = request.app[CTX], request["user_id"]
    removed = ctx.users.remove_bookings(uid, {b["order_id"] for b in ctx.users.bookings(uid) if booking.is_inactive(b)})
    return web.json_response({"bookings": ctx.users.bookings(uid), "removed": removed})


async def start_monitor(request: web.Request):
    ctx, uid = request.app[CTX], request["user_id"]
    require_site_account(ctx)
    data = await body(request)
    trip_date, _, _, train = trip_params(data)
    m = Monitor(
        chat_id=uid,
        passengers=chosen_passengers(ctx, uid, data.get("passports")),
        from_name=str(data.get("from_name") or ""),
        to_name=str(data.get("to_name") or ""),
        trip_date=trip_date,
        train=train,
        car_type="*",
        strict=bool(data.get("strict", True)),
        categories=chosen_categories(data),
    )
    ctx.monitors.add(m)
    count = len(ctx.monitors.active())
    return web.json_response({"monitor": monitor_info(m), "every_seconds": ctx.poll_interval * count})


def require_owner(request: web.Request) -> AppContext:
    ctx = request.app[CTX]
    if request["user_id"] != ctx.owner_id:
        raise ApiError("owner_only", 403)
    return ctx


async def site_login(request: web.Request):
    ctx = require_owner(request)
    data = await body(request)
    try:
        phone = normalize_phone(str(data.get("phone") or ""))
    except ValueError:
        raise ApiError("bad_phone")
    await ctx.session.set_account(phone, str(data.get("password") or ""))
    return web.json_response({"site_status": ctx.session.status(lang_of(request))})


async def index(request: web.Request):
    return web.FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


def create_app(ctx: AppContext) -> web.Application:
    app = web.Application(middlewares=[api_middleware])
    app[CTX] = ctx
    app.router.add_get("/", index)
    app.router.add_get("/api/me", me)
    app.router.add_post("/api/passengers", add_passenger)
    app.router.add_delete("/api/passengers/{passport}", delete_passenger)
    app.router.add_get("/api/tickets", tickets)
    app.router.add_delete("/api/monitors/{id}", delete_monitor)
    app.router.add_post("/api/bookings/{order_id}/cancel", cancel_booking)
    app.router.add_post("/api/bookings/clear-inactive", clear_bookings)
    app.router.add_delete("/api/bookings/{order_id}", delete_booking)
    app.router.add_get("/api/stations", stations)
    app.router.add_get("/api/trains", trains)
    app.router.add_post("/api/places", places)
    app.router.add_post("/api/book", book)
    app.router.add_post("/api/monitor", start_monitor)
    app.router.add_post("/api/site-login", site_login)
    app.router.add_static("/schemas", car_schemas.SCHEMA_DIR)  # vagon sxemalari (saytdan olingan SVG)
    return app


async def start_server(ctx: AppContext, port: int) -> web.AppRunner:
    runner = web.AppRunner(create_app(ctx), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()  # faqat mahalliy — tashqariga tunnel orqali
    return runner


async def start_tunnel(port: int, timeout: float = 60) -> tuple[asyncio.subprocess.Process, str]:
    """cloudflared "quick tunnel" ni ishga tushiradi va https://….trycloudflare.com manzilini qaytaradi."""
    if not CLOUDFLARED.exists():
        raise RuntimeError(f"{CLOUDFLARED} topilmadi")
    proc = await asyncio.create_subprocess_exec(
        str(CLOUDFLARED), "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )

    async def wait_ready() -> str:
        url = None
        while line := await proc.stderr.readline():
            text = line.decode(errors="ignore")
            m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", text)
            if m:
                url = m.group(0)
            if url and "Registered tunnel connection" in text:
                return url
        raise RuntimeError("cloudflared to'xtab qoldi")

    try:
        url = await asyncio.wait_for(wait_ready(), timeout)
    except Exception:
        proc.terminate()
        raise

    async def drain():  # stderr bufferi to'lib, jarayon qotib qolmasligi uchun
        while await proc.stderr.readline():
            pass

    asyncio.create_task(drain())
    return proc, url
