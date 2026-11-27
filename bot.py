"""uzrailpass.uz orqali poyezd chiptasi uchun ketma-ket joy band qiluvchi Telegram bot."""
import asyncio
import html
import logging
import os
import re
from datetime import datetime, date, timedelta

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (BufferedInputFile, CallbackQuery, InputMediaPhoto, KeyboardButton, MenuButtonWebApp, Message,
                           ReplyKeyboardMarkup, ReplyKeyboardRemove, WebAppInfo)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from dotenv import load_dotenv

import booking
import car_schemas
import site_proxy
import webapp
from access import AccessStore
from car_render import CarRenderer
from timers import BookingTimers, is_paid
from monitor import Monitor, MonitorManager
from passengers import GENDER_LABEL, doc_type_label, guess_doc_type, parse_passenger_text
from categories import describe, train_categories
from site_auth import SiteSession, normalize_phone
from users import UserStore
from uzrail import Passenger, UzRailClient, UzRailError

load_dotenv()
BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
access = AccessStore(OWNER_ID, {int(x) for x in os.getenv("ALLOWED_IDS", "").replace(" ", "").split(",") if x})
ALLOWED_IDS = access.ids  # umumiy to'plam: bot egasi tugma bilan ruxsat bersa, darhol kuchga kiradi
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "1"))
WEBAPP_PORT = int(os.getenv("WEBAPP_PORT", "8080"))
WEBAPP_URL: str | None = None  # main() da aniqlanadi (WEBAPP_URL yoki cloudflared tunneli)

router = Router()
client = UzRailClient()
session = SiteSession()
users = UserStore()
renderer = CarRenderer()  # vagon sxemasi rasmi
monitors: MonitorManager  # main() da yaratiladi
timers: BookingTimers  # main() da yaratiladi

MENU_APP = "📱 Ilovani ochish"
MENU_BUY = "🎫 Bilet olish"
MENU_TICKETS = "🎟 Chiptalarim"
MENU_MONITORS = "🔔 Kuzatuvlarim"
MENU_STATUS = "ℹ️ Holat"
MENU_LOGIN = "🔑 Saytga kirish"
MENU_USERS = "👥 Foydalanuvchilar"
MENU_ADMIN = "🛠 Admin panel"
MENU_CANCEL = "❌ Bekor qilish"

# (tugma matni, saytdagi stansiya nomi)
POPULAR_STATIONS = [("Toshkent", "TOSHKENT"), ("Samarqand", "SAMARQAND"), ("Buxoro", "BUXORO"),
                    ("Navoiy", "NAVOIY"), ("Qarshi", "QARSHI"), ("Termiz", "TERMIZ"),
                    ("Andijon", "ANDIJON"), ("Namangan", "NAMANGAN"), ("Qo'qon", "QOQON"),
                    ("Jizzax", "JIZZAX"), ("Urganch", "URGENCH"), ("Xiva", "XIVA"), ("Nukus", "NUKUS")]
WEEKDAYS = ["Du", "Se", "Ch", "Pa", "Ju", "Sh", "Ya"]


class Reg(StatesGroup):
    phone = State()
    site_password = State()


class Login(StatesGroup):
    phone = State()
    password = State()


class AdminEdit(StatesGroup):
    """Bot egasi admin panelda foydalanuvchi ma'lumotini o'zgartiradi (data: uid, idx)."""
    name = State()
    phone = State()
    passenger = State()


class Booking(StatesGroup):
    select = State()
    p_data = State()
    p_confirm = State()
    from_station = State()
    to_station = State()
    trip_date = State()
    train = State()
    car_type = State()
    seats = State()  # vagon sxemasidan joy tanlash
    confirm = State()


# ---------- klaviaturalar ----------

def main_menu(user_id: int) -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    if WEBAPP_URL:
        kb.button(text=MENU_APP)  # bosilganda joriy manzil bilan tugma yuboriladi (tunnel manzili o'zgaradi)
    kb.button(text=MENU_BUY)
    kb.button(text=MENU_TICKETS)
    kb.button(text=MENU_MONITORS)
    kb.button(text=MENU_STATUS)
    if user_id == OWNER_ID:
        if WEBAPP_URL:  # bosilganda xabarsiz, darhol admin ilovasi ochiladi (kalit — webapp.admin_key)
            url = f"{WEBAPP_URL.rstrip('/')}/admin?k={webapp.admin_key(BOT_TOKEN, OWNER_ID)}"
            kb.button(text=MENU_ADMIN, web_app=WebAppInfo(url=url))
        else:
            kb.button(text=MENU_ADMIN)
        kb.button(text=MENU_LOGIN)
    kb.button(text=MENU_CANCEL)
    kb.adjust(*([1] if WEBAPP_URL else []), 1, 2, 2, 2)
    return kb.as_markup(resize_keyboard=True)


def contact_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Telefon raqamni yuborish", request_contact=True)]],
        resize_keyboard=True, one_time_keyboard=True,
    )


def monitor_buttons(kb: InlineKeyboardBuilder):
    kb.button(text="🔔 Kuzatish: faqat ketma-ket joy", callback_data="mon:1")
    kb.button(text="🔔 Kuzatish: bir vagonda bo'lsa yetarli", callback_data="mon:0")


def station_keyboard(exclude: str | None = None):
    kb = InlineKeyboardBuilder()
    for i, (label, site_name) in enumerate(POPULAR_STATIONS):
        if site_name != (exclude or "").upper():
            kb.button(text=label, callback_data=f"pop:{i}")
    kb.adjust(3)
    return kb.as_markup()


def date_keyboard():
    kb = InlineKeyboardBuilder()
    today = date.today()
    for i in range(10):
        d = today + timedelta(days=i)
        label = "Bugun" if i == 0 else "Ertaga" if i == 1 else WEEKDAYS[d.weekday()]
        kb.button(text=f"{label} {d:%d.%m}", callback_data=f"date:{d.isoformat()}")
    kb.adjust(2)
    return kb.as_markup()


# ---------- yordamchi funksiyalar ----------

def money(value: int) -> str:
    return f"{value:,}".replace(",", " ") + " so'm"


def free_seats(t: dict) -> int:
    return sum(c.get("freeSeats", 0) for c in t.get("cars") or [])


def train_label(t: dict) -> str:
    dep = t["departureDate"][-5:]
    arv = t["arrivalDate"][-5:]
    free = free_seats(t)
    seats = f"{free} joy" if free else "🔴 joy yo'q"
    return f"{dep} → {arv} | {t['number']} {t.get('brand') or ''} | {seats}"


def passenger_name(p: dict) -> str:
    return html.escape(Passenger(**p).full_name)


def passengers_from(data: dict) -> list[Passenger]:
    return [Passenger(**p) for p in data["passengers"]]


# ---------- ruxsat ----------

