"""Tekshiruv: reys vagonlari va bo'sh joylari saytdan qanday kelyapti.

    .venv/bin/python deploy/diag_places.py TOSHKENT BUXORO 2026-11-26 709
(qayerdan, qayerga, sana YYYY-MM-DD, poyezd raqami yoki uning boshi)
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import car_schemas  # noqa: E402
import uzrail  # noqa: E402


async def station(c, name):
    found = await c.search_stations(name)
    if not found:
        sys.exit(f"Stansiya topilmadi: {name}")
    s = next((x for x in found if x.get("name", "").upper() == name.upper()), found[0])
    print(f"Stansiya {name}: {s.get('name')} kod={s.get('code')}")
    return s["code"]


async def main(frm, to, date, number):
    c = uzrail.UzRailClient()
    try:
        a, b = await station(c, frm), await station(c, to)
        trains = await c.list_trains(date, a, b)
        print(f"Reyslar: {len(trains)} ta")
        train = next((t for t in trains if str(t.get("number", "")).startswith(number)), None)
        if not train:
            sys.exit(f"{number} reysi topilmadi: {[t.get('number') for t in trains]}")
        cars = train.get("cars") or []
        print(f"Reys {train['number']} ro'yxatda: bo'sh joy = {sum(x.get('freeSeats', 0) for x in cars)}, trainId={train.get('trainId')}")
        body = {"depDate": date, "depStationCode": a, "arvStationCode": b,
                "trainNumber": train["number"], "trainId": train.get("trainId")}
        await c._refresh_csrf()
        for i in range(2):
            r = await c._http.post("/api/v1/handbook/trains", json=body)
            print(f"\n[{i + 1}] /api/v1/handbook/trains -> HTTP {r.status_code}, {len(r.content)} bayt")
            try:
                data = r.json()
            except ValueError:
                print("JSON emas:", r.text[:300])
                continue
            t = ((data or {}).get("data") or {}).get("train")
            if not t:
                print("data.train yo'q. Javob:", json.dumps(data, ensure_ascii=False)[:600])
                continue
            groups = t.get("carGroup") or []
            print(f"carGroup: {len(groups)} ta; kalitlar: {sorted(t.keys())}")
            for g in groups[:6]:
                gc = g.get("cars") or []
                print(f"  {car_schemas.group_code(g)}: vagonlar={len(gc)}, joylar={[len(x.get('places') or []) for x in gc]}"
                      f", vagon kalitlari={sorted(gc[0].keys()) if gc else []}")
            print("Ilovaga boradigan vagonlar:", len(car_schemas.cars(t)))
            break
    finally:
        await c.close()


if __name__ == "__main__":
    if len(sys.argv) != 5:
        sys.exit(__doc__)
    asyncio.run(main(*sys.argv[1:]))
