"""Saytga telefon raqam + parol bilan fon rejimida (headless brauzer) kirish.

Sayt kirish so'rovini reCAPTCHA v3 bilan himoyalaydi, shuning uchun login
oddiy HTTP so'rov bilan emas, ko'rinmas Chromium brauzer orqali qilinadi.
Olingan token faylga saqlanadi va muddati tugashidan oldin avtomatik yangilanadi.

Har bir foydalanuvchi o'z akkaunti bilan kiradi (SessionStore): bron, to'lov va kuzatuv shu akkauntda bo'ladi.
"""
import asyncio
import json
import logging
import re
from pathlib import Path

from playwright.async_api import TimeoutError as PwTimeout

import shared_browser

from uzrail import BASE_URL, UzRailError, decode_jwt, token_expired

ACCOUNT_FILE = Path(__file__).with_name("account.json")
TOKEN_FILE = Path(__file__).with_name("site_token.txt")
ACCOUNTS_FILE = Path(__file__).with_name("site_accounts.json")  # boshqa foydalanuvchilarning akkauntlari
NOT_LINKED_UZ = ("Sayt akkauntingiz ulanmagan. Botda «👤 Profil» → «🔑 Saytga kirish» yoki ilovada Profil "
                 "bo'limidan eticket.uzrailpass.uz raqamingiz va parolingiz bilan kiring.")
NOT_LINKED_RU = ("Аккаунт сайта не привязан. Войдите с номером и паролем eticket.uzrailpass.uz: в боте "
                 "«👤 Profil» → «🔑 Saytga kirish» или в разделе «Профиль» приложения.")
log = logging.getLogger(__name__)


class LoginError(UzRailError):
    pass


def normalize_phone(raw: str) -> str:
    """'+998 90 123-45-67' -> '998901234567'"""
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 9:
        digits = "998" + digits
    if not re.fullmatch(r"998\d{9}", digits):
        raise ValueError("Telefon raqam noto'g'ri. Masalan: +998901234567")
    return digits


async def browser_login(phone: str, password: str) -> str:
    """Ko'rinmas brauzerda kiradi; Chromium qulasa ("Target crashed"), yangi brauzerda bir marta qayta urinadi."""
    try:
        return await _browser_login(phone, password)
    except LoginError:
        raise
    except Exception as e:
        if "crash" not in str(e).lower() and "closed" not in str(e).lower():
            raise
        log.warning("Saytga kirishda Chromium quladi, qayta urinilmoqda: %s", str(e).splitlines()[0])
        return await _browser_login(phone, password)


async def _browser_login(phone: str, password: str) -> str:
    """Ko'rinmas brauzerda login sahifasini to'ldirib, token qaytaradi."""
    async with shared_browser.use() as browser:
        page = await browser.new_page(locale="uz-UZ")
        try:
            await page.goto(f"{BASE_URL}/uz/auth/login", wait_until="networkidle", timeout=60_000)
            await page.fill("input[formcontrolname=phone]", phone[-9:])  # +998 maska ichida bor
            await page.fill("input[formcontrolname=password]", password)
            async with page.expect_response(
                lambda r: r.url.endswith("/api/v1/auth/login") and r.request.method == "POST", timeout=60_000
            ) as resp_info:
                await page.click("button[type=submit]")
            resp = await resp_info.value
            try:
                data = await resp.json()
            except Exception:
                data = {}
            if resp.status != 200:
                error = (data.get("errors") or [{}])[0]
                fallback = data.get("message") or f"HTTP {resp.status}"
                raise LoginError(f"Saytga kirib bo'lmadi: {error.get('messageUz') or fallback}",
                                 f"Не удалось войти на сайт: {error.get('messageRu') or fallback}")
            token = data.get("token")
            if not token:  # javobda bo'lmasa, sayt o'zi sessionStorage ga yozadi
                await page.wait_for_function("() => sessionStorage.getItem('token')", timeout=20_000)
                token = await page.evaluate("() => sessionStorage.getItem('token')")
            return token
        except PwTimeout as e:
            raise LoginError("Sayt javob bermadi (login vaqti tugadi).",
                             "Сайт не ответил (время входа истекло).") from e
        finally:
            await page.close()


