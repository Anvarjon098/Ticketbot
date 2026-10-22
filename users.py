"""Bot foydalanuvchilari: telefon raqami, saqlangan yo'lovchilar va bronlar tarixi."""
import json
import time
from pathlib import Path

USERS_FILE = Path(__file__).with_name("users.json")


class UserStore:
    def __init__(self):
        self._data: dict[str, dict] = {}
        if USERS_FILE.exists():
            self._data = json.loads(USERS_FILE.read_text(encoding="utf-8"))

    def ids(self) -> list[int]:
        return [int(uid) for uid in self._data]

    def get(self, user_id: int) -> dict:
        return self._data.get(str(user_id), {})

    def update(self, user_id: int, **fields):
        self._data.setdefault(str(user_id), {}).update(fields)
        USERS_FILE.write_text(json.dumps(self._data, ensure_ascii=False, indent=1), encoding="utf-8")

    # ---------- yo'lovchilar (birinchisi — foydalanuvchining o'zi) ----------

    def passengers(self, user_id: int) -> list[dict]:
        user = self.get(user_id)
        if "saved" in user:
            return user["saved"]
        return [user["profile"]] if user.get("profile") else []  # eski format

    def add_passenger(self, user_id: int, passenger: dict, is_self: bool = False):
        """Yo'lovchini saqlaydi (shu pasport bo'lsa — yangilaydi).

        is_self bo'lsa foydalanuvchining o'z ma'lumoti sifatida ro'yxat boshidagi yozuv almashtiriladi.
        """
        current = self.passengers(user_id)
        if is_self:
            companions = [p for p in current[1:] if p["passport"] != passenger["passport"]]
            saved = [passenger] + companions
        elif any(p["passport"] == passenger["passport"] for p in current):
            saved = [passenger if p["passport"] == passenger["passport"] else p for p in current]
        else:
            saved = current + [passenger]
        self.update(user_id, saved=saved)

    def remove_passenger(self, user_id: int, passport: str) -> dict | None:
        saved = self.passengers(user_id)
        removed = next((p for p in saved if p["passport"] == passport), None)
        self.update(user_id, saved=[p for p in saved if p["passport"] != passport])
        return removed

    # ---------- bronlar tarixi ----------

    def bookings(self, user_id: int) -> list[dict]:
        return self.get(user_id).get("bookings", [])

    def add_booking(self, user_id: int, booking: dict):
        self.update(user_id, bookings=[{**booking, "created": time.time()}] + self.bookings(user_id))

    def find_booking(self, order_id: str) -> tuple[int, dict] | None:
        """Buyurtma raqami bo'yicha bronni va uning egasini topadi."""
        for uid, data in self._data.items():
            for booking in data.get("bookings", []):
                if booking.get("order_id") == order_id:
                    return int(uid), booking
        return None

    def add_booking_message(self, order_id: str, chat_id: int, message_id: int):
        """Bron haqidagi bot xabarini eslab qoladi — taymer uni yangilab turadi (bot qayta ishga tushsa ham)."""
        found = self.find_booking(order_id)
        if not found:
            return
        uid, _ = found
        bookings = [{**b, "messages": b.get("messages", []) + [[chat_id, message_id]]}
                    if b.get("order_id") == order_id else b for b in self.bookings(uid)]
        self.update(uid, bookings=bookings)

    def remove_bookings(self, user_id: int, order_ids: set[str]) -> int:
        """Bronlarni ro'yxatdan o'chiradi (faqat botdagi tarix; saytdagi buyurtmaga tegmaydi)."""
        bookings = self.bookings(user_id)
        kept = [b for b in bookings if b.get("order_id") not in order_ids]
        self.update(user_id, bookings=kept)
        return len(bookings) - len(kept)

    def mark_cancelled(self, user_id: int, order_id: str):
        self.set_status(user_id, order_id, "cancelled")

    def set_status(self, user_id: int, order_id: str, status: str):
        """status: "cancelled" — bekor qilingan, "failed" — saytda amalga oshmagan, "expired", "paid"."""
        self.update_booking(user_id, order_id, status=status, status_at=time.time())

    def update_booking(self, user_id: int, order_id: str, **fields):
        """Bron yozuvidagi maydonlarni yangilaydi (masalan approval="approved")."""
        bookings = [{**b, **fields} if b.get("order_id") == order_id else b for b in self.bookings(user_id)]
        self.update(user_id, bookings=bookings)
