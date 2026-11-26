#!/usr/bin/env bash
# Ubuntu 22.04/24.04 (yoki Debian 12) serverga botni o'rnatadi. Root sifatida ishga tushiring:
#   sudo bash deploy/install.sh
# Qayta ishga tushirsa bo'ladi (kod yangilangandan keyin ham) — mavjud ma'lumot fayllariga tegmaydi.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/ticketbot}"
APP_USER="${APP_USER:-ticketbot}"
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo ">> Tizim paketlari"
apt-get update -y
apt-get install -y python3 python3-venv python3-pip git rsync fonts-dejavu-core

echo ">> Foydalanuvchi: $APP_USER"
id "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home --home-dir "/home/$APP_USER" --shell /usr/sbin/nologin "$APP_USER"

echo ">> Kod: $SRC_DIR -> $APP_DIR"
mkdir -p "$APP_DIR"
if [ "$SRC_DIR" != "$APP_DIR" ]; then
  # .env va *.json ma'lumot fayllari ustidan yozilmaydi
  rsync -a --exclude .git --exclude .venv --exclude .env \
    --exclude account.json --exclude site_accounts.json --exclude users.json --exclude monitors.json --exclude access.json \
    --exclude site_token.txt --exclude '*.log' "$SRC_DIR/" "$APP_DIR/"
fi

echo ">> Python muhiti"
[ -d "$APP_DIR/.venv" ] || python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

echo ">> Chromium (Playwright) va uning tizim kutubxonalari"
PLAYWRIGHT_BROWSERS_PATH="$APP_DIR/.browsers" "$APP_DIR/.venv/bin/python" -m playwright install --with-deps chromium

[ -f "$APP_DIR/.env" ] || { cp "$APP_DIR/.env.example" "$APP_DIR/.env"; echo "!! $APP_DIR/.env yaratildi — BOT_TOKEN, OWNER_ID, WEBAPP_URL ni to'ldiring"; }
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
# root keyinchalik "git pull" qila olishi uchun
git config --system --get-all safe.directory 2>/dev/null | grep -qx "$APP_DIR" || git config --system --add safe.directory "$APP_DIR"
chmod 600 "$APP_DIR/.env"

echo ">> systemd xizmati"
sed -e "s#/opt/ticketbot#$APP_DIR#g" -e "s#User=ticketbot#User=$APP_USER#" \
  "$APP_DIR/deploy/ticketbot.service" > /etc/systemd/system/ticketbot.service
systemctl daemon-reload
systemctl enable ticketbot
systemctl restart ticketbot

echo
echo "Tayyor. Holat:   systemctl status ticketbot"
echo "Loglar:          journalctl -u ticketbot -f"
