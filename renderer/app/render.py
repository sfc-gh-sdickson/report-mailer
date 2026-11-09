"""Turn an HTML or PDF report into email-safe static images (and optionally a PDF).

Email clients do not execute JavaScript, so charts built with Chart.js, Plotly,
ECharts, Vega, D3, Highcharts, etc. must be rendered *before* sending. We load the
report in headless Chromium, wait until the charts have finished drawing, and take
a full-page screenshot that is sliced into email-friendly PNG strips.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from PIL import Image

log = logging.getLogger(__name__)

# Disables CSS animations/transitions so screenshots never catch a half-drawn chart.
_FREEZE_CSS = """
*, *::before, *::after {
  animation-duration: 0s !important; animation-delay: 0s !important;
  transition-duration: 0s !important; transition-delay: 0s !important;
  caret-color: transparent !important;
}
"""

# Turns off the JS animations of the common charting libraries when they are present.
# Runs before any page script, so it applies to charts created during load.
_FREEZE_JS = """
(() => {
  const patch = () => {
    try { if (window.Chart && window.Chart.defaults) { window.Chart.defaults.animation = false; } } catch (e) {}
    try { if (window.Highcharts && window.Highcharts.setOptions) {
      window.Highcharts.setOptions({ plotOptions: { series: { animation: false } }, chart: { animation: false } }); } } catch (e) {}
    try { if (window.echarts && !window.__rmEchartsPatched) {
      const init = window.echarts.init; window.__rmEchartsPatched = true;
      window.echarts.init = function () { const c = init.apply(this, arguments);
        const so = c.setOption.bind(c); c.setOption = (o, ...r) => so(Object.assign({}, o, { animation: false }), ...r); return c; };
    } } catch (e) {}
  };
  patch();
  document.addEventListener('DOMContentLoaded', patch);
  const t = setInterval(patch, 25); setTimeout(() => clearInterval(t), 5000);
})();
"""

# Resolves when the page signals readiness, or when the DOM has stopped changing.
# Report authors can set `window.REPORT_READY = true` for a deterministic signal.
_WAIT_STABLE_JS = """
async (quietMs) => {
  if (document.fonts && document.fonts.ready) { await document.fonts.ready; }
  if ('REPORT_READY' in window) {
    const start = Date.now();
    while (window.REPORT_READY !== true && Date.now() - start < 60000) {
      await new Promise(r => setTimeout(r, 100));
    }
    return 'REPORT_READY';
  }
  return await new Promise(resolve => {
    let timer = setTimeout(done, quietMs);
    const obs = new MutationObserver(() => { clearTimeout(timer); timer = setTimeout(done, quietMs); });
    obs.observe(document.documentElement, { subtree: true, childList: true, attributes: true, characterData: true });
    const hardStop = setTimeout(done, 30000);
    function done() { obs.disconnect(); clearTimeout(hardStop); resolve('DOM_QUIET'); }
  });
}
"""


@dataclass
class RenderOptions:
    viewport_width: int = 1200
    device_scale_factor: int = 2
    slice_height_px: int = 1400
    timeout_ms: int = 90_000
    extra_wait_ms: int = 750
    wait_selector: str | None = None
    allowed_hosts: list[str] = field(default_factory=list)
    make_pdf: bool = True
    pdf_page_format: str = "SINGLE_PAGE"  # SINGLE_PAGE | LETTER | A4


@dataclass
class RenderResult:
    slices: list[Path]
    full_png: Path
    pdf: Path | None
    width_px: int
    height_px: int
    blocked_requests: list[str]


def _host_allowed(url: str, allowed: list[str]) -> bool:
    parsed = urlparse(url)
    if parsed.scheme in ("file", "data", "blob", "about"):
        return True
    host = (parsed.hostname or "").lower()
    return any(host == a or host.endswith("." + a) for a in allowed)


def render_html(html_path: Path, out_dir: Path, opts: RenderOptions) -> RenderResult:
    """Render a local HTML file. Network access is limited to opts.allowed_hosts."""
    from playwright.sync_api import sync_playwright

    out_dir.mkdir(parents=True, exist_ok=True)
    blocked: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--disable-dev-shm-usage", "--font-render-hinting=none"])
        try:
            ctx = browser.new_context(
                viewport={"width": opts.viewport_width, "height": 900},
                device_scale_factor=opts.device_scale_factor,
                reduced_motion="reduce",
                color_scheme="light",
                java_script_enabled=True,
                accept_downloads=False,
            )
            ctx.add_init_script(_FREEZE_JS)

            def _route(route):
                url = route.request.url
                if _host_allowed(url, opts.allowed_hosts):
                    return route.continue_()
                blocked.append(url)
                return route.abort()

            ctx.route("**/*", _route)
            page = ctx.new_page()
            page.set_default_timeout(opts.timeout_ms)
            page.goto(html_path.resolve().as_uri(), wait_until="load")
            page.add_style_tag(content=_FREEZE_CSS)
            try:
                page.wait_for_load_state("networkidle", timeout=min(opts.timeout_ms, 30_000))
            except Exception:  # long-polling pages never go idle; the stability wait below still applies
                log.warning("networkidle not reached; continuing with DOM stability wait")
            if opts.wait_selector:
                page.wait_for_selector(opts.wait_selector, state="visible")
            signal = page.evaluate(_WAIT_STABLE_JS, 500)
            log.info("Render readiness signal: %s", signal)
            page.wait_for_timeout(opts.extra_wait_ms)

            full_png = out_dir / "report_full.png"
            page.screenshot(path=str(full_png), full_page=True, type="png", animations="disabled")

            pdf_path = None
            if opts.make_pdf:
                pdf_path = out_dir / "report.pdf"
                page.emulate_media(media="screen")
                if opts.pdf_page_format == "SINGLE_PAGE":
                    height = page.evaluate("Math.ceil(document.documentElement.scrollHeight)")
                    page.pdf(path=str(pdf_path), width=f"{opts.viewport_width}px", height=f"{height + 2}px",
                             print_background=True, margin={"top": "0", "bottom": "0", "left": "0", "right": "0"})
                else:
                    page.pdf(path=str(pdf_path), format="Letter" if opts.pdf_page_format == "LETTER" else "A4",
                             landscape=True, print_background=True,
                             margin={"top": "0.4in", "bottom": "0.4in", "left": "0.4in", "right": "0.4in"})
        finally:
            browser.close()

    if blocked:
        log.warning("Blocked %d request(s) to hosts outside RM_RENDER_ALLOWED_HOSTS: %s",
                    len(blocked), sorted({urlparse(u).hostname for u in blocked}))
    slices, (w, h) = slice_image(full_png, out_dir, opts.slice_height_px)
    return RenderResult(slices=slices, full_png=full_png, pdf=pdf_path, width_px=w, height_px=h,
                        blocked_requests=blocked)


def render_pdf(pdf_path: Path, out_dir: Path, opts: RenderOptions, max_pages: int = 20) -> RenderResult:
    """Rasterize a PDF report into page images; the original PDF is kept for attachment."""
    import pypdfium2 as pdfium

    out_dir.mkdir(parents=True, exist_ok=True)
    doc = pdfium.PdfDocument(str(pdf_path))
    pages: list[Path] = []
    try:
        for i in range(min(len(doc), max_pages)):
            page = doc[i]
            # Scale so a page renders ~1600px wide (crisp at 800px display width).
            scale = 1600 / page.get_width()
            img = page.render(scale=scale).to_pil().convert("RGB")
            target = out_dir / f"page_{i + 1:03d}.png"
            img.save(target, format="PNG", optimize=True)
            pages.append(target)
            page.close()
    finally:
        doc.close()
    if not pages:
        raise ValueError(f"{pdf_path.name} has no pages")
    w, h = Image.open(pages[0]).size
    return RenderResult(slices=pages, full_png=pages[0], pdf=pdf_path, width_px=w, height_px=h, blocked_requests=[])


def slice_image(full_png: Path, out_dir: Path, slice_height: int) -> tuple[list[Path], tuple[int, int]]:
    """Cut a tall screenshot into horizontal strips.

    Single very tall images are scaled down or cropped by several mail clients
    (notably Outlook desktop), so we send a stack of strips that render seamlessly.
    """
    img = Image.open(full_png).convert("RGB")
    width, height = img.size
    slices: list[Path] = []
    top, idx = 0, 1
    while top < height:
        bottom = min(top + slice_height, height)
        part = img.crop((0, top, width, bottom))
        target = out_dir / f"slice_{idx:03d}.png"
        part.save(target, format="PNG", optimize=True)
        slices.append(target)
        top, idx = bottom, idx + 1
    return slices, (width, height)


def shrink_for_budget(images: list[Path], budget_bytes: int) -> list[Path]:
    """Re-encode images as JPEG (quality 85, then 70) if the PNG set exceeds the provider's size budget."""
    def total(paths: list[Path]) -> int:
        return sum(os.path.getsize(p) for p in paths)

    if total(images) <= budget_bytes:
        return images
    converted: list[Path] = images
    for quality in (85, 70):
        converted = []
        for p in images:  # always re-encode from the lossless originals
            target = p.with_suffix(f".q{quality}.jpg")
            Image.open(p).convert("RGB").save(target, format="JPEG", quality=quality, optimize=True, progressive=True)
            converted.append(target)
        if total(converted) <= budget_bytes:
            log.info("Images re-encoded as JPEG q%d to fit the %d-byte budget", quality, budget_bytes)
            return converted
    raise ValueError(f"Rendered images ({total(converted)} bytes) exceed the mail size budget ({budget_bytes} bytes)")
