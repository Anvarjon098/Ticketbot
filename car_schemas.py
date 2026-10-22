"""Vagon sxemalari (saytdagi rasmlar) va foydalanuvchi o'zi tanlagan joylar.

Sxemalar saytdan olingan SVG fayllar (webapp/schemas): har bir joy — <g id="raqam">, uning bo'sh/band
holatdagi ranglari data-f / data-b atributlarida. index.json: sayt sxema nomi -> fayl va joy raqamlari.
"""
import json
from pathlib import Path

from categories import group_code, name as category_name
from seats import SeatChoice

SCHEMA_DIR = Path(__file__).with_name("webapp") / "schemas"
_INDEX: dict | None = None


def index() -> dict:
    global _INDEX
    if _INDEX is None:
        path = SCHEMA_DIR / "index.json"
        _INDEX = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    return _INDEX


def schema_file(car: dict) -> str | None:
    """Vagon sxemasi fayli (saytdagi nomi bo'yicha) yoki None — noma'lum sxema."""
    entry = index().get(((car.get("schema") or {}).get("name") or ""))
    return entry["file"] if entry else None


TIERS = ("upper", "lower")  # saytdagi "Faqat tepasi" / "Faqat pastki"


def tier_of(seat: int) -> str:
    """Saytdagidek: juft raqam — tepa (yuqori) o'rin, toq — pastki."""
    return "upper" if seat % 2 == 0 else "lower"


def has_tiers(group: dict, car: dict) -> bool:
    """Tepa/pastki filtri ko'rsatiladimi: sayt kabi — yotoqli o'rin ma'lumoti bor va SV emas."""
    detail = car.get("seatDetail") or {}
    if all(detail.get(k) is None for k in ("down", "lateralDn", "up", "lateralUp")):
        return False
    return (group.get("typeShow") or "") not in ("SV", "СВ")


def car_key(group: dict, car: dict) -> str:
    """Vagonni aniqlovchi kalit: kategoriya kodi + vagon raqami (bir vagon turli tariflarda bo'lishi mumkin)."""
    return f"{group_code(group)}|{car['number']}"


def cars(places: dict | None, categories: list[str] | None = None) -> list[dict]:
    """Bo'sh joyi bor vagonlar: raqami, kategoriyasi, narxi, bo'sh joylari va sxemasi."""
    allowed = set(categories) if categories else None
    out = []
    for group in (places or {}).get("carGroup") or []:
        code = group_code(group)
        if allowed is not None and code not in allowed:
            continue
        for car in group.get("cars") or []:
            free = sorted(set(car.get("places") or []))
            if not free:
                continue
            schema = car.get("schema") or {}
            out.append({
                "key": car_key(group, car),
                "number": car["number"],
                "category": code,
                "name_uz": category_name(code, "uz", group.get("typeShow")),
                "name_ru": category_name(code, "ru", group.get("typeShow")),
                "price": group.get("tariff") or 0,
                "free": free,
                "schema": schema_file(car),
                "seats_count": schema.get("seatsCount") or (max(free) if free else 0),
                "tiers": has_tiers(group, car),
            })
    out.sort(key=lambda c: (int(c["number"]) if c["number"].isdigit() else 999, c["price"]))
    return out


def pick(places: dict | None, key: str, seats: list[int], tier: str | None = None) -> SeatChoice | None:
    """Foydalanuvchi tanlagan joylar hali ham bo'shmi — bo'lsa bron uchun SeatChoice, aks holda None.

    tier ("upper"/"lower") — saytdagi "Faqat tepasi/pastki" filtri: joylar shu qatordan bo'lishi kerak.
    """
    wanted = sorted({int(s) for s in seats})
    if tier not in TIERS:
        tier = None
    for group in (places or {}).get("carGroup") or []:
        for car in group.get("cars") or []:
            if car_key(group, car) != key:
                continue
            if not wanted or not set(wanted) <= set(car.get("places") or []):
                return None
            if tier and (not has_tiers(group, car) or any(tier_of(s) != tier for s in wanted)):
                return None
            return SeatChoice(group, car, wanted, wanted[-1] - wanted[0] == len(wanted) - 1, tier)
    return None
