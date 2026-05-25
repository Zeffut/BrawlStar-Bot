"""Capture a screenshot from the connected Android phone and save it as a
state-detection template.

Usage:
    python tools/capture_template.py --state lobby --name play_button
    python tools/capture_template.py --state lobby --name play_button --crop 1500,1000,1900,1200

`--crop x1,y1,x2,y2` is optional. If omitted, saves the full screenshot
(you can crop it later in an image editor).
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from bsbot.controls.adb import AdbController

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = REPO_ROOT / "src" / "bsbot" / "data" / "state_templates"


def grab_screenshot(adb: AdbController) -> np.ndarray:
    """Use `adb exec-out screencap -p` for a quick PNG dump."""
    # exec-out returns binary PNG over stdout.
    import subprocess

    cmd = ["adb", "-s", adb.device.serial, "exec-out", "screencap", "-p"]
    raw = subprocess.check_output(cmd)
    img = Image.open(__import__("io").BytesIO(raw))
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--state", required=True, choices=[
        "lobby", "match", "end", "popup", "starting", "disconnect"
    ])
    p.add_argument("--name", required=True, help="filename (no extension), e.g. 'play_button'")
    p.add_argument(
        "--crop", default=None,
        help="x1,y1,x2,y2 (in screen pixels). If omitted, saves whole screenshot.",
    )
    p.add_argument("--delay", type=float, default=0.5, help="seconds to wait before grab")
    args = p.parse_args()

    adb = AdbController.connect()
    print(f"Connected to {adb.device.serial} ({adb.screen_width}x{adb.screen_height})")
    print(f"Capturing in {args.delay}s…")
    time.sleep(args.delay)
    frame = grab_screenshot(adb)
    print(f"Got frame {frame.shape[1]}x{frame.shape[0]}")

    if args.crop:
        try:
            x1, y1, x2, y2 = (int(v) for v in args.crop.split(","))
        except Exception:
            print(f"Invalid --crop value: {args.crop}", file=sys.stderr)
            return 1
        frame = frame[y1:y2, x1:x2]
        print(f"Cropped to {frame.shape[1]}x{frame.shape[0]}")

    out_dir = TEMPLATES_DIR / args.state
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{args.name}.png"
    cv2.imwrite(str(out_path), frame)
    print(f"Saved: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
