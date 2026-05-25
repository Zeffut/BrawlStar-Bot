"""CaptureWorker — pulls frames from py-scrcpy-client into a LatestSlot.

scrcpy starts its own internal threads to decode H.264 from the phone. We
register a frame callback that just writes the latest frame into our
LatestSlot (drop-old semantics), so the VisionWorker always reads the
freshest frame.
"""
from __future__ import annotations

import logging
import threading
import time

import numpy as np

from bsbot.buses import LatestSlot

logger = logging.getLogger(__name__)


class CaptureWorker(threading.Thread):
    """Run a scrcpy client and push frames into `frame_slot`.

    The actual frame-decoding loop lives inside scrcpy.Client; this thread's
    only job is to start scrcpy, monitor for disconnects, and stop cleanly
    when `stop_event` is set.
    """

    def __init__(
        self,
        frame_slot: LatestSlot[np.ndarray],
        stop_event: threading.Event,
        device_serial: str = "",
        max_width: int = 1280,
        bitrate: int = 8_000_000,
        max_fps: int = 60,
        connection_timeout_ms: int = 5000,
    ):
        super().__init__(name="CaptureWorker", daemon=True)
        self.frame_slot = frame_slot
        self.stop_event = stop_event
        self.device_serial = device_serial
        self.max_width = max_width
        self.bitrate = bitrate
        self.max_fps = max_fps
        self.connection_timeout_ms = connection_timeout_ms

        self._client = None  # type: ignore[assignment]
        self._frames_received = 0
        self._last_frame_time = 0.0

    @property
    def client(self):
        """Return the underlying scrcpy.Client (or None if not started)."""
        return self._client

    @property
    def frames_received(self) -> int:
        return self._frames_received

    @property
    def last_frame_age_s(self) -> float:
        if self._last_frame_time == 0.0:
            return float("inf")
        return time.monotonic() - self._last_frame_time

    def run(self) -> None:
        # Imported lazily so tests/users without scrcpy installed can still import the module.
        import scrcpy

        logger.info(
            "CaptureWorker starting (serial=%r, max_width=%d, fps=%d)",
            self.device_serial or "<first>",
            self.max_width,
            self.max_fps,
        )

        device_arg = self.device_serial if self.device_serial else None
        self._client = scrcpy.Client(
            device=device_arg,
            max_width=self.max_width,
            bitrate=self.bitrate,
            max_fps=self.max_fps,
            block_frame=False,  # we want None frames too so we can detect freezes
            stay_awake=True,
            connection_timeout=self.connection_timeout_ms,
        )

        def on_frame(frame):
            # scrcpy gives us a numpy BGR frame or None (on disconnect/init).
            if frame is None:
                return
            self._frames_received += 1
            self._last_frame_time = time.monotonic()
            if self._frames_received == 1:
                logger.info("CaptureWorker: first frame received! shape=%s", frame.shape)
            elif self._frames_received % 600 == 0:
                logger.info("CaptureWorker: %d frames received", self._frames_received)
            self.frame_slot.set(frame)

        self._client.add_listener(scrcpy.EVENT_FRAME, on_frame)

        try:
            # threaded=True so .start() returns immediately and scrcpy runs in
            # its own thread. We then sit on stop_event.
            self._client.start(threaded=True)
        except Exception as exc:
            logger.exception("scrcpy failed to start: %s", exc)
            self.stop_event.set()
            return

        # Monitor loop: stop when asked, also detect stale stream.
        while not self.stop_event.is_set():
            if self._frames_received > 0 and self.last_frame_age_s > 5.0:
                logger.warning("scrcpy stream stale (%.1fs since last frame)", self.last_frame_age_s)
                # No automatic reconnect yet; surface to main thread.
            time.sleep(0.5)

        self.stop()
        logger.info("CaptureWorker stopped (received %d frames)", self._frames_received)

    def stop(self) -> None:
        if self._client is not None:
            try:
                self._client.stop()
            except Exception as exc:
                logger.debug("scrcpy stop raised %s (ignored)", exc)
