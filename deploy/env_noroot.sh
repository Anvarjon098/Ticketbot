# install_noroot.sh va botctl.sh uchun umumiy muhit (source qilinadi)
_APP="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
_L="$_APP/.chromelibs"
export PLAYWRIGHT_BROWSERS_PATH="$_APP/.browsers"
export LD_LIBRARY_PATH="$_L/usr/lib/x86_64-linux-gnu:$_L/lib/x86_64-linux-gnu:$_L/usr/lib:${LD_LIBRARY_PATH:-}"
export FONTCONFIG_FILE="$_L/fonts.conf"
export PYTHONIOENCODING=utf-8
export PYTHONUNBUFFERED=1
