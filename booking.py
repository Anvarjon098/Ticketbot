"""Bron qilish: joy topish, saytga yuborish va natijani yozib qo'yish (bot, ilova va kuzatuv uchun umumiy)."""
import logging
import re
import time

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from categories import group_code, name as category_name
from order_events import OrderEvents, is_sales_closed, message_text
from seats import SeatChoice, choose_seats
from site_auth import SiteSession
from uzrail import BASE_URL, AuthError, Passenger, UzRailClient, UzRailError, decode_jwt

log = logging.getLogger(__name__)


async def find_seats(client: UzRailClient, trip_date: str, from_code: str, to_code: str,
                     train: dict, count: int, car_type: str | None = None,
                     categories: list[str] | None = None) -> SeatChoice | None:
    """categories — ruxsat etilgan kategoriya kodlari (bo'sh/None — istalgani)."""
    places = await client.train_places(trip_date, from_code, to_code, train)
    return choose_seats(places or {}, count, None if car_type in (None, "*") else car_type, categories)


class ReservationFailed(UzRailError):
    """Sayt buyurtmani qabul qildi, lekin bron amalga oshmadi (sabab — sayt xabarida)."""


class SalesClosed(ReservationFailed):
    """Poyezd jo'nashiga oz qolgan — sayt bu reysga bron qilmaydi (qayta urinish foydasiz)."""


class OrderNotActive(UzRailError):
    """Buyurtmani bekor qilib bo'lmaydi: u saytda amalga oshmagan (FAILED)."""


async def _token(session: SiteSession, force: bool = False) -> str:
    return await session.get_token(force=force)


async def reserve(client: UzRailClient, session: SiteSession, train: dict,
                  choice: SeatChoice, passengers: list[dict], timeout: float = 90) -> str:
    """Bron qiladi va saytdan haqiqiy natijani kutadi.

    Sayt darhol buyurtma raqamini beradi, natija esa WebSocket orqali keladi. Bron bo'lmasa ReservationFailed
    (sayt sababi bilan). Kanal javob bermasa — buyurtma holati (snapshot) tekshiriladi.
    """
    args = (train, choice.group, choice.car, choice.seats, [Passenger(**p) for p in passengers])
    extra = {"tier": choice.tier} if choice.tier else {}
    token = await _token(session)
    async with OrderEvents(str(decode_jwt(token).get("id", ""))) as events:
        try:
            order_id = await client.reserve(token, *args, **extra)
        except AuthError:
            token = await _token(session, force=True)
            order_id = await client.reserve(token, *args, **extra)
        try:
            event = await events.wait(order_id, timeout)
        except Exception as e:  # kanal xatosi bronni yo'qotmasin — holat snapshot orqali tekshiriladi
            log.warning("Bron %s: kanal xatosi: %s", order_id, e)
            event = None
    if event is not None and event.get("status") != "Success":
        log.warning("Bron amalga oshmadi %s: %s", order_id, event)
        error = SalesClosed if is_sales_closed(event) else ReservationFailed
        raise error(f"Bron amalga oshmadi: {message_text(event, 'uz')}",
                                f"Бронь не оформлена: {message_text(event, 'ru')}")
    # Muvaffaqiyat xabari kelgan bo'lsa ham (yoki kanaldan javob kelmasa) — buyurtma holatini saytdan tekshiramiz.
    # Buyurtma raqami allaqachon bor: tekshiruv xato bersa (tarmoq, token) bronni yo'qotmaymiz — aks holda
    # kuzatuv buni "rad etildi" deb qayta bron qiladi. Haqiqiy holatni keyin taymer saytdan aniqlaydi.
    lifecycle = None
    for attempt in range(2):
        try:
            lifecycle = (await client.order_snapshot(token, order_id)).get("lifecycle")
            break
        except AuthError:
            try:
                token = await _token(session, force=True)
            except UzRailError as e:
                log.warning("Bron %s holatini tekshirib bo'lmadi (login): %s", order_id, e)
                break
        except UzRailError as e:
            log.warning("Bron %s holatini tekshirib bo'lmadi: %s", order_id, e)
            break
    if lifecycle == "FAILED":
        raise ReservationFailed("Bron amalga oshmadi (sayt buyurtmani rad etdi).",
                                "Бронь не оформлена (сайт отклонил заказ).")
    log.info("Bron %s: kanal=%s, holat=%s", order_id, event and event.get("state"), lifecycle)
    return order_id


