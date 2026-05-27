"""Configurable Telegram alerts.

Each event has a toggle (`enabled`) and a `template` string with Python
str.format placeholders. Settings live in `cfg/alerts.toml` and can be
edited at runtime — the file is re-read before each send so changes
take effect immediately without restarting the bot.
"""
from __future__ import annotations

import logging
import threading
import tomllib
from pathlib import Path
from typing import Any

log = logging.getLogger("alerts")

CFG_PATH = Path(__file__).resolve().parent / "cfg" / "alerts.toml"
CFG_EXAMPLE = Path(__file__).resolve().parent / "cfg" / "alerts.toml.example"


def _ensure_cfg_exists() -> None:
    """Create cfg/alerts.toml from the example if missing.

    The user-edited file is gitignored so it survives auto-deploys.
    The example ships in the repo with safe defaults.
    """
    if CFG_PATH.exists():
        return
    if CFG_EXAMPLE.exists():
        try:
            CFG_PATH.write_text(CFG_EXAMPLE.read_text())
            log.info("alerts.toml created from example")
        except Exception:
            log.exception("alerts.toml init from example failed")

_lock = threading.Lock()
_cache: dict | None = None
_cache_mtime: float = 0.0


def _load() -> dict:
    """Reload config if the file changed on disk; return parsed dict."""
    global _cache, _cache_mtime
    _ensure_cfg_exists()
    with _lock:
        try:
            mtime = CFG_PATH.stat().st_mtime
        except FileNotFoundError:
            return _cache or {}
        if _cache is None or mtime != _cache_mtime:
            try:
                with CFG_PATH.open("rb") as f:
                    _cache = tomllib.load(f)
                _cache_mtime = mtime
            except Exception:
                log.exception("alerts.toml parse failed; keeping previous cache")
        return _cache or {}


def format_alert(event: str, **fields: Any) -> str | None:
    """Return the formatted alert text, or None if the event is disabled
    or has no template. Returning None means: don't send anything.

    Special-case for the `match` event: respects
    `[match.filter]` to skip alerts for specific results.
    """
    cfg = _load().get(event, {})
    if not cfg.get("enabled", False):
        return None
    if event == "match":
        result = fields.get("result", "")
        if not cfg.get("filter", {}).get(result, True):
            return None
    tpl = cfg.get("template")
    if not tpl:
        return None
    try:
        return tpl.format(**fields)
    except (KeyError, IndexError, ValueError) as exc:
        log.warning("alert format failed for %s: %s", event, exc)
        return None


def is_enabled(event: str) -> bool:
    return bool(_load().get(event, {}).get("enabled", False))