def access_keyboard(user_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Ruxsat berish", callback_data=f"acc:ok:{user_id}")
    kb.button(text="❌ Rad etish", callback_data=f"acc:no:{user_id}")
    return kb.as_markup()


@router.message(~F.from_user.id.in_(ALLOWED_IDS))
async def not_allowed(message: Message):
    user = message.from_user
    status = access.status(user.id)
    if status == "pending":
        await message.answer("⏳ So'rovingiz bot egasiga yuborilgan. Ruxsat berilishi bilan sizga xabar keladi.")
        return
    if status == "denied":
        await message.answer("Sizga bu botdan foydalanishga ruxsat berilmagan.")
        return
    access.request(user.id, user.full_name, user.username)
    await message.answer(
        f"Bu bot faqat ruxsat berilgan foydalanuvchilar uchun.\n"
        f"Sizning Telegram ID: <code>{user.id}</code>\n\n"
        "✉️ Bot egasiga so'rov yuborildi. Ruxsat berilishi bilan sizga xabar keladi.")
    username = f" (@{user.username})" if user.username else ""
    await message.bot.send_message(
        OWNER_ID,
        f"👤 Yangi foydalanuvchi botdan foydalanmoqchi:\n<b>{html.escape(user.full_name)}</b>{html.escape(username)}\n"
        f"ID: <code>{user.id}</code>\n\n"
        "Ruxsat bersangiz, u bilet olishi va kuzatuv qo'yishi mumkin bo'ladi "
        "(bronlar sizning sayt akkauntingiz orqali qilinadi).",
        reply_markup=access_keyboard(user.id))


@router.callback_query(~F.from_user.id.in_(ALLOWED_IDS))
async def not_allowed_call(call: CallbackQuery):
    await call.answer("Ruxsat yo'q", show_alert=True)


async def apply_access(bot: Bot, user_id: int, grant: bool) -> str:
    """Ruxsat beradi yoki oladi (kuzatuvlari to'xtatiladi) va foydalanuvchiga xabar beradi. Natija fe'li."""
    if grant:
        access.grant(user_id)
        try:
            if WEBAPP_URL:
                await bot.set_chat_menu_button(
                    chat_id=user_id, menu_button=MenuButtonWebApp(text="Ilova", web_app=WebAppInfo(url=WEBAPP_URL)))
            await bot.send_message(user_id, "✅ Bot egasi sizga ruxsat berdi! Boshlash uchun /start ni bosing.")
        except Exception:
            logging.warning("Foydalanuvchiga xabar yuborilmadi: %s", user_id)
        return "Ruxsat berildi"
    was_allowed = user_id in ALLOWED_IDS
    access.deny(user_id)
    for m in [m for m in monitors.monitors.values() if m.chat_id == user_id]:
        monitors.remove(m.id)  # ruxsati olinganning kuzatuvlari to'xtatiladi
    try:
        await bot.send_message(user_id, "Sizga bu botdan foydalanishga ruxsat berilmagan.",
                               reply_markup=ReplyKeyboardRemove())
    except Exception:
        logging.warning("Foydalanuvchiga xabar yuborilmadi: %s", user_id)
    return "Ruxsat olib qo'yildi" if was_allowed else "Rad etildi"


@router.callback_query(F.data.startswith("acc:"), F.from_user.id == OWNER_ID)
async def access_decision(call: CallbackQuery):
    """Yangi foydalanuvchi so'rovi xabaridagi tugmalar."""
    _, action, uid = call.data.split(":")
    user_id = int(uid)
    if user_id == OWNER_ID:
        await call.answer("O'zingizning ruxsatingizni olib bo'lmaydi", show_alert=True)
        return
    verb = await apply_access(call.bot, user_id, action == "ok")
    icon = "✅" if action == "ok" else "🚫"
    await call.message.edit_text(f"{icon} {verb}: <b>{html.escape(access.label(user_id))}</b> (ID <code>{user_id}</code>)")
    await call.answer(verb)


# ---------- 1-qadam: telefon raqam ----------

@router.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    if WEBAPP_URL:  # ilova tugmasini shu chat uchun joriy manzilga yangilaymiz
        try:
            await message.bot.set_chat_menu_button(
                chat_id=message.chat.id, menu_button=MenuButtonWebApp(text="Ilova", web_app=WebAppInfo(url=WEBAPP_URL)))
        except Exception:
            logging.warning("Menyu tugmasi o'rnatilmadi: %s", message.chat.id)
    phone = users.get(message.from_user.id).get("phone")
    if not phone:
        await message.answer(
            "Assalomu alaykum! Bu bot <b>eticket.uzrailpass.uz</b> saytidan barcha yo'lovchilar uchun "
            "<b>ketma-ket joylarni</b> topib bron qiladi, joyi tugagan reyslarni kuzatib, "
            "joy chiqishi bilan avtomatik bron qiladi.\n\n"
            "Boshlash uchun pastdagi <b>📱 Telefon raqamni yuborish</b> tugmasini bosing.",
            reply_markup=contact_keyboard(),
        )
        await state.set_state(Reg.phone)
        return
    await message.answer(f"Xush kelibsiz! Raqamingiz: +{phone}\nKerakli bo'limni tanlang 👇",
                         reply_markup=main_menu(message.from_user.id))


@router.message(Reg.phone, F.contact)
async def got_contact(message: Message, state: FSMContext):
    if message.contact.user_id != message.from_user.id:
        await message.answer("Iltimos, o'zingizning raqamingizni tugma orqali yuboring.",
                             reply_markup=contact_keyboard())
        return
    phone = normalize_phone(message.contact.phone_number)
    users.update(message.from_user.id, phone=phone)
    if message.from_user.id == OWNER_ID and not session.account:
        kb = InlineKeyboardBuilder()
        kb.button(text="⏭ Keyinroq", callback_data="skip_login")
        await message.answer(f"✅ Raqam saqlandi: +{phone}", reply_markup=main_menu(message.from_user.id))
        await message.answer(
            "Bot saytga shu raqam orqali kiradi. <b>eticket.uzrailpass.uz</b> dagi parolingizni yuboring "
            "(xabar darhol o'chiriladi):",
            reply_markup=kb.as_markup(),
        )
        await state.update_data(phone=phone)
        await state.set_state(Reg.site_password)
        return
    await state.clear()
    await message.answer(f"✅ Raqam saqlandi: +{phone}\nKerakli bo'limni tanlang 👇",
                         reply_markup=main_menu(message.from_user.id))


@router.message(Reg.phone)
async def contact_expected(message: Message):
    await message.answer("Pastdagi <b>📱 Telefon raqamni yuborish</b> tugmasini bosing.",
                         reply_markup=contact_keyboard())


@router.callback_query(Reg.site_password, F.data == "skip_login")
async def skip_login(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text(f"Mayli. Keyinroq <b>{MENU_LOGIN}</b> tugmasi orqali kirishingiz mumkin.")


# ---------- menyu tugmalari (istalgan holatda ishlaydi) ----------

@router.message(F.text == MENU_CANCEL)
@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Bekor qilindi.", reply_markup=main_menu(message.from_user.id))


@router.message(F.text == MENU_APP)
async def open_app(message: Message):
    if not WEBAPP_URL:
        await message.answer("Ilova hozircha ishlamayapti.")
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="📱 Ilovani ochish", web_app=WebAppInfo(url=WEBAPP_URL))
    await message.answer("Ilovani ochish uchun tugmani bosing 👇", reply_markup=kb.as_markup())


# ---------- admin panel (faqat bot egasi): foydalanuvchilar, saytga kirish, ma'lumotlarni tahrirlash ----------

def user_name(uid: int) -> str:
    return users.display_name(uid) or access.info(uid).get("name") or "—"


def admin_panel():
    """Admin panel: ilova (foydalanuvchilar, qidiruv, statistika) va botdagi tezkor tugmalar."""
    kb = InlineKeyboardBuilder()
    if WEBAPP_URL:  # pastki menyu tugmasidan ochilgan ilovaga Telegram foydalanuvchi ma'lumotini bermaydi
        kb.button(text="🛠 Admin panelni ochish", web_app=WebAppInfo(url=WEBAPP_URL.rstrip("/") + "/admin"))
    kb.button(text=MENU_USERS, callback_data="adm:users")
    kb.button(text=MENU_LOGIN, callback_data="adm:login")
    kb.adjust(*([1] if WEBAPP_URL else []), 2)
    return (f"🛠 <b>Admin panel</b>\n\nSayt: {html.escape(session.status())}\n"
            f"Foydalanuvchilar: {len(ALLOWED_IDS - {OWNER_ID})} ta ruxsat berilgan"), kb.as_markup()


def users_list():
    """Barcha foydalanuvchilar bitta xabarda (raqamlangan ro'yxat): tahrirlash va ruxsat tugmalari bilan."""
    groups = [("⏳ So'rov kutmoqda", access.listed("pending")),
              ("✅ Ruxsat berilgan", sorted(ALLOWED_IDS - {OWNER_ID})),
              ("🚫 Ruxsat yo'q", access.listed("denied"))]
    total = sum(len(ids) for _, ids in groups)
    kb = InlineKeyboardBuilder()
    if not total:
        kb.button(text="⬅️ Admin panel", callback_data="adm:home")
        return ("Hozircha botdan faqat siz foydalanasiz.\n"
                "Boshqa odam botga yozsa, sizga <b>✅ Ruxsat berish</b> tugmasi bilan so'rov keladi."), kb.as_markup()
    lines, n, sizes = [f"👥 <b>Foydalanuvchilar ({total})</b>"], 0, []
    for title, ids in groups:
        if not ids:
            continue
        lines.append(f"\n<b>{title}:</b>")
        for uid in ids:
            n += 1
            info = access.info(uid)
            name = user_name(uid)
            username = f"@{info['username']}" if info.get("username") else "username yo'q"
            phone = users.get(uid).get("phone")
            lines.append(f"{n}. <b>{html.escape(name, quote=False)}</b> — {html.escape(username, quote=False)}\n"
                         f"    ID <code>{uid}</code>" + (f", +{phone}" if phone else ""))
            if title.startswith("⏳"):
                kb.button(text=f"✅ {n}. {name[:18]}", callback_data=f"acl:ok:{uid}")
                kb.button(text=f"❌ {n}", callback_data=f"acl:no:{uid}")
            else:
                kb.button(text=f"✏️ {n}. {name[:18]}", callback_data=f"adu:{uid}")
                kb.button(text=f"🚫 {n}" if title.startswith("✅") else f"✅ {n}",
                          callback_data=f"acl:{'no' if title.startswith('✅') else 'ok'}:{uid}")
            sizes.append(2)
    kb.button(text="⬅️ Admin panel", callback_data="adm:home")
    kb.adjust(*sizes, 1)
    lines.append("\n✏️ — ma'lumotlarini ko'rish va o'zgartirish, 🚫 / ✅ — ruxsatni olish / berish")
    return "\n".join(lines), kb.as_markup()


def passenger_line(p: dict) -> str:
    born = date.fromisoformat(p["birthday"])
    doc = doc_type_label(p.get("doc_type") or guess_doc_type(p["passport"]))
    return (f"{html.escape(Passenger(**p).full_name)} — {born:%d.%m.%Y}, {GENDER_LABEL[p['gender']]}, "
            f"{doc} {p['passport']}")


def user_card(uid: int):
    """Bitta foydalanuvchi: ma'lumotlari va ularni o'zgartirish tugmalari."""
    info, user = access.info(uid), users.get(uid)
    status = {"allowed": "✅ ruxsat berilgan", "pending": "⏳ so'rov kutmoqda",
              "denied": "🚫 ruxsat yo'q"}.get(access.status(uid), "—")
    saved = users.passengers(uid)
    plist = "\n".join(f"{i + 1}. {passenger_line(p)}" + (" <i>(o'zi)</i>" if i == 0 else "")
                      for i, p in enumerate(saved)) or "—"
    text = (f"👤 <b>{html.escape(user_name(uid), quote=False)}</b>"
            + (f" — @{html.escape(info['username'])}" if info.get("username") else "") + "\n"
            f"ID <code>{uid}</code>\n"
            f"Telefon: {'+' + user['phone'] if user.get('phone') else '—'}\n"
            f"Holat: {status}\n"
            f"Bronlar: {len(user.get('bookings', []))} ta\n\n"
            f"<b>Yo'lovchilar:</b>\n{plist}")
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Ism", callback_data=f"aun:{uid}")
    kb.button(text="📞 Telefon", callback_data=f"auf:{uid}")
    for i, p in enumerate(saved):
        kb.button(text=f"👤 {i + 1}. {Passenger(**p).full_name[:28]}", callback_data=f"adp:{uid}:{i}")
    kb.button(text="➕ Yo'lovchi qo'shish", callback_data=f"adn:{uid}")
    allowed = uid in ALLOWED_IDS
    kb.button(text="🚫 Ruxsatni olish" if allowed else "✅ Ruxsat berish",
              callback_data=f"acu:{'no' if allowed else 'ok'}:{uid}")
    kb.button(text="⬅️ Ro'yxatga", callback_data="adm:users")
    kb.adjust(2, *([1] * len(saved)), 1, 2)
    return text, kb.as_markup()


async def show_user_card(message: Message, uid: int, edit: bool = False):
    text, markup = user_card(uid)
    if edit:
        await message.edit_text(text, reply_markup=markup)
    else:
        await message.answer(text, reply_markup=markup)


@router.message(F.text == MENU_ADMIN, F.from_user.id == OWNER_ID)
async def admin_home(message: Message, state: FSMContext):
    await state.clear()
    text, markup = admin_panel()
    await message.answer(text, reply_markup=markup)


@router.message(F.text == MENU_USERS, F.from_user.id == OWNER_ID)
async def list_users(message: Message):
    text, markup = users_list()
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("adm:"), F.from_user.id == OWNER_ID)
async def admin_nav(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer()
    action = call.data.split(":", 1)[1]
    if action == "login":
        await ask_site_login(call.message, state)
        return
    text, markup = admin_panel() if action == "home" else users_list()
    await call.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data.startswith("acl:"), F.from_user.id == OWNER_ID)
