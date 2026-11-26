#!/usr/bin/env bash
# Root'siz serverda botni boshqarish (systemd o'rniga):
#   bash deploy/botctl.sh start|stop|restart|status|log
# start — fonda ishga tushiradi; bot yiqilsa 10 soniyada qayta ishga tushadi. Terminalni yopsa ham ishlayveradi.
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$APP_DIR"
PIDFILE="$APP_DIR/watchdog.pid"

# PID fayldagi jarayon haqiqatan bizning kuzatuvchimizmi (konteyner qayta yonsa, PID boshqa dasturga tegishli bo'lishi mumkin)
running() { [ -f "$PIDFILE" ] && ps -p "$(cat "$PIDFILE")" -o args= 2>/dev/null | grep -q "env_noroot.sh"; }

case "${1:-}" in
  start)
    if running; then echo "Bot allaqachon ishlayapti (PID $(cat "$PIDFILE"))"; exit 0; fi
    setsid nohup bash -c '
      source deploy/env_noroot.sh
      while true; do
        # Eski loglar logs papkasida saqlanadi (oxirgi 10 tasi)
        if [ -s bot.log ]; then
          mkdir -p logs && mv -f bot.log "logs/bot-$(date +%Y%m%d-%H%M%S).log"
          ls -1t logs/bot-*.log 2>/dev/null | tail -n +11 | xargs -r rm -f
        fi
        .venv/bin/python bot.py >>bot.log 2>&1
        echo "$(date "+%F %T") bot to'\''xtadi, 10 soniyadan keyin qayta ishga tushadi" >>bot.log
        sleep 10
      done' >/dev/null 2>&1 < /dev/null &
    echo $! > "$PIDFILE"
    sleep 3; echo "Ishga tushdi. Log: bash deploy/botctl.sh log" ;;
  stop)
    if running; then kill -- -"$(cat "$PIDFILE")" 2>/dev/null || kill "$(cat "$PIDFILE")"; fi
    pkill -u "$(id -u)" -f "$APP_DIR/.venv/bin/python bot.py" 2>/dev/null
    pkill -u "$(id -u)" -f ".venv/bin/python bot.py" 2>/dev/null
    rm -f "$PIDFILE"; echo "To'xtatildi" ;;
  restart) "$0" stop; sleep 2; "$0" start ;;
  status)  if running; then echo "Ishlayapti (PID $(cat "$PIDFILE"))"; else echo "Ishlamayapti"; fi ;;
  log)     tail -n 50 -f bot.log ;;
  *) echo "Foydalanish: bash deploy/botctl.sh start|stop|restart|status|log"; exit 1 ;;
esac
