"""Auto-count owned hypercharges from the live Brawl Stars collection.

Strategy: the hypercharge flame is only reliably isolable on a brawler's
DETAIL screen (magenta flame in the bottom-right ability slot, on a solid blue
background); grid color detection is too noisy and brawlace's P11 list is stale.
So we scan the collection grid to find P11 brawlers (power-badge OCR), open each
P11 card, and detect the flame on its detail screen. Pure helpers are unit-tested;
navigation I/O is validated live. adb only.
"""
from __future__ import annotations
import logging
import re
import subprocess
import time

log = logging.getLogger("read_hypercharges")

# OpenCV HSV (H 0-180). Hot-magenta hypercharge flame. Calibrated on real Mi9T
# detail screens: bottom-right slot region gives Maisie(HC)=8010 px, Shelly(no HC)=0.
HC_HSV_LO = (145, 120, 120)
HC_HSV_HI = (172, 255, 255)
HC_MIN_PX = 300

# Resolution-aware geometry, ratios of the landscape frame (w,h). "wide" =
# ~19.5:9 phone (Mi9T 2340x1080), calibrated live. 16:9 emulator left for later.
_GEOM_WIDE = {
    "brawlers_btn": (0.060, 0.435),
    "back_arrow":   (0.025, 0.052),
    "scroll":       (0.60, 0.78, 0.60, 0.30, 500),  # x, y_from, (unused), y_to, ms
    "cells": [
        (0.10, 0.36, 0.13, 0.47), (0.38, 0.63, 0.13, 0.47), (0.655, 0.905, 0.13, 0.47),
        (0.10, 0.36, 0.50, 0.84), (0.38, 0.63, 0.50, 0.84), (0.655, 0.905, 0.50, 0.84),
    ],
    "badge_in_cell": (0.02, 0.20, 0.80, 0.99),   # power badge, fractions OF the cell
    "name_in_cell":  (0.20, 0.98, 0.80, 0.99),   # name strip, fractions OF the cell
    "detail_hc":     (0.90, 0.99, 0.18, 0.40),   # bottom-right HC slot on detail screen
    "detail_name":   (0.06, 0.40, 0.12, 0.24),   # brawler name on detail header
}
MAX_SCROLLS = 12
MAX_TAPS = 10


def _geom_for(w: int, h: int) -> dict:
    """Geometry for the frame aspect. Only the ~19.5:9 phone set is calibrated;
    it is returned for every aspect until a 16:9 emulator set is added."""
    return _GEOM_WIDE


def _parse_power(ocr_text: "str | None") -> "int | None":
    """Parse a brawler power level (1..11) from a power-badge OCR string."""
    m = re.findall(r"\d+", ocr_text or "")
    if not m:
        return None
    val = int(max(m, key=len))
    return val if 1 <= val <= 11 else None


