"""Joy kategoriyalari (xizmat klassi): VIP, Biznes, Ekonom, SV, Kupe, Plaskart, Umumiy va boshqalar.

Sayt har bir narxni "classServiceType" kodi bilan beradi (reyslar ro'yxatida — tariffs[].classServiceType,
joylar xaritasida — carGroup.services.type). Bir xil "O'rindiqli" vagon turi ichida Biznes va Ekonom
bo'lishi mumkin, shuning uchun joy kategoriya kodi bo'yicha tanlanadi.

Qaysi poyezdda qaysi kategoriyalar borligi train_catalog.json da (poyezd raqami bo'yicha) saqlanadi —
u eticket.uzrailpass.uz dagi barcha asosiy ichki, viloyatlararo va xalqaro yo'nalishlardan yig'ilgan.
Joyi tugagan reysda kategoriyalar saytdan kelmaydi — shunda shu ma'lumotnoma ishlatiladi.
"""
import json
import re
from pathlib import Path

CATALOG_FILE = Path(__file__).with_name("train_catalog.json")

# Ma'nosi aniq kodlar: kod -> (o'zbekcha, ruscha)
CLASS_NAMES = {
    "1В": ("VIP", "VIP"),
    "1С": ("Biznes", "Бизнес"),
    "2Е": ("Ekonom", "Эконом"),
    "2Б": ("Ekonom", "Эконом"),
    "2В": ("Ekonom", "Эконом"),
    "1Л": ("SV", "СВ"),
    "2К": ("Kupe", "Купе"),
    "3П": ("Plaskart", "Плацкарт"),
    "3О": ("Umumiy", "Общий"),
}
# Boshqa kodlar uchun nom vagon turidan olinadi (saytdagi o'zbekcha nom -> (o'zbekcha, ruscha))
CAR_TYPE_NAMES = {
    "sv": ("SV", "СВ"),
    "lyuks": ("Lyuks", "Люкс"),
    "kupe": ("Kupe", "Купе"),
    "plaskartli": ("Plaskart", "Плацкарт"),
    "umumiy": ("Umumiy", "Общий"),
    "o'rindiqli": ("O'rindiqli", "Сидячий"),
    "сидячий": ("O'rindiqli", "Сидячий"),
}
ORDER = list(CLASS_NAMES)

# Ma'lumotnomada yo'q yangi poyezdlar uchun: poyezd turi (brand) -> kategoriya kodlari
BRAND_CLASSES = {
    "afrosiyob": ["1В", "1С", "2Е"],
    "jaloliddin manguberdi": ["1В", "1С", "2Е"],
    "o'zbekiston": ["1В", "1С", "2Б"],
    "sharq": ["1В", "1С", "2В"],
    "nasaf": ["1В", "1С", "2В"],
    "nu": ["2В"],
    "tezyurar": ["1А", "1Л", "2К", "3П"],
    "yo'lovchi": ["1Л", "2К", "3П", "3О"],
}
BRAND_ALIASES = {
    "жалолиддин мангуберди": "jaloliddin manguberdi",
    "афросиёб": "afrosiyob",
    "шарк": "sharq",
    "насаф": "nasaf",
    "ўзбекистон": "o'zbekiston",
    "узбекистан": "o'zbekiston",
    "скорый": "tezyurar",
    "cкорый": "tezyurar",  # saytda birinchi harf lotincha "C" bilan keladi
    "пассажирский": "yo'lovchi",
}
_LOOKALIKES = str.maketrans("EeCcBKkOoAaHMPpTXx", "ЕеСсВКкОоАаНМРрТХх")


def _load_catalog() -> dict:
    if CATALOG_FILE.exists():
        return json.loads(CATALOG_FILE.read_text(encoding="utf-8")).get("trains", {})
    return {}


TRAINS = _load_catalog()  # poyezd raqami (faqat raqamlar, "082Ф" -> "82") -> {"brand", "route", "classes": {kod: vagon turi}}


def normalize(code: str | None) -> str:
    """Kodni saytdagi ko'rinishga keltiradi (lotincha o'xshash harflar -> kirill): '2E' -> '2Е'."""
    return (code or "").strip().upper().translate(_LOOKALIKES)


def train_key(number: str | None) -> str:
    digits = re.sub(r"\D", "", number or "").lstrip("0")
    return digits or (number or "")


SEATED_CODES = {"1В", "1С", "2Е", "2Б", "2В"}  # o'rindiqli vagonlardagi VIP/Biznes/Ekonom


def name(code: str, lang: str = "uz", car_type: str | None = None) -> str:
    """Kategoriya nomi. Noma'lum kod bo'lsa — vagon turi va kod: "Kupe (2Д)".

    Xalqaro poyezdlarda o'sha kodlar boshqa ma'noda keladi (masalan №1 Toshkent—Olmaotada 2Б — Kupe),
    shuning uchun vagon turi o'rindiqli bo'lmasa, nom vagon turidan olinadi.
    """
    i = 1 if lang == "ru" else 0
    code = normalize(code)
    car = CAR_TYPE_NAMES.get((car_type or "").strip().lower())
    if code in CLASS_NAMES and not (code in SEATED_CODES and car and car[0] != "O'rindiqli"):
        return CLASS_NAMES[code][i]
    car = car or CAR_TYPE_NAMES.get((_catalog_car(code) or "").strip().lower())
    return f"{car[i]} ({code})" if car else code


