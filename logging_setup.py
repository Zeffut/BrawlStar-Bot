"""Central logging configuration.

Loud-by-default for our own modules; silent for noisy third-party libs
(torch, onnxruntime, easyocr, uvicorn). Logs go to both stderr and a
rotating file in `logs/bot.log` so you can `tail -f` while running and
still have history after a crash.

Usage:
    from logging_setup import setup_logging
    setup_logging()             # call once near process start

Modules then do:
    import logging
    log = logging.getLogger(__name__)
    log.info("…")               # appears in both console and file
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path

# Loggers we own — kept at DEBUG level so anything we explicitly log is
# captured.
_OUR_LOGGERS = [
    "telegram_main", "stage_manager", "account_detect",
    "panel", "panel.app", "db", "alerts", "worker_pool",
    "host_bootstrap", "device", "cloud_sync",
    "bot",  # generic fallback
]

# Noisy libs — silenced unless something is wrong (WARNING+).
_NOISY = [
    "uvicorn", "uvicorn.access", "uvicorn.error",
    "fastapi", "asyncio", "torch", "easyocr",
    "onnxruntime", "PIL", "matplotlib", "urllib3", "requests",
]

LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "bot.log"

_FORMAT = "%(asctime)s.%(msecs)03d %(levelname)-7s %(name)-20s %(message)s"
_DATEFMT = "%H:%M:%S"


def setup_logging(level: int = logging.DEBUG) -> None:
    """Configure all loggers. Idempotent."""
    # Earlier code did `logging.disable(logging.CRITICAL)` to silence
    # 3rd-party noise — undo it so our loggers can emit again.
    logging.disable(logging.NOTSET)

    root = logging.getLogger()
    # Don't let root spam — our named loggers handle propagation.
    root.setLevel(logging.WARNING)
    # Clear any existing handlers (in case setup is called twice).
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    # 1. Console (stderr).
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.INFO)
    console.setFormatter(fmt)
    root.addHandler(console)

    # 2. Rotating file — 5 MB × 5 backups = ~25 MB max on disk.
    fh = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5,
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # 3. Our loggers — let everything through to handlers.
    for name in _OUR_LOGGERS:
        lg = logging.getLogger(name)
        lg.setLevel(logging.DEBUG)
        lg.disabled = False
        lg.propagate = True

    # 4. Noisy libs — keep them quiet.
    for name in _NOISY:
        lg = logging.getLogger(name)
        lg.setLevel(logging.WARNING)

    # Emit a marker so log files have a clear "session started" line.
    logging.getLogger("bot").info(
        "=== logging initialized (pid=%d, file=%s) ===",
        os.getpid(), LOG_FILE,
    )
