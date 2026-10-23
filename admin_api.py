"""Admin panel (faqat bot egasi) API'si: foydalanuvchilar (qidiruv, tahrirlash, ruxsat) va statistika.

Sahifa — webapp/admin.html (/admin), botdagi "🛠 Admin panel" tugmasi orqali ochiladi. /api/admin/...
so'rovlari webapp.api_middleware da Telegram imzosi bilan tekshiriladi, bu yerda esa faqat bot egasiga
ruxsat beriladi (webapp.require_owner).
"""
import time

from aiohttp import web

import webapp
from passengers import validate_fields
from site_auth import normalize_phone

def _uid(request: web.Request) -> int:
    try:
        return int(request.match_info["uid"])
    except ValueError:
        raise webapp.ApiError("bad_request")


def _user_ids(ctx: webapp.AppContext) -> list[int]:
    ids = set(ctx.users.ids()) | set(ctx.allowed_ids) | {ctx.owner_id}
    if ctx.access:
        ids |= set(ctx.access.listed("pending")) | set(ctx.access.listed("denied"))
    return sorted(ids)


def _booking_state(b: dict) -> str:
    """Bron holati: paid / active / cancelled / failed / expired (muddati o'tgan, holati yozilmagan bo'lsa ham)."""
    if b.get("status"):
        return b["status"]
    return "expired" if b.get("expires_at") and b["expires_at"] <= time.time() else "active"


def _summary(ctx: webapp.AppContext, uid: int) -> dict:
    user = ctx.users.get(uid)
    info = ctx.access.info(uid) if ctx.access else {}
    bookings = user.get("bookings", [])
    passengers = ctx.users.passengers(uid)
    monitors = [m for m in ctx.monitors.monitors.values() if m.chat_id == uid]
    tg_name = info.get("name") or user.get("name") or ""
    name = ctx.users.display_name(uid) or tg_name
    status = "owner" if uid == ctx.owner_id else (ctx.access.status(uid) if ctx.access else
                                                  "allowed" if uid in ctx.allowed_ids else "new")
    search = " ".join(str(x) for x in [
        name, tg_name, info.get("username") or "", uid, user.get("phone") or "",
        *(f"{p['last_name']} {p['first_name']} {p.get('middle_name', '')} {p['passport']}" for p in passengers),
        *(b.get("order_id", "") for b in bookings)]).lower()
    return {"id": uid, "name": name, "tg_name": tg_name, "custom_name": bool(user.get("display_name")),
            "username": info.get("username"), "phone": user.get("phone"), "status": status,
            "since": info.get("at"), "passengers": passengers, "bookings": len(bookings),
            "paid": sum(1 for b in bookings if _booking_state(b) == "paid"),
            "monitors": sum(1 for m in monitors if m.active), "search": search}


async def users_list(request: web.Request):
    ctx = webapp.require_owner(request)
    return web.json_response({"users": [_summary(ctx, uid) for uid in _user_ids(ctx)]})


async def user_detail(request: web.Request):
    ctx, uid = webapp.require_owner(request), _uid(request)
    monitors = sorted((m for m in ctx.monitors.monitors.values() if m.chat_id == uid),
                      key=lambda m: m.created, reverse=True)
    bookings = [{**b, "state": _booking_state(b)} for b in ctx.users.bookings(uid)]
    for b in bookings:
        b.pop("messages", None)
    return web.json_response({"user": _summary(ctx, uid), "bookings": bookings,
                              "monitors": [webapp.monitor_info(m) for m in monitors]})


async def user_update(request: web.Request):
    """Ism (bo'sh — Telegram ismiga qaytadi) va telefon."""
    ctx, uid = webapp.require_owner(request), _uid(request)
    data = await webapp.body(request)
    fields = {}
    if "name" in data:
        fields["display_name"] = " ".join(str(data["name"] or "").split())[:60] or None
    if "phone" in data:
        try:
            fields["phone"] = normalize_phone(str(data["phone"] or "")) if data["phone"] else None
        except ValueError:
            raise webapp.ApiError("bad_phone")
    ctx.users.update(uid, **fields)
    return web.json_response({"user": _summary(ctx, uid)})


