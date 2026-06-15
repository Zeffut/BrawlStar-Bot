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
import queue
import subprocess
import threading
import time

import numpy as np

log = logging.getLogger(__name__)


def _iter_nal_units(buf: bytes):
    """Yield (nal_type, start_offset) for each Annex-B NAL start code in buf.

    Start codes are 00 00 01 or 00 00 00 01; the NAL type is the low 5 bits
    of the byte right after the start code. Used to locate SPS(7)/PPS(8) so a
    late-joining MSE viewer can be primed with the codec config."""
    n = len(buf)
    i = 0
    while i < n - 4:
        if buf[i] == 0 and buf[i + 1] == 0:
            if buf[i + 2] == 1:
                yield buf[i + 3] & 0x1F, i
                i += 3
                continue
            if buf[i + 2] == 0 and buf[i + 3] == 1 and i < n - 5:
                yield buf[i + 4] & 0x1F, i
                i += 4
                continue
        i += 1


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
        # --- raw H264 fan-out (live streaming passthrough) ---------
        # Subscribers (the worker_link stream task) get the raw Annex-B byte
        # chunks straight off screenrecord — no decode, no JPEG re-encode.
        # Populated only while ≥1 subscriber is attached, so there's zero
        # overhead when nobody is watching.
        self._raw_subs: set[queue.Queue] = set()
        self._raw_lock = threading.Lock()
        self._codec_init: bytes | None = None    # cached SPS+PPS (Annex-B)
        self._sps: bytes | None = None
        self._pps: bytes | None = None
        self._force_respawn = False              # set on first viewer → fresh keyframe
        # Async decode (2026-06-15): the capture thread only reads stdout + fans
        # raw chunks to live viewers; a SEPARATE thread decodes frames for the
        # bot's vision. This stops the grind's heavy PyAV decode from throttling
        # the live-stream fan-out (the "feed cuts when the bot is busy" cause).
        # Drop-oldest on a full queue → worst case last_frame goes briefly stale
        # and _grab falls back to screencap (the existing behaviour).
        self._decode_q: "queue.Queue[bytes]" = queue.Queue(maxsize=60)
        self._decode_thread: "threading.Thread | None" = None

    # ---- lifecycle -----------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        # Probe device resolution once so we can compute the recorder size.
        self._device_w, self._device_h = self._probe_resolution()
        self._decode_thread = threading.Thread(target=self._decode_loop, daemon=True,
                                               name=f"screenrec-decode-{self.serial}")
        self._decode_thread.start()
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

    # ---- raw H264 fan-out (streaming) ----------------------------

    def subscribe_raw(self) -> queue.Queue:
        """Attach a live-stream viewer. Returns a Queue of raw H264 byte
        chunks (Annex-B). On the FIRST subscriber we force a screenrecord
        respawn so a fresh keyframe (SPS/PPS + IDR) is emitted within ~1s —
        screenrecord's default I-frame interval is ~10s, so without this a
        viewer could wait that long for the first decodable frame."""
        q: queue.Queue = queue.Queue(maxsize=240)  # ~a few seconds of chunks
        with self._raw_lock:
            first = not self._raw_subs
            self._raw_subs.add(q)
        if first:
            # The capture loop polls this flag every read (~ms) and respawns
            # once — no terminate() here (that would double-respawn).
            self._force_respawn = True
        return q

    def unsubscribe_raw(self, q: queue.Queue) -> None:
        with self._raw_lock:
            self._raw_subs.discard(q)

    def get_codec_init(self) -> bytes | None:
        """Cached SPS+PPS (Annex-B) so a late viewer can be primed."""
        return self._codec_init

    def _fan_out_raw(self, chunk: bytes) -> None:
        with self._raw_lock:
            subs = list(self._raw_subs)
        for q in subs:
            try:
                q.put_nowait(chunk)
            except queue.Full:
                pass  # slow viewer — drop (cloud also drops stale)

    def _scan_codec_init(self, chunk: bytes) -> None:
        """Track SPS/PPS NALs so we can prime late viewers. Cheap: only runs
        while a viewer is attached. We keep a small tail to survive start
        codes split across chunk boundaries."""
        buf = (self._init_scan_tail + chunk) if getattr(self, "_init_scan_tail", b"") else chunk
        marks = list(_iter_nal_units(buf))
        for idx, (ntype, off) in enumerate(marks):
            if ntype not in (7, 8):
                continue
            end = marks[idx + 1][1] if idx + 1 < len(marks) else len(buf)
            nal = buf[off:end]
            if ntype == 7:
                self._sps = nal
            elif ntype == 8:
                self._pps = nal
        if self._sps and self._pps:
            self._codec_init = self._sps + self._pps
        # Keep a short tail (start codes are ≤4 bytes; keep a bit more slack).
        self._init_scan_tail = buf[-8:]

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
        # This thread now ONLY reads stdout + fans raw chunks to live viewers +
        # hands chunks to the decode thread (_decode_loop). Keeping the PyAV decode
        # OFF this thread means a heavy decode can't delay the next stdout read, so
        # the live-stream fan-out stays smooth even while the bot grinds.
        # Exponential backoff for the device-disconnected case: when
        # the cable is pulled, screenrecord exits immediately (no
        # device). Without a backoff we'd respawn the process tens of
        # thousands of times per second, burning CPU for nothing.
        backoff_s = 2
        spawn_started_at = 0.0
        consecutive_fast_deaths = 0
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
                forced = False
                while not self._stop:
                    chunk = self._proc.stdout.read(8192)
                    if not chunk:
                        break
                    # Raw H264 passthrough for live streaming: fan the bytes
                    # straight to viewers + track SPS/PPS. Only active while a
                    # viewer is attached (zero cost otherwise).
                    if self._raw_subs:
                        self._scan_codec_init(chunk)
                        self._fan_out_raw(chunk)
                    # First viewer just attached → respawn for a fresh keyframe.
                    # MUST terminate the current proc first: otherwise _proc.wait()
                    # below blocks 5s on a live process and the orphaned
                    # screenrecord conflicts with the new one (one per device).
                    if self._force_respawn:
                        self._force_respawn = False
                        log.info("screenrec: forced respawn for fresh stream keyframe")
                        forced = True
                        try:
                            self._proc.terminate()
                        except Exception:
                            pass
                        break
                    # Hand the chunk to the decode thread. Drop-oldest if it's
                    # momentarily behind — the bot only needs the latest frame, and
                    # never block the capture/fan-out loop on the decode queue.
                    try:
                        self._decode_q.put_nowait(chunk)
                    except queue.Full:
                        try:
                            self._decode_q.get_nowait()
                        except queue.Empty:
                            pass
                        try:
                            self._decode_q.put_nowait(chunk)
                        except queue.Full:
                            pass
                # Subprocess ended (time limit or crash). Loop respawns.
                self._proc.wait(timeout=5)
                self.alive = False
                # A deliberate respawn (first stream viewer → fresh keyframe) is
                # NOT a device death: respawn immediately, don't count it toward
                # the fast-death disable, don't back off.
                if forced:
                    backoff_s = 2
                    consecutive_fast_deaths = 0
                    continue
                run_duration = time.time() - spawn_started_at
                # If screenrecord lasted at least 5s we consider the
                # device healthy — reset the backoff. Otherwise the
                # device is unreachable; grow the wait up to 30s.
                if run_duration >= 5:
                    backoff_s = 2
                    consecutive_fast_deaths = 0
                    log.info("screenrec respawn after %ds (frames=%d)",
                             self.time_limit_s, self.frames_decoded)
                else:
                    consecutive_fast_deaths += 1
                    log.warning("screenrec died after %.1fs (frames=%d) — "
                                "device likely disconnected, backoff %ds",
                                run_duration, self.frames_decoded, backoff_s)
                    # Permanent disable after 10 fast deaths in a row:
                    # the device probably doesn't support screenrecord
                    # at all (BlueStacks and most emulators don't have
                    # a real screen). Fall back to adb screencap polls.
                    if consecutive_fast_deaths >= 10:
                        log.warning("screenrec gave up after %d fast deaths — "
                                    "ScreenRecorder disabled, falling back to "
                                    "adb screencap polls",
                                    consecutive_fast_deaths)
                        self._stop = True
                        return
                    time.sleep(backoff_s)
                    backoff_s = min(backoff_s * 2, 30)
            except Exception:
                log.exception("screenrec loop crashed — restarting in 5s")
                self.alive = False
                time.sleep(5)

    def _decode_loop(self) -> None:
        """Decode H264 chunks (fed by _loop's queue) into BGR frames for the bot's
        vision. Runs on its OWN thread so the capture/fan-out loop is never blocked
        by decode — keeps the live stream smooth during heavy gameplay. If this
        thread falls behind or dies, last_frame just goes stale and _grab falls
        back to an adb screencap (the existing behaviour)."""
        import av
        codec = av.CodecContext.create("h264", "r")
        while not self._stop:
            try:
                chunk = self._decode_q.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                packets = codec.parse(chunk)
            except Exception:
                log.debug("packet parse failed", exc_info=True)
                continue
            for pkt in packets:
                try:
                    frames = codec.decode(pkt)
                except Exception:
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
            # NOTE (2026-06-15): do NOT lower max_width here. game_api._grab()
            # PREFERS this stream frame for state() (speed), and state_finder's
            # templates/regions are calibrated for the 1280-wide stream. Dropping
            # to 960x432 made state_finder misclassify the lobby/menus as
            # match/brawler_selection → the grind navigated erratically and never
            # launched a match. The panel video lag must be fixed elsewhere (e.g.
            # a separate downscaled panel stream), NOT by shrinking this shared one.
            r = ScreenRecorder(serial)
            r.start()
            _INSTANCE = r
            return r
        except Exception:
            log.exception("failed to start ScreenRecorder")
            return None
