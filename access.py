"""Botdan foydalanish ruxsatlari: so'rovlar, bot egasi bergan va olib qo'ygan ruxsatlar (access.json)."""
import json
import time
from pathlib import Path

from jsonfile import write_json

ACCESS_FILE = Path(__file__).with_name("access.json")


class AccessStore:
    """allowed — ruxsat berilganlar, pending — javob kutayotgan so'rovlar, denied — rad etilgan/ruxsati olinganlar.

    `ids` — bot va ilova filtrlari ishlatadigan umumiy to'plam: o'zgarishlar darhol kuchga kiradi.
    """

    def __init__(self, owner_id: int, env_ids: set[int]):
        self.owner_id = owner_id
        self._data = {"allowed": {}, "pending": {}, "denied": {}}
        if ACCESS_FILE.exists():
            self._data.update(json.loads(ACCESS_FILE.read_text(encoding="utf-8")))
        denied = {int(uid) for uid in self._data["denied"]}
        self.ids: set[int] = ((env_ids | {int(uid) for uid in self._data["allowed"]}) - denied) | {owner_id}

    def _save(self):
        write_json(ACCESS_FILE, self._data)

    def status(self, user_id: int) -> str:
        """"allowed", "pending", "denied" yoki "new"."""
        if user_id in self.ids:
            return "allowed"
        for key in ("pending", "denied"):
            if str(user_id) in self._data[key]:
                return key
        return "new"

    def listed(self, key: str) -> list[int]:
        """key: "pending" yoki "denied"."""
        return [int(uid) for uid in self._data[key]]

    def info(self, user_id: int) -> dict:
        for key in ("allowed", "pending", "denied"):
            if str(user_id) in self._data[key]:
                return self._data[key][str(user_id)]
        return {}

    def request(self, user_id: int, name: str, username: str | None):
        self._data["pending"][str(user_id)] = {"name": name, "username": username, "at": time.time()}
        self._save()

    def _move(self, user_id: int, target: str) -> dict:
        info = self.info(user_id)
        for key in ("allowed", "pending", "denied"):
            self._data[key].pop(str(user_id), None)
        self._data[target][str(user_id)] = {**info, "at": time.time()}
        self._save()
        return info

    def grant(self, user_id: int) -> dict:
        self.ids.add(user_id)
        return self._move(user_id, "allowed")

    def deny(self, user_id: int) -> dict:
        """So'rovni rad etadi yoki berilgan ruxsatni olib qo'yadi (bot egasidan tashqari)."""
        if user_id == self.owner_id:
            return {}
        self.ids.discard(user_id)
        return self._move(user_id, "denied")

    def label(self, user_id: int) -> str:
        info = self.info(user_id)
        name = info.get("name") or str(user_id)
        return f"{name} (@{info['username']})" if info.get("username") else name