async def cancel(client: UzRailClient, session: SiteSession, order_id: str) -> None:
    """Bronni bekor qiladi; sayt tokeni eskirgan bo'lsa qayta kirib, yana bir marta urinadi.

    Sayt rad etsa va buyurtma umuman amalga oshmagan bo'lsa (FAILED) — OrderNotActive; saytda allaqachon
    bekor qilingan bo'lsa (CANCELLED, masalan saytning o'zida) — xato emas, bekor qilingan deb hisoblanadi.
    """
    token = await _token(session)
    try:
        try:
            await client.cancel(token, order_id)
        except AuthError:
            token = await _token(session, force=True)
            await client.cancel(token, order_id)
    except UzRailError:
        try:
            lifecycle = (await client.order_snapshot(token, order_id)).get("lifecycle")
        except UzRailError:
            lifecycle = None
        if lifecycle == "CANCELLED":
            return
        if lifecycle == "FAILED":
            raise OrderNotActive("Bu bron saytda amalga oshmagan edi — bekor qilish shart emas, joy band qilinmagan.",
                                 "Эта бронь на сайте не была оформлена — отменять нечего, места не заняты.")
        raise


def cancel_markup(order_id: str) -> InlineKeyboardMarkup:
    """Bron haqidagi bot xabari ostidagi "bekor qilish" tugmasi."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        # callback_data 64 bayt bilan cheklangan — juda uzun raqam qisqartiriladi (bot prefiks bo'yicha topadi)
        InlineKeyboardButton(text="❌ Bronni bekor qilish", callback_data=f"cxl:{order_id}"[:60])]])


HOLD_MINUTES = 12  # sayt to'lanmagan bronni shuncha daqiqa saqlaydi
INACTIVE_STATUSES = ("cancelled", "failed", "expired")


def is_inactive(record: dict) -> bool:
    """Bekor qilingan, amalga oshmagan yoki to'lov muddati tugagan (to'lanmagan) bron — ro'yxatdan o'chirish mumkin."""
    if record.get("status") in INACTIVE_STATUSES:
        return True
    # taymer hali belgilamagan bo'lsa ham: to'lov muddati tugagan
    return not record.get("status") and bool(record.get("expires_at")) and record["expires_at"] <= time.time()


async def payment_deadline(client: UzRailClient, session: SiteSession, order_id: str) -> float:
    """To'lov muddati (unix vaqt): saytdagi aniq vaqt, bo'lmasa — hozirdan 12 daqiqa."""
    try:
        deadline = await client.payment_deadline(await session.get_token(), order_id)
    except UzRailError:
        deadline = None
    return deadline.timestamp() if deadline else time.time() + HOLD_MINUTES * 60


def pay_url(order_id: str) -> str:
    return f"{BASE_URL}/uz/pages/current-order/{order_id}"


def booking_record(order_id: str, train: dict, choice: SeatChoice, passengers: list[dict],
                   from_name: str, to_name: str) -> dict:
    return {
        "order_id": order_id,
        "train_number": train["number"],
        "brand": train.get("brand") or "",
        "departure": train["departureDate"],
        "arrival": train["arrivalDate"],
        "from_name": from_name,
        "to_name": to_name,
        "car_number": choice.car["number"],
        "category": group_code(choice.group),
        "car_type": category_name(group_code(choice.group), "uz", choice.group.get("typeShow")),
        "car_type_ru": category_name(group_code(choice.group), "ru", choice.group.get("typeShow")),
        "seats": choice.seats,
        "passengers": [Passenger(**p).full_name for p in passengers],
        "price": (choice.group.get("tariff") or 0) * len(passengers),
        "pay_url": pay_url(order_id),
    }


SERVICE_FEE = 50_000  # kuzatuv orqali avtomatik bron qilingan chipta uchun xizmat haqi (so'm)


def som(value: int) -> str:
    return f"{value:,}".replace(",", " ") + " so'm"


def total_price(b: dict) -> int:
    """To'lanadigan summa: chipta narxi + xizmat haqi (kuzatuv orqali bron bo'lsa)."""
    return (b.get("price") or 0) + (b.get("service_fee") or 0)


def booking_text(b: dict, with_pay: bool = True, pay_button: bool = False) -> str:
    """Bron tafsilotlari; with_pay=False — to'lov havolasisiz (to'lovni bot egasi qiladigan foydalanuvchi uchun).

    pay_button=True — havola o'rniga xabar ostidagi "Saytda to'lash" tugmasi (bot egasining akkaunti bilan).
    """
    text = (
        f"Poyezd {b['train_number']} {b['brand']}, {b['from_name']} → {b['to_name']}\n"
        f"Jo'nash: {b['departure']}\n"
        f"Vagon {b['car_number']} ({b['car_type']}), joylar: {', '.join(map(str, b['seats']))}\n"
        f"Yo'lovchilar: {', '.join(b['passengers'])}\n"
        f"Buyurtma: {b['order_id']}"
    )
    if b.get("price"):
        text += f"\nNarxi: ≈ {som(b['price'])}"
    if b.get("service_fee"):
        text += f"\nXizmat haqi: {som(b['service_fee'])}\nJami: ≈ {som(total_price(b))}"
    if not with_pay:
        return text
    if pay_button:
        return (text + "\n\n⏳ Bron vaqtinchalik — pastdagi «💳 Saytda to'lash» tugmasi bilan tezda to'lang "
                       "(sayt akkauntingiz bilan ochiladi).")
    return (text + "\n\n⏳ Bron vaqtinchalik — to'lovni saytda tezda amalga oshiring:\n"
            f"{b['pay_url']}\n"
            f"yoki {BASE_URL}/uz/cabinet/orders")


def with_pay_button(markup: InlineKeyboardMarkup, url: str) -> InlineKeyboardMarkup:
    """Tugmalar ustiga "Saytda to'lash" (sayt akkaunti bilan ochiladigan havola) qo'shadi."""
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💳 Saytda to'lash", url=url)],
                                                 *markup.inline_keyboard])


