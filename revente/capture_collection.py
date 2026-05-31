"""Best-effort: navigate to the brawler collection and save screenshots
so skins / hypercharges / maxed brawlers can be read by eye (no brittle
per-brawler enumeration).

Self-contained: adb only. Returns the saved PNG paths.
Coords calibrated on BlueStacks 2560×1440 (16:9 ratios).
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

OUT = Path(__file__).resolve().parent / "captures"
# BRAWLERS button: label OCR'd at y≈0.476, "new" badge at y≈0.421 → cell
# centre ≈ y0.45. (y0.55-0.60 is the Starr Road bar below — do NOT tap there.)
BRAWLERS_BTN = (0.055, 0.45)
BACK_ARROW = (0.04, 0.06)      # top-left back arrow (returns to lobby)


def _sh(serial: str, *args) -> None:
    subprocess.run(["adb", "-s", serial, "shell", *args], capture_output=True, timeout=15)


def _wh(serial: str) -> tuple[int, int]:
    out = subprocess.run(["adb", "-s", serial, "shell", "wm", "size"],
                         capture_output=True, timeout=10).stdout.decode()
    import re
    m = re.search(r"(\d+)x(\d+)", out)
    return (int(m.group(1)), int(m.group(2))) if m else (2560, 1440)


def _tap(serial: str, w: int, h: int, xr: float, yr: float) -> None:
    _sh(serial, "input", "tap", str(int(w * xr)), str(int(h * yr)))


def _screencap(serial: str, path: Path) -> None:
    out = subprocess.run(["adb", "-s", serial, "exec-out", "screencap", "-p"],
                         capture_output=True, timeout=15, check=True)
    path.write_bytes(out.stdout)


def capture(tag: str, serial: str, scrolls: int = 2) -> list[str]:
    """Open the collection, capture the grid + `scrolls` scrolled views,
    return to lobby. Returns saved PNG paths."""
    OUT.mkdir(exist_ok=True)
    w, h = _wh(serial)
    safe = tag.lstrip("#") or "account"
    paths: list[Path] = []

    _tap(serial, w, h, *BRAWLERS_BTN)
    time.sleep(2.0)
    p = OUT / f"{safe}_collection_0.png"
    _screencap(serial, p); paths.append(p)

    for i in range(1, scrolls + 1):
        # scroll the grid up (swipe bottom→top on the right 2/3)
        _sh(serial, "input", "swipe",
            str(int(w * 0.6)), str(int(h * 0.78)),
            str(int(w * 0.6)), str(int(h * 0.32)), "500")
        time.sleep(1.2)
        p = OUT / f"{safe}_collection_{i}.png"
        _screencap(serial, p); paths.append(p)

    _tap(serial, w, h, *BACK_ARROW)   # back to lobby (top-left arrow)
    time.sleep(1.0)
    return [str(p) for p in paths]


if __name__ == "__main__":
    import sys
    tag = sys.argv[1] if len(sys.argv) > 1 else "account"
    serial = sys.argv[2] if len(sys.argv) > 2 else "127.0.0.1:5555"
    for pth in capture(tag, serial):
        print(pth)
