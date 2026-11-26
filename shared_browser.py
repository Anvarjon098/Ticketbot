"""Bitta umumiy ko'rinmas Chromium (Playwright) — saytga kirish va vagon rasmi uchun.

Har bir Chromium ~75 ta jarayon/oqim ochadi. Kichik serverlarda jarayonlar soni cheklangan (masalan, 120),
shuning uchun ikkita brauzer bir vaqtda ochilsa limit oshib, Chromium qulaydi ("Target crashed").
Bu modul hamma uchun bitta brauzerni ishlatadi va u bir necha daqiqa kerak bo'lmasa yopiladi.
"""
import asyncio
import logging
import os
from contextlib import asynccontextmanager

from playwright.async_api import Browser, async_playwright

log = logging.getLogger(__name__)
IDLE_CLOSE = 300  # soniya
# Jarayon/oqimlar sonini kamaytiradigan sozlamalar (~69 -> ~57). CHROMIUM_ARGS bilan almashtirish mumkin.
DEFAULT_ARGS = "--renderer-process-limit=1 --no-zygote --num-raster-threads=1 --in-process-gpu --disable-gpu"
LAUNCH_ARGS = os.getenv("CHROMIUM_ARGS", DEFAULT_ARGS).split()

_lock = asyncio.Lock()
_pw = None
_browser: Browser | None = None
_users = 0
_closer: asyncio.Task | None = None


async def _shutdown():
    global _pw, _browser
    try:
        if _browser:
            await _browser.close()
    except Exception:
        log.debug("Brauzerni yopishda xato", exc_info=True)
    try:
        if _pw:
            await _pw.stop()
    except Exception:
        log.debug("Playwright'ni to'xtatishda xato", exc_info=True)
    _pw = _browser = None


async def _close_later():
    await asyncio.sleep(IDLE_CLOSE)
    async with _lock:
        if _users == 0:
            await _shutdown()


@asynccontextmanager
async def use():
    """async with shared_browser.use() as browser: ...  — umumiy brauzerni beradi."""
    global _pw, _browser, _users, _closer
    async with _lock:
        if _closer:
            _closer.cancel()
            _closer = None
        if _browser is None or not _browser.is_connected():  # birinchi marta yoki qulagan bo'lsa
            await _shutdown()
            _pw = await async_playwright().start()
            _browser = await _pw.chromium.launch(headless=True, args=LAUNCH_ARGS)
        _users += 1
        browser = _browser
    broken = False
    try:
        yield browser
    except Exception as e:
        # "Target crashed" va h.k. — brauzer ishdan chiqqan bo'lishi mumkin: keyingi safar yangisi ochiladi
        broken = any(w in str(e).lower() for w in ("crash", "closed", "disconnected"))
        raise
    finally:
        async with _lock:
            _users -= 1
            if broken and _browser is browser:
                log.warning("Chromium ishdan chiqdi — yopildi, keyingi so'rovda qayta ishga tushiriladi")
                await _shutdown()  # buzilgan brauzer — uni ishlatayotgan boshqa so'rovlar ham baribir xato beradi
            if _users == 0 and _browser is not None:
                _closer = asyncio.create_task(_close_later())


async def close():
    global _closer
    async with _lock:
        if _closer:
            _closer.cancel()
            _closer = None
        await _shutdown()