async def list_users_decision(call: CallbackQuery):
    """Ro'yxatdagi tugma: ruxsat beradi / oladi va o'sha xabardagi ro'yxatni yangilaydi."""
    _, action, uid = call.data.split(":")
    if int(uid) == OWNER_ID:
        await call.answer("O'zingizning ruxsatingizni olib bo'lmaydi", show_alert=True)
        return
    verb = await apply_access(call.bot, int(uid), action == "ok")
    await call.answer(f"{verb}: {access.label(int(uid))}")
    text, markup = users_list()
    await call.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data.startswith("acu:"), F.from_user.id == OWNER_ID)
async def user_card_access(call: CallbackQuery):
    """Foydalanuvchi kartasidagi ruxsat tugmasi."""
    _, action, uid = call.data.split(":")
    if int(uid) == OWNER_ID:
        await call.answer("O'zingizning ruxsatingizni olib bo'lmaydi", show_alert=True)
        return
    verb = await apply_access(call.bot, int(uid), action == "ok")
    await call.answer(verb)
    await show_user_card(call.message, int(uid), edit=True)


@router.callback_query(F.data.startswith("adu:"), F.from_user.id == OWNER_ID)
async def admin_user(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer()
    await show_user_card(call.message, int(call.data.split(":")[1]), edit=True)


@router.callback_query(F.data.startswith("adp:"), F.from_user.id == OWNER_ID)
async def admin_passenger(call: CallbackQuery):
    _, uid, i = call.data.split(":")
    saved = users.passengers(int(uid))
    if int(i) >= len(saved):
        await call.answer("Yo'lovchi topilmadi", show_alert=True)
        return
    await call.answer()
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ O'zgartirish", callback_data=f"ape:{uid}:{i}")
    kb.button(text="🗑 O'chirish", callback_data=f"apd:{uid}:{i}")
    kb.button(text="⬅️ Orqaga", callback_data=f"adu:{uid}")
    kb.adjust(2, 1)
    who = " (foydalanuvchining o'zi)" if i == "0" else ""
    await call.message.edit_text(f"👤 <b>{html.escape(user_name(int(uid)), quote=False)}</b> — "
                                 f"{int(i) + 1}-yo'lovchi{who}:\n\n{passenger_card(saved[int(i)])}",
                                 reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("apd:"), F.from_user.id == OWNER_ID)
async def admin_passenger_delete(call: CallbackQuery):
    _, uid, i = call.data.split(":")
    saved = users.passengers(int(uid))
    if int(i) < len(saved):
        removed = users.remove_passenger(int(uid), saved[int(i)]["passport"])
        await call.answer(f"O'chirildi: {Passenger(**removed).full_name}")
    await show_user_card(call.message, int(uid), edit=True)


@router.callback_query(F.data.regexp(r"^(aun|auf|ape|adn):"), F.from_user.id == OWNER_ID)
async def admin_edit_start(call: CallbackQuery, state: FSMContext):
    """O'zgartirish: yangi qiymat keyingi xabarda yuboriladi (bekor qilish — ❌ Bekor qilish)."""
    kind, uid, *rest = call.data.split(":")
    idx = int(rest[0]) if rest else None
    await state.clear()
    await state.update_data(uid=int(uid), idx=idx)
    await call.answer()
    name = html.escape(user_name(int(uid)), quote=False)
    if kind == "aun":
        await call.message.answer(f"<b>{name}</b> — yangi ismni yozing:")
        await state.set_state(AdminEdit.name)
    elif kind == "auf":
        await call.message.answer(f"<b>{name}</b> — yangi telefon raqamni yozing (masalan <code>+998901234567</code>):")
        await state.set_state(AdminEdit.phone)
    else:
        what = f"{idx + 1}-yo'lovchining yangi ma'lumotini" if idx is not None else "yangi yo'lovchi ma'lumotini"
        await call.message.answer(f"<b>{name}</b> — {what} kiriting.\n{PASSENGER_FORM}")
        await state.set_state(AdminEdit.passenger)


@router.message(AdminEdit.name, F.text, F.from_user.id == OWNER_ID)
async def admin_edit_name(message: Message, state: FSMContext):
    name = " ".join(message.text.split())[:60]
    if not name:
        await message.answer("Ism bo'sh bo'lmasin — qayta yozing:")
        return
    uid = (await state.get_data())["uid"]
    users.update(uid, display_name=name)
    await state.clear()
    await message.answer("✅ Ism saqlandi.")
    await show_user_card(message, uid)


@router.message(AdminEdit.phone, F.text, F.from_user.id == OWNER_ID)
async def admin_edit_phone(message: Message, state: FSMContext):
    try:
        phone = normalize_phone(message.text)
    except ValueError as e:
        await message.answer(f"{html.escape(str(e))}\nQayta yozing:")
        return
    uid = (await state.get_data())["uid"]
    users.update(uid, phone=phone)
    await state.clear()
    await message.answer("✅ Telefon raqam saqlandi.")
    await show_user_card(message, uid)


@router.message(AdminEdit.passenger, F.text, F.from_user.id == OWNER_ID)
async def admin_edit_passenger(message: Message, state: FSMContext):
    try:
        passenger = parse_passenger_text(message.text)
    except ValueError as e:
        await message.answer(f"Quyidagilar topilmadi yoki noto'g'ri:\n{html.escape(str(e))}\n\n{PASSENGER_FORM}")
        return
    data = await state.get_data()
    uid, idx = data["uid"], data["idx"]
    if idx is not None and idx < len(users.passengers(uid)):
        users.replace_passenger(uid, idx, passenger)
    else:  # yangi: ro'yxat bo'sh bo'lsa — foydalanuvchining o'zi
        users.add_passenger(uid, passenger, is_self=not users.passengers(uid))
    await state.clear()
    await message.answer(f"✅ Saqlandi:\n\n{passenger_card(passenger)}")
    await show_user_card(message, uid)


@router.message(F.text == MENU_STATUS)
@router.message(Command("status"))
async def status(message: Message):
    await message.answer(
        f"Sayt akkaunti: {html.escape(session.status())}\n"
        f"Faol kuzatuvlar: {len(monitors.active())} (umumiy tezlik: {1 / POLL_INTERVAL:g} so'rov/sek)"
    )


@router.message(F.text == MENU_MONITORS)
@router.message(Command("monitors"))
async def list_monitors(message: Message):
    own = [m for m in monitors.monitors.values() if message.from_user.id in (OWNER_ID, m.chat_id)]
    if not own:
        await message.answer("Kuzatuvlar yo'q.")
        return
    for m in own:
        state = (f"✅ bron qilingan: {m.order_id}" if m.order_id
                 else f"🔔 faol, {m.checks} marta tekshirildi" if m.active else "⏹ to'xtatilgan")
        kb = InlineKeyboardBuilder()
        kb.button(text="🗑 O'chirish", callback_data=f"del:{m.id}")
        await message.answer(f"{html.escape(m.title())}\n{state}", reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("del:"))
async def delete_monitor(call: CallbackQuery):
    m = monitors.monitors.get(call.data.split(":", 1)[1])
    if not m or call.from_user.id not in (OWNER_ID, m.chat_id):
        await call.answer("Topilmadi")
        return
    monitors.remove(m.id)
    await call.message.edit_text(f"🗑 O'chirildi: {html.escape(m.title())}")


# ---------- bronlar va ularni bekor qilish ----------

@router.message(F.text == MENU_TICKETS)
@router.message(Command("tickets"))
async def list_bookings(message: Message):
    bookings = users.bookings(message.from_user.id)
    if not bookings:
        await message.answer("Bron qilingan chiptalar yo'q.")
        return
    labels = {"cancelled": "❌ BEKOR QILINGAN", "failed": "⚠️ SAYTDA AMALGA OSHMAGAN",
              "expired": "⌛ MUDDATI TUGAGAN (to'lanmagan)", "paid": "✅ TO'LANGAN"}
    for b in bookings[:10]:
        status = b.get("status") or ("expired" if booking.is_inactive(b) else None)
        if status in labels:
            await message.answer(labels[status] + "\n" + html.escape(booking.booking_text(b).split("\n\n")[0]),
                                 reply_markup=remove_markup(b["order_id"]) if booking.is_inactive(b) else None)
            continue
        text, markup = timers.view(b, message.chat.id)  # foydalanuvchining tasdiq kutayotgan broni — to'lovsiz
        sent = await message.answer(text, parse_mode=None, reply_markup=markup)
        if b.get("expires_at"):
            timers.start(message.chat.id, sent.message_id, b["order_id"])
    inactive = sum(booking.is_inactive(b) for b in bookings)
    if inactive:
        kb = InlineKeyboardBuilder()
        kb.button(text=f"🧹 Hammasini tozalash ({inactive})", callback_data="bclr")
        await message.answer("Bekor qilingan, amalga oshmagan va muddati tugagan bronlarni ro'yxatdan o'chirish "
                             "(saytdagi buyurtmalarga ta'sir qilmaydi):", reply_markup=kb.as_markup())


def remove_markup(order_id: str):
    kb = InlineKeyboardBuilder()
    kb.button(text="🗑 Ro'yxatdan o'chirish", callback_data=f"bdel:{order_id}"[:60])
    return kb.as_markup()


@router.callback_query(F.data.startswith("bdel:"))
async def remove_booking(call: CallbackQuery):
    found = _find_own_booking(call.from_user.id, call.data.split(":", 1)[1])
    if not found:
        await call.answer("Bron topilmadi", show_alert=True)
        return
    owner_id, b = found
    if not booking.is_inactive(b):
        await call.answer("Faol yoki to'langan bronni o'chirib bo'lmaydi — avval bekor qiling.", show_alert=True)
        return
    users.remove_bookings(owner_id, {b["order_id"]})
    await call.answer("Ro'yxatdan o'chirildi")
    try:
        await call.message.delete()
    except Exception:
        await call.message.edit_text(f"🗑 Ro'yxatdan o'chirildi: {html.escape(b['order_id'])}")


@router.callback_query(F.data == "bclr")
async def clear_bookings(call: CallbackQuery):
    uid = call.from_user.id
    removed = users.remove_bookings(uid, {b["order_id"] for b in users.bookings(uid) if booking.is_inactive(b)})
    await call.answer()
    await call.message.edit_text(f"🧹 {removed} ta bron ro'yxatdan o'chirildi.")


def _find_own_booking(user_id: int, order_prefix: str) -> tuple[int, dict] | None:
    """Callback'dagi buyurtma raqami (64 bayt chekloviga ko'ra qisqargan bo'lishi mumkin) bo'yicha bronni topadi."""
    found = users.find_booking(order_prefix)
    if not found:
        for uid in users.ids():
            for b in users.bookings(uid):
                if str(b.get("order_id", "")).startswith(order_prefix):
                    found = (uid, b)
    if found and user_id in (OWNER_ID, found[0]):
        return found
    return None


async def cancel_order(call: CallbackQuery, owner_id: int, b: dict, approval: str | None = None) -> bool:
    """Bronni saytda bekor qiladi va barcha bot xabarlarini (foydalanuvchida ham) yakunlaydi."""
    try:
        await booking.cancel(client, session, b["order_id"])
    except booking.OrderNotActive as e:
        users.set_status(owner_id, b["order_id"], "failed")
        await timers.finalize(b["order_id"])
        await call.message.answer(html.escape(str(e)))
        return False
    except UzRailError as e:
        await call.message.edit_reply_markup(reply_markup=timers.view(b, call.message.chat.id)[1])
        await call.message.answer(
            f"❌ Bekor qilib bo'lmadi: {html.escape(str(e))}\n"
            "To'langan chiptani saytning o'zida qaytarish kerak (Buyurtmalarim → Qaytarish).")
        return False
    if approval:
        users.update_booking(owner_id, b["order_id"], approval=approval)
    users.mark_cancelled(owner_id, b["order_id"])
    await timers.finalize(b["order_id"])
    return True


# ---------- boshqa foydalanuvchilarning bronlarini tasdiqlash (bot egasi) ----------

def _awaiting_ticket(call: CallbackQuery) -> tuple[int, dict] | None:
    """Chiptasi hali yuborilmagan (to'lanmagan yoki to'langan) boshqa foydalanuvchi broni."""
    found = _find_own_booking(call.from_user.id, call.data.split(":", 1)[1])
    if found and found[1].get("approval") == "pending" and found[1].get("status") in (None, "paid"):
        return found
    return None


@router.callback_query(F.data.startswith("apr:"), F.from_user.id == OWNER_ID)
async def approve_booking(call: CallbackQuery):
    """Bot egasi saytda to'lagan (tugma faqat to'lovdan keyin chiqadi) — chipta PDF ini egasiga yuboradi."""
    found = _awaiting_ticket(call)
    if not found:
        await call.answer("Bron topilmadi yoki allaqachon yakunlangan", show_alert=True)
        return
    b = found[1]
    if b.get("status") != "paid":  # eski xabardagi tugma — saytdan tekshiramiz
        lifecycle = await timers.lifecycle(b["order_id"])
        if not is_paid(lifecycle):
            await call.answer(f"Sayt bu bronni hali to'langan deb ko'rsatmayapti (holat: {lifecycle or 'nomalum'}). "
                              "Avval «💳 Saytda to'lash» orqali to'lang.", show_alert=True)
            return
        users.set_status(found[0], b["order_id"], "paid")
        await timers.finalize(b["order_id"])  # PDF hali tayyor bo'lmasa ham "Tasdiqlash" tugmasi qolsin
    await call.answer("Chipta yuborilmoqda...")
    try:
        sent = await timers.deliver(b["order_id"])
    except (UzRailError, TelegramAPIError) as e:
        await call.message.answer(f"❌ Chipta yuborilmadi: {html.escape(str(e))}\n«✅ Tasdiqlash» ni qayta bosing.")
        return
    await call.message.answer("✅ Chipta (PDF) egasiga yuborildi." if sent else
                              "⚠️ Sayt chipta PDF faylini hali bermadi — biroz kutib «✅ Tasdiqlash» ni qayta bosing.")


@router.callback_query(F.data.startswith("crd:"), F.from_user.id == OWNER_ID)
async def send_card(call: CallbackQuery):
    found = _awaiting_ticket(call)
    if not found or found[1].get("status"):
        await call.answer("Bron topilmadi yoki allaqachon yakunlangan", show_alert=True)
        return
    if not timers.card():
        await call.answer("Karta raqamingiz kiritilmagan: Ilova → Profil → To'lov kartasi", show_alert=True)
        return
    try:
        await timers.send_card(found[1]["order_id"])
    except TelegramAPIError as e:
        await call.answer(f"Yuborilmadi: {e}", show_alert=True)
        return
    await call.answer("Karta raqami yuborildi")


@router.callback_query(F.data.startswith("tpd:"))
async def transfer_done(call: CallbackQuery):
    """Foydalanuvchi chipta pulini bot egasining kartasiga o'tkazdi."""
    found = _find_own_booking(call.from_user.id, call.data.split(":", 1)[1])
    if not found or not await timers.report_transfer(found[1]["order_id"], call.from_user.id):
        await call.answer("Bron topilmadi, yakunlangan yoki xabar allaqachon berilgan", show_alert=True)
        return
    await call.answer("Bot egasiga xabar berildi — u tekshirib, chiptani to'laydi", show_alert=True)
    try:  # alohida karta xabaridagi tugma (bron xabarini refresh() allaqachon yangilagan)
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass


@router.callback_query(F.data.startswith("rej:"), F.from_user.id == OWNER_ID)
async def reject_booking_ask(call: CallbackQuery):
    prefix = call.data.split(":", 1)[1]
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Ha, rad etish", callback_data=f"rjy:{prefix}"[:60])
    kb.button(text="↩️ Yo'q", callback_data=f"rjn:{prefix}"[:60])
    kb.adjust(2)
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("rjn:"), F.from_user.id == OWNER_ID)
async def reject_booking_no(call: CallbackQuery):
    await call.answer()
    found = _find_own_booking(call.from_user.id, call.data.split(":", 1)[1])  # "Saytda to'lash" ham qaytadi
    await call.message.edit_reply_markup(reply_markup=timers.view(found[1], call.message.chat.id)[1] if found else None)


