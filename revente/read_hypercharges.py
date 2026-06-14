"""Auto-count owned hypercharges from the live Brawl Stars collection.

Hard-won live findings drive this design (Mi9T, 2026-06-14):
- The hypercharge flame is only reliably isolable on a brawler's DETAIL screen
  (magenta flame in the bottom-right ability slot, on a solid blue background).
- BUT a NON-maxed brawler's detail shows purple power-up circles in that same
  corner → false magenta. So HC only counts when the detail is also MAXED
  ("NIVEAU MAX" text, matched fuzzily on the large yellow label).
- The stylised HUD font makes per-card name/power OCR unreliable, and brawlace's
  P11 list is stale. So we DON'T trust grid OCR: we open every card, gate on
  maxed+flame, and dedup brawlers by a perceptual hash of the detail portrait
  (no name OCR). Opening the collection may land on a detail, so we normalise to
  the grid first.
Pure helpers (_parse_power, _magenta_count, _detail_has_hypercharge) are unit-
tested on real fixtures; navigation is validated live. adb only.
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
    "detail_hc":     (0.90, 0.99, 0.18, 0.40),   # bottom-right HC slot on detail screen
    "detail_maxed":  (0.78, 1.00, 0.80, 0.93),   # "NIVEAU MAX !" yellow label (P11 only)
    "portrait":      (0.30, 0.70, 0.18, 0.82),   # brawler render — perceptual-hash dedup
}
MAX_SCROLLS = 15
MAX_TAPS = 60


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


def _crop(pil_image, w: int, h: int, region) -> "object":
    x0, x1, y0, y1 = region
    return pil_image.crop((int(w * x0), int(h * y0), int(w * x1), int(h * y1)))


def _detail_has_hypercharge(pil_image, w: int, h: int) -> bool:
    """True if the detail screen's bottom-right ability slot shows the HC flame.
    NOTE: only meaningful on a MAXED detail — a non-maxed detail shows purple
    power-up circles in the same corner. Callers must gate with `_detail_is_maxed`."""
    return _magenta_count(_crop(pil_image, w, h, _geom_for(w, h)["detail_hc"])) >= HC_MIN_PX


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


def _detail_is_maxed(pil_image, w: int, h: int) -> bool:
    """True if the detail screen shows the maxed (P11) 'NIVEAU MAX' label.
    OCR is fuzzy on the stylised font, so we match the 'max' substring."""
    return "max" in _ocr_text(_crop(pil_image, w, h, _geom_for(w, h)["detail_maxed"]))


def _portrait_hash(pil_image, w: int, h: int) -> str:
    """Coarse perceptual hash of the brawler render — dedups brawlers across
    scroll overlap without relying on (unreliable) name OCR."""
    crop = _crop(pil_image, w, h, _geom_for(w, h)["portrait"]).convert("L").resize((12, 12))
    px = list(crop.getdata())
    avg = sum(px) / len(px)
    return "".join("1" if p >= avg else "0" for p in px)


def _on_collection(img, w: int, h: int) -> bool:
    """The grid screen shows the 'BRAWLERS (n/m)' header; a detail screen does not."""
    return "brawler" in _ocr_text(img.crop((int(w * 0.30), 0, int(w * 0.70), int(h * 0.10))))


def _ensure_grid(serial: str, w: int, h: int, g: dict) -> bool:
    """Normalise to the collection GRID. Opening the collection from the lobby may
    land on a brawler detail, so: tap BRAWLERS, and if we're on a detail, tap back."""
    for _ in range(5):
        img = _screencap(serial)
        if _on_collection(img, w, h):
            return True
        # Either still on the lobby (open it) or on a detail (back out to the grid).
        _tap(serial, w, h, *g["brawlers_btn"])
        time.sleep(2.0)
        img = _screencap(serial)
        if _on_collection(img, w, h):
            return True
        _tap(serial, w, h, *g["back_arrow"])
        time.sleep(1.5)
    return False


def count_hypercharges(serial: str) -> dict:
    """Open the collection, open every brawler, and count those that are maxed AND
    show the hypercharge flame. Dedups by portrait hash (names don't OCR reliably).
    Returns {"count": int|None, "brawlers": []}. count=None on a navigation failure
    (caller keeps hypercharges=0). `brawlers` is currently always [] — the stylised
    name font is not OCR-reliable, so only the count is reported."""
    try:
        probe = _screencap(serial)
        w, h = probe.size
        g = _geom_for(w, h)
        if not _ensure_grid(serial, w, h, g):
            return {"count": None, "brawlers": []}

        # NOTE: we do NOT re-check _on_collection mid-scan. The collection header
        # OCR is flaky, and a false "grid lost" used to reopen the collection at the
        # TOP, making the next view all-seen → premature break before deep brawlers.
        # Instead we trust the deterministic flow: tap a cell → detail; back → SAME
        # grid position (verified, diff≈0); swipe → still grid. Portrait-hash dedup
        # absorbs scroll overlap and any tap that didn't open a detail (grid hash
        # repeats → seen). `empty_views` counts consecutive views with no new brawler
        # so transient OCR/scroll hiccups don't end the scan on the first dry view.
        seen: set = set()
        hc = 0
        empty_views = 0
        scrolls = 0
        taps = 0
        while scrolls <= MAX_SCROLLS and taps < MAX_TAPS and empty_views < 2:
            new_this_view = 0
            for (cx0, cx1, cy0, cy1) in g["cells"]:
                if taps >= MAX_TAPS:
                    break
                _tap(serial, w, h, (cx0 + cx1) / 2, (cy0 + cy1) / 2)
                time.sleep(1.8)
                taps += 1
                detail = _screencap(serial)
                ph = _portrait_hash(detail, w, h)
                if ph not in seen:
                    seen.add(ph)
                    new_this_view += 1
                    if _detail_is_maxed(detail, w, h) and _detail_has_hypercharge(detail, w, h):
                        hc += 1
                _tap(serial, w, h, *g["back_arrow"])
                time.sleep(1.2)
            empty_views = empty_views + 1 if new_this_view == 0 else 0
            _swipe(serial, w, h, g)
            time.sleep(1.3)
            scrolls += 1
        return {"count": hc, "brawlers": []}
    except Exception:
        log.exception("count_hypercharges failed")
        return {"count": None, "brawlers": []}
