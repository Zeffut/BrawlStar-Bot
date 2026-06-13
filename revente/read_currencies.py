"""Read trophies / gems / gold from a Brawl Stars lobby screenshot.

Self-contained (no game_api import): captures via adb directly and OCRs
with **easyocr** (the only engine that reads Brawl Stars' stylised HUD
font — tesseract fails on it even with clean thresholded glyphs).

`parse_currency_number` is pure (unit-tested). `read_lobby_numbers`
is exercised live against the BlueStacks emulator.

Crop ratios calibrated + VERIFIED LIVE 2026-05-31 on a 2560×1440
BlueStacks lobby (16:9 — ratios transfer to 1920×1080): gems & gold read
exactly; trophies is also read here but the authoritative trophy total
comes from brawlace (sum over brawlers) in the orchestrator.
"""
from __future__ import annotations

import io
import re
import subprocess

# (y0, y1, x0, x1) as ratios of the lobby frame — top bar, left→right.
# The HUD layout depends on the device ASPECT RATIO, not just the pixel size:
# the top-right currency cluster sits at different x on a 16:9 emulator vs a
# tall ~19.5:9 phone, so a single ratio set does NOT transfer. We keep one set
# per aspect bucket and pick by w/h.

# 16:9 emulator (BlueStacks 2560×1440 / 1920×1080). Verified live 2026-05-31.
_CROPS_16_9 = {
    "trophies": (0.020, 0.078, 0.218, 0.285),
    "gems":     (0.015, 0.078, 0.635, 0.715),
    "gold":     (0.015, 0.078, 0.735, 0.825),
}

# ~19.5:9 phone (Mi9T 2340×1080, landscape). Verified live 2026-06-13.
# Cluster order here is bling | gold | gems. Trophies is intentionally OMITTED:
# the stylised HUD font makes easyocr drop/duplicate a digit (reads 251770 for
# 25170), so the orchestrator must take trophies from brawlace / delta-tracking,
# never from this OCR.
_CROPS_WIDE = {
    "bling": (0.017, 0.067, 0.684, 0.747),
    "gold":  (0.017, 0.067, 0.753, 0.834),
    "gems":  (0.017, 0.067, 0.848, 0.900),
}


def _crops_for(w: int, h: int) -> dict:
    """Pick the crop set for the frame's aspect ratio (phone vs 16:9 emulator)."""
    return _CROPS_WIDE if (w / h) > 1.95 else _CROPS_16_9

_READER = None


def _ocr_digits_reader():
    """Return the lazily-created shared easyocr Reader (the only engine
    that reads the stylised Brawl Stars HUD font)."""
    global _READER
    if _READER is None:
        import easyocr
        _READER = easyocr.Reader(["en"], gpu=False, verbose=False)
    return _READER


def _ocr_digits(pil_image) -> str:
    """OCR a crop to digits via the shared easyocr Reader."""
    import numpy as np
    res = _ocr_digits_reader().readtext(np.array(pil_image),
                                        allowlist="0123456789", detail=0)
    return "".join(res)


def parse_currency_number(ocr_text: str) -> int | None:
    """Extract a currency integer from a noisy OCR string.

    Removes thousands separators (space/comma/dot between digits), then
    returns the longest digit run (tie -> largest), mirroring the
    trophy-OCR heuristic in game_api._ocr_trophies.
    """
    joined = re.sub(r"(?<=\d)[ ,.](?=\d)", "", ocr_text)
    runs = re.findall(r"\d+", joined)
    if not runs:
        return None
    return int(max(runs, key=lambda s: (len(s), int(s))))


def _screencap(serial: str) -> bytes:
    """Raw PNG bytes of the device screen via `adb exec-out screencap -p`."""
    out = subprocess.run(
        ["adb", "-s", serial, "exec-out", "screencap", "-p"],
        capture_output=True, timeout=15, check=True,
    )
    return out.stdout


def read_lobby_numbers(serial: str) -> dict:
    """Return the lobby currencies (keys depend on device: gems/gold always,
    plus trophies on 16:9 or bling on a phone) from the current lobby screen.
    Assumes Brawl Stars is at the lobby.

    Crops are picked per aspect ratio (`_crops_for`), so this works on both the
    BlueStacks emulator and a tall phone. Each crop is upscaled 3× before OCR.
    """
    from PIL import Image
    img = Image.open(io.BytesIO(_screencap(serial))).convert("RGB")
    w, h = img.size
    result: dict[str, int | None] = {}
    for name, (y0, y1, x0, x1) in _crops_for(w, h).items():
        crop = img.crop((int(w * x0), int(h * y0), int(w * x1), int(h * y1)))
        result[name] = parse_currency_number(_ocr_digits(crop))
    return result
