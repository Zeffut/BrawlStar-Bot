"""Live phone viewer with overlay — uses scrcpy for fluid capture.

- Capture: scrcpy stream (60 fps native).
- Annotation: ONNX + state_finder in background thread at ~3 fps. Stores
  the rendered overlay image.
- Display: Tkinter at ~30 fps. Always shows the freshest raw frame; the
  last annotation overlay is BLENDED on top (so game motion stays fluid
  even when detections lag a bit).

Usage:
    python tools/live_view.py [--annot-fps 3] [--conf 0.85] [--serial NAME]

Close the window or Ctrl-C to quit.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
import tkinter as tk
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageTk

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "tools"))
os.environ.setdefault("ORT_LOGGING_LEVEL", "3")

from bsbot.vision.state_finder import StateFinder  # noqa: E402
from debug_overlay import compute_detections, draw_from_detections, load_detectors  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("live_view")
TEMPLATES_DIR = REPO_ROOT / "src" / "bsbot" / "data" / "state_templates"


class LiveView:
    def __init__(self, annot_fps: float, conf: float, max_width: int,
                 serial: str | None = None):
        self.annot_interval = 1.0 / max(annot_fps, 0.1)
        self.conf = conf
        self.max_width = max_width
        self.serial = serial  # scrcpy device serial; None = first device
        self.state_finder = StateFinder(TEMPLATES_DIR)
        self.detectors = load_detectors()

        # Shared state
        self._latest_raw: np.ndarray | None = None  # BGR
        self._latest_raw_lock = threading.Lock()
        # Last DETECTIONS (lightweight dict from compute_detections). Redrawn
        # on every fresh raw frame for fluid motion + visible overlay.
        self._latest_detections: dict | None = None
        self._latest_detections_lock = threading.Lock()
        self._stop = threading.Event()

        # Tk UI
        self.root = tk.Tk()
        self.root.title("BrawlStar-Bot — live view (scrcpy)")
        self.root.geometry("1100x600")
        self.root.configure(background="black")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.label = tk.Label(self.root, bg="black")
        self.label.pack(fill=tk.BOTH, expand=True)
        self._photo: ImageTk.PhotoImage | None = None

        # Background workers
        self._scrcpy_client = None
        threading.Thread(target=self._scrcpy_loop, daemon=True).start()
        threading.Thread(target=self._annotate_loop, daemon=True).start()
        self.root.after(33, self._refresh_ui)

    # ---------------------------------------------------- scrcpy capture

    def _scrcpy_loop(self) -> None:
        import scrcpy
        log.info("Starting scrcpy client (serial=%r)…", self.serial or "<first>")
        # Lower max_width + lower bitrate to reduce scrcpy encode/decode
        # buffer latency (~100-300ms saved at native 2560x1440 8Mbps).
        self._scrcpy_client = scrcpy.Client(
            device=self.serial if self.serial else None,
            max_width=1280, bitrate=4_000_000, max_fps=60,
            block_frame=False, connection_timeout=5000,
        )

        def on_frame(frame):
            if frame is None:
                return
            with self._latest_raw_lock:
                self._latest_raw = frame

        self._scrcpy_client.add_listener(scrcpy.EVENT_FRAME, on_frame)
        try:
            self._scrcpy_client.start(threaded=True)
        except Exception as exc:
            log.exception("scrcpy start failed: %s", exc)
            self._stop.set()
            return
        log.info("scrcpy streaming")
        # Just keep the thread alive; scrcpy runs in its own threads.
        while not self._stop.is_set():
            time.sleep(0.5)
        try:
            self._scrcpy_client.stop()
        except Exception:
            pass

    # ---------------------------------------------------- annotation

    def _annotate_loop(self) -> None:
        """Compute detections + annotation OVERLAY only (drawn separately on
        the latest raw frame during display). Stored as a transparent-ish
        overlay; for simplicity we keep the full annotated image and use it
        as a snapshot for display when raw + annotation timestamps drift."""
        n = 0
        while not self._stop.is_set():
            t0 = time.monotonic()
            with self._latest_raw_lock:
                frame = self._latest_raw
            if frame is None:
                time.sleep(0.1)
                continue
            try:
                det = compute_detections(frame, self.state_finder, self.detectors,
                                         conf_thresh=self.conf)
                with self._latest_detections_lock:
                    self._latest_detections = det
                n += 1
                if n % 15 == 0:
                    log.info("%d annot — state=%s", n, det.get("state"))
            except Exception:
                log.exception("annotate fail")
            elapsed = time.monotonic() - t0
            if elapsed < self.annot_interval:
                time.sleep(self.annot_interval - elapsed)

    # ---------------------------------------------------- UI refresh

    def _refresh_ui(self) -> None:
        if self._stop.is_set():
            return
        try:
            # Show the freshest raw frame for fluid motion. Redraw the
            # (cached) detection overlay on it each refresh.
            with self._latest_raw_lock:
                raw = self._latest_raw
            with self._latest_detections_lock:
                det = self._latest_detections
            if raw is None:
                img = None
            elif det is None:
                img = raw  # no overlay yet
            else:
                img, _info = draw_from_detections(raw, det)
            if img is not None:
                wnd_w = max(50, self.label.winfo_width())
                wnd_h = max(50, self.label.winfo_height())
                ih, iw = img.shape[:2]
                scale = min(wnd_w / iw, wnd_h / ih)
                new_w = max(1, int(iw * scale))
                new_h = max(1, int(ih * scale))
                rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
                pil = Image.fromarray(rgb).resize((new_w, new_h), Image.BILINEAR)
                self._photo = ImageTk.PhotoImage(pil)
                self.label.configure(image=self._photo)
        except Exception:
            log.exception("ui refresh failed")
        self.root.after(33, self._refresh_ui)  # ~30 fps display

    def _on_close(self) -> None:
        log.info("Closing…")
        self._stop.set()
        self.root.after(50, self.root.destroy)

    def run(self) -> None:
        self.root.mainloop()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--annot-fps", type=float, default=3.0,
                   help="annotation (ONNX) rate; lower = less CPU")
    p.add_argument("--conf", type=float, default=0.85)
    p.add_argument("--max-width", type=int, default=1280, help="(unused)")
    p.add_argument("--serial", type=str, default=None,
                   help="ADB device serial (e.g. 'emulator-5554' for BlueStacks)")
    args = p.parse_args()
    LiveView(args.annot_fps, args.conf, args.max_width, args.serial).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
