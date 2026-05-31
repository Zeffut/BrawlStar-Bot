"""Read trophies / gems / gold from a Brawl Stars lobby screenshot.

Self-contained (no game_api import): captures via adb directly and OCRs
with the tesseract binary through subprocess (the pytesseract wrapper is
broken on this host — it chokes decoding tesseract's stderr).

`parse_currency_number` is pure (unit-tested). `read_lobby_numbers`
is exercised live against the BlueStacks emulator.

Crop ratios calibrated + VISUALLY VERIFIED 2026-05-31 on a 2560×1440
BlueStacks lobby (16:9 — ratios transfer to 1920×1080): each crop lands
exactly on its number.

⚠️ OCR-ENGINE LIMITATION (2026-05-31): tesseract CANNOT read Brawl Stars'
stylised HUD font (rounded, thick-outlined digits) even with clean
thresholded glyphs. This is why the bot itself uses **easyocr**. To make
this reader work, swap `_tesseract()` for an easyocr-based OCR (heavy:
pulls PyTorch). The crops + parse + capture pipeline are correct and
engine-agnostic — only the OCR call needs upgrading.
"""
from __future__ import annotations

import io
import re
import subprocess
import tempfile
from pathlib import Path

# (y0, y1, x0, x1) as ratios of the lobby frame — top bar, left→right.
_CROPS = {
    "trophies": (0.020, 0.075, 0.210, 0.280),
    "gems":     (0.015, 0.075, 0.525, 0.605),
    "gold":     (0.015, 0.075, 0.635, 0.725),
}


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


def _tesseract(png_bytes: bytes) -> str:
    """OCR a PNG via the tesseract CLI. Returns raw text ('' on failure)."""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(png_bytes)
        path = f.name
    try:
        out = subprocess.run(
            ["tesseract", path, "stdout", "--psm", "7"],
            capture_output=True, timeout=20,
        )
        return out.stdout.decode("utf-8", errors="ignore").strip()
    except Exception:
        return ""
    finally:
        Path(path).unlink(missing_ok=True)


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
        crop = crop.convert("L").resize((crop.width * 3, crop.height * 3))
        buf = io.BytesIO()
        crop.save(buf, format="PNG")
        result[name] = parse_currency_number(_tesseract(buf.getvalue()))
    return result