class _OwnerStorage:
    """Bot egasining akkaunti — eski fayllarda (account.json, site_token.txt)."""

    def account(self) -> dict | None:
        return json.loads(ACCOUNT_FILE.read_text(encoding="utf-8")) if ACCOUNT_FILE.exists() else None

    def token(self) -> str | None:
        return TOKEN_FILE.read_text(encoding="utf-8").strip() if TOKEN_FILE.exists() else None

    def save(self, account: dict | None, token: str | None):
        for path, value in ((ACCOUNT_FILE, json.dumps(account) if account else None), (TOKEN_FILE, token)):
            if value:
                path.write_text(value, encoding="utf-8")
                _private(path)
            elif path.exists():
                path.unlink()


class _UserStorage:
    """Boshqa foydalanuvchilarning akkauntlari — site_accounts.json da ({user_id: {phone, password, token}})."""

    def __init__(self, user_id: int):
        self.key = str(user_id)

    @staticmethod
    def _all() -> dict:
        return json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8")) if ACCOUNTS_FILE.exists() else {}

    def account(self) -> dict | None:
        entry = self._all().get(self.key) or {}
        return {"phone": entry["phone"], "password": entry["password"]} if entry.get("phone") else None

    def token(self) -> str | None:
        return (self._all().get(self.key) or {}).get("token")

    def save(self, account: dict | None, token: str | None):
        data = self._all()
        if account:
            data[self.key] = {**account, "token": token}
        else:
            data.pop(self.key, None)
        ACCOUNTS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        _private(ACCOUNTS_FILE)


def _private(path: Path):
    try:
        path.chmod(0o600)  # parol va token — faqat bot egasi (server foydalanuvchisi) o'qiy olsin
    except OSError:
        pass


class SiteSession:
    """Bitta foydalanuvchining sayt akkaunti va tokeni; kerak bo'lsa avtomatik qayta kiradi."""

    def __init__(self, storage=None):
        self._store = storage or _OwnerStorage()
        self._lock = asyncio.Lock()
        self._token = self._store.token()

    @property
    def account(self) -> dict | None:
        return self._store.account()

    @property
    def phone(self) -> str | None:
        return (self.account or {}).get("phone")

    async def set_account(self, phone: str, password: str) -> str:
        """Yangi raqamni biriktiradi: avval login qilib tekshiradi, keyin saqlaydi."""
        phone = normalize_phone(phone)
        async with self._lock:
            token = await browser_login(phone, password)
            self._token = token
            self._store.save({"phone": phone, "password": password}, token)
        return phone

    def logout(self):
        """Akkauntni uzadi: raqam, parol va token o'chiriladi."""
        self._token = None
        self._store.save(None, None)

    async def get_token(self, force: bool = False) -> str:
        """Yaroqli token qaytaradi (muddati 5 daqiqadan kam qolgan bo'lsa yangilaydi)."""
        async with self._lock:
            if not force and self._token and not token_expired(self._token, margin=300):
                return self._token
            account = self.account
            if not account:
                raise LoginError(NOT_LINKED_UZ, NOT_LINKED_RU)
            log.info("Saytga qayta kirilmoqda (%s)", account["phone"])
            self._token = await browser_login(account["phone"], account["password"])
            self._store.save(account, self._token)
            return self._token

    def status(self, lang: str = "uz") -> str:
        ru = lang == "ru"
        if not self.account:
            return "вход на сайт не выполнен" if ru else "saytga kirilmagan"
        if self._token and not token_expired(self._token):
            state = "активен" if ru else "faol"
        else:
            state = "токен истёк (обновится автоматически)" if ru else "token eskirgan (avtomatik yangilanadi)"
        user = decode_jwt(self._token or "").get("sub") or self.phone
        return f"+{self.phone} — {state} ({user})"


class SessionStore:
    """Har bir foydalanuvchining o'z sayt akkaunti: bron, to'lov va kuzatuv shu akkaunt bilan qilinadi."""

    def __init__(self, owner_id: int):
        self.owner_id = owner_id
        self._sessions: dict[int, SiteSession] = {}

    def get(self, user_id: int) -> SiteSession:
        user_id = int(user_id)
        if user_id not in self._sessions:
            storage = _OwnerStorage() if user_id == self.owner_id else _UserStorage(user_id)
            self._sessions[user_id] = SiteSession(storage)
        return self._sessions[user_id]

    def linked(self, user_id: int) -> bool:
        return bool(self.get(user_id).account)