def _catalog_car(code: str) -> str | None:
    for info in TRAINS.values():
        if code in info.get("classes", {}):
            return info["classes"][code]
    return None


def brand_classes(brand: str | None) -> list[str]:
    key = (brand or "").strip().lower()
    return BRAND_CLASSES.get(BRAND_ALIASES.get(key, key), [])


def known_classes(train: dict) -> dict[str, str | None]:
    """Ma'lumotnoma bo'yicha shu poyezdning kategoriyalari: {kod: vagon turi}.

    Poyezd raqami bo'yicha kuzatilganlar + poyezd turining to'liq to'plami (nomli poyezdlarda — Afrosiyob,
    Sharq va h.k. — kategoriyalar bir xil). Oddiy "Yo'lovchi" poyezdlarida tarkib har xil, shuning uchun
    raqam ma'lumotnomada bo'lsa, faqat o'sha poyezdniki olinadi.
    """
    key = train_key(train.get("number"))
    info = TRAINS.get(key)
    classes = dict(info["classes"]) if info else {}
    if not classes and key.isdigit():
        # juft poyezd (123/124 — qarama-qarshi yo'nalishdagi bir xil tarkib)
        pair = TRAINS.get(str(int(key) + 1 if int(key) % 2 else int(key) - 1))
        classes = dict(pair["classes"]) if pair else {}
    brand = (train.get("brand") or (info or {}).get("brand") or "").strip().lower()
    if not classes or BRAND_ALIASES.get(brand, brand) != "yo'lovchi":
        for code in brand_classes(brand):
            classes.setdefault(code, None)
    return classes


def group_code(group: dict) -> str:
    """Joylar xaritasidagi guruhning kategoriya kodi."""
    code = (group.get("services") or {}).get("type")
    if not code:
        cars = group.get("cars") or [{}]
        code = (cars[0].get("schema") or {}).get("service")
    return normalize(code)


def _sort_key(code: str) -> tuple:
    """Avval klass raqami (1 — VIP/Biznes/SV, 2 — Ekonom/Kupe, 3 — Plaskart/Umumiy), keyin ma'lum tartib."""
    digit = int(code[0]) if code[:1].isdigit() else 9
    return (digit, ORDER.index(code) if code in ORDER else len(ORDER), code)


def _entry(code: str, free: int, price, car_type: str | None) -> dict:
    return {"code": code, "name_uz": name(code, "uz", car_type), "name_ru": name(code, "ru", car_type),
            "free": free, "price": price, "detail": {"down": 0, "up": 0, "side_down": 0, "side_up": 0}}


def _add_detail(entry: dict, seat_detail: dict | None):
    """Yotoqli vagonlarda: pastki / yuqori / yon pastki / yon yuqori o'rinlar soni."""
    d = seat_detail or {}
    for key, src in (("down", "down"), ("up", "up"), ("side_down", "lateralDn"), ("side_up", "lateralUp")):
        entry["detail"][key] += d.get(src) or 0


def train_categories(train: dict, places: dict | None = None) -> list[dict]:
    """Reysning barcha kategoriyalari: bo'sh joylar soni va narxi bilan.

    places berilsa (joylar xaritasi) — undagi aniq ma'lumot, aks holda reyslar ro'yxatidagi narxlar.
    Ma'lumotnomadagi, lekin hozir joyi yo'q kategoriyalar ham qo'shiladi (free=0) — ularni kuzatish mumkin.
    """
    found: dict[str, dict] = {}
    if places:
        for group in places.get("carGroup") or []:
            code = group_code(group)
            entry = found.setdefault(code, _entry(code, 0, group.get("tariff"), group.get("typeShow")))
            for car in group.get("cars") or []:
                entry["free"] += len(car.get("places") or [])
                _add_detail(entry, car.get("seatDetail"))
    else:
        for car in train.get("cars") or []:
            tariffs = car.get("tariffs") or []
            # Yotoqli vagonli poyezdlarda (Tezyurar, Yo'lovchi) sayt narx yonidagi joylar sonini 0 deb beradi —
            # to'g'ri son vagon darajasida (car.freeSeats). Narxlardagi yig'indi kam bo'lsa, farq birinchi narxga qo'shiladi.
            missing = max((car.get("freeSeats") or 0) - sum(t.get("freeSeats") or 0 for t in tariffs), 0)
            for i, tariff in enumerate(tariffs):
                code = normalize(tariff.get("classServiceType"))
                entry = found.setdefault(code, _entry(code, 0, tariff.get("tariff"), car.get("type")))
                entry["free"] += (tariff.get("freeSeats") or 0) + (missing if i == 0 else 0)
                if i == 0:
                    _add_detail(entry, car.get("seatDetail"))
                if tariff.get("tariff") and (not entry["price"] or tariff["tariff"] < entry["price"]):
                    entry["price"] = tariff["tariff"]
    for code, car_type in known_classes(train).items():
        found.setdefault(code, _entry(code, 0, None, car_type))
    return sorted(found.values(), key=lambda e: _sort_key(e["code"]))


def describe(codes: list[str] | None, lang: str = "uz", train: dict | None = None) -> str:
    """Kategoriyalar ro'yxati matni; train berilsa — nomlar shu poyezdning vagon turlariga qarab."""
    if not codes:
        return "istalgan kategoriya" if lang != "ru" else "любая категория"
    cars = known_classes(train) if train else {}
    return ", ".join(name(c, lang, cars.get(normalize(c))) for c in codes)
