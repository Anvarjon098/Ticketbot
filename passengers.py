"""Yo'lovchi ma'lumotlarini tekshirish (bot matnidan ham, ilova formasidan ham)."""
import re
from datetime import date

GENDER_WORDS = {
    "ERKAK": "MALE", "E": "MALE", "M": "MALE", "MALE": "MALE", "ЭРКАК": "MALE", "МУЖ": "MALE", "МУЖСКОЙ": "MALE", "Э": "MALE",
    "AYOL": "FEMALE", "A": "FEMALE", "F": "FEMALE", "FEMALE": "FEMALE", "АЁЛ": "FEMALE", "ЖЕН": "FEMALE",
    "ЖЕНСКИЙ": "FEMALE", "А": "FEMALE", "Ж": "FEMALE",
}
GENDER_LABEL = {"MALE": "Erkak", "FEMALE": "Ayol"}

# Hujjat turlari — saytdagi kodlar (eticket.uzrailpass.uz docTypeList). "PASSPORT" kabi boshqa qiymatni
# Express sotuv tizimi tanimaydi va bronni "US97: первым элементом ... должен быть документ" xatosi bilan rad etadi.
DOC_TYPES = {"ПУ": ("Pasport", "Паспорт"), "УЛ": ("ID-karta", "ID-карта")}
ID_CARD_SERIES = {"AD", "AE"}  # O'zbekiston ID-kartalari seriyasi; qolganlari (AA, AB, AC, FA, ...) — pasport


def guess_doc_type(passport: str) -> str:
    return "УЛ" if (passport or "")[:2].upper() in ID_CARD_SERIES else "ПУ"


def doc_type_label(doc_type: str, lang: str = "uz") -> str:
    return DOC_TYPES.get(doc_type, DOC_TYPES["ПУ"])[1 if lang == "ru" else 0]
NAME_RE = r"[A-ZА-ЯЁЎҚҒҲ'ʻʼ`-]+"
CYR_TO_LAT = str.maketrans("АВЕКМНОРСТХ", "ABEKMHOPCTX")  # pasport seriyasi kirillda yozilsa


# Tekshiruv xabarlari: (o'zbekcha, ruscha)
ERRORS = {
    "birthday": ("tug'ilgan sana (KK.OO.YYYY)", "дата рождения (ДД.ММ.ГГГГ)"),
    "birthday_future": ("tug'ilgan sana kelajakda bo'lishi mumkin emas", "дата рождения не может быть в будущем"),
    "under_16": ("16 yoshdan kichik yo'lovchilarni bot qo'llab-quvvatlamaydi — ularni saytdan oling",
                 "пассажиры младше 16 лет ботом не поддерживаются — оформите их на сайте"),
    "passport": ("pasport (masalan AA1234567)", "паспорт (например AA1234567)"),
    "gender": ("jinsi", "пол"),
    "last_name": ("familiya", "фамилия"),
    "first_name": ("ism", "имя"),
    "middle_name": ("otasining ismi", "отчество"),
    "letters_only": ("faqat harflar", "только буквы"),
    "check": ("Tekshiring", "Проверьте"),
}


def _t(key: str, lang: str) -> str:
    return ERRORS[key][1 if lang == "ru" else 0]


def _check_birthday(birthday: date | None, errors: list[str], lang: str = "uz"):
    if not birthday:
        errors.append(_t("birthday", lang))
    elif birthday > date.today():
        errors.append(_t("birthday_future", lang))
    elif (date.today() - birthday).days < 16 * 365:
        errors.append(_t("under_16", lang))


def parse_passenger_text(text: str) -> dict:
    """Bitta xabardan yo'lovchi ma'lumotini ajratib oladi (maydonlar tartibi muhim emas)."""
    rest = text.upper()
    errors = []

    passport = None
    m = re.search(r"\b([A-ZА-Я]{2})\s?(\d{7})\b", rest)
    if m:
        passport = (m.group(1) + m.group(2)).translate(CYR_TO_LAT)
        rest = rest[:m.start()] + " " + rest[m.end():]
    if not passport or not re.fullmatch(r"[A-Z]{2}\d{7}", passport):
        errors.append("pasport (masalan AA1234567)")

    birthday = None
    m = re.search(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{4})\b", rest)
    if m:
        try:
            birthday = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            pass
        rest = rest[:m.start()] + " " + rest[m.end():]
    _check_birthday(birthday, errors)

    words = [w for w in re.split(r"[\s,;]+", rest) if w]
    # hujjat turi (ixtiyoriy): "ID", "ID-KARTA" yoki "PASPORT"; yozilmasa — seriyasiga qarab
    doc_type = None
    for w in list(words):
        if w in ("ID", "ID-KARTA", "IDKARTA", "ID-КАРТА", "АЙДИ"):
            doc_type = "УЛ"; words.remove(w)
        elif w in ("PASPORT", "PASSPORT", "ПАСПОРТ"):
            doc_type = "ПУ"; words.remove(w)
    genders = [w for w in words if w in GENDER_WORDS]
    gender = GENDER_WORDS[genders[-1]] if genders else None
    if genders:
        words.remove(genders[-1])
    if not gender:
        errors.append("jinsi (Erkak yoki Ayol)")

    names = [w for w in words if re.fullmatch(NAME_RE, w)]
    if len(names) < 2 or len(names) != len(words):
        errors.append("familiya va ism (faqat harflar)")

    if errors:
        raise ValueError("\n".join(f"• {e}" for e in errors))
    return {"last_name": names[0], "first_name": names[1], "middle_name": " ".join(names[2:]),
            "passport": passport, "birthday": birthday.isoformat(), "gender": gender,
            "doc_type": doc_type or guess_doc_type(passport)}


def validate_fields(fields: dict, lang: str = "uz") -> dict:
    """Ilova formasidagi alohida maydonlarni tekshiradi. birthday: YYYY-MM-DD. lang: xato xabarlari tili."""
    errors = []
    names = {}
    for key, required in (("last_name", True), ("first_name", True), ("middle_name", False)):
        value = " ".join(str(fields.get(key) or "").upper().split())
        if value and not all(re.fullmatch(NAME_RE, w) for w in value.split()):
            errors.append(f"{_t(key, lang)} ({_t('letters_only', lang)})")
        elif required and not value:
            errors.append(_t(key, lang))
        names[key] = value
    passport = re.sub(r"\s", "", str(fields.get("passport") or "")).upper().translate(CYR_TO_LAT)
    if not re.fullmatch(r"[A-Z]{2}\d{7}", passport):
        errors.append(_t("passport", lang))
    try:
        birthday = date.fromisoformat(str(fields.get("birthday") or ""))
    except ValueError:
        birthday = None
    _check_birthday(birthday, errors, lang)
    gender = fields.get("gender")
    if gender not in GENDER_LABEL:
        errors.append(_t("gender", lang))
    if errors:
        raise ValueError(f"{_t('check', lang)}: " + ", ".join(errors))
    doc_type = fields.get("doc_type") if fields.get("doc_type") in DOC_TYPES else guess_doc_type(passport)
    return {**names, "passport": passport, "birthday": birthday.isoformat(), "gender": gender, "doc_type": doc_type}
