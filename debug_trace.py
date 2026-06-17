# debug_trace.py
"""Ultra-detailed debug tracing: structured JSONL events + reused-frame captures.

Every decision point can emit `trace(event, data=..., frame=..., crop=...)`. The
hot path only ENQUEUES; a daemon writer thread does the JPEG encode + disk write.
Captures REUSE an already-decoded frame (the live stream's get_frame() or a
caller-passed frame) — they NEVER trigger a fresh `adb screencap` (that contends
with the shared screenrecord and stalls the live feed / breaks the grind).

Best-effort: any exception is swallowed; the bot is never impacted.

Config (env):
    BOT_DEBUG_TRACE = off | on (default) | verbose
    TRACE_CAPTURE_MIN_INTERVAL_S (default 3.0)
    TRACE_CAPTURE_MAX_MB (default 400)
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from pathlib import Path

log = logging.getLogger("trace")

_TRACE_DIR = Path(__file__).resolve().parent / "logs" / "trace"
_CAPTURE_DIR = _TRACE_DIR / "captures"


def _envf(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except Exception:
        return float(default)


_MODE = (os.environ.get("BOT_DEBUG_TRACE", "on") or "on").strip().lower()
_MIN_INTERVAL = _envf("TRACE_CAPTURE_MIN_INTERVAL_S", 3.0)
_MAX_BYTES = int(_envf("TRACE_CAPTURE_MAX_MB", 400) * 1024 * 1024)

_Q: "queue.Queue | None" = None
_WRITER: "threading.Thread | None" = None
_START_LOCK = threading.Lock()
_last_capture_at: dict[str, float] = {}
_seq = 0
_seq_lock = threading.Lock()


def _safe(s) -> str:
    return "".join(c if (c.isalnum() or c in "-_") else "_" for c in str(s))[:40]


def _summary(data) -> str:
    if not data:
        return ""
    try:
        return " ".join(f"{k}={v}" for k, v in list(data.items())[:6])
    except Exception:
        return ""


_account_cache: dict = {"tag": None, "at": 0.0}


def _account():
    try:
        now = time.time()
        if now - _account_cache["at"] > 60.0:
            import device
            tag, _ = device.account_override()
            _account_cache["tag"] = tag
            _account_cache["at"] = now
        return _account_cache["tag"]
    except Exception:
        return _account_cache.get("tag")


def _latest_stream_frame():
    """Latest decoded stream frame as an RGB ndarray, or None. Never screencaps."""
    try:
        import cv2
        import screen_capture
        rec = screen_capture.get()
        if rec is None:
            return None
        f = rec.get_frame()
        age = rec.get_frame_age()
        if f is not None and age is not None and age < 6.0:
            return cv2.cvtColor(f, cv2.COLOR_BGR2RGB)
    except Exception:
        pass
    return None


def _ensure_writer() -> None:
    global _Q, _WRITER
    if _WRITER is not None and _WRITER.is_alive():
        return
    with _START_LOCK:
        if _WRITER is not None and _WRITER.is_alive():
            return
        _TRACE_DIR.mkdir(parents=True, exist_ok=True)
        _CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
        _Q = queue.Queue(maxsize=32)
        _WRITER = threading.Thread(target=_writer_loop, name="debug-trace", daemon=True)
        _WRITER.start()


def trace(event, data=None, frame=None, crop=None, capture=True,
          level="info", tag=None, force_capture=False) -> None:
    """Record a debug event (+ optional reused-frame capture). Best-effort."""
    if _MODE == "off":
        return
    try:
        try:
            lvl = level if level in ("debug", "info", "warning", "error") else "info"
            getattr(log, lvl)("%s %s", event, _summary(data))
        except Exception:
            pass
        if capture and frame is None:
            frame = _latest_stream_frame()  # reused only; never a screencap
        rec = {
            "ts": int(time.time() * 1000),
            "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "event": event,
            "tag": tag,
            "account": _account(),
            "data": data or {},
        }
        _ensure_writer()
        item = (rec, frame if capture else None, crop if capture else None,
                bool(force_capture))
        try:
            _Q.put_nowait(item)
        except queue.Full:
            try:
                _Q.get_nowait()
                _Q.task_done()
                _Q.put_nowait(item)
            except Exception:
                pass
    except Exception:
        try:
            log.debug("trace() failed", exc_info=True)
        except Exception:
            pass


def _write_jpeg(img, path: Path, quality: int = 70) -> bool:
    try:
        import numpy as np
        from PIL import Image
        path.parent.mkdir(parents=True, exist_ok=True)
        pil = Image.fromarray(img) if isinstance(img, np.ndarray) else img
        pil.convert("RGB").save(str(path), format="JPEG", quality=quality)
        return True
    except Exception:
        try:
            log.debug("trace jpeg write failed", exc_info=True)
        except Exception:
            pass
        return False


def _append_jsonl(rec: dict) -> None:
    day = time.strftime("%Y%m%d")
    path = _TRACE_DIR / f"events-{day}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")


def _enforce_retention() -> None:
    try:
        files = sorted(_CAPTURE_DIR.glob("*.jpg"), key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in files)
        i = 0
        while total > _MAX_BYTES and i < len(files):
            try:
                sz = files[i].stat().st_size
                files[i].unlink(missing_ok=True)
                total -= sz
            except Exception:
                pass
            i += 1
    except Exception:
        pass


def _writer_loop() -> None:
    while True:
        try:
            rec, frame, crop, force = _Q.get()
        except Exception:
            continue
        try:
            cap_name = None
            if frame is not None:
                ev = rec["event"]
                now = time.time()
                throttled = (not force and _MODE != "verbose"
                             and (now - _last_capture_at.get(ev, 0.0)) < _MIN_INTERVAL)
                if throttled:
                    rec["capture_throttled"] = True
                else:
                    global _seq
                    with _seq_lock:
                        _seq += 1
                        seq = _seq
                    cap_name = f"{_safe(ev)}_{rec['ts']}_{seq}.jpg"
                    if _write_jpeg(frame, _CAPTURE_DIR / cap_name):
                        rec["capture"] = cap_name
                        _last_capture_at[ev] = now
                        if crop is not None:
                            crop_name = f"{_safe(ev)}_{rec['ts']}_{seq}.crop.jpg"
                            if _write_jpeg(crop, _CAPTURE_DIR / crop_name):
                                rec["crop"] = crop_name
                    else:
                        cap_name = None
            _append_jsonl(rec)
            if cap_name is not None:
                _enforce_retention()
        except Exception:
            try:
                log.debug("trace writer record failed", exc_info=True)
            except Exception:
                pass
        finally:
            try:
                _Q.task_done()
            except Exception:
                pass


def _flush(timeout: float = 3.0) -> None:
    """Block until queued records are written (test helper only)."""
    deadline = time.time() + timeout
    while _Q is not None and getattr(_Q, "unfinished_tasks", 0) > 0 and time.time() < deadline:
        time.sleep(0.01)


def _reset_for_tests() -> None:
    """Clear throttle memory between tests (test helper only).
    Intentionally does NOT reset _Q/_WRITER: a single persistent daemon writer
    is reused across tests; the module global _Q and the thread's closure
    reference the same queue object."""
    _last_capture_at.clear()
