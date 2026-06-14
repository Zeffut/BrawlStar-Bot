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
Pure helpers (_parse_power, _magenta_count, _detail_has_hypercharge,
_detail_is_maxed) are unit-tested on real fixtures and PROVEN correct, including
live (Shelly P11 → maxed True; Bull P1 → excluded; Maisie HC → detected).

RELIABILITY CAVEAT — the per-brawler DETECTION is solid, but the full-collection
ENUMERATION is best-effort and currently UNDER-COUNTS: live OCR drifts frame to
frame (e.g. "NIVEAU MAX" reads "nlveaumaa", the X→a), the top grid view's bottom
row mis-taps, and reaching deep brawlers via scroll+portrait-dedup is unreliable.
So `count_hypercharges` is a precise-but-low-recall LOWER BOUND, exposed opt-in
(estimate_account --scan-hc), not authoritative. This is the project's known
brawler-navigation fragility. adb only.
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
    # the detail screen is a horizontal CAROUSEL: a low right→left swipe goes to the
    # NEXT brawler (stays on a detail). This is the robust enumeration — no grid,
    # no scroll, no cell taps, no reorder/coverage issues. (x_from, y, x_to, y, ms)
    "carousel_next": (0.81, 0.83, 0.21, 0.83, 250),
}
MAX_SCROLLS = 24
MAX_TAPS = 110
MAX_CAROUSEL = 130          # safety cap on carousel steps (≫ any owned-brawler count)


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

    The stylised font OCRs inconsistently: live reads of "NIVEAU MAX" come back as
    "nlveaumaa", "nlmeaumaa", "nlleeau maa" (the X reads as 'a'!), so matching "max"
    alone misses real P11s. We match any stable fragment of NIVEAU/MAX. A non-maxed
    detail shows upgrade numbers ("20 20") / power circles instead — none of which
    contain these fragments."""
    t = _ocr_text(_crop(pil_image, w, h, _geom_for(w, h)["detail_maxed"]))
    return any(k in t for k in ("eau", "niv", "max", "maa"))


def _portrait_hash(pil_image, w: int, h: int) -> str:
    """Coarse perceptual hash of the brawler render — dedups brawlers across
    scroll overlap without relying on (unreliable) name OCR."""
    crop = _crop(pil_image, w, h, _geom_for(w, h)["portrait"]).convert("L").resize((12, 12))
    px = list(crop.getdata())
    avg = sum(px) / len(px)
    return "".join("1" if p >= avg else "0" for p in px)


def _grid_header_text(img, w: int, h: int) -> str:
    return _ocr_text(img.crop((int(w * 0.28), 0, int(w * 0.72), int(h * 0.11))))


def _looks_like_grid(text: str) -> bool:
    """The grid's top-centre header is 'BRAWLERS (n/103)'. OCR garbles it, so match
    its stablest fragments: the middle of bRAWLers and the fixed total '103'. A detail
    screen's top-centre is empty (name is top-left, currencies top-right) → no match."""
    return any(k in text for k in ("rawl", "brawl", "103", "/10"))


def _on_collection(serial: str, w: int, h: int, samples: int = 2) -> bool:
    """True if on the collection grid — voted over a few frames (OCR is flaky)."""
    for i in range(samples):
        if _looks_like_grid(_grid_header_text(_screencap(serial), w, h)):
            return True
        if i < samples - 1:
            time.sleep(0.4)
    return False


def _ensure_grid(serial: str, w: int, h: int, g: dict) -> bool:
    """Normalise to the collection GRID. Opening the collection from the lobby may
    land on a brawler detail, so: tap BRAWLERS, and if we're on a detail, tap back."""
    for _ in range(6):
        if _on_collection(serial, w, h):
            return True
        # Either still on the lobby (open it) or on a detail (back out to the grid).
        _tap(serial, w, h, *g["brawlers_btn"])
        time.sleep(2.2)
        if _on_collection(serial, w, h):
            return True
        _tap(serial, w, h, *g["back_arrow"])
        time.sleep(1.6)
    return False


def _img_mean_diff(a, b) -> float:
    """Mean absolute pixel difference between two same-size RGB PIL images.
    A robust, OCR-free 'did the screen change?' signal: grid↔detail differ a lot
    (≫30); the same grid before/after a no-op action differs ≈0."""
    import numpy as np
    aa = np.asarray(a, dtype="int16")
    bb = np.asarray(b, dtype="int16")
    if aa.shape != bb.shape:
        return 255.0
    return float(np.abs(aa - bb).mean())


