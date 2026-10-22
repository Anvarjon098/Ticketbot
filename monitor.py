"""Joyi tugagan reyslarni kuzatish va joy chiqishi bilan avtomatik bron qilish."""
import asyncio
import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

from booking import booking_record, booking_text, payment_deadline, reserve
from categories import describe
from seats import SeatChoice, choose_seats
from site_auth import SiteSession
from uzrail import RateLimitError, UzRailClient, UzRailError

MONITORS_FILE = Path(__file__).with_name("monitors.json")
log = logging.getLogger(__name__)

Notify = Callable[..., Awaitable[None]]  # notify(chat_id, text, order_id=None)
OnBooked = Callable[[int, dict, str], Awaitable[object]]  # on_booked(chat_id, record, headline)


@dataclass
class Monitor:
    chat_id: int
    passengers: list[dict]
    from_name: str
    to_name: str
    trip_date: str  # YYYY-MM-DD
    train: dict  # reyslar ro'yxatidagi poyezd ma'lumoti
    car_type: str  # "*" — istalgan vagon turi
    strict: bool  # True: faqat raqamlari uzluksiz joylar bo'lsa bron qilinadi
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    active: bool = True
    checks: int = 0
    created: float = field(default_factory=time.time)
    order_id: str | None = None
    categories: list[str] | None = None  # kategoriya kodlari (["2Е"] — faqat Ekonom); None — istalgani

    @property
    def departure(self) -> datetime:
        return datetime.strptime(self.train["departureDate"], "%d.%m.%Y %H:%M")

    def title(self) -> str:
        if self.categories:
            car = f", {describe(self.categories, train=self.train)}"
        else:
            car = "" if self.car_type == "*" else f", {self.car_type}"
        mode = "ketma-ket" if self.strict else "bir vagonda"
        return (f"{self.from_name} → {self.to_name}, {self.train['departureDate']}, "
                f"poyezd {self.train['number']}{car}, {len(self.passengers)} kishi ({mode})")


