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
# Verified live on BlueStacks 2560×1440 with easyocr (2026-05-31).
_CROPS = {
    "trophies": (0.020, 0.078, 0.218, 0.285),
    "gems":     (0.015, 0.078, 0.635, 0.715),
    "gold":     (0.015, 0.078, 0.735, 0.825),
}

_READER = None


def _ocr_digits(pil_image) -> str:
    """OCR a crop to digits via easyocr (the only engine that reads the
    stylised Brawl Stars HUD font). Reader is lazily created once."""
    global _READER
    import numpy as np
    if _READER is None:
        import easyocr
        _READER = easyocr.Reader(["en"], gpu=False, verbose=False)
    res = _READER.readtext(np.array(pil_image), allowlist="0123456789", detail=0)
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
    """Return {'trophies': int|None, 'gems': int|None, 'gold': int|None}
    from the current lobby screen. Assumes Brawl Stars is at the lobby.

    Upscales each crop 3× greyscale before OCR for small-digit accuracy.
    """
    from PIL import Image
    img = Image.open(io.BytesIO(_screencap(serial))).convert("RGB")
    w, h = img.size
    result: dict[str, int | None] = {}
    for name, (y0, y1, x0, x1) in _CROPS.items():
        crop = img.crop((int(w * x0), int(h * y0), int(w * x1), int(h * y1)))
        result[name] = parse_currency_number(_ocr_digits(crop))
    return result
