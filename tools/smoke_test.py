"""Smoke test — verify ADB connection, scrcpy stream, and ONNX inference.

Does NOT touch Brawl Stars or send any input. Safe to run.

Usage:
    python tools/smoke_test.py
"""
from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

import cv2
import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from bsbot.controls.adb import AdbController  # noqa: E402
from bsbot.vision.detect import Detect  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("smoke")


def step(title):
    log.info("=" * 60)
    log.info(title)
    log.info("=" * 60)


def test_adb() -> bool:
    step("1/3  ADB connection")
    try:
        adb = AdbController.connect()
    except Exception as exc:
        log.error("ADB connection failed: %s", exc)
        return False
    log.info("OK: connected to %s (%dx%d)", adb.device.serial, adb.screen_width, adb.screen_height)
    return True


def test_screencap() -> bool:
    step("2/3  Screencap (no scrcpy)")
    import subprocess

    try:
        out = subprocess.check_output(["adb", "exec-out", "screencap", "-p"])
        if len(out) < 1024:
            log.error("Screencap output suspiciously small (%d bytes)", len(out))
            return False
        log.info("OK: %d KB PNG received", len(out) // 1024)
        debug_path = REPO_ROOT / "debug_screencap.png"
        debug_path.write_bytes(out)
        log.info("Saved screenshot to %s", debug_path)
        return True
    except subprocess.CalledProcessError as exc:
        log.error("Screencap failed: %s", exc)
        return False


def test_onnx() -> bool:
    step("3/3  ONNX inference")
    models_dir = REPO_ROOT / "src" / "bsbot" / "models"
    paths = [
        ("tileDetector.onnx", ["wall", "bush", "close_bush"]),
        ("brawlersInGame.onnx", None),
        ("mainInGameModel.onnx", None),
        ("startingScreenModel.onnx", None),
    ]
    all_ok = True
    # Use a recent screenshot if present, otherwise a synthetic image.
    debug = REPO_ROOT / "debug_screencap.png"
    if debug.exists():
        frame = cv2.imread(str(debug))
        log.info("Using debug_screencap.png as input (%dx%d)", frame.shape[1], frame.shape[0])
    else:
        rng = np.random.default_rng(0)
        frame = rng.integers(0, 255, size=(1080, 1920, 3), dtype=np.uint8)
        log.info("Using random 1920x1080 frame as input")

    for model_name, classes in paths:
        path = models_dir / model_name
        if not path.exists():
            log.error("Model missing: %s", path)
            all_ok = False
            continue
        try:
            t0 = time.perf_counter()
            d = Detect(str(path), classes=classes)
            t_load = time.perf_counter() - t0
            t1 = time.perf_counter()
            out = d.detect_objects(frame)
            t_inf = time.perf_counter() - t1
            n = sum(len(v) for v in out.values())
            log.info(
                "OK: %-25s | load=%4.0fms | infer=%4.0fms | %d dets on %s",
                model_name, t_load * 1000, t_inf * 1000, n, d.active_provider,
            )
        except Exception as exc:
            log.exception("FAIL: %s: %s", model_name, exc)
            all_ok = False
    return all_ok


def main() -> int:
    results = {
        "adb": test_adb(),
        "screencap": test_screencap(),
        "onnx": test_onnx(),
    }
    step("Summary")
    for k, v in results.items():
        log.info("  %-12s %s", k, "PASS" if v else "FAIL")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