class MonitorManager:
    MAX_FAILS = 3
    FAIL_COOLDOWN = 30  # sekund

    def __init__(self, client: UzRailClient, session: SiteSession, notify: Notify,
                 owner_id: int, interval: float = 1.0, on_booked: OnBooked | None = None):
        self.client = client
        self.session = session
        self.notify = notify
        self.on_booked = on_booked
        self.owner_id = owner_id
        self.interval = interval
        self.monitors: dict[str, Monitor] = {}
        self._fails: dict[str, int] = {}  # ketma-ket rad etilgan bronlar soni
        self._cooldown: dict[str, float] = {}  # rad etilgandan keyin shu vaqtgacha bron qilinmaydi
        self._turn = 0
        self._last_alert: dict[str, float] = {}
        self._load()

    # ---------- saqlash ----------

    def _load(self):
        if MONITORS_FILE.exists():
            for item in json.loads(MONITORS_FILE.read_text(encoding="utf-8")):
                m = Monitor(**item)
                self.monitors[m.id] = m

    def _save(self):
        MONITORS_FILE.write_text(
            json.dumps([asdict(m) for m in self.monitors.values()], ensure_ascii=False, indent=1),
            encoding="utf-8",
        )

    def add(self, monitor: Monitor):
        self.monitors[monitor.id] = monitor
        self._save()

    def remove(self, monitor_id: str) -> Monitor | None:
        m = self.monitors.pop(monitor_id, None)
        self._save()
        return m

    def active(self) -> list[Monitor]:
        return [m for m in self.monitors.values() if m.active]

    # ---------- xabarlar ----------

    async def _tell(self, monitor: Monitor | None, text: str, order_id: str | None = None):
        """Bot egasiga va kuzatuvni yaratgan foydalanuvchiga xabar beradi (bron bo'lsa — bekor qilish tugmasi bilan)."""
        targets = {self.owner_id} | ({monitor.chat_id} if monitor else set())
        for chat_id in targets:
            try:
                await self.notify(chat_id, text, order_id=order_id)
            except Exception:
                log.exception("Xabar yuborilmadi: %s", chat_id)

    async def _alert(self, key: str, text: str, every: float = 600):
        """Bir xil ogohlantirishni har `every` sekundda bir martadan ko'p yubormaydi."""
        if time.time() - self._last_alert.get(key, 0) >= every:
            self._last_alert[key] = time.time()
            await self._tell(None, text)

    # ---------- asosiy sikl ----------

    async def run(self):
        """Har `interval` sekundda bitta so'rov: faol kuzatuvlar navbatma-navbat tekshiriladi."""
        backoff = 0
        asyncio.create_task(self._keep_logged_in())
        while True:
            started = time.monotonic()
            monitors = self.active()
            if monitors:
                monitor = monitors[self._turn % len(monitors)]
                self._turn += 1
                try:
                    await self._check(monitor)
                    backoff = 0
                except RateLimitError as e:
                    backoff = min(max(backoff * 2, 30), 300)
                    log.warning("Bloklandi, %s s kutiladi: %s", backoff, e)
                    await self._alert("ratelimit", f"⚠️ {e}\nKuzatuv {backoff} sekundga to'xtatildi.")
                    await asyncio.sleep(backoff)
                except UzRailError as e:
                    log.warning("Tekshiruv xatosi (%s): %s", monitor.id, e)
                except Exception:
                    log.exception("Kutilmagan xato (%s)", monitor.id)
            await asyncio.sleep(max(0.0, self.interval - (time.monotonic() - started)))

    async def _keep_logged_in(self):
        """Kuzatuv bor ekan, sayt tokenini oldindan yangilab turadi — bron paytida login kutilmasin."""
        while True:
            if self.active():
                try:
                    await self.session.get_token()
                except UzRailError as e:
                    await self._alert("login", f"⚠️ Saytga kirishda xato: {e}")
                except Exception as e:
                    log.exception("Login xatosi")
                    await self._alert("login", f"⚠️ Saytga kirishda xato: {e.__class__.__name__}")
            await asyncio.sleep(60)

    async def _check(self, m: Monitor):
        if m.departure <= datetime.now():
            m.active = False
            self._save()
            await self._tell(m, f"⌛ Poyezd jo'nab ketdi, kuzatuv to'xtatildi:\n{m.title()}")
            return
        m.checks += 1
        sub = m.train["subRoute"]
        places = await self.client.train_places(m.trip_date, sub["depStationCode"], sub["arvStationCode"], m.train)
        if not places:
            return
        car_type = None if m.car_type == "*" else m.car_type
        choice = choose_seats(places, len(m.passengers), car_type, m.categories)
        if not choice or (m.strict and not choice.consecutive):
            return
        if time.time() < self._cooldown.get(m.id, 0):
            return  # yaqinda rad etilgan — biroz kutamiz
        log.info("Joy topildi (%s): vagon %s, %s", m.id, choice.car["number"], choice.seats)
        await self._book(m, choice)

    async def _book(self, m: Monitor, choice: SeatChoice):
        try:
            order_id = await reserve(self.client, self.session, m.train, choice, m.passengers)
        except RateLimitError:
            raise
        except UzRailError as e:
            # Joy boshqa xaridorga ketgan bo'lishi mumkin — biroz kutib, kuzatish davom etadi.
            # Ketma-ket rad etilsa (masalan, yo'lovchi ma'lumoti xato) — saytga bron yog'dirmaslik uchun to'xtatamiz.
            fails = self._fails.get(m.id, 0) + 1
            self._fails[m.id] = fails
            self._cooldown[m.id] = time.time() + self.FAIL_COOLDOWN
            if fails >= self.MAX_FAILS:
                m.active = False
                self._save()
                await self._tell(m, f"⛔ Kuzatuv to'xtatildi: bron {fails} marta rad etildi.\nSabab: {e}\n{m.title()}\n"
                                    "Yo'lovchi ma'lumotlarini tekshirib, kuzatuvni qayta yarating.")
            else:
                await self._alert(f"book:{m.id}", f"⚠️ Joy topildi, lekin bron bo'lmadi: {e}\n"
                                                  f"Kuzatish davom etmoqda.\n{m.title()}", every=120)
            return
        self._fails.pop(m.id, None)
        m.active = False
        m.order_id = order_id
        self._save()
        record = booking_record(order_id, m.train, choice, m.passengers, m.from_name, m.to_name)
        record["expires_at"] = await payment_deadline(self.client, self.session, order_id)
        headline = "🎉 KUZATUVDAGI REYSDA JOY BRON QILINDI!"
        if self.on_booked:  # saqlaydi va xabar beradi (boshqa foydalanuvchiniki — egasining tasdig'i bilan)
            await self.on_booked(m.chat_id, record, headline)
        else:
            await self._tell(m, f"{headline}\n" + booking_text(record), order_id=order_id)
