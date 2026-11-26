"""Tekshiruv: Chromium saytning kirish sahifasini ocha oladimi va jarayonlar limitiga qancha yaqin keladi.

    source deploy/env_noroot.sh && .venv/bin/python deploy/diag_chromium.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import shared_browser  # noqa: E402
from uzrail import BASE_URL  # noqa: E402

CG = Path("/sys/fs/cgroup")


def pids() -> str:
    try:
        return (CG / "pids.current").read_text().strip()
    except OSError:
        return "?"


async def main():
    try:
        limit = (CG / "pids.max").read_text().strip()
    except OSError:
        limit = "?"
    print(f"Jarayonlar limiti: {limit}; hozir: {pids()}")
    print("Chromium sozlamalari:", " ".join(shared_browser.LAUNCH_ARGS))
    peak = 0

    async def watch():
        nonlocal peak
        while True:
            p = pids()
            if p.isdigit():
                peak = max(peak, int(p))
            await asyncio.sleep(0.1)

    w = asyncio.create_task(watch())
    try:
        async with shared_browser.use() as browser:
            page = await browser.new_page(locale="uz-UZ")
            await page.goto(f"{BASE_URL}/uz/auth/login", wait_until="networkidle", timeout=60_000)
            ok = await page.locator("input[formcontrolname=phone]").count()
            await asyncio.sleep(2)
            await page.close()
        print("Natija: kirish sahifasi ochildi" + ("" if ok else " (lekin telefon maydoni topilmadi)"))
    except Exception as e:
        print("Natija: XATO —", str(e).splitlines()[0])
    finally:
        w.cancel()
        await shared_browser.close()
    print(f"Eng ko'p jarayonlar: {peak} / {limit}")


asyncio.run(main())
