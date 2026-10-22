"""Bo'sh joylar ichidan N kishi uchun ketma-ket joylarni tanlash."""
from dataclasses import dataclass

from categories import group_code, normalize


@dataclass
class SeatChoice:
    group: dict  # kategoriya guruhi (carGroup)
    car: dict  # vagon
    seats: list[int]
    consecutive: bool  # True bo'lsa joylar raqamlari uzluksiz (masalan 21,22,23)
    tier: str | None = None  # "upper"/"lower" — saytdagi "Faqat tepasi/pastki" (bronda ham shunday so'raladi)


def best_window(places: list[int], n: int) -> list[int] | None:
    """Ketma-ket n ta bo'sh joyning eng ixcham oynasini qaytaradi.

    Agar raqamlari uzluksiz n ta joy bo'lsa (21,22,23) aynan o'shani,
    bo'lmasa raqamlar oralig'i eng kichik bo'lgan n ta joyni qaytaradi.
    """
    free = sorted(set(places))
    if n <= 0 or len(free) < n:
        return None
    best = None
    for i in range(len(free) - n + 1):
        window = free[i:i + n]
        if best is None or window[-1] - window[0] < best[-1] - best[0]:
            best = window
            if best[-1] - best[0] == n - 1:
                break
    return best


def choose_seats(train_places: dict, n: int, car_type: str | None = None,
                 categories: list[str] | None = None) -> SeatChoice | None:
    """Barcha vagonlar ichidan n kishi uchun eng yaxshi joylarni tanlaydi.

    categories berilsa (masalan ["2Е"] — Ekonom), faqat shu kategoriyadagi vagonlar ko'riladi.
    car_type — eski kuzatuvlar uchun (vagon turi nomi bo'yicha filtr).
    Birinchi navbatda uzluksiz joylar, keyin eng ixcham joylashuv; teng bo'lsa — arzonroq kategoriya.
    """
    allowed = {normalize(c) for c in categories} if categories else None
    best: SeatChoice | None = None
    groups = sorted(train_places.get("carGroup") or [], key=lambda g: g.get("tariff") or 0)
    for group in groups:
        if allowed is not None and group_code(group) not in allowed:
            continue
        if car_type and car_type != "*" and car_type not in (group.get("typeShow"), group.get("type")):
            continue
        for car in group.get("cars") or []:
            window = best_window(car.get("places") or [], n)
            if not window:
                continue
            choice = SeatChoice(group, car, window, window[-1] - window[0] == n - 1)
            if choice.consecutive:
                return choice
            if best is None or window[-1] - window[0] < best.seats[-1] - best.seats[0]:
                best = choice
    return best