def approval_markup(order_id: str, paid: bool = False) -> InlineKeyboardMarkup:
    """Boshqa foydalanuvchining broni: bot egasi pulni olib saytda to'laydi yoki rad etadi.

    paid=True — saytda to'langan: faqat "Tasdiqlash" (chipta PDF egasiga yuboriladi). Tasdiqlash tugmasi
    to'lovdan oldin ko'rsatilmaydi.
    """
    if paid:
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Tasdiqlash — chiptani yuborish", callback_data=f"apr:{order_id}"[:60])]])
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💳 Karta raqamini yuborish", callback_data=f"crd:{order_id}"[:60]),
        InlineKeyboardButton(text="❌ Rad etish", callback_data=f"rej:{order_id}"[:60]),
    ]])


def transfer_markup(order_id: str, text: str = "✅ To'lov qildim") -> InlineKeyboardMarkup:
    """Foydalanuvchi: pulni kartaga o'tkazgach bot egasiga xabar beradi."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=text, callback_data=f"tpd:{order_id}"[:60])]])


FEE_BUTTON = "✅ Xizmat haqini o'tkazdim"


def fee_due(record: dict) -> bool:
    """Xizmat haqi bot egasiga hali o'tkazilmagan (foydalanuvchi o'zi saytda to'laydigan bron)."""
    return bool(record.get("fee_due")) and not record.get("fee_reported") and record.get("status") not in (
        "cancelled", "failed", "expired")


def fee_text(record: dict, card: dict | None) -> str:
    """Foydalanuvchiga: xizmat haqini bot egasining kartasiga o'tkazish."""
    fee = som(record.get("service_fee") or 0)
    if not card:
        return f"💼 Xizmat haqi: {fee} — bot egasiga o'tkazing (karta raqamini undan so'rang)."
    holder = f"\nKarta egasi: {card['holder']}" if card.get("holder") else ""
    return (f"💼 Xizmat haqi: {fee}\n"
            f"Karta: {format_card(card['number'])}{holder}\n"
            f"Chipta pulini saytda o'zingiz to'laysiz, xizmat haqini esa shu kartaga o'tkazing "
            f"va «{FEE_BUTTON}» ni bosing.")


def format_card(number: str) -> str:
    digits = re.sub(r"\D", "", number or "")
    return " ".join(digits[i:i + 4] for i in range(0, len(digits), 4))


def card_text(record: dict, card: dict) -> str:
    """Foydalanuvchiga: chipta pulini bot egasining kartasiga o'tkazish."""
    price = som(total_price(record))
    holder = f"\nKarta egasi: {card['holder']}" if card.get("holder") else ""
    return (f"💳 Chipta puli: ≈ {price}\n"
            f"Karta: {format_card(card['number'])}{holder}\n\n"
            "Pulni shu kartaga o'tkazing. Bot egasi chiptani saytda to'lab tasdiqlagach, chipta shu yerga yuboriladi.")