def _magenta_count(pil_crop) -> int:
    """Count hot-magenta (hypercharge-flame) pixels in a PIL crop."""
    import numpy as np
    import cv2
    hsv = cv2.cvtColor(np.array(pil_crop), cv2.COLOR_RGB2HSV)
    mask = cv2.inRange(hsv, np.array(HC_HSV_LO), np.array(HC_HSV_HI))
    return int(mask.sum() // 255)


def _detail_has_hypercharge(pil_image, w: int, h: int) -> bool:
    """True if the detail screen's bottom-right ability slot shows the HC flame."""
    x0, x1, y0, y1 = _geom_for(w, h)["detail_hc"]
    crop = pil_image.crop((int(w * x0), int(h * y0), int(w * x1), int(h * y1)))
    return _magenta_count(crop) >= HC_MIN_PX


def _screencap(serial: str):
    import io
    from PIL import Image
    out = subprocess.run(["adb", "-s", serial, "exec-out", "screencap", "-p"],
                         capture_output=True, timeout=15, check=True)
    return Image.open(io.BytesIO(out.stdout)).convert("RGB")


def _tap(serial: str, w: int, h: int, xr: float, yr: float) -> None:
    subprocess.run(["adb", "-s", serial, "shell", "input", "tap",
                    str(int(w * xr)), str(int(h * yr))], capture_output=True, timeout=10)


def _swipe(serial: str, w: int, h: int, g: dict) -> None:
    x, y0, _unused, y1, ms = g["scroll"]
    subprocess.run(["adb", "-s", serial, "shell", "input", "swipe",
                    str(int(w * x)), str(int(h * y0)), str(int(w * x)), str(int(h * y1)),
                    str(ms)], capture_output=True, timeout=10)


def _ocr_text(pil_crop, allow: "str | None" = None) -> str:
    """easyocr a crop to a lowercase string (reuses read_currencies' lazy reader)."""
    import numpy as np
    from revente.read_currencies import _ocr_digits_reader
    rd = _ocr_digits_reader()
    kw = {"detail": 0}
    if allow:
        kw["allowlist"] = allow
    res = rd.readtext(np.array(pil_crop), **kw)
    return " ".join(res).lower().strip()


def _on_collection(serial: str, w: int, h: int) -> bool:
    img = _screencap(serial)
    crop = img.crop((int(w * 0.30), 0, int(w * 0.70), int(h * 0.10)))
    return "brawler" in _ocr_text(crop)


def _detail_brawler_name(img, w: int, h: int) -> "str | None":
    x0, x1, y0, y1 = _geom_for(w, h)["detail_name"]
    txt = _ocr_text(img.crop((int(w * x0), int(h * y0), int(w * x1), int(h * y1))))
    return txt or None


def count_hypercharges(serial: str) -> dict:
    """Open the collection, find P11 brawlers, confirm each one's hypercharge on
    its detail screen. Returns {"count": int|None, "brawlers": [str]}. count=None
    on a navigation failure (caller keeps hypercharges=0)."""
    try:
        probe = _screencap(serial)
        w, h = probe.size
        g = _geom_for(w, h)
        for _ in range(3):
            if _on_collection(serial, w, h):
                break
            _tap(serial, w, h, *g["brawlers_btn"])
            time.sleep(2.5)
        else:
            return {"count": None, "brawlers": []}

        seen: set = set()
        hc: list = []
        scrolls = 0
        taps = 0
        while scrolls <= MAX_SCROLLS:
            view = _screencap(serial)
            new_on_view = False
            for (cx0, cx1, cy0, cy1) in g["cells"]:
                cw, ch = (cx1 - cx0), (cy1 - cy0)
                nx0, nx1, ny0, ny1 = g["name_in_cell"]
                name = _ocr_text(view.crop((
                    int(w * (cx0 + cw * nx0)), int(h * (cy0 + ch * ny0)),
                    int(w * (cx0 + cw * nx1)), int(h * (cy0 + ch * ny1)))))
                if not name or name in seen:
                    continue
                seen.add(name)
                new_on_view = True
                bx0, bx1, by0, by1 = g["badge_in_cell"]
                power = _parse_power(_ocr_text(view.crop((
                    int(w * (cx0 + cw * bx0)), int(h * (cy0 + ch * by0)),
                    int(w * (cx0 + cw * bx1)), int(h * (cy0 + ch * by1)))), allow="0123456789"))
                if power is not None and power < 9:
                    continue  # only P11 can have HC; tap ambiguous(None)/>=9 to be safe
                if taps >= MAX_TAPS:
                    log.warning("hc scan: MAX_TAPS hit, stopping early")
                    break
                _tap(serial, w, h, (cx0 + cx1) / 2, (cy0 + cy1) / 2)
                time.sleep(2.0)
                taps += 1
                detail = _screencap(serial)
                dname = _detail_brawler_name(detail, w, h)
                if dname and _detail_has_hypercharge(detail, w, h):
                    hc.append(dname)
                _tap(serial, w, h, *g["back_arrow"])
                time.sleep(1.5)
            if not new_on_view:
                break
            _swipe(serial, w, h, g)
            time.sleep(1.3)
            scrolls += 1
        return {"count": len(hc), "brawlers": sorted(set(hc))}
    except Exception:
        log.exception("count_hypercharges failed")
        return {"count": None, "brawlers": []}
