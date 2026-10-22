"""Poyezdlar kategoriyalari ma'lumotnomasini (train_catalog.json) saytdan qayta yig'ish.

Barcha asosiy ichki, viloyatlararo va xalqaro yo'nalishlar bo'yicha bir necha sanadagi reyslarni ko'rib,
har bir poyezd raqami uchun narx kategoriyalarini (classServiceType) yozib oladi. Avvalgi ma'lumotnomadagi
kategoriyalar o'chirilmaydi — yangilari qo'shiladi. Taxminan 15–20 daqiqa ishlaydi.

    .venv\\Scripts\\python update_catalog.py
"""
import asyncio
import datetime
import json
import sys

from categories import CATALOG_FILE, normalize, train_key
from uzrail import UzRailClient, UzRailError

S = dict(TOSHKENT="2900000", SAMARQAND="2900700", BUXORO="2900800", QARSHI="2900750", TERMIZ="2900255",
         DENOV="2900780", SARIOSIYO="2900868", ANDIJON="2900680", NAMANGAN="2900940", MARGILON="2900920",
         QOQON="2900880", JIZZAX="2900720", GULISTON="2900850", NAVOIY="2900930", URGENCH="2900790", XIVA="2900172",
         NUKUS="2900970", QUNGIROD="2900885", SHAHRISABZ="2900796", BOYSUN="2900872", ANGREN="2900679", POP="2900693",
         UCHQUDUQ="2900105", TURTKUL="2900103", KATTAQURGON="2900860", QUMQURGON="2900864", SHURCHI="2900866",
         XUJAND="2900691",
         MOSKVA="2000003", SARATOV="2020000", UFA="2024600", KAZAN="2060500", SAMARA="2024000", CHELYABINSK="2040000",
         NOVOSIBIRSK="2044001", ALMATY="2700000", SHYMKENT="2700770", BISHKEK="5900001", DUSHANBE="6600001",
         VOLGOGRAD="2020500", EKATERINBURG="2030000", NNOVGOROD="2060001", ORENBURG="2040210")
FROM_TASHKENT = [s for s in S if s != "TOSHKENT"]
CROSS = [("ANDIJON", "SAMARQAND"), ("ANDIJON", "BUXORO"), ("ANDIJON", "TERMIZ"), ("ANDIJON", "QARSHI"),
         ("SAMARQAND", "TERMIZ"), ("SAMARQAND", "BUXORO"), ("SAMARQAND", "URGENCH"), ("SAMARQAND", "NUKUS"),
         ("SAMARQAND", "QARSHI"), ("BUXORO", "URGENCH"), ("BUXORO", "NUKUS"), ("BUXORO", "TERMIZ"),
         ("QARSHI", "TERMIZ"), ("URGENCH", "NUKUS"), ("NAMANGAN", "SAMARQAND"), ("NAMANGAN", "BUXORO"),
         ("MARGILON", "SAMARQAND"), ("QOQON", "SAMARQAND"), ("NAVOIY", "URGENCH"), ("TERMIZ", "ANDIJON"),
         ("NUKUS", "TOSHKENT"), ("URGENCH", "TOSHKENT"), ("TERMIZ", "TOSHKENT"), ("ANDIJON", "TOSHKENT"),
         ("BUXORO", "TOSHKENT"), ("SAMARQAND", "TOSHKENT")]
ROUTES = [("TOSHKENT", x) for x in FROM_TASHKENT] + CROSS
DAYS = [2, 9, 20, 35]
BRAND_FIX = {"ЖАЛОЛИДДИН МАНГУБЕРДИ": "Jaloliddin Manguberdi"}


async def main():
    catalog = json.loads(CATALOG_FILE.read_text(encoding="utf-8")) if CATALOG_FILE.exists() else {"trains": {}}
    trains = catalog["trains"]
    client = UzRailClient("uz")
    for n, (a, b) in enumerate(ROUTES, 1):
        for d in DAYS:
            day = (datetime.date.today() + datetime.timedelta(days=d)).isoformat()
            found = []
            for attempt in range(3):
                try:
                    found = await client.list_trains(day, S[a], S[b])
                    break
                except UzRailError as e:
                    print(f"  qayta urinish ({e})", flush=True)
                    await asyncio.sleep(5 * (attempt + 1))
            for t in found:
                info = trains.setdefault(train_key(t["number"]), {"brand": "", "route": "", "classes": {}})
                info["brand"] = info["brand"] or BRAND_FIX.get(t.get("brand") or "", t.get("brand") or "")
                origin = t.get("originRoute") or {}
                route = f'{origin.get("depStationName")} — {origin.get("arvStationName")}'
                if route not in info["route"]:
                    info["route"] = "; ".join(filter(None, [info["route"], route]))
                for car in t.get("cars") or []:
                    for tariff in car.get("tariffs") or []:
                        info["classes"].setdefault(normalize(tariff.get("classServiceType")), car.get("type"))
            await asyncio.sleep(0.6)
        print(f"[{n}/{len(ROUTES)}] {a} -> {b}: {len(trains)} ta poyezd", flush=True)
    await client.close()
    catalog.update(source="eticket.uzrailpass.uz", collected=datetime.date.today().isoformat(),
                   trains=dict(sorted(trains.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 99999)))
    CATALOG_FILE.write_text(json.dumps(catalog, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Saqlandi: {CATALOG_FILE} ({len(trains)} ta poyezd). Botni qayta ishga tushiring.")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
