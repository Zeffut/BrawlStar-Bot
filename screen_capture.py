"""High-fps screen capture via `adb screenrecord` H264 pipe.

Replaces the broken scrcpy-client + PyAV>=10 chain. Spawns a long-lived
`adb exec-out screenrecord --output-format=h264 -` subprocess and
decodes the H264 byte stream with PyAV in a daemon thread. The most
recent decoded BGR ndarray is exposed via `get_frame()`.

Benefits vs `adb screencap -p`:
  - 25-30 fps sustained (vs 1-2 fps with PNG screencap)
  - 10x less data on the wire (H264 vs PNG)
  - No PNG encode on the phone + no PNG decode on the host

Auto-respawns the subprocess (screenrecord caps at 180s by default,
and crashes occasionally on some devices).
"""
from __future__ import annotations

import logging
import subprocess
import threading
import time
from typing import Any

import numpy as np

log = logging.getLogger(__name__)


class ScreenRecorder:
    """Continuous H264 screen stream from an Android device via ADB.

    Run as a daemon thread; one per device.
    """

    def __init__(self, serial: str, *,
                 max_width: int = 1280,
                 bitrate: int = 8_000_000,
                 time_limit_s: int = 175,
                 ) -> None:
        self.serial = serial
        self.max_width = max_width
        self.bitrate = bitrate
        self.time_limit_s = time_limit_s
        self.last_frame: np.ndarray | None = None     # BGR
        self.last_frame_time: float = 0.0
        self.frames_decoded: int = 0
        self._lock = threading.Lock()
        self._stop = False
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None
        self.alive: bool = False
        self._device_w: int | None = None
        self._device_h: int | None = None

    # ---- lifecycle -----------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        # Probe device resolution once so we can compute the recorder size.
        self._device_w, self._device_h = self._probe_resolution()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                         name=f"screenrec-{self.serial}")
        self._thread.start()
        log.info("ScreenRecorder started for %s (%sx%s)",
                 self.serial, self._device_w, self._device_h)

    def stop(self) -> None:
        self._stop = True
        if self._proc is not None:
            try: self._proc.terminate()
            except Exception: pass
        self.alive = False

    # ---- read API ------------------------------------------------

    def get_frame(self) -> np.ndarray | None:
        """Return the latest decoded BGR frame, or None until first frame."""
        with self._lock:
            return None if self.last_frame is None else self.last_frame

    def get_frame_age(self) -> float | None:
        if self.last_frame_time == 0:
            return None
        return time.time() - self.last_frame_time

    # ---- internal ------------------------------------------------

    def _probe_resolution(self) -> tuple[int, int]:
        """`adb shell wm size` → 'Physical size: 1080x2340'."""
        try:
            out = subprocess.run(
                ["adb", "-s", self.serial, "shell", "wm", "size"],
                capture_output=True, text=True, timeout=5, check=False,
            ).stdout
            for line in out.splitlines():
                if "size:" in line.lower():
                    spec = line.split(":")[-1].strip()
                    w, h = spec.split("x")
                    return int(w), int(h)
        except Exception:
            pass
        return 1080, 2340  # Mi 9T default landscape rotated

    def _record_size(self) -> str:
        """Pick a recording size respecting --size constraints.

        screenrecord requires WxH multiples of 16 typically. We use the
        landscape orientation (BS forces landscape) capped at max_width.
        """
        # Aspect ratio of the physical screen (landscape).
        w, h = max(self._device_w or 1080, self._device_h or 2340), \
               min(self._device_w or 1080, self._device_h or 2340)
        target_w = min(self.max_width, w)
        # Round to multiple of 16.
        target_w = (target_w // 16) * 16
        target_h = int(target_w * h / w)
        target_h = (target_h // 16) * 16
        return f"{target_w}x{target_h}"

    def _loop(self) -> None:
        import av
        codec = av.CodecContext.create("h264", "r")
        # Exponential backoff for the device-disconnected case: when
        # the cable is pulled, screenrecord exits immediately (no
        # device). Without a backoff we'd respawn the process tens of
        # thousands of times per second, burning CPU for nothing.
        backoff_s = 2
        spawn_started_at = 0.0
        while not self._stop:
            try:
                size = self._record_size()
                cmd = ["adb", "-s", self.serial, "exec-out", "screenrecord",
                       "--output-format=h264",
                       f"--bit-rate={self.bitrate}",
                       f"--size={size}",
                       f"--time-limit={self.time_limit_s}",
                       "-"]
                log.info("screenrec spawn: %s", " ".join(cmd[-5:]))
                spawn_started_at = time.time()
                self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                              stderr=subprocess.DEVNULL)
                self.alive = True
                while not self._stop:
                    chunk = self._proc.stdout.read(8192)
                    if not chunk:
                        break
                    try:
                        packets = codec.parse(chunk)
                    except Exception:
                        log.debug("packet parse failed", exc_info=True)
                        continue
                    for pkt in packets:
                        try:
                            frames = codec.decode(pkt)
                        except (av.error.InvalidDataError, Exception):
                            continue
                        for f in frames:
                            try:
                                arr = f.to_ndarray(format="bgr24")
                            except Exception:
                                continue
                            with self._lock:
                                self.last_frame = arr
                                self.last_frame_time = time.time()
                                self.frames_decoded += 1
                # Subprocess ended (time limit or crash). Loop respawns.
                self._proc.wait(timeout=5)
                self.alive = False
                run_duration = time.time() - spawn_started_at
                # If screenrecord lasted at least 5s we consider the
                # device healthy — reset the backoff. Otherwise the
                # device is unreachable; grow the wait up to 30s.
                if run_duration >= 5:
                    backoff_s = 2
                    log.info("screenrec respawn after %ds (frames=%d)",
                             self.time_limit_s, self.frames_decoded)
                else:
                    log.warning("screenrec died after %.1fs (frames=%d) — "
                                "device likely disconnected, backoff %ds",
                                run_duration, self.frames_decoded, backoff_s)
                    time.sleep(backoff_s)
                    backoff_s = min(backoff_s * 2, 30)
            except Exception:
                log.exception("screenrec loop crashed — restarting in 5s")
                self.alive = False
                time.sleep(5)


# ---- module-level singleton (one device per host) -----------------

_INSTANCE: ScreenRecorder | None = None
_INIT_LOCK = threading.Lock()


def get(serial: str | None = None) -> ScreenRecorder | None:
    """Return the running ScreenRecorder, lazily starting it on first call.

    `serial` defaults to `device.adb_serial()`.
    """
    global _INSTANCE
    if _INSTANCE is not None:
        return _INSTANCE
    with _INIT_LOCK:
        if _INSTANCE is not None:
            return _INSTANCE
        if serial is None:
            try:
                import device as _device
                serial = _device.adb_serial()
            except Exception:
                log.warning("no serial available for ScreenRecorder")
                return None
        try:
            r = ScreenRecorder(serial)
            r.start()
            _INSTANCE = r
            return r
        except Exception:
            log.exception("failed to start ScreenRecorder")
            return None