@router.callback_query(F.data.startswith("rjy:"), F.from_user.id == OWNER_ID)
async def reject_booking_yes(call: CallbackQuery):
    found = _find_own_booking(call.from_user.id, call.data.split(":", 1)[1])
    if not found or found[1].get("status"):
        await call.answer("Bron topilmadi yoki allaqachon yakunlangan", show_alert=True)
        return
    await call.answer("Rad etilmoqda...")
    await cancel_order(call, found[0], found[1], approval="rejected")


@router.callback_query(F.data.startswith("cxl:"))
async def cancel_booking_ask(call: CallbackQuery):
    prefix = call.data.split(":", 1)[1]
    found = _find_own_booking(call.from_user.id, prefix)
    if not found:
        await call.answer("Bron topilmadi", show_alert=True)
        return
    if found[1].get("status") in ("cancelled", "failed"):
        await call.answer("Bu bron allaqachon bekor qilingan yoki amalga oshmagan", show_alert=True)
        return
    if found[1].get("approval") and call.from_user.id != OWNER_ID:
        await call.answer("Bu bronni bot egasi boshqaradi — bekor qilish uchun unga yozing.", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Ha, bekor qilish", callback_data=f"cxy:{prefix}")
    kb.button(text="↩️ Yo'q", callback_data=f"cxn:{prefix}")
    kb.adjust(2)
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("cxn:"))
async def cancel_booking_no(call: CallbackQuery):
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=booking.cancel_markup(call.data.split(":", 1)[1]))


