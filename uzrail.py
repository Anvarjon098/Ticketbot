"""eticket.uzrailpass.uz sayti API'si bilan ishlovchi asinxron klient."""
import asyncio
import base64
import binascii
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime

import httpx

from passengers import DOC_TYPES, guess_doc_type

BASE_URL = "https://eticket.uzrailpass.uz"
log = logging.getLogger(__name__)


class UzRailError(Exception):
    def __init__(self, message: str, ru: str | None = None):
        super().__init__(message)
        self.ru = ru  # ilovada ruscha ko'rsatish uchun (bo'lmasa — asl matn)

    def text(self, lang: str = "uz") -> str:
        return self.ru if lang == "ru" and self.ru else str(self)


class AuthError(UzRailError):
    """Saytga kirish tokeni yaroqsiz — qayta login kerak."""


class RateLimitError(UzRailError):
    """Sayt juda ko'p so'rov uchun vaqtincha bloklagan."""


@dataclass
class Passenger:
    last_name: str
    first_name: str
    middle_name: str
    passport: str
    birthday: str  # YYYY-MM-DD
    gender: str  # MALE / FEMALE
    doc_type: str = ""  # "ПУ" — pasport, "УЛ" — ID-karta; bo'sh bo'lsa seriyasiga qarab aniqlanadi

    @property
    def document_type(self) -> str:
        return self.doc_type if self.doc_type in DOC_TYPES else guess_doc_type(self.passport)

    @property
    def full_name(self) -> str:
        return " ".join(p for p in (self.last_name, self.first_name, self.middle_name) if p)


def clean_name(value: str) -> str:
    """Ismni sayt qabul qiladigan ko'rinishga keltiradi: faqat harflar va bo'sh joy.

    Saytning o'z formasi ham boshqa belgilarni yozdirmaydi. Apostrof bilan yuborilgan ism ("SULAYMON O'G'LI")
    Express tizimida "US97: первым элементом информации о пассажире должен быть документ" xatosi bilan rad etiladi.
    """
    letters = "".join(ch if ch.isalpha() or ch.isspace() else "" for ch in value or "")
    return " ".join(letters.split()).upper()


def decode_jwt(token: str) -> dict:
    """Imzoni tekshirmasdan JWT ichidagi ma'lumotni o'qiydi (id, sub, exp)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError):
        return {}


def token_expired(token: str, margin: int = 0) -> bool:
    """margin — muddati tugashiga shuncha sekund qolgan bo'lsa ham "tugagan" deb hisoblanadi."""
    exp = decode_jwt(token).get("exp")
    return exp is not None and datetime.now().timestamp() + margin > exp


