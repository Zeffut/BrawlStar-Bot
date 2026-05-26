"""Resolve which ADB device serial the bot talks to.

Resolution order:
  1. $BSBOT_ADB_SERIAL env var (explicit override)
  2. `cfg/device.toml` → serial = "…"
  3. First "device" (online) returned by `adb devices` — covers the
     common case of a single phone over USB or a single emulator
"""
from __future__ import annotations

import logging
import os
import subprocess
import tomllib
from pathlib import Path

log = logging.getLogger(__name__)

CFG_PATH = Path(__file__).resolve().parent / "cfg" / "device.toml"
DEFAULT_FALLBACK = "emulator-5554"


def _from_env() -> str | None:
    v = os.environ.get("BSBOT_ADB_SERIAL")
    return v.strip() if v else None


def _from_cfg() -> str | None:
    if not CFG_PATH.exists():
        return None
    try:
        with CFG_PATH.open("rb") as f:
            data = tomllib.load(f)
        return (data.get("serial") or "").strip() or None
    except Exception:
        log.exception("device.toml parse failed")
        return None


def _from_adb() -> str | None:
    try:
        out = subprocess.check_output(
            ["adb", "devices"], stderr=subprocess.DEVNULL, timeout=5
        ).decode("utf-8", errors="replace")
    except Exception:
        return None
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("List of"):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            return parts[0]
    return None


_cached: str | None = None


def adb_serial() -> str:
    """Return the device serial the bot should use. Cached after first call."""
    global _cached
    if _cached:
        return _cached
    val = _from_env() or _from_cfg() or _from_adb() or DEFAULT_FALLBACK
    log.info("adb device serial resolved → %s", val)
    _cached = val
    return val


def account_override() -> tuple[str | None, str | None]:
    """Return (tag, name) override from cfg/device.toml if set, else (None, None).

    Useful when brawlace.com (or other tag validation services) is
    blocked by Cloudflare or unreachable. The user can hardcode their
    tag in cfg/device.toml to skip auto-detection entirely:

        serial = "22002522"
        account_tag = "QPRCQ9BV2"
        account_name = "zeffut5.0"
    """
    if not CFG_PATH.exists():
        return None, None
    try:
        with CFG_PATH.open("rb") as f:
            data = tomllib.load(f)
        tag = (data.get("account_tag") or "").strip().lstrip("#").upper() or None
        name = (data.get("account_name") or "").strip() or None
        return tag, name
    except Exception:
        log.exception("device.toml account override read failed")
        return None, None
