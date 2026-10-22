"""Saytga telefon raqam + parol bilan fon rejimida (headless brauzer) kirish.

Sayt kirish so'rovini reCAPTCHA v3 bilan himoyalaydi, shuning uchun login
oddiy HTTP so'rov bilan emas, ko'rinmas Chromium brauzer orqali qilinadi.
Olingan token faylga saqlanadi va muddati tugashidan oldin avtomatik yangilanadi.
"""
import asyncio
import json
import logging
import re
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as PwTimeout

from uzrail import BASE_URL, UzRailError, decode_jwt, token_expired

ACCOUNT_FILE = Path(__file__).with_name("account.json")
TOKEN_FILE = Path(__file__).with_name("site_token.txt")
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
    """Ko'rinmas brauzerda login sahifasini to'ldirib, token qaytaradi."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page(locale="uz-UZ")
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
            await browser.close()


class SiteSession:
    """Login ma'lumotlari va tokenni boshqaradi; kerak bo'lsa avtomatik qayta kiradi."""

    def __init__(self):
        self._lock = asyncio.Lock()
        self._token = TOKEN_FILE.read_text(encoding="utf-8").strip() if TOKEN_FILE.exists() else None

    @property
    def account(self) -> dict | None:
        if ACCOUNT_FILE.exists():
            return json.loads(ACCOUNT_FILE.read_text(encoding="utf-8"))
        return None

    @property
    def phone(self) -> str | None:
        return (self.account or {}).get("phone")

    async def set_account(self, phone: str, password: str) -> str:
        """Yangi raqamni biriktiradi: avval login qilib tekshiradi, keyin saqlaydi."""
        phone = normalize_phone(phone)
        async with self._lock:
            token = await browser_login(phone, password)
            ACCOUNT_FILE.write_text(json.dumps({"phone": phone, "password": password}), encoding="utf-8")
            self._save(token)
        return phone

    async def get_token(self, force: bool = False) -> str:
        """Yaroqli token qaytaradi (muddati 5 daqiqadan kam qolgan bo'lsa yangilaydi)."""
        async with self._lock:
            if not force and self._token and not token_expired(self._token, margin=300):
                return self._token
            account = self.account
            if not account:
                raise LoginError("Saytga kirilmagan. Botda \"Saytga kirish\" yoki ilovada Profil bo'limidan kiring.",
                                 "Вход на сайт не выполнен. Войдите через «Saytga kirish» в боте "
                                 "или в разделе «Профиль» приложения.")
            log.info("Saytga qayta kirilmoqda (%s)", account["phone"])
            self._save(await browser_login(account["phone"], account["password"]))
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

    def _save(self, token: str):
        self._token = token
        TOKEN_FILE.write_text(token, encoding="utf-8")