class UzRailClient:
    def __init__(self, lang: str = "uz"):
        self._http = httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=40,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
                "Accept": "application/json",
                "Accept-Language": lang,
                "device-type": "BROWSER",
                "Origin": BASE_URL,
                "Referer": f"{BASE_URL}/{lang}/home",
            },
        )

    async def close(self):
        await self._http.aclose()

    async def _refresh_csrf(self):
        await self._http.get("/api/v1/csrf-token")
        xsrf = self._http.cookies.get("XSRF-TOKEN")
        if xsrf:
            self._http.headers["X-XSRF-TOKEN"] = xsrf

    async def _post(self, url: str, body: dict, auth: str | None = None) -> dict:
        headers = {"Authorization": f"Bearer {auth}"} if auth else None
        for attempt in range(2):
            try:
                if "X-XSRF-TOKEN" not in self._http.headers:
                    await self._refresh_csrf()
                resp = await self._http.post(url, json=body, headers=headers)
                if resp.status_code == 403:
                    await self._refresh_csrf()
                    resp = await self._http.post(url, json=body, headers=headers)
                break
            except httpx.TransportError as e:
                # Server eski ulanishni uzib qo'yishi mumkin — bir marta qayta urinamiz
                if attempt == 1:
                    raise UzRailError(f"Sayt bilan aloqa xatosi: {e.__class__.__name__}",
                                      f"Ошибка связи с сайтом: {e.__class__.__name__}") from e
            except httpx.HTTPError as e:
                raise UzRailError(f"Sayt bilan aloqa xatosi: {e.__class__.__name__}",
                                  f"Ошибка связи с сайтом: {e.__class__.__name__}") from e
        if resp.status_code == 204:  # masalan, reysda bo'sh joy yo'q
            return {}
        if resp.status_code == 401:
            raise AuthError("Saytga kirish tokeni yaroqsiz yoki muddati o'tgan.",
                            "Токен входа на сайт недействителен или истёк.")
        if resp.status_code == 429:
            raise RateLimitError("Sayt vaqtincha bloklab qo'ydi (juda ko'p so'rov).",
                                 "Сайт временно заблокировал запросы (слишком много запросов).")
        try:
            data = resp.json()
        except ValueError:
            raise UzRailError(f"Saytdan noto'g'ri javob keldi (HTTP {resp.status_code}).",
                              f"Сайт вернул некорректный ответ (HTTP {resp.status_code}).")
        message = _error_message(data)
        if message and "blocked" in message.lower():
            raise RateLimitError(f"Sayt vaqtincha bloklab qo'ydi: {message}",
                                 f"Сайт временно заблокировал запросы: {message}")
        if resp.status_code >= 400:
            log.warning("Sayt xatosi %s -> HTTP %s: %s", url, resp.status_code, resp.text[:300])
            if not message and resp.status_code in (424, 500, 502, 503, 504):
                raise UzRailError(f"Sayt vaqtincha ishlamayapti (HTTP {resp.status_code}). Birozdan keyin qayta urinib ko'ring.",
                                  f"Сайт временно не работает (HTTP {resp.status_code}). Попробуйте чуть позже.")
            raise UzRailError(message or f"HTTP {resp.status_code}")
        return data

    async def search_stations(self, name: str) -> list[dict]:
        data = await self._post("/api/v1/handbook/stations/list", {"name": name})
        return ((data or {}).get("data") or {}).get("stations") or []

    async def list_trains(self, date: str, dep_code: str, arv_code: str) -> list[dict]:
        """date: YYYY-MM-DD"""
        body = {
            "directions": {"forward": {"date": date, "depStationCode": dep_code, "arvStationCode": arv_code}},
            "routeType": "INTERCITY",
        }
        data = await self._post("/api/v3/handbook/trains/list", body)
        directions = ((data or {}).get("data") or {}).get("directions") or {}
        return (directions.get("forward") or {}).get("trains") or []

    async def train_places(self, date: str, dep_code: str, arv_code: str, train: dict) -> dict | None:
        """Poyezd vagonlari va bo'sh (saytda ko'k rangda ko'rinadigan) joylar ro'yxati."""
        body = {
            "depDate": date,
            "depStationCode": dep_code,
            "arvStationCode": arv_code,
            "trainNumber": train["number"],
            "trainId": train.get("trainId"),
        }
        for attempt in range(2):
            try:
                data = await self._post("/api/v1/handbook/trains", body)
                return ((data or {}).get("data") or {}).get("train")
            except RateLimitError:
                raise
            except UzRailError as e:
                # Joyi tugagan ba'zi reyslarda sayt 204 o'rniga 400 "Unexpected status" qaytaradi — bu "joy yo'q" degani.
                # Ba'zan vaqtinchalik ham bo'ladi, shuning uchun bir marta qayta so'raymiz.
                if "unexpected status" not in str(e).lower():
                    raise
                if attempt == 0:
                    await asyncio.sleep(0.5)
        return None

    async def reserve(self, site_token: str, train: dict, group: dict, car: dict,
                      seats: list[int], passengers: list[Passenger], tier: str | None = None) -> str:
        """Tanlangan joylarni bron qiladi va buyurtma ID sini qaytaradi.

        tier — "upper"/"lower": saytdagi "Faqat tepasi/pastki" kabi faqat shu qatordan joy so'raladi.
        """
        requirements = {"seatsRange": f"{min(seats)}-{max(seats)}"}
        if tier == "upper":
            requirements["seatsTop"] = len(seats)
        elif tier == "lower":
            requirements["seatsBottom"] = len(seats)
        claims = decode_jwt(site_token)
        dep = datetime.strptime(train["departureDate"], "%d.%m.%Y %H:%M")
        arv = datetime.strptime(train["arrivalDate"], "%d.%m.%Y %H:%M")
        sub = train["subRoute"]
        tickets = [
            {
                "passengerInfo": {
                    "fullName": "=".join([clean_name(p.last_name), clean_name(p.first_name), clean_name(p.middle_name)]),
                    "documentId": p.passport,
                    "documentType": p.document_type,  # saytdagi kod: "ПУ" yoki "УЛ"
                    "gender": p.gender,
                    # saytning formasi bilan bir xil: 3 harfli kod. "UZ" bilan Express "US508: неверная
                    # информация после страны гражданства" xatosi bilan rad etadi
                    "citizenship": "UZB",
                    "birthday": p.birthday,
                    "regionId": "03",
                    "discountType": "REGULAR",
                    "referenceDocument": None,
                    "prefix": None,
                    "tariffCode": None,
                    "child": None,
                },
                "pricingMode": "FULL",
                "hasPatronymics": bool(clean_name(p.middle_name)),
            }
            for p in passengers
        ]
        body = {
            "channel": "WEB",
            "orderKind": "RETAIL",
            "paymentMode": "CARD",
            "groupType": "INDIVIDUALS",
            "orderType": "INDIVIDUAL",
            "providerType": "EXPRESS",
            "documentMode": "NAMED",
            "subItems": [
                {
                    "trainInfo": {
                        "trainNumber": train["number"],
                        "carNumber": car["number"],
                        "carType": group["type"],
                        "serviceClass": (group.get("services") or {}).get("type") or car["schema"]["service"],
                        "directionSequence": 1,
                        "brand": train.get("brand"),
                    },
                    "departure": {
                        "code": sub["depStationCode"],
                        "name": sub["depStationName"],
                        "date": dep.strftime("%Y-%m-%d"),
                        "time": dep.strftime("%H:%M:00"),
                    },
                    "arrival": {
                        "code": sub["arvStationCode"],
                        "name": sub["arvStationName"],
                        "date": arv.strftime("%Y-%m-%d"),
                        "time": arv.strftime("%H:%M:00"),
                    },
                    "hasInsurance": False,
                    "hasCateringOrder": False,
                    "hasGreenTicket": False,
                    "reserveSeatRequirements": requirements,
                    "tickets": tickets,
                }
            ],
            "webCustomer": {
                "id": str(claims.get("id", "")),
                "username": claims.get("sub", ""),
                "ip": "eticket.uzrailpass.uz",
            },
        }
        data = await self._post("/api/v3/universal-orders/reserve", body, auth=site_token)
        order_id = data.get("response") if isinstance(data, dict) else None
        if not order_id:
            raise UzRailError(_error_message(data) or "Buyurtma yaratilmadi.",
                              _error_message(data) or "Заказ не создан.")
        return str(order_id)

    async def order_snapshot(self, site_token: str, order_id: str) -> dict:
        """Buyurtma holati: {"lifecycle": "FAILED"|..., "items": [...]}."""
        body = {"orderId": order_id, "userId": str(decode_jwt(site_token).get("id", ""))}
        data = await self._post("/api/v3/universal-orders/get-snapshot", body, auth=site_token)
        return (data or {}).get("response") or {}

    async def ticket_pdf(self, site_token: str, order_id: str) -> bytes | None:
        """To'langan buyurtma chiptasi (PDF). Sayt base64 qaytaradi: matn yoki {"pdf": ...} (response ichida ham)."""
        try:
            resp = await self._http.get(f"/api/v1/document/{order_id}/pdf",
                                        headers={"Authorization": f"Bearer {site_token}"})
            if resp.status_code != 200:
                log.warning("Chipta PDF olinmadi %s: HTTP %s %s", order_id, resp.status_code, resp.text[:200])
                return None
            if resp.content.startswith(b"%PDF"):
                return resp.content
            data = resp.json()
            for _ in range(3):  # {"response": {"pdf": "..."}} kabi o'ramlarni ochamiz
                if isinstance(data, dict):
                    data = data.get("pdf") or data.get("response") or data.get("data")
            if isinstance(data, str) and data:
                pdf = base64.b64decode(re.sub(r"^data:[^;]*;base64,|\s", "", data))
                return pdf if pdf.startswith(b"%PDF") else None
        except (httpx.HTTPError, ValueError, binascii.Error) as e:
            log.warning("Chipta PDF olinmadi %s: %s", order_id, e)
        return None

    async def payment_deadline(self, site_token: str, order_id: str) -> datetime | None:
        """Bronni to'lash muddati (sayt bronni 12 daqiqa saqlaydi). Javob: response.response.expiryTime."""
        try:
            resp = await self._http.get(f"/api/v3/universal-orders/query/get/payment-end-time/{order_id}",
                                        headers={"Authorization": f"Bearer {site_token}"})
            inner = ((resp.json() or {}).get("response") or {}).get("response") or {}
            return datetime.fromisoformat(inner["expiryTime"]) if inner.get("expiryTime") else None
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
            return None

    async def cancel(self, site_token: str, order_id: str) -> None:
        """To'lanmagan buyurtmani (bronni) bekor qiladi. To'langan chipta saytda alohida qaytariladi."""
        claims = decode_jwt(site_token)
        body = {
            "orderId": order_id,
            "webCustomer": {"id": str(claims.get("id", "")), "username": claims.get("sub", ""),
                            "ip": "eticket.uzrailpass.uz"},
            "cashierOperator": None,
        }
        await self._post("/api/v3/universal-orders/cancel", body, auth=site_token)


def _error_message(data) -> str | None:
    if not isinstance(data, dict):
        return None
    if data.get("message"):
        return str(data["message"])
    err = data.get("error")
    if isinstance(err, list) and err and isinstance(err[0], dict):  # {"error": [{"code": -1, "message": "..."}]}
        return err[0].get("message")
    if isinstance(err, dict):
        errors = err.get("errors") or []
        if errors and isinstance(errors[0], dict):
            return errors[0].get("message")
        return err.get("message")
    errors = data.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        return errors[0].get("message")
    return None
