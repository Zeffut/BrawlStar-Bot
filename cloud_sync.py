"""Push bot events to the cloud panel on the VPS.

Configuration in `cfg/cloud.toml`:
    enabled = true
    url = "https://panel.example.com"
    token = "shared-secret"
    instance_id = "mac-zeffut"   # unique per worker
    name = "Mac de Zeffut"       # display label

All push functions are fire-and-forget — failures are logged but never
break the bot. A background heartbeat thread keeps the instance row
fresh on the VPS so the dashboard knows whether each worker is alive.
"""
from __future__ import annotations

import logging
import threading
import time
import tomllib
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger(__name__)

CFG_PATH = Path(__file__).resolve().parent / "cfg" / "cloud.toml"

_cfg: dict | None = None
_session_map: dict[int, int] = {}  # local session_id → cloud session_id
_heartbeat_thread: threading.Thread | None = None


def _load_cfg() -> dict:
    global _cfg
    if _cfg is None:
        if not CFG_PATH.exists():
            _cfg = {"enabled": False}
            return _cfg
        try:
            with CFG_PATH.open("rb") as f:
                _cfg = tomllib.load(f)
        except Exception:
            log.exception("cloud.toml parse failed; disabling cloud sync")
            _cfg = {"enabled": False}
    return _cfg


def is_enabled() -> bool:
    cfg = _load_cfg()
    return bool(cfg.get("enabled")) and bool(cfg.get("url")) and bool(cfg.get("token"))


def _post(endpoint: str, payload: dict) -> dict | None:
    cfg = _load_cfg()
    if not is_enabled():
        return None
    url = cfg["url"].rstrip("/") + endpoint
    payload = {"instance_id": cfg["instance_id"], **payload}
    try:
        r = requests.post(
            url, json=payload, timeout=5,
            headers={"Authorization": f"Bearer {cfg['token']}"},
        )
        if r.status_code >= 400:
            log.warning("cloud %s → %d: %s", endpoint, r.status_code, r.text[:200])
            return None
        return r.json()
    except requests.RequestException as exc:
        log.warning("cloud %s failed: %s", endpoint, exc)
        return None


# --------------------------------------------------------------- API


def heartbeat(metadata: dict | None = None) -> None:
    cfg = _load_cfg()
    _post("/api/sync/heartbeat", {
        "name": cfg.get("name"),
        "metadata": metadata or {},
    })


def account(tag: str, name: str | None = None) -> None:
    _post("/api/sync/account", {"tag": tag, "name": name})


def session_start(tag: str, brawler: str, target: int,
                  start_trophies: int | None, local_session_id: int) -> None:
    res = _post("/api/sync/session_start", {
        "tag": tag, "brawler": brawler,
        "target_trophies": target,
        "start_trophies": start_trophies,
        "started_at": time.time(),
    })
    if res and "session_id" in res:
        _session_map[local_session_id] = res["session_id"]


def session_end(local_session_id: int, status: str = "stopped",
                end_trophies: int | None = None) -> None:
    cloud_sid = _session_map.pop(local_session_id, None)
    if cloud_sid is None:
        return
    _post("/api/sync/session_end", {
        "session_id": cloud_sid,
        "status": status,
        "end_trophies": end_trophies,
        "ended_at": time.time(),
    })


def match(tag: str, local_session_id: int | None, brawler: str, result: str,
          trophies_before: int | None, trophies_after: int | None,
          account_trophies_after: int | None) -> None:
    _post("/api/sync/match", {
        "tag": tag,
        "session_id": _session_map.get(local_session_id) if local_session_id else None,
        "brawler": brawler, "result": result,
        "trophies_before": trophies_before,
        "trophies_after": trophies_after,
        "account_trophies_after": account_trophies_after,
        "timestamp": time.time(),
    })


def event(type_: str, payload: dict | None = None, tag: str | None = None) -> None:
    _post("/api/sync/event", {
        "type": type_, "payload": payload or {}, "tag": tag,
        "timestamp": time.time(),
    })


# --------------------------------------------------- background heartbeat


def start_heartbeat_loop(interval_s: float = 30.0) -> None:
    """Spawn a daemon thread that pings the cloud every interval_s.

    Also re-pushes every known local account every cycle so the cloud
    DB recovers automatically after Dokploy redeploys (which wipe the
    SQLite volume).
    """
    global _heartbeat_thread
    if _heartbeat_thread is not None or not is_enabled():
        return
    def loop():
        # local import to avoid circular at module load
        import db as _db
        while True:
            try:
                heartbeat()
                # Re-sync all known accounts so the cloud always sees them.
                for acc in _db.list_accounts():
                    try:
                        account(acc["tag"], acc.get("name"))
                    except Exception:
                        pass
            except Exception:
                log.exception("heartbeat tick failed")
            time.sleep(interval_s)
    _heartbeat_thread = threading.Thread(target=loop, daemon=True, name="cloud-heartbeat")
    _heartbeat_thread.start()
    log.info("cloud heartbeat thread started (every %ss)", interval_s)
