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

## Serverga ko'chirish (Linux VPS)
Kompyuter o'chiq bo'lsa ham bot 24/7 ishlashi uchun. Ubuntu 22.04/24.04, kamida 1 GB RAM (Chromium uchun; 2 GB tavsiya).

> ⚠️ eticket.uzrailpass.uz ba'zan chet el IP larini bloklaydi. Server olishdan oldin (yoki darhol keyin)
> tekshiring: `curl -I https://eticket.uzrailpass.uz` — javob kelmasa, O'zbekistondagi VPS oling.

**1. Kompyuterda botni to'xtating** (bir token bilan ikkita bot ishlamasin):
`powershell -ExecutionPolicy Bypass -File stop_bot.ps1` va Startup'dagi `UzRailBot.vbs` ni o'chiring.

**2. Kodni serverga yuklang va o'rnating:**
```bash
git clone https://github.com/anvarjon098/ticketbot.git /opt/ticketbot
sudo bash /opt/ticketbot/deploy/install.sh
```

**3. Maxfiy/ma'lumot fayllarini kompyuterdan ko'chiring** (ular GitHub'da yo'q). Windows PowerShell'da:
```powershell
scp .env account.json users.json monitors.json access.json site_token.txt root@SERVER_IP:/opt/ticketbot/
```
Keyin serverda: `chown ticketbot:ticketbot /opt/ticketbot/*.json /opt/ticketbot/.env /opt/ticketbot/site_token.txt`

**4. Ilova uchun HTTPS manzil** (Tailscale manzili endi ishlamaydi) — ikkitadan biri:
- **Domen bo'lsa (tavsiya):** domen A yozuvini server IP siga qarating, Caddy o'rnating
  (`apt install caddy`), `deploy/Caddyfile` dagi domenni almashtirib `/etc/caddy/Caddyfile` ga ko'chiring,
  `systemctl reload caddy`. `.env` da `WEBAPP_URL=https://vps.doliyev.uz`.
- **Domen bo'lmasa:** `.env` da `WEBAPP_URL=` bo'sh qoldiring va cloudflared o'rnating
  (`curl -L -o /opt/ticketbot/bin/cloudflared https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 && chmod +x /opt/ticketbot/bin/cloudflared`).
  Manzil har qayta ishga tushganda o'zgaradi, bot uni o'zi yangilaydi.

**5. Ishga tushirish:** `systemctl restart ticketbot`. Botga `/start` yozing.

| Amal | Buyruq |
|---|---|
| Holat | `systemctl status ticketbot` |
| Loglar | `journalctl -u ticketbot -f` |
| Qayta ishga tushirish / to'xtatish | `systemctl restart ticketbot` / `systemctl stop ticketbot` |
| Kodni yangilash | `cd /opt/ticketbot && git pull && sudo bash deploy/install.sh` |

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
band qilinadi (12 daqiqa), lekin to'lovni bot egasi o'z akkauntidan qiladi:
- foydalanuvchiga bot egasining **karta raqami** va chipta summasi yuboriladi (botdagi bron xabarida va
  ilovadagi Chiptalar bo'limida). Karta ilovada kiritiladi: **Profil → To'lov kartasi** (faqat egasiga ko'rinadi);
- pulni o'tkazgach foydalanuvchi **✅ To'lov qildim** ni bosadi (botda yoki ilovada) — egasiga **💸** xabari
  **💳 Saytda to'lash** tugmasi bilan keladi;
- egasiga **🔔 ... bron qildi** xabari keladi: taymer, **💳 Saytda to'lash**, **💳 Karta raqamini
  yuborish** (kartani qayta yuboradi), **❌ Rad etish**;
- pul kelgach egasi saytda to'laydi. Bot har 30 soniyada saytdan tekshiradi (buyurtma holati yoki chipta
  holati PAID/CONFIRMED) va to'lov o'tgach egasiga **💰 To'lov o'tdi** xabarini **✅ Tasdiqlash** tugmasi
  bilan yuboradi — bu tugma to'lovdan oldin chiqmaydi;
- **✅ Tasdiqlash** bosilganda bot chiptaning PDF faylini saytdan olib foydalanuvchiga yuboradi. Sayt PDF ni
  hali bermasa (bir necha urinishdan keyin), egasiga aytiladi va tugmani keyinroq qayta bosish mumkin;
- to'lov havolasi va bekor qilish tugmasi foydalanuvchiga berilmaydi;
- rad etilsa — bron saytda bekor qilinadi va foydalanuvchiga xabar boradi;
- 12 daqiqada to'lanmasa — bron bekor bo'ladi, ikkala tomonga ham xabar boradi.

Egasining o'z bronlari oldingidek (to'lov havolasi va bekor qilish tugmasi bilan).

