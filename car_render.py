"""Bot uchun vagon sxemasi rasmi (PNG): saytdagi sxema, bo'sh joylar ko'k, band — kulrang, tanlangan — yashil.

Ko'rinmas Chromium (Playwright) da chiziladi. Uzun vagon telefonda o'qilishi uchun ikki qatorga bo'lib
ko'rsatiladi. Brauzer xotira egallamasligi uchun bir necha daqiqa ishlatilmasa yopiladi.
"""
import asyncio
import html
import json
import logging

from playwright.async_api import async_playwright

from car_schemas import SCHEMA_DIR

log = logging.getLogger(__name__)
IDLE_CLOSE = 300  # soniya

PAGE = """<!doctype html><html><head><meta charset="utf-8"><style>
body { margin: 0; background: #fff; font: 15px Arial, sans-serif; color: #15171c; }
#root { display: inline-block; padding: 14px 16px; background: #fff; }
.title { font-size: 20px; font-weight: 700; margin-bottom: 2px; }
.sub { color: #6b7280; margin-bottom: 10px; }
.half { width: %(width)dpx; overflow: hidden; margin-bottom: 8px; border-radius: 6px; }
.half svg { display: block; width: 100%%; height: auto; }
.legend { display: flex; gap: 18px; color: #6b7280; font-size: 14px; margin-top: 4px; }
.legend i { display: inline-block; width: 14px; height: 14px; border-radius: 3px; vertical-align: -2px; margin-right: 6px; }
.grid { display: grid; grid-template-columns: repeat(10, 52px); gap: 6px; margin: 6px 0 10px; }
.grid div { height: 40px; border-radius: 8px; display: grid; place-items: center; font-weight: 700; color: #fff; }
</style></head><body><div id="root">
<div class="title">%(title)s</div><div class="sub">%(sub)s</div>
<div id="body"></div>
<div class="legend"><span><i style="background:#187cee"></i>bo'sh</span><span><i style="background:#e4e4e4"></i>band</span>
<span><i style="background:#5cb85c"></i>tanlangan</span></div>
</div><script>
(() => {  // sahifa qayta ishlatiladi — o'zgaruvchilar global bo'lmasin
const FREE = new Set(%(free)s), PICKED = new Set(%(picked)s), SVG = %(svg)s, TOTAL = %(total)d;
const WHITE = /^(#fff(fff)?|white|rgb\\(255, 255, 255\\))$/i;
function applyStyle(el, spec) {
  for (const part of (spec || "").split(";")) { const k = part.indexOf(":"); if (k > 0) el.style.setProperty(part.slice(0, k), part.slice(k + 1)); }
}
function paint(svg) {
  svg.querySelectorAll("g[id]").forEach(g => {
    if (!/^\\d+$/.test(g.id) || g.parentElement.closest("g[data-seat]")) return;
    g.dataset.seat = 1;
    const s = +g.id, st = PICKED.has(s) ? "picked" : FREE.has(s) ? "free" : "busy";
    g.querySelectorAll("[data-f]").forEach(el => {
      applyStyle(el, st === "busy" ? el.dataset.b : el.dataset.f);
      if (st !== "picked") return;
      for (const prop of ["fill", "stroke"]) {
        const m = el.dataset.f.match(new RegExp("(^|;)" + prop + ":([^;]+)"));
        if (m && m[2] !== "none" && !WHITE.test(m[2].trim())) el.style.setProperty(prop, "#5cb85c");
      }
    });
  });
}
const body = document.getElementById("body");
if (SVG) {
  for (let i = 0; i < 2; i++) {  // vagonning chap va o'ng yarmi — ikki qatorda
    const div = document.createElement("div"); div.className = "half"; div.innerHTML = SVG; body.appendChild(div);
    const svg = div.querySelector("svg"), vb = svg.viewBox.baseVal, w = vb.width / 2 + 20;
    svg.setAttribute("viewBox", `${i ? vb.x + vb.width - w : vb.x} ${vb.y} ${w} ${vb.height}`);
    paint(svg);
  }
} else {
  const grid = document.createElement("div"); grid.className = "grid";
  for (let s = 1; s <= TOTAL; s++) {
    const d = document.createElement("div"); d.textContent = s;
    d.style.background = PICKED.has(s) ? "#5cb85c" : FREE.has(s) ? "#187cee" : "#e4e4e4";
    if (!FREE.has(s) && !PICKED.has(s)) d.style.color = "#bebebe";
    grid.appendChild(d);
  }
  body.appendChild(grid);
}
})();
</script></body></html>"""


class CarRenderer:
    def __init__(self):
        self._lock = asyncio.Lock()
        self._pw = None
        self._browser = None
        self._page = None
        self._closer: asyncio.Task | None = None

    async def _ensure(self):
        if self._page is None:
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch()
            self._page = await self._browser.new_page(viewport={"width": 1000, "height": 600}, device_scale_factor=2)

    async def _close_later(self):
        await asyncio.sleep(IDLE_CLOSE)
        async with self._lock:
            await self.close()

    async def close(self):
        if self._browser:
            try:
                await self._browser.close()
                await self._pw.stop()
            except Exception:
                log.exception("Brauzerni yopishda xato")
        self._pw = self._browser = self._page = None

    async def render(self, car: dict, picked: list[int], title: str, sub: str) -> bytes:
        """car — car_schemas.cars() elementi. PNG baytlarini qaytaradi."""
        svg = (SCHEMA_DIR / car["schema"]).read_text(encoding="utf-8") if car.get("schema") else ""
        page_html = PAGE % {
            "width": 900, "title": html.escape(title), "sub": html.escape(sub),
            "free": json.dumps(car["free"]), "picked": json.dumps(picked), "svg": json.dumps(svg),
            "total": max([car.get("seats_count") or 0, *car["free"]]),
        }
        async with self._lock:
            if self._closer:
                self._closer.cancel()
            await self._ensure()
            await self._page.set_content(page_html)
            png = await self._page.locator("#root").screenshot(type="png")
            self._closer = asyncio.create_task(self._close_later())
            return png
