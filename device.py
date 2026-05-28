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
# Re-resolve the serial periodically — phones get unplugged, ADB
# reboots, sleeves get knocked. We don't want to cache a stale value
# forever.
_CACHE_TTL_S = 30.0


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
_cached_at: float = 0.0


def _cached_serial_alive(serial: str) -> bool:
    """Return True if `serial` still appears as 'device' in adb devices."""
    try:
        import time as _t
        out = subprocess.check_output(
            ["adb", "devices"], stderr=subprocess.DEVNULL, timeout=5
        ).decode("utf-8", errors="replace")
        for line in out.splitlines():
            parts = line.strip().split()
            if len(parts) >= 2 and parts[0] == serial and parts[1] == "device":
                return True
    except Exception:
        pass
    return False


class DeviceNotConnected(RuntimeError):
    """Raised when no ADB device is currently authorized."""


def adb_serial() -> str:
    """Return the device serial the bot should use.

    Re-resolves periodically (every 30 s) and re-resolves immediately
    if the previously cached serial is no longer reachable. Raises
    DeviceNotConnected if no device can be found — callers should
    treat that as 'wait for the phone to be plugged back in' instead
    of falling back to a phantom 'emulator-5554'.
    """
    import time as _t
    global _cached, _cached_at
    now = _t.time()
    # Env + cfg overrides win, but still validate they're reachable.
    forced = _from_env() or _from_cfg()
    if forced:
        if _cached == forced and (now - _cached_at) < _CACHE_TTL_S:
            return _cached
        if _cached_serial_alive(forced):
            if _cached != forced:
                log.info("adb device serial resolved → %s (forced)", forced)
            _cached, _cached_at = forced, now
            return forced
        # Forced but not reachable: still return it (caller will know
        # to surface the error), but don't cache so we re-check soon.
        raise DeviceNotConnected(f"forced serial {forced!r} not reachable")
    # No override: check cache validity.
    if _cached and (now - _cached_at) < _CACHE_TTL_S and _cached_serial_alive(_cached):
        return _cached
    val = _from_adb()
    if val:
        if _cached != val:
            log.info("adb device serial resolved → %s", val)
        _cached, _cached_at = val, now
        return val
    # Invalidate cache so next call re-checks.
    _cached, _cached_at = None, 0.0
    raise DeviceNotConnected("no ADB device authorized — plug in the phone")


_device_size_cache: tuple[int, int] | None = None


def device_size() -> tuple[int, int]:
    """Return (width, height) in LANDSCAPE orientation.

    ADB `input tap` takes coordinates in DEVICE pixel space (not the
    downscaled frame from screenrecord). Use this for any tap/swipe.
    Cached.
    """
    global _device_size_cache
    if _device_size_cache is not None:
        return _device_size_cache
    try:
        out = subprocess.run(
            ["adb", "-s", adb_serial(), "shell", "wm", "size"],
            capture_output=True, text=True, timeout=5, check=False,
        ).stdout
        for line in out.splitlines():
            if "size:" in line.lower():
                a, b = line.split(":")[-1].strip().split("x")
                a, b = int(a), int(b)
                _device_size_cache = (max(a, b), min(a, b))  # landscape
                log.info("device size resolved → %dx%d (landscape)",
                         *_device_size_cache)
                return _device_size_cache
    except Exception:
        log.exception("device_size failed; falling back to 2340x1080")
    _device_size_cache = (2340, 1080)
    return _device_size_cache


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
