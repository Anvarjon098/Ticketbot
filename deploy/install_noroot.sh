#!/usr/bin/env bash
# Root (sudo) huquqisiz serverga o'rnatish (Debian 12 / Ubuntu). Hammasi bot papkasi ichiga tushadi:
#   .venv/        — Python kutubxonalari
#   .browsers/    — Playwright Chromium (headless shell)
#   .chromelibs/  — Chromium uchun tizim kutubxonalari (.deb paketlardan ochilgan, apt o'rnatmaydi)
# Ishga tushirish:  bash deploy/install_noroot.sh      (qayta ishga tushirsa bo'ladi)
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LIBS="$APP_DIR/.chromelibs"
cd "$APP_DIR"
export PLAYWRIGHT_BROWSERS_PATH="$APP_DIR/.browsers"

echo ">> 1/4 Python muhiti"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

echo ">> 2/4 Chromium kutubxonalari (root'siz)"
APT="$APP_DIR/.aptcache"
rm -rf "$APT"; mkdir -p "$APT/lists/partial" "$APT/archives/partial"
O=(-o "Dir::State::Lists=$APT/lists" -o "Dir::Cache=$APT" -o "Dir::Cache::archives=$APT/archives"
   -o "Dir::State::status=/var/lib/dpkg/status" -o Debug::NoLocking=1 -o "APT::Sandbox::User=$(id -un)")
apt-get "${O[@]}" -qq update 2>&1 | grep -v "Permission denied" || true
PKGS="libatomic1 libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libatspi2.0-0 libcups2 libdrm2 libdbus-1-3
      libxkbcommon0 libxcomposite1 libxdamage1 libxext6 libxfixes3 libxrandr2 libx11-6 libxcb1 libgbm1
      libpango-1.0-0 libcairo2 libasound2 libglib2.0-0 libexpat1 libfontconfig1 libfreetype6
      fontconfig-config fonts-dejavu-core"
NEED=()
for p in $(apt-cache "${O[@]}" depends --recurse --no-recommends --no-suggests --no-conflicts --no-breaks \
             --no-replaces --no-enhances $PKGS | grep '^[a-z0-9]' | sort -u); do
  dpkg -s "$p" >/dev/null 2>&1 || NEED+=("$p")
done
echo "   yuklanadi: ${#NEED[@]} ta paket"
if [ ${#NEED[@]} -gt 0 ]; then
  (cd "$APT/archives" && apt-get "${O[@]}" -qq download "${NEED[@]}")
  rm -rf "$LIBS"; mkdir -p "$LIBS"
  for f in "$APT"/archives/*.deb; do dpkg-deb -x "$f" "$LIBS"; done
fi
rm -rf "$APT"
mkdir -p "$LIBS"
# Shriftlar sozlamasi (tizimdagi va ochilgan shriftlar)
cat > "$LIBS/fonts.conf" <<EOF
<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "fonts.dtd">
<fontconfig>
  <dir>/usr/share/fonts</dir>
  <dir>$LIBS/usr/share/fonts</dir>
  <cachedir>$LIBS/fontcache</cachedir>
</fontconfig>
EOF

echo ">> 3/4 Chromium"
source "$APP_DIR/deploy/env_noroot.sh"
.venv/bin/python -m playwright install --only-shell chromium 2>/dev/null \
  || .venv/bin/python -m playwright install chromium

echo ">> 4/4 Tekshiruv"
MISSING=$(find "$PLAYWRIGHT_BROWSERS_PATH" -type f \( -name chrome-headless-shell -o -name headless_shell -o -name chrome \) \
          -exec ldd {} \; 2>/dev/null | grep "not found" | sort -u || true)
if [ -n "$MISSING" ]; then
  echo "!! Yetishmayotgan kutubxonalar:"; echo "$MISSING"; exit 1
fi
.venv/bin/python - <<'EOF'
import asyncio
from playwright.async_api import async_playwright
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch()
        pg = await b.new_page()
        await pg.set_content("<b>Salom, Привет</b>")
        await pg.screenshot()
        await b.close()
asyncio.run(main())
print("   Chromium ishlayapti")
EOF
[ -f .env ] || { cp .env.example .env; echo "!! .env yaratildi — uni to'ldiring"; }
chmod 600 .env
echo
echo "Tayyor. Botni boshqarish:  bash deploy/botctl.sh start | stop | restart | status | log"
