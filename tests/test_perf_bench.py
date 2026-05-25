"""Performance benchmarks — measure ONNX inference and state_finder latency.

Run only when explicitly requested:
    pytest tests/test_perf_bench.py -v -m bench

These tests do not assert hard thresholds (perf varies by machine) but print
timings to stdout. CI can read these to detect regressions over time.
"""
from __future__ import annotations

import time
from pathlib import Path

import cv2
import numpy as np
import pytest

from bsbot.vision.detect import Detect
from bsbot.vision.state_finder import StateFinder

pytestmark = pytest.mark.bench

REPO_ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = REPO_ROOT / "src" / "bsbot" / "models"
TEMPLATES_DIR = REPO_ROOT / "src" / "bsbot" / "data" / "state_templates"
FIXTURE_LOBBY = REPO_ROOT / "tests" / "fixtures" / "states" / "lobby"


def _sample_frame() -> np.ndarray:
    """Use the first lobby fixture if available, else a synthetic 2340x1080."""
    pngs = list(FIXTURE_LOBBY.glob("*.png"))
    if pngs:
        f = cv2.imread(str(pngs[0]))
        if f is not None:
            return f
    rng = np.random.default_rng(0)
    return rng.integers(0, 255, size=(1080, 2340, 3), dtype=np.uint8)


def _timed(fn, *, runs: int = 20):
    fn()  # warmup
    times = []
    for _ in range(runs):
        t = time.perf_counter()
        fn()
        times.append(time.perf_counter() - t)
    times.sort()
    n = len(times)
    return {
        "min_ms": round(times[0] * 1000, 2),
        "p50_ms": round(times[n // 2] * 1000, 2),
        "p95_ms": round(times[int(n * 0.95)] * 1000, 2),
        "max_ms": round(times[-1] * 1000, 2),
    }


def test_state_finder_latency():
    sf = StateFinder(TEMPLATES_DIR)
    frame = _sample_frame()
    timings = _timed(lambda: sf.detect(frame))
    print(f"state_finder.detect(): {timings}")
    # Soft assert: 95th percentile should be under 200ms.
    assert timings["p95_ms"] < 500, f"too slow: {timings}"


def test_onnx_tile_latency():
    d = Detect(str(MODELS_DIR / "tileDetector.onnx"), classes=["wall", "bush", "close_bush"])
    frame = _sample_frame()
    timings = _timed(lambda: d.detect_objects(frame, conf_thresh=0.6))
    print(f"tileDetector inference: {timings}  | backend={d.active_provider}")
    assert timings["p95_ms"] < 300, f"tile detector too slow: {timings}"


def test_onnx_brawler_latency():
    d = Detect(str(MODELS_DIR / "brawlersInGame.onnx"))
    frame = _sample_frame()
    timings = _timed(lambda: d.detect_objects(frame, conf_thresh=0.78))
    print(f"brawlersInGame inference: {timings}  | backend={d.active_provider}")
    assert timings["p95_ms"] < 300, f"brawler detector too slow: {timings}"