@router.callback_query(F.data.startswith("cxy:"))
async def cancel_booking_yes(call: CallbackQuery):
    found = _find_own_booking(call.from_user.id, call.data.split(":", 1)[1])
    if not found:
        await call.answer("Bron topilmadi", show_alert=True)
        return
    owner_id, b = found
    if b.get("approval") and call.from_user.id != OWNER_ID:
        await call.answer("Bu bronni bot egasi boshqaradi.", show_alert=True)
        return
    await call.answer("Bekor qilinmoqda...")
    if not await cancel_order(call, owner_id, b):
        return
    if call.from_user.id != OWNER_ID:
        await call.bot.send_message(OWNER_ID, f"❌ Bron bekor qilindi: {b['order_id']} "
                                              f"({b['from_name']} → {b['to_name']}, {b['departure']})", parse_mode=None)


# ---------- saytga kirish (faqat bot egasi) ----------

@router.message(F.text == MENU_LOGIN)
@router.message(Command("login"))
async def login_start(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        await message.answer("Bu bo'lim faqat bot egasi uchun.")
        return
    await ask_site_login(message, state)


async def ask_site_login(message: Message, state: FSMContext):
    """Saytga kirish: raqamni so'raydi (menyu tugmasi, /login yoki admin panel)."""
    await state.clear()
    phone = users.get(OWNER_ID).get("phone")
    kb = InlineKeyboardBuilder()
    if phone:
        kb.button(text=f"📱 +{phone} bilan kirish", callback_data="login_saved")
    await message.answer(
        f"Hozir: {html.escape(session.status())}\n\n"
        "Saytga qaysi raqam bilan kiraman? Tugmani bosing yoki boshqa raqam yozing "
        "(masalan <code>+998901234567</code>):",
        reply_markup=kb.as_markup(),
    )
    await state.set_state(Login.phone)


@router.callback_query(Login.phone, F.data == "login_saved")
async def login_saved_phone(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.update_data(phone=users.get(call.from_user.id)["phone"])
    await state.set_state(Login.password)
    await call.message.edit_text("Sayt parolini yuboring (xabar darhol o'chiriladi):")


@router.message(Login.phone, F.text)
async def login_phone(message: Message, state: FSMContext):
    try:
        phone = normalize_phone(message.text)
    except ValueError as e:
        await message.answer(str(e))
        return
    await state.update_data(phone=phone)
    await state.set_state(Login.password)
    await message.answer("Sayt parolini yuboring (xabar darhol o'chiriladi):")


@router.message(Login.password, F.text)
@router.message(Reg.site_password, F.text)
async def login_password(message: Message, state: FSMContext):
    password = message.text
    try:
        await message.delete()
    except Exception:
        pass
    phone = (await state.get_data())["phone"]
    await state.clear()
    wait = await message.answer("Saytga fon rejimida kirilmoqda...")
    try:
        await session.set_account(phone, password)
    except UzRailError as e:
        await wait.edit_text(f"❌ {html.escape(str(e))}\nQayta urinish: {MENU_LOGIN}")
        return
    except Exception as e:
        logging.exception("Login xatosi")
        await wait.edit_text(f"❌ Kutilmagan xato: {html.escape(e.__class__.__name__)}\nQayta urinish: {MENU_LOGIN}")
        return
    await wait.edit_text(f"✅ Saytga kirildi: {html.escape(session.status())}")


# ---------- 2-qadam: yo'lovchilar ----------

@router.message(F.text == MENU_BUY)
async def buy(message: Message, state: FSMContext):
    await state.clear()
    user = users.get(message.from_user.id)
    if not user.get("phone"):
        await message.answer("Avval telefon raqamingizni yuboring.", reply_markup=contact_keyboard())
        await state.set_state(Reg.phone)
        return
    saved = saved_passengers(message.from_user.id)
    if not saved:
        await message.answer("Avval o'zingiz haqingizdagi ma'lumotni kiriting — bir marta so'rayman, keyin eslab qolaman.")
        await ask_passenger(message, state, is_self=True)
        return
    await state.update_data(selected=[saved[0]["passport"]])
    await show_selection(message, state, message.from_user.id)


def saved_passengers(user_id: int) -> list[dict]:
    """Foydalanuvchining saqlangan yo'lovchilari (birinchisi — o'zi)."""
    return users.passengers(user_id)


SELECT_TEXT = "Kimga bilet olamiz? Yo'lovchilarni belgilang (✅ — tanlangan):"


async def show_selection(message: Message, state: FSMContext, user_id: int, edit: bool = False):
    saved = saved_passengers(user_id)
    passports = {p["passport"] for p in saved}
    selected = [s for s in (await state.get_data()).get("selected", []) if s in passports]
    await state.update_data(selected=selected)
    kb = InlineKeyboardBuilder()
    for i, p in enumerate(saved):
        mark = "✅" if p["passport"] in selected else "⬜"
        kb.button(text=f"{mark} {Passenger(**p).full_name}", callback_data=f"sel:{i}")
    kb.button(text="➕ Yangi yo'lovchi qo'shish", callback_data="sel:new")
    if saved:
        kb.button(text="🗑 Ro'yxatdan o'chirish", callback_data="sel:rm")
    if selected:
        kb.button(text=f"▶️ Davom etish ({len(selected)} kishi)", callback_data="sel:done")
    kb.adjust(1)
    if edit:
        await message.edit_text(SELECT_TEXT, reply_markup=kb.as_markup())
    else:
        await message.answer(SELECT_TEXT, reply_markup=kb.as_markup())
    await state.set_state(Booking.select)


@router.callback_query(Booking.select, F.data.startswith("sel:"))
async def selection_action(call: CallbackQuery, state: FSMContext):
    await call.answer()
    action = call.data.split(":", 1)[1]
    user_id = call.from_user.id
    saved = saved_passengers(user_id)
    selected = (await state.get_data()).get("selected", [])
    if action == "new":
        await call.message.edit_reply_markup()
        await ask_passenger(call.message, state, is_self=False)
    elif action == "rm":
        kb = InlineKeyboardBuilder()
        for i, p in enumerate(saved):
            kb.button(text=f"🗑 {Passenger(**p).full_name}", callback_data=f"rm:{i}")
        kb.button(text="⬅️ Orqaga", callback_data="sel:back")
        kb.adjust(1)
        await call.message.edit_text("Qaysi yo'lovchini ro'yxatdan o'chiramiz?", reply_markup=kb.as_markup())
    elif action == "back":
        await show_selection(call.message, state, user_id, edit=True)
    elif action == "done":
        passengers = [p for p in saved if p["passport"] in selected]
        names = "\n".join(f"{i + 1}. {passenger_name(p)}" for i, p in enumerate(passengers))
        await state.update_data(passengers=passengers)
        await call.message.edit_text(f"Yo'lovchilar ({len(passengers)} kishi):\n{names}")
        await call.message.answer("Qayerdan jo'naysiz? Tugmani bosing yoki stansiya nomini yozing:",
                                  reply_markup=station_keyboard())
        await state.set_state(Booking.from_station)
    else:
        passport = saved[int(action)]["passport"]
        selected = [s for s in selected if s != passport] if passport in selected else selected + [passport]
        await state.update_data(selected=selected)
        await show_selection(call.message, state, user_id, edit=True)


@router.callback_query(Booking.select, F.data.startswith("rm:"))
async def remove_saved(call: CallbackQuery, state: FSMContext):
    saved = saved_passengers(call.from_user.id)
    i = int(call.data.split(":")[1])
    if i < len(saved):
        removed = users.remove_passenger(call.from_user.id, saved[i]["passport"])
        await call.answer(f"O'chirildi: {Passenger(**removed).full_name}")
    await show_selection(call.message, state, call.from_user.id, edit=True)


PASSENGER_FORM = (
    "Bitta xabarda yuboring — har biri yangi qatorda:\n"
    "<code>Familiya Ism Otasining_ismi\n"
    "Tug'ilgan sana\n"
    "Jinsi\n"
    "Pasport yoki ID-karta seriya va raqami</code>\n\n"
    "Masalan:\n"
    "<code>Karimov Aziz Akmalovich\n"
    "15.03.1990\n"
    "Erkak\n"
    "AA1234567</code>\n\n"
    "Hujjat turi seriyadan aniqlanadi (AD, AE — ID-karta, qolganlari — pasport). "
    "Boshqacha bo'lsa, oxiriga <code>ID</code> yoki <code>Pasport</code> deb yozing."
)


def passenger_card(p: dict) -> str:
    born = date.fromisoformat(p["birthday"])
    return (f"Familiya: <b>{html.escape(p['last_name'])}</b>\n"
            f"Ism: <b>{html.escape(p['first_name'])}</b>\n"
            f"Otasining ismi: <b>{html.escape(p['middle_name']) or '—'}</b>\n"
            f"Tug'ilgan sana: <b>{born:%d.%m.%Y}</b>\n"
            f"Jinsi: <b>{GENDER_LABEL[p['gender']]}</b>\n"
            f"Hujjat: <b>{doc_type_label(p.get('doc_type') or guess_doc_type(p['passport']))}</b> {p['passport']}")


async def ask_passenger(message: Message, state: FSMContext, is_self: bool):
    await state.update_data(is_self=is_self, pending={})
    who = "O'zingiz haqingizdagi ma'lumotni" if is_self else "Yangi yo'lovchi ma'lumotini"
    await message.answer(f"{who} kiriting.\n{PASSENGER_FORM}")
    await state.set_state(Booking.p_data)


@router.message(Booking.p_data, F.text)
async def got_passenger_data(message: Message, state: FSMContext):
    try:
        passenger = parse_passenger_text(message.text)
    except ValueError as e:
        await message.answer(f"Quyidagilar topilmadi yoki noto'g'ri:\n{html.escape(str(e))}\n\n{PASSENGER_FORM}")
        return
    if passenger["passport"] in [p["passport"] for p in saved_passengers(message.from_user.id)]:
        selected = (await state.get_data()).get("selected", [])
        if passenger["passport"] not in selected:
            await state.update_data(selected=selected + [passenger["passport"]])
        await message.answer("Bu pasport ro'yxatda bor — belgilab qo'ydim.")
        await show_selection(message, state, message.from_user.id)
        return
    await state.update_data(pending=passenger)
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Saqlash", callback_data="pconf:ok")
    kb.button(text="✏️ Qayta kiritish", callback_data="pconf:edit")
    kb.adjust(2)
    await message.answer(f"Ma'lumotlar to'g'rimi?\n\n{passenger_card(passenger)}", reply_markup=kb.as_markup())
    await state.set_state(Booking.p_confirm)


@router.callback_query(Booking.p_confirm, F.data.startswith("pconf:"))
async def passenger_confirmed(call: CallbackQuery, state: FSMContext):
    await call.answer()
    data = await state.get_data()
    if call.data == "pconf:edit":
        await call.message.edit_reply_markup()
        await ask_passenger(call.message, state, data["is_self"])
        return
    passenger = data["pending"]
    await call.message.edit_text(f"✅ Ro'yxatga saqlandi:\n\n{passenger_card(passenger)}")
    users.add_passenger(call.from_user.id, passenger, is_self=data["is_self"])
    await state.update_data(selected=data.get("selected", []) + [passenger["passport"]], pending={})
    await show_selection(call.message, state, call.from_user.id)


# ---------- 3-qadam: yo'nalish ----------

async def search_station(message: Message, state: FSMContext, query: str):
    """Stansiyani qidiradi: aniq bitta mos kelsa o'zi tanlaydi, aks holda variantlarni ko'rsatadi."""
    try:
        stations = (await client.search_stations(query))[:10]
    except UzRailError as e:
        await message.answer(f"Stansiyalarni olishda xato: {html.escape(str(e))}")
        return
    if not stations:
        await message.answer("Bunday stansiya topilmadi, boshqacha yozib ko'ring.")
        return
    exact = [s for s in stations if s["name"].upper() == query.upper()]
    if len(stations) == 1 or len(exact) == 1:
        await station_chosen(message, state, (exact or stations)[0])
        return
    await state.update_data(station_options=stations)
    kb = InlineKeyboardBuilder()
    for i, s in enumerate(stations):
        kb.button(text=s["name"], callback_data=f"st:{i}")
    kb.adjust(2)
    await message.answer("Stansiyani tanlang:", reply_markup=kb.as_markup())


async def station_chosen(message: Message, state: FSMContext, station: dict):
    current = await state.get_state()
    data = await state.get_data()
    if current == Booking.from_station.state:
        await state.update_data(from_code=station["code"], from_name=station["name"])
        await message.answer(f"Qayerdan: <b>{html.escape(station['name'])}</b>\n\n"
                             "Qayerga borasiz? Tugmani bosing yoki stansiya nomini yozing:",
                             reply_markup=station_keyboard(exclude=station["name"]))
        await state.set_state(Booking.to_station)
        return
    if station["code"] == data["from_code"]:
        await message.answer("Jo'nash va borish stansiyasi bir xil bo'lmasin. Boshqasini tanlang.")
        return
    await state.update_data(to_code=station["code"], to_name=station["name"])
    await message.answer(
        f"Yo'nalish: <b>{html.escape(data['from_name'])} → {html.escape(station['name'])}</b>\n\n"
        "Sanani tanlang yoki yozing (<code>KK.OO.YYYY</code>):",
        reply_markup=date_keyboard(),
    )
    await state.set_state(Booking.trip_date)


@router.message(Booking.from_station, F.text)
@router.message(Booking.to_station, F.text)
async def station_query(message: Message, state: FSMContext):
    await search_station(message, state, message.text.strip())


@router.callback_query(Booking.from_station, F.data.startswith("pop:"))
@router.callback_query(Booking.to_station, F.data.startswith("pop:"))
async def popular_station(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await call.message.edit_reply_markup()
    await search_station(call.message, state, POPULAR_STATIONS[int(call.data.split(":")[1])][1])


@router.callback_query(Booking.from_station, F.data.startswith("st:"))
@router.callback_query(Booking.to_station, F.data.startswith("st:"))
async def station_option(call: CallbackQuery, state: FSMContext):
    await call.answer()
    options = (await state.get_data())["station_options"]
    station = options[int(call.data.split(":")[1])]
    await call.message.edit_text(f"✅ {html.escape(station['name'])}")
    await station_chosen(call.message, state, station)


# ---------- 4-qadam: sana va reys ----------

@router.callback_query(Booking.trip_date, F.data.startswith("date:"))
async def date_button(call: CallbackQuery, state: FSMContext):
    await call.answer()
    trip = date.fromisoformat(call.data.split(":")[1])
    await call.message.edit_text(f"Sana: <b>{trip:%d.%m.%Y}</b>")
    await show_trains(call.message, state, trip)


@router.message(Booking.trip_date, F.text)
async def date_typed(message: Message, state: FSMContext):
    try:
        trip = datetime.strptime(message.text.strip(), "%d.%m.%Y").date()
    except ValueError:
        await message.answer("Sana formati noto'g'ri. Masalan: <code>28.10.2026</code>")
        return
    if trip < date.today():
        await message.answer("O'tgan sanani tanlab bo'lmaydi.")
        return
    await show_trains(message, state, trip)


async def show_trains(message: Message, state: FSMContext, trip: date):
    data = await state.get_data()
    wait = await message.answer("🔎 Reyslar qidirilmoqda...")
    try:
        trains = await client.list_trains(trip.isoformat(), data["from_code"], data["to_code"])
    except UzRailError as e:
        await wait.edit_text(f"Xato: {html.escape(str(e))}")
        return
    trains = [t for t in trains if datetime.strptime(t["departureDate"], "%d.%m.%Y %H:%M") > datetime.now()]
    trains.sort(key=lambda t: datetime.strptime(t["departureDate"], "%d.%m.%Y %H:%M"))
    if not trains:
        await wait.edit_text(f"{trip:%d.%m.%Y} kuni bu yo'nalishda reys yo'q. Boshqa sanani tanlang:",
                             reply_markup=date_keyboard())
        return
    await state.update_data(trip_date=trip.isoformat(), trains=trains)
    kb = InlineKeyboardBuilder()
    for i, t in enumerate(trains):
        kb.button(text=train_label(t), callback_data=f"train:{i}")
    kb.adjust(1)
    await wait.edit_text(
        f"{trip:%d.%m.%Y} kungi reyslar (jo'nash → kelish).\n"
        "Reysni tanlang yoki jo'nash vaqtini yozing (masalan <code>21:12</code>).\n"
        "🔴 — joy yo'q: tanlasangiz, bot kuzatib, joy chiqishi bilan bron qiladi.",
        reply_markup=kb.as_markup(),
    )
    await state.set_state(Booking.train)


@router.message(Booking.train, F.text)
async def train_by_time(message: Message, state: FSMContext):
    m = re.fullmatch(r"(\d{1,2})[:.](\d{2})", message.text.strip())
    if not m:
        await message.answer("Vaqtni <code>SS:MM</code> ko'rinishida yozing yoki tugmani bosing.")
        return
    wanted = int(m.group(1)) * 60 + int(m.group(2))
    trains = (await state.get_data())["trains"]

    def minutes(t):
        hh, mm = t["departureDate"][-5:].split(":")
        return int(hh) * 60 + int(mm)

    idx = min(range(len(trains)), key=lambda i: abs(minutes(trains[i]) - wanted))
    if minutes(trains[idx]) != wanted:
        await message.answer(f"Aynan shu vaqtga reys yo'q, eng yaqini tanlandi: {trains[idx]['departureDate'][-5:]}")
    await select_train(message, state, idx)


@router.callback_query(Booking.train, F.data.startswith("train:"))
async def train_chosen(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await call.message.edit_reply_markup()
    await select_train(call.message, state, int(call.data.split(":")[1]))


async def select_train(message: Message, state: FSMContext, idx: int):
    data = await state.get_data()
    train = data["trains"][idx]
    if train.get("trainId"):
        await message.answer("Bu reys (Sorbon tizimi) hozircha bot orqali qo'llab-quvvatlanmaydi. Boshqa reys tanlang.")
        return
    await state.update_data(train_idx=idx, categories=None)
    try:
        places = await client.train_places(data["trip_date"], data["from_code"], data["to_code"], train)
    except UzRailError as e:
        await message.answer(f"Xato: {html.escape(str(e))}")
        return
    # joyi borlari aniq son bilan + shu poyezd turidagi joyi tugagan kategoriyalar (kuzatish uchun)
    cats = train_categories(train, places) if places and places.get("carGroup") else train_categories(train)
    if not cats:
        kb = InlineKeyboardBuilder()
        monitor_buttons(kb)
        kb.adjust(1)
        await message.answer(
            f"Reys: <b>{html.escape(train_label(train))}</b>\n"
            "Hozir bo'sh joy yo'q. Kuzatuvga qo'yaymi? Joy chiqishi bilan avtomatik bron qilinadi.",
            reply_markup=kb.as_markup(),
        )
        await state.set_state(Booking.confirm)
        return
    await state.update_data(cats=cats, picked_cats=[])
    await message.answer(
        f"Reys: <b>{html.escape(train_label(train))}</b>\n\n{seats_breakdown(cats)}\n\n"
        "Qaysi kategoriyalar sizga mos? Bir yoki bir nechtasini belgilab, <b>▶️ Davom etish</b> ni bosing.\n"
        "Masalan, faqat <b>Ekonom</b> belgilansa — Biznes ochilsa ham olinmaydi.",
        reply_markup=category_keyboard(cats, []),
    )
    await state.set_state(Booking.car_type)


def seats_breakdown(cats: list[dict]) -> str:
    """Jami bo'sh joylar va kategoriyalar bo'yicha taqsimot (yotoqli vagonlarda — pastki/yuqori o'rinlar)."""
    labels = (("down", "pastki"), ("up", "yuqori"), ("side_down", "yon pastki"), ("side_up", "yon yuqori"))
    lines = [f"<b>Jami bo'sh joylar: {sum(c['free'] for c in cats)}</b>"]
    for c in cats:
        if not c["free"]:
            lines.append(f"• {html.escape(c['name_uz'])} — joy yo'q")
            continue
        detail = ", ".join(f"{label} {c['detail'][k]}" for k, label in labels if c.get("detail", {}).get(k))
        price = f", {money(c['price'])}" if c.get("price") else ""
        lines.append(f"• {html.escape(c['name_uz'])} — {c['free']} joy{price}" + (f" ({detail})" if detail else ""))
    return "\n".join(lines)


def category_keyboard(cats: list[dict], picked: list[str]):
    kb = InlineKeyboardBuilder()
    for i, c in enumerate(cats):
        mark = "✅" if c["code"] in picked else "⬜"
        seats = f"{c['free']} joy" + (f", {money(c['price'])}" if c.get("price") else "") if c["free"] else "joy yo'q"
        kb.button(text=f"{mark} {c['name_uz']} — {seats}", callback_data=f"cat:{i}")
    kb.button(text="Farqi yo'q (istalgani)", callback_data="cat:*")
    kb.button(text=f"▶️ Davom etish{' (' + str(len(picked)) + ')' if picked else ''}", callback_data="cat:go")
    kb.adjust(1)
    return kb.as_markup()


# ---------- 5-qadam: vagon sxemasidan joy tanlash, bron va kuzatuv ----------

async def load_cars(state: FSMContext) -> list[dict]:
    """Tanlangan kategoriyalardagi joyi bor vagonlar (saytdan yangi ma'lumot)."""
    data = await state.get_data()
    train = data["trains"][data["train_idx"]]
    places = await client.train_places(data["trip_date"], data["from_code"], data["to_code"], train)
    return car_schemas.cars(places, data.get("categories"))


MAX_SEAT_BUTTONS = 90  # Telegram tugmalar chegarasi: 100 ta


TIER_LABELS = {"all": "Hammasi", "upper": "⬆️ Faqat tepasi", "lower": "⬇️ Faqat pastki"}


def car_tier(car: dict, tier: str | None) -> str:
    """Saytdagi "Faqat tepasi / pastki" filtri faqat yotoqli vagonlarda (SV dan tashqari) bor."""
    return tier if car.get("tiers") and tier in car_schemas.TIERS else "all"


def tier_seats(car: dict, tier: str) -> list[int]:
    """Filtrga mos bo'sh joylar: juft raqam — tepa, toq — pastki (saytdagidek)."""
    return car["free"] if tier == "all" else [s for s in car["free"] if car_schemas.tier_of(s) == tier]


def seats_keyboard(cars: list[dict], idx: int, picked: list[int], n: int, tier: str = "all"):
    car = cars[idx]
    tier = car_tier(car, tier)
    kb = InlineKeyboardBuilder()
    sizes = []
    if len(cars) > 1:
        kb.button(text="◀️", callback_data="car:prev")
        kb.button(text=f"🚃 {car['number']}-vagon ({idx + 1}/{len(cars)})", callback_data="car:list")
        kb.button(text="▶️", callback_data="car:next")
        sizes.append(3)
    if car.get("tiers"):
        for key, label in TIER_LABELS.items():
            mark = "✅ " if key == tier else ""
            kb.button(text=f"{mark}{label} ({len(tier_seats(car, key))})", callback_data=f"tier:{key}")
        sizes.append(3)
    seats = tier_seats(car, tier)[:MAX_SEAT_BUTTONS]
    for s in seats:
        kb.button(text=f"✅{s}" if s in picked else str(s), callback_data=f"seat:{s}")
    sizes += [6] * (len(seats) // 6) + ([len(seats) % 6] if len(seats) % 6 else [])
    kb.button(text=f"🎫 Bron qilish ({len(picked)}/{n})", callback_data="book")
    kb.button(text="❌ Bekor qilish", callback_data="abort")
    sizes += [1, 1]
    kb.adjust(*sizes)
    return kb.as_markup()


def seats_caption(car: dict, picked: list[int], n: int, tier: str = "all") -> str:
    price = f"\nNarx: {money(car['price'])} × {n} ≈ {money(car['price'] * n)}" if car.get("price") else ""
    chosen = f"\nTanlandi: <b>{', '.join(map(str, sorted(picked)))}</b>" if picked else ""
    shown = f" · ko'rsatilgan: <b>{TIER_LABELS[tier]}</b>" if tier != "all" else ""
    return (f"🚃 <b>{car['number']}-vagon</b> · {html.escape(car['name_uz'])} · {len(car['free'])} ta bo'sh joy{shown}{price}\n"
            f"🟦 bo'sh  ⬜ band  🟩 tanlangan\n"
            f"Rasmdagi ko'k joylardan <b>{n} ta</b> tanlang — pastdagi raqamlarni bosing.{chosen}")


async def car_photo(car: dict, picked: list[int], data: dict, tier: str = "all") -> BufferedInputFile:
    train = data["trains"][data["train_idx"]]
    shown = {**car, "free": tier_seats(car, tier)}  # filtrga tushmagan joylar saytdagidek band ko'rinadi
    title = f"{car['number']}-vagon · {car['name_uz']}" + (f" · {TIER_LABELS[tier].split(' ', 1)[1]}" if tier != "all" else "")
    png = await renderer.render(
        shown, picked, title,
        f"Poyezd {train['number']} {train.get('brand') or ''}, {train['departureDate']} · bo'sh joylar: {len(shown['free'])}")
    return BufferedInputFile(png, filename=f"vagon-{car['number']}.png")


async def show_car(message: Message, state: FSMContext, edit: bool):
    """Vagon sxemasi rasmini (yoki yangilangan rasmni) joy tugmalari bilan yuboradi."""
    data = await state.get_data()
    cars, idx, picked, n = data["cars"], data["car_idx"], data["picked"], len(data["passengers"])
    car = cars[idx]
    tier = car_tier(car, data.get("tier"))
    photo = await car_photo(car, picked, data, tier)
    caption, markup = seats_caption(car, picked, n, tier), seats_keyboard(cars, idx, picked, n, tier)
    if edit:
        await message.edit_media(InputMediaPhoto(media=photo, caption=caption, parse_mode="HTML"), reply_markup=markup)
    else:
        await message.answer_photo(photo, caption=caption, reply_markup=markup)


@router.callback_query(Booking.car_type, F.data.startswith("cat:"))
async def category_chosen(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    picked_key = call.data.split(":", 1)[1]
    picked = list(data.get("picked_cats") or [])
    if picked_key.isdigit():  # belgilash / olib tashlash
        code = data["cats"][int(picked_key)]["code"]
        picked = [c for c in picked if c != code] if code in picked else picked + [code]
        await state.update_data(picked_cats=picked)
        await call.answer()
        await call.message.edit_reply_markup(reply_markup=category_keyboard(data["cats"], picked))
        return
    await call.answer()
    await call.message.edit_reply_markup()
    categories = picked if picked_key == "go" and picked else None
    await state.update_data(categories=categories)
    chosen_cats = [c for c in data["cats"] if not categories or c["code"] in categories]
    train = data["trains"][data["train_idx"]]
    n = len(data["passengers"])
    kb = InlineKeyboardBuilder()
    if not any(c["free"] for c in chosen_cats):
        monitor_buttons(kb)
        kb.adjust(1)
        await call.message.answer(
            f"Tanlangan kategoriyada ({html.escape(describe(categories, train=train))}) hozir bo'sh joy yo'q.\n"
            "Kuzatuvga qo'yaymi? Shu kategoriyada joy chiqishi bilan avtomatik bron qilinadi.",
            reply_markup=kb.as_markup(),
        )
        await state.set_state(Booking.confirm)
        return
    wait = await call.message.answer("🚃 Vagon sxemasi tayyorlanmoqda...")
    try:
        cars = await load_cars(state)
    except UzRailError as e:
        await wait.edit_text(f"Xato: {html.escape(str(e))}")
        return
    if not cars:
        monitor_buttons(kb)
        kb.adjust(1)
        await wait.edit_text(
            f"Hozir bo'sh joy qolmadi ({html.escape(describe(categories, train=train))}). "
            "Kuzatuvga qo'yaymi? Joy chiqishi bilan bron qilinadi.",
            reply_markup=kb.as_markup(),
        )
        await state.set_state(Booking.confirm)
        return
    # yo'lovchilar soniga yetadigan birinchi vagondan boshlaymiz
    idx = next((i for i, c in enumerate(cars) if len(c["free"]) >= n), 0)
    await state.update_data(cars=cars, car_idx=idx, picked=[], tier="all")
    await state.set_state(Booking.seats)
    await show_car(call.message, state, edit=False)
    await wait.delete()


@router.callback_query(Booking.seats, F.data.startswith("car:"))
async def switch_car(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    action, count = call.data.split(":")[1], len(data["cars"])
    if action == "list" or count < 2:
        await call.answer(f"Vagonlar: {', '.join(c['number'] for c in data['cars'])}")
        return
    await call.answer()
    await state.update_data(car_idx=(data["car_idx"] + (1 if action == "next" else -1)) % count, picked=[])
    await show_car(call.message, state, edit=True)


@router.callback_query(Booking.seats, F.data.startswith("tier:"))
async def switch_tier(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    tier = call.data.split(":")[1]
    if car_tier(data["cars"][data["car_idx"]], data.get("tier")) == tier:
        await call.answer()
        return
    await call.answer(TIER_LABELS.get(tier, ""))
    await state.update_data(tier=tier, picked=[])  # saytdagidek: filtr almashsa tanlov tozalanadi
    await show_car(call.message, state, edit=True)


@router.callback_query(Booking.seats, F.data.startswith("seat:"))
async def toggle_seat(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    seat, picked, n = int(call.data.split(":")[1]), list(data["picked"]), len(data["passengers"])
    if seat in picked:
        picked.remove(seat)
    elif n == 1:
        picked = [seat]
    elif len(picked) >= n:
        await call.answer(f"{n} kishi uchun {n} ta joy tanlash mumkin. Avval birini olib tashlang.", show_alert=True)
        return
    else:
        picked.append(seat)
    await call.answer(f"Tanlandi: {len(picked)}/{n}")
    await state.update_data(picked=picked)
    await show_car(call.message, state, edit=True)


@router.callback_query(Booking.seats, F.data == "abort")
async def abort_seats(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer()
    await call.message.edit_reply_markup()
    await call.message.answer("Bekor qilindi.", reply_markup=main_menu(call.from_user.id))


@router.callback_query(Booking.confirm, F.data == "abort")
async def abort(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_reply_markup()
    await call.message.answer("Bekor qilindi.", reply_markup=main_menu(call.from_user.id))


@router.callback_query(Booking.confirm, F.data.startswith("mon:"))
async def start_monitor(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await call.message.edit_reply_markup()
    if not session.account:
        await call.message.answer(f"⚠️ Avval bot egasi <b>{MENU_LOGIN}</b> orqali saytga kirishi kerak.")
        return
    data = await state.get_data()
    m = Monitor(
        chat_id=call.from_user.id,
        passengers=data["passengers"],
        from_name=data["from_name"],
        to_name=data["to_name"],
        trip_date=data["trip_date"],
        train=data["trains"][data["train_idx"]],
        car_type="*",
        strict=call.data == "mon:1",
        categories=data.get("categories"),
    )
    monitors.add(m)
    await state.clear()
    count = len(monitors.active())
    await call.message.answer(
        f"🔔 Kuzatuv boshlandi:\n{html.escape(m.title())}\n\n"
        f"Har {POLL_INTERVAL * count:g} sekundda tekshiriladi (faol kuzatuvlar: {count}). "
        f"Joy chiqishi bilan bron qilib, xabar beraman.\nRo'yxat: <b>{MENU_MONITORS}</b>",
        reply_markup=main_menu(call.from_user.id),
    )


BOOKING_NOW: set[int] = set()  # hozir bron qilinayotgan foydalanuvchilar — tugma ikki marta bosilsa ikkinchi bron bo'lmasin


@router.callback_query(Booking.seats, F.data == "book")
async def book(call: CallbackQuery, state: FSMContext):
    uid = call.from_user.id
    if uid in BOOKING_NOW:
        await call.answer("Bron qilinmoqda, kuting...")
        return
    BOOKING_NOW.add(uid)
    try:
        await _book(call, state)
    finally:
        BOOKING_NOW.discard(uid)


async def _book(call: CallbackQuery, state: FSMContext):
    if await state.get_state() != Booking.seats.state:  # oldingi bosish bronni allaqachon yakunlagan
        await call.answer()
        return
    data = await state.get_data()
    picked, n = data["picked"], len(data["passengers"])
    if len(picked) != n:
        await call.answer(f"Har bir yo'lovchiga joy tanlang: {len(picked)}/{n}", show_alert=True)
        return
    if not session.account:
        await call.answer()
        await call.message.answer(f"⚠️ Avval bot egasi <b>{MENU_LOGIN}</b> orqali saytga kirishi kerak.")
        return
    await call.answer("Bron qilinmoqda...")
    await call.message.edit_reply_markup()
    train = data["trains"][data["train_idx"]]
    car = data["cars"][data["car_idx"]]
    wait = await call.message.answer(f"⏳ {car['number']}-vagon, joylar {', '.join(map(str, sorted(picked)))} bron qilinmoqda...")
    try:
        # Joylar shu orada band bo'lib qolgan bo'lishi mumkin — yangi ma'lumot bilan tekshiramiz
        places = await client.train_places(data["trip_date"], data["from_code"], data["to_code"], train)
        tier = car_tier(car, data.get("tier"))
        choice = car_schemas.pick(places, car["key"], picked, None if tier == "all" else tier)
        if not choice:
            cars = car_schemas.cars(places, data.get("categories"))
            await wait.delete()
            if not cars:
                kb = InlineKeyboardBuilder()
                monitor_buttons(kb)
                kb.adjust(1)
                await call.message.answer("Afsuski, joylar band bo'lib qoldi. Kuzatuvga qo'yaymi?",
                                          reply_markup=kb.as_markup())
                await state.set_state(Booking.confirm)
                return
            idx = next((i for i, c in enumerate(cars) if c["key"] == car["key"]), 0)
            await state.update_data(cars=cars, car_idx=idx, picked=[s for s in picked if s in cars[idx]["free"]])
            await call.message.answer("⚠️ Tanlangan joylardan biri band bo'lib qoldi. Sxema yangilandi — boshqa joy tanlang.")
            await show_car(call.message, state, edit=False)
            return
        order_id = await booking.reserve(client, session, train, choice, data["passengers"])
    except UzRailError as e:
        await wait.edit_text(f"Bron qilishda xato: {html.escape(str(e))}")
        await show_car(call.message, state, edit=False)  # qayta urinish yoki boshqa joy tanlash uchun
        return
    await wait.delete()
    await state.clear()
    record = booking.booking_record(order_id, train, choice, data["passengers"], data["from_name"], data["to_name"])
    record["expires_at"] = await booking.payment_deadline(client, session, order_id)
    # foydalanuvchiga va bot egasiga taymerli xabar; boshqa foydalanuvchiniki — egasining tasdig'i va to'lovini kutadi
    await timers.announce(call.from_user.id, record, name=call.from_user.full_name)


async def main():
    global monitors, WEBAPP_URL, timers
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    timers = BookingTimers(bot, users, client, session, OWNER_ID)
    timers.resume_all()  # bot qayta ishga tushgan bo'lsa — davom etayotgan bron taymerlari

    async def notify(chat_id: int, text: str, order_id: str | None = None):
        markup = booking.cancel_markup(order_id) if order_id else None
        sent = await bot.send_message(chat_id, text, parse_mode=None, reply_markup=markup)
        if order_id:  # kuzatuv orqali qilingan bron — to'lov taymeri
            timers.start(chat_id, sent.message_id, order_id)

    monitors = MonitorManager(client, session, notify, OWNER_ID, POLL_INTERVAL, on_booked=timers.announce)
    dp = Dispatcher()
    dp.include_router(router)
    watcher = asyncio.create_task(monitors.run())

    # Ilova (Telegram Mini App): mahalliy veb-server + HTTPS tunnel
    client_ru = UzRailClient(lang="ru")
    ctx = webapp.AppContext(bot=bot, client=client, session=session, users=users, monitors=monitors,
                            owner_id=OWNER_ID, allowed_ids=ALLOWED_IDS, poll_interval=POLL_INTERVAL,
                            client_ru=client_ru, timers=timers,
                            proxy=site_proxy.SiteProxy(bot.token, session, client),
                            access=access, set_access=lambda uid, grant: apply_access(bot, uid, grant))
    runner = await webapp.start_server(ctx, WEBAPP_PORT)
    tunnel = None
    try:
        WEBAPP_URL = os.getenv("WEBAPP_URL") or None
        if not WEBAPP_URL:
            tunnel, WEBAPP_URL = await webapp.start_tunnel(WEBAPP_PORT)
        menu = MenuButtonWebApp(text="Ilova", web_app=WebAppInfo(url=WEBAPP_URL))
        await bot.set_chat_menu_button(menu_button=menu)
        ctx.public_url = timers.pay_base = WEBAPP_URL  # "Saytda to'lash" — sayt bot egasining akkaunti bilan ochiladi
        # Umumiy tugma Telegram'da kechikib yangilanadi — ma'lum foydalanuvchilarga darhol alohida o'rnatamiz
        for chat_id in ALLOWED_IDS | set(users.ids()):
            try:
                await bot.set_chat_menu_button(chat_id=chat_id, menu_button=menu)
            except Exception:
                logging.warning("Menyu tugmasi o'rnatilmadi: %s", chat_id)
        logging.info("Ilova manzili: %s", WEBAPP_URL)
    except Exception:
        logging.exception("Ilovani ishga tushirib bo'lmadi — bot ilovasiz ishlaydi")
    try:
        await dp.start_polling(bot)
    finally:
        watcher.cancel()
        if tunnel:
            tunnel.terminate()
        await runner.cleanup()
        await ctx.proxy.close()
        await renderer.close()
        await client.close()
        await client_ru.close()


if __name__ == "__main__":
    asyncio.run(main())
