"""Benchmark phone capture latency: ADB screencap vs scrcpy stream.

Usage: python tools/bench_latency.py
"""
from __future__ import annotations

import os
import statistics
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
os.environ.setdefault("ORT_LOGGING_LEVEL", "3")

import subprocess  # noqa: E402

import cv2  # noqa: E402


def _stats(ms: list[float]) -> dict:
    ms.sort()
    n = len(ms)
    return {
        "n": n,
        "min": round(ms[0], 1),
        "p50": round(ms[n // 2], 1),
        "p95": round(ms[int(n * 0.95)], 1),
        "max": round(ms[-1], 1),
        "mean": round(statistics.mean(ms), 1),
        "fps_p50": round(1000.0 / ms[n // 2], 1) if ms[n // 2] > 0 else float("inf"),
        "fps_p95": round(1000.0 / ms[int(n * 0.95)], 1) if ms[int(n * 0.95)] > 0 else float("inf"),
    }


def bench_screencap(runs: int = 30) -> dict:
    """Measure ADB screencap round-trip latency."""
    ms: list[float] = []
    # Warmup
    subprocess.check_output(["adb", "exec-out", "screencap", "-p"])
    for _ in range(runs):
        t = time.perf_counter()
        subprocess.check_output(["adb", "exec-out", "screencap", "-p"])
        ms.append((time.perf_counter() - t) * 1000.0)
    return _stats(ms)


def bench_scrcpy(duration_s: float = 5.0) -> dict:
    """Measure scrcpy frame inter-arrival times."""
    import scrcpy

    times: list[float] = []
    last_t = [None]

    def on_frame(frame):
        if frame is None:
            return
        now = time.perf_counter()
        if last_t[0] is not None:
            times.append((now - last_t[0]) * 1000.0)
        last_t[0] = now

    client = scrcpy.Client(max_width=0, bitrate=8_000_000, max_fps=60, block_frame=False)
    client.add_listener(scrcpy.EVENT_FRAME, on_frame)
    client.start(threaded=True)
    try:
        time.sleep(duration_s)
    finally:
        client.stop()

    return _stats(times) if times else {"error": "no frames"}


def main() -> int:
    print("=" * 60)
    print(" ADB screencap latency (30 runs)")
    print("=" * 60)
    s = bench_screencap(30)
    print(f"  latency ms: min={s['min']}  p50={s['p50']}  p95={s['p95']}  max={s['max']}")
    print(f"  throughput: {s['fps_p50']} fps (p50), {s['fps_p95']} fps (p95)")
    print()
    print("=" * 60)
    print(" scrcpy stream frame rate (5s sample)")
    print("=" * 60)
    s = bench_scrcpy(5.0)
    if "error" in s:
        print(f"  ERROR: {s['error']}")
    else:
        print(f"  frame interval ms: min={s['min']}  p50={s['p50']}  p95={s['p95']}  max={s['max']}")
        print(f"  fps: {s['fps_p50']} (p50), {s['fps_p95']} (p95)")
        print(f"  total frames: {s['n']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