async def user_access(request: web.Request):
    ctx, uid = webapp.require_owner(request), _uid(request)
    if uid == ctx.owner_id or not ctx.set_access:
        raise webapp.ApiError("bad_request")
    data = await webapp.body(request)
    await ctx.set_access(uid, bool(data.get("grant")))
    return web.json_response({"user": _summary(ctx, uid)})


async def passenger_save(request: web.Request):
    """Yo'lovchini qo'shadi yoki index-dagisini o'z o'rnida almashtiradi (birinchisi — foydalanuvchining o'zi)."""
    ctx, uid = webapp.require_owner(request), _uid(request)
    data = await webapp.body(request)
    try:
        passenger = validate_fields(data, webapp.lang_of(request))
    except ValueError as e:
        raise webapp.ApiError(str(e))
    index = data.get("index")
    if isinstance(index, int) and 0 <= index < len(ctx.users.passengers(uid)):
        ctx.users.replace_passenger(uid, index, passenger)
    else:
        ctx.users.add_passenger(uid, passenger, is_self=not ctx.users.passengers(uid))
    return web.json_response({"user": _summary(ctx, uid)})


async def passenger_delete(request: web.Request):
    ctx, uid = webapp.require_owner(request), _uid(request)
    saved = ctx.users.passengers(uid)
    try:
        passenger = saved[int(request.match_info["index"])]
    except (ValueError, IndexError):
        raise webapp.ApiError("bad_request")
    ctx.users.remove_passenger(uid, passenger["passport"])
    return web.json_response({"user": _summary(ctx, uid)})


async def stats(request: web.Request):
    """Statistika uchun xom yozuvlar — davr bo'yicha yig'ish sahifada (filtr o'zgarganda qayta so'rov shart emas)."""
    ctx = webapp.require_owner(request)
    names = {uid: ctx.users.display_name(uid) or (ctx.access.info(uid).get("name") if ctx.access else None)
             or str(uid) for uid in _user_ids(ctx)}
    bookings = [{"order_id": b.get("order_id"), "user_id": uid, "user": names.get(uid, str(uid)),
                 "created": b.get("created"), "state": _booking_state(b), "price": b.get("price") or 0,
                 "route": f"{b.get('from_name')} → {b.get('to_name')}", "passengers": len(b.get("passengers") or []),
                 "departure": b.get("departure"), "train": b.get("train_number")}
                for uid in ctx.users.ids() for b in ctx.users.bookings(uid)]
    monitors = [{"id": m.id, "user_id": m.chat_id, "user": names.get(m.chat_id, str(m.chat_id)),
                 "created": m.created, "state": "booked" if m.order_id else "active" if m.active else "stopped",
                 "route": f"{m.from_name} → {m.to_name}", "checks": m.checks}
                for m in ctx.monitors.monitors.values()]
    return web.json_response({"bookings": bookings, "monitors": monitors, "now": time.time()})


async def page(request: web.Request):
    return web.FileResponse(webapp.STATIC_DIR / "admin.html", headers={"Cache-Control": "no-cache"})


def add_routes(app: web.Application):
    app.router.add_get("/admin", page)
    app.router.add_get("/api/admin/users", users_list)
    app.router.add_get("/api/admin/users/{uid}", user_detail)
    app.router.add_post("/api/admin/users/{uid}", user_update)
    app.router.add_post("/api/admin/users/{uid}/access", user_access)
    app.router.add_post("/api/admin/users/{uid}/passengers", passenger_save)
    app.router.add_delete("/api/admin/users/{uid}/passengers/{index}", passenger_delete)
    app.router.add_get("/api/admin/stats", stats)
