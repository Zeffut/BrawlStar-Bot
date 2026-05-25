"""Logging setup — rich console + structured JSONL session log."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path

try:
    from rich.logging import RichHandler
    HAS_RICH = True
except ImportError:
    HAS_RICH = False


class JsonlFileHandler(logging.Handler):
    """Append every LogRecord as a JSON line to a file."""

    def __init__(self, path: Path):
        super().__init__()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fp = open(path, "a", encoding="utf-8")

    def emit(self, record: logging.LogRecord) -> None:
        try:
            payload = {
                "ts": datetime.utcfromtimestamp(record.created).isoformat() + "Z",
                "level": record.levelname,
                "logger": record.name,
                "msg": record.getMessage(),
            }
            if record.exc_info:
                payload["exc"] = self.format(record).splitlines()[-5:]
            self._fp.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self._fp.flush()
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        try:
            self._fp.close()
        except Exception:
            pass
        super().close()


def setup_logging(level: str = "INFO", session_dir: Path | None = None) -> Path | None:
    """Configure root logger with console + optional JSONL file.

    Returns the path of the JSONL log file (or None if `session_dir` not set).
    """
    root = logging.getLogger()
    root.setLevel(level.upper())
    # Clear pre-existing handlers (pytest etc.).
    for h in list(root.handlers):
        root.removeHandler(h)

    if HAS_RICH:
        console_handler = RichHandler(rich_tracebacks=True, markup=False)
        console_handler.setFormatter(logging.Formatter("%(message)s"))
    else:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
    root.addHandler(console_handler)

    log_file = None
    if session_dir is not None:
        log_file = session_dir / f"session-{datetime.now().strftime('%Y%m%d-%H%M%S')}.jsonl"
        root.addHandler(JsonlFileHandler(log_file))
    return log_file


def default_session_dir() -> Path:
    return Path(os.path.expanduser("~/.bsbot/logs"))
