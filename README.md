# UzRailPass ketma-ket joy boti

Telegram bot eticket.uzrailpass.uz saytidan barcha yo'lovchilar uchun **bitta vagonda ketma-ket joylarni**
topib bron qiladi. Joyi tugagan reysni kuzatib, joy chiqishi bilan **avtomatik bron qiladi** va bot egasiga xabar beradi.

## Ishga tushirish

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m playwright install chromium
copy .env.example .env     # BOT_TOKEN va OWNER_ID ni to'ldiring
.venv\Scripts\python bot.py
```

Botda `/start` → **📱 Telefon raqamni yuborish** tugmasi. Bot egasi uchun shu raqam bilan saytga kirish
taklif qilinadi (sayt parolini yuborasiz). Bot saytga ko'rinmas (headless) brauzerda kiradi va tokenni
muddati tugashidan oldin o'zi yangilab turadi. Raqam va parol `account.json` faylida ochiq holda
saqlanadi — bu faylni hech kimga bermang.

## Doimiy manzil va avtomatik ishga tushish
Ilova doimiy manzilda ishlaydi: **https://desktop-qjn6npi.taila6c82c.ts.net** (Tailscale Funnel, bepul).
Manzil `.env` dagi `WEBAPP_URL` da; u bo'lsa vaqtinchalik cloudflared tunneli ishlatilmaydi.

- Kompyuter yoqilganda bot o'zi ishga tushadi:
  `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\UzRailBot.vbs` → `start_bot.ps1`.
- `start_bot.ps1` botni ko'rinmas holda ishga tushiradi va yiqilsa 10 soniyada qayta ishga tushiradi.
- To'xtatish: `powershell -ExecutionPolicy Bypass -File stop_bot.ps1`
- Qo'lda ishga tushirish: `powershell -ExecutionPolicy Bypass -File start_bot.ps1` (yoki UzRailBot.vbs ni ikki marta bosing)
- Avtomatik ishga tushishni o'chirish: Startup papkasidagi `UzRailBot.vbs` ni o'chiring.
- Funnel holati: `"C:\Program Files\Tailscale\tailscale.exe" funnel status`;
  o'chirish: `tailscale funnel --https=443 off`.

Ilova faqat kompyuter yoqiq va internetga ulangan paytda ishlaydi.

## Ilova (Telegram Mini App)
Botdagi **Ilova** (chap pastdagi menyu tugmasi) yoki **📱 Ilovani ochish** tugmasi orqali ochiladi.
Bo'limlar: **Profil** (o'z ma'lumotlari, sayt akkaunti), **Chiptalar** (bronlar, to'lov havolasi, kuzatuvlar),
**Hamrohlar** (qo'shish/tahrirlash/o'chirish), **Bilet olish** (yo'nalish → sana → reys → yo'lovchilar →
kategoriya → vagon sxemasidan joy tanlash → bron yoki kuzatuv).

## Vagon sxemalari
`webapp/schemas/` — saytdan olingan 91 ta vagon sxemasi (SVG; Afrosiyob, Sharq, Nasaf, Rotem, CRRC,
Talgo, kupe, plaskart, SV va boshqalar). Sayt har bir vagon uchun sxema nomini beradi
(`car-std-coupe-38.svg` kabi), `index.json` uni faylga bog'laydi. Har bir joy `<g id="raqam">`, uning
bo'sh/band ranglari `data-f`/`data-b` atributlarida. Sxemasi topilmagan vagon uchun raqamlar to'ri
ko'rsatiladi. Botdagi rasm `car_render.py` da (Playwright) chiziladi.

Ilova bot ichidagi veb-serverda (`webapp.py`, `webapp/index.html`) ishlaydi va `bin/cloudflared.exe`
orqali HTTPS'ga chiqariladi. Tunnel manzili har ishga tushganda o'zgaradi — bot uni o'zi aniqlab, menyu
tugmasini yangilaydi. Doimiy server/domen bo'lsa `.env` da `WEBAPP_URL` ni bering. Har bir so'rov
Telegram imzosi bilan tekshiriladi.

## Joy kategoriyalari
Bron va kuzatuvda buyurtmachi bir yoki bir nechta kategoriyani belgilaydi (belgilanmasa — istalgani).
Masalan, faqat **Ekonom** belgilansa, Biznes ochilsa ham olinmaydi — bot Ekonom chiqishini kutadi.

| Poyezd | Raqamlar | Kategoriyalar (sayt kodi) |
|---|---|---|
| Afrosiyob | 763–778 | VIP (1В), Biznes (1С), Ekonom (2Е) |
| Jaloliddin Manguberdi | 751–753 | VIP (1В), Biznes (1С), Ekonom (2Е) |
| O'zbekiston | 733, 734 | VIP (1В), Biznes (1С), Ekonom (2Б) |
| Sharq | 709–712 | VIP (1В), Biznes (1С), Ekonom (2В) |
| Nasaf | 703, 704, 715, 716 | VIP (1В), Biznes (1С), Ekonom (2В) |
| NU | 729–732 | Ekonom (2В) |
| Tezyurar | 54 | SV (1Л), Lyuks (1А), Kupe (2К), Plaskart (3П) |
| Yo'lovchi (ichki) | 56–130 | Kupe (2К), Plaskart (3П); ba'zilarida SV (1Л), Umumiy (3О) |
| Toshkent — Olmaota | 1 | SV (1Б), Kupe (2Б, 2Д), O'rindiqli (2С) |
| Toshkent — Moskva | 305 | Kupe (2Л, 2У), Plaskart (3Л, 3У) |
| Toshkent — Volgograd | 123, 124 | Kupe (2У), Plaskart (3Л) |
| Xo'jand — Volgograd | 359 | Kupe (2К), Plaskart (3П) |

Har bir poyezd raqami uchun to'liq ro'yxat `train_catalog.json` da (52 ta poyezd). Joyi bor kategoriyalar
har safar saytdan olinadi; ma'lumotnoma joyi tugagan reysda qaysi kategoriyalarni kuzatish mumkinligini
ko'rsatish uchun ishlatiladi. Yangi poyezdlar qo'shilsa: `.venv\Scripts\python update_catalog.py`
(~20 daqiqa), keyin botni qayta ishga tushiring.

## Boshqa foydalanuvchilarning bronlari (egasi tasdiqlaydi va to'laydi)
Ruxsat berilgan foydalanuvchi bilet olganda (botda, ilovada yoki kuzatuv orqali) joy saytda darhol
band qilinadi (12 daqiqa), lekin to'lovni bot egasi qiladi:
- egasiga **🔔 TASDIQLASH KERAK** xabari keladi: to'lov havolasi, taymer, **✅ Tasdiqlash / ❌ Rad etish**;
- foydalanuvchi "bot egasi tasdiqlab to'lashini kuting" holatini ko'radi — to'lov havolasi va bekor
  qilish tugmasi unga berilmaydi;
- tasdiqlansa — foydalanuvchiga xabar boradi, egasi saytda to'laydi; bot har 30 soniyada saytdan
  tekshiradi va to'langanda foydalanuvchiga **✅ TO'LANDI** xabarini yuboradi;
- rad etilsa — bron saytda bekor qilinadi va foydalanuvchiga xabar boradi;
- 12 daqiqada to'lanmasa — bron bekor bo'ladi, ikkala tomonga ham xabar boradi.

Egasining o'z bronlari oldingidek (to'lov havolasi va bekor qilish tugmasi bilan).

## Bronni bekor qilish
To'lanmagan bronni bekor qilish mumkin (joylar saytda bo'shatiladi):
- botda — bron haqidagi xabar ostidagi **❌ Bronni bekor qilish** tugmasi yoki **🎟 Chiptalarim** menyusi;
- ilovada — **Chiptalar** bo'limidagi **❌ Bronni bekor qilish**.

Bekor qilishdan oldin tasdiq so'raladi. To'langan chiptani saytning o'zida qaytarish kerak.

## Menyu tugmalari
- **🎫 Bilet olish** — yangi bron/kuzatuv
- **🔔 Kuzatuvlarim** — kuzatuvlar ro'yxati va o'chirish
- **ℹ️ Holat** — sayt akkaunti holati
- **🔑 Saytga kirish** — sayt raqami/parolini o'zgartirish (faqat bot egasi)
- **👥 Foydalanuvchilar** — ruxsat so'rovlari, ruxsat berish/olish (faqat bot egasi). Begona odam
  botga yozsa, egasiga **✅ Ruxsat berish / ❌ Rad etish** tugmalari bilan xabar keladi (`access.json`)
- **❌ Bekor qilish**

## Bot bosqichlari
1. Telefon raqam — tugma orqali (faqat birinchi marta)
2. Familiya va ism → pasport → tug'ilgan sana → jins (tugma). Keyingi safar saqlangan ma'lumot taklif qilinadi
3. **➕ Hamroh qo'shish** (xuddi shu savollar) yoki **▶️ Davom etish**
4. Qayerdan / qayerga — mashhur stansiya tugmasi yoki nomini yozing
5. Sana — 10 kunlik tugmalar yoki `28.10.2026`
6. Reys — tugma yoki jo'nash vaqti (`21:12`). 🔴 — joyi yo'q reys
7. Joy bo'lsa: kategoriya → **vagon sxemasi rasmi** (saytdagidek: bo'sh joy ko'k, band — kulrang,
   tanlangan — yashil) → ◀️ ▶️ bilan vagon tanlanadi, pastdagi raqamlardan har bir yo'lovchiga joy
   belgilanadi → **🎫 Bron qilish**. Bot joyni o'zi tanlamaydi.
   Joy bo'lmasa: **Kuzatish** ("faqat ketma-ket" yoki "bir vagonda bo'lsa yetarli") — joy chiqqanda
   kuzatuv ketma-ket joylarni avtomatik oladi (kutib o'tirmaslik uchun)
8. To'lovni saytda qiling (bron vaqtinchalik)

## Kuzatuv
- Barcha kuzatuvlar uchun umumiy tezlik — `POLL_INTERVAL` (standart 1 sekundda 1 so'rov);
  3 ta kuzatuv bo'lsa, har biri 3 sekundda bir tekshiriladi.
- Sayt bloklasa, bot 30 sekunddan 5 daqiqagacha kutib, keyin davom etadi va egasini ogohlantiradi.
- Kuzatuvlar `monitors.json` da saqlanadi — bot qayta ishga tushsa ham davom etadi.
- Poyezd jo'nab ketgach, kuzatuv avtomatik to'xtaydi.

## Cheklovlar
- Faqat 16 yoshdan katta yo'lovchilar.
- Faqat "Express" tizimidagi reyslar ("Sorbon" reyslari qo'llab-quvvatlanmaydi).
- To'lov bot orqali emas, saytda amalga oshiriladi.