## "💳 Saytda to'lash" — qayta login qilmasdan
Sayt login tokenini faqat `sessionStorage` da saqlaydi, shuning uchun oddiy havola har safar login so'raydi.
Bot egasining bron xabarlarida (va ilovadagi "Saytda to'lash" tugmasida) havola sayt bot egasining
akkaunti bilan ochiladi: sayt ilova manzili orqali (proksi, `site_proxy.py`) ochiladi va bot tokeni avtomatik
qo'yiladi. To'lov saytning o'z sahifasida (karta + SMS kod) qilinadi. Havola imzolangan, bron muddati
(+15 daqiqa) davomida amal qiladi va faqat bot egasiga yuboriladi — uni boshqalarga bermang.

## Bronni bekor qilish
To'lanmagan bronni bekor qilish mumkin (joylar saytda bo'shatiladi):
- botda — bron haqidagi xabar ostidagi **❌ Bronni bekor qilish** tugmasi yoki **🎟 Chiptalarim** menyusi;
- ilovada — **Chiptalar** bo'limidagi **❌ Bronni bekor qilish**.

Bekor qilishdan oldin tasdiq so'raladi. To'langan chiptani saytning o'zida qaytarish kerak.

## Menyu tugmalari
- **🎫 Bilet olish** — yangi bron/kuzatuv
- **🔔 Kuzatuvlarim** — kuzatuvlar ro'yxati va o'chirish
- **ℹ️ Holat** — sayt akkaunti holati
- **🛠 Admin panel** (faqat bot egasi) — menyu tugmasi xabarsiz, darhol alohida ilovani ochadi
  (`/admin`, `webapp/admin.html`, API — `admin_api.py`). Menyu tugmasidan ochilgan ilovaga Telegram
  initData bermaydi, shuning uchun havolada bot tokeni bilan imzolangan kalit bor (`?k=`, 30 kun amal
  qiladi, faqat `/api/admin/...` uchun); **/start** menyuni va kalitni yangilaydi:
  - **Foydalanuvchilar** — qidiruv (ism, @username, ID, telefon, yo'lovchi ismi, pasport, buyurtma raqami),
    holat bo'yicha filtr; kartada ism/telefon, yo'lovchilarni (qo'shish, tahrirlash, o'chirish) va ruxsatni
    o'zgartirish, bronlar va kuzatuvlar ro'yxati;
  - **Statistika** — davr (bugun / 7 / 30 kun / hammasi): bron qilingan, sotib olingan (summa), to'lov
    kutilmoqda, bekor / muddati o'tgan, kuzatuvlar; kunlik grafik (jadval ko'rinishi bilan), ko'p bron
    qilingan yo'nalishlar, foydalanuvchilar kesimi.

  Admin bergan ism `users.json` da `display_name` bo'lib saqlanadi. Begona odam botga yozsa, egasiga
  **✅ Ruxsat berish / ❌ Rad etish** tugmalari bilan xabar keladi (`access.json`).
- **🔑 Saytga kirish** (faqat bot egasi) — sayt raqami/parolini o'zgartirish
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