def _vote_detail(serial: str, w: int, h: int, samples: int = 3) -> tuple:
    """Majority vote of (maxed, hypercharge) over `samples` frames of the detail
    currently open. The detail animates, so one frame's OCR of 'NIVEAU MAX' drifts;
    voting stabilises it. Returns (maxed: bool, hypercharge: bool)."""
    mv = hv = 0
    for i in range(samples):
        img = _screencap(serial)
        if _detail_is_maxed(img, w, h):
            mv += 1
            if _detail_has_hypercharge(img, w, h):
                hv += 1
        if i < samples - 1:
            time.sleep(0.4)
    maxed = mv * 2 >= samples
    return maxed, (maxed and hv * 2 >= samples)


def check_current_detail(serial: str, samples: int = 3) -> dict:
    """Inspect the brawler DETAIL screen currently open on the device — RELIABLE,
    because it does no navigation (the fragile part). Open a brawler's detail
    yourself (or via the bot), then call this. Returns {"maxed", "hypercharge"};
    `hypercharge` is only meaningful when `maxed` is True (non-maxed details show
    purple power circles in the same slot). Uses multi-frame voting (animated UI)."""
    probe = _screencap(serial)
    w, h = probe.size
    maxed, hc = _vote_detail(serial, w, h, samples)
    return {"maxed": maxed, "hypercharge": hc}


# diff thresholds (mean abs RGB diff): grid↔detail is large; grid↔same-grid tiny.
_DETAIL_OPENED_DIFF = 18.0   # post-tap screen differs this much from the grid → a detail opened
_SCROLL_BOTTOM_DIFF = 6.0    # grid barely changed after a swipe → at the bottom


def _swipe_carousel_next(serial: str, w: int, h: int, g: dict) -> None:
    """On a brawler DETAIL screen, swipe to the NEXT brawler (the detail is a
    horizontal carousel: a low right→left swipe advances it)."""
    x0, y, x1, _y, ms = g["carousel_next"]
    subprocess.run(["adb", "-s", serial, "shell", "input", "swipe",
                    str(int(w * x0)), str(int(h * y)), str(int(w * x1)), str(int(h * y)),
                    str(ms)], capture_output=True, timeout=10)


def _enter_detail(serial: str, w: int, h: int, g: dict) -> bool:
    """Reach a brawler DETAIL screen (the carousel start) using IMAGE DIFFS only — no
    header OCR (which proved unreliable). From the lobby: open the collection and tap a
    cell; whether the collection opens to the grid or straight to a detail, a cell tap
    leaves us on a detail. Confirmed by the screen differing a lot from the lobby."""
    lobby = _screencap(serial)
    cx0, cx1, cy0, cy1 = g["cells"][0]
    for _ in range(4):
        _tap(serial, w, h, *g["brawlers_btn"])
        time.sleep(2.2)
        _tap(serial, w, h, (cx0 + cx1) / 2, (cy0 + cy1) / 2)
        time.sleep(2.2)
        if _img_mean_diff(lobby, _screencap(serial)) >= _DETAIL_OPENED_DIFF:
            return True
        _tap(serial, w, h, *g["back_arrow"])  # popup/blocked → clear and retry
        time.sleep(1.0)
    return False


def count_hypercharges(serial: str) -> dict:
    """Count brawlers that are maxed AND own the hypercharge flame, by walking the
    brawler-detail CAROUSEL. Returns {"count": int|None, "brawlers": []}; count=None
    on a navigation failure (caller keeps hypercharges=0).

    Why a carousel walk (after ~50 failed grid-tap iterations): the brawler detail is
    a horizontal carousel — one low right→left swipe = next brawler, staying on a
    detail. This sidesteps every grid fragility (dynamic order, scroll coverage, cell
    mis-taps, popups). Per brawler we use MULTI-FRAME VOTING (the detail animates) for
    maxed+HC, and dedup by a portrait perceptual hash to stop when the carousel wraps
    back to a brawler we've already seen."""
    try:
        probe = _screencap(serial)
        w, h = probe.size
        g = _geom_for(w, h)
        if not _enter_detail(serial, w, h, g):
            return {"count": None, "brawlers": []}

        seen: set = set()
        hc = 0
        dup_streak = 0
        for _ in range(MAX_CAROUSEL):
            ph = _portrait_hash(_screencap(serial), w, h)
            if ph in seen:
                # A single repeat usually means the swipe didn't advance (animation
                # timing), NOT a wrap. Re-swipe and retry; only stop after several
                # consecutive repeats (genuinely wrapped around or stuck).
                dup_streak += 1
                if dup_streak >= 3:
                    break
                _swipe_carousel_next(serial, w, h, g)
                time.sleep(1.5)
                continue
            dup_streak = 0
            seen.add(ph)
            _maxed, has_hc = _vote_detail(serial, w, h)
            if has_hc:
                hc += 1
            _swipe_carousel_next(serial, w, h, g)
            time.sleep(1.4)
        return {"count": hc, "brawlers": []}
    except Exception:
        log.exception("count_hypercharges failed")
        return {"count": None, "brawlers": []}
