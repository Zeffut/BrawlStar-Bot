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


def _cloud_get(endpoint: str) -> dict | None:
    cfg = _load_cfg()
    if not is_enabled():
        return None
    url = cfg["url"].rstrip("/") + endpoint
    try:
        r = requests.get(url, timeout=10,
                         headers={"Authorization": f"Bearer {cfg['token']}"})
        if r.status_code >= 400:
            return None
        return r.json()
    except requests.RequestException as exc:
        log.warning("cloud GET %s failed: %s", endpoint, exc)
        return None


def sync_history_to_cloud() -> None:
    """Push every local match newer than cloud's known watermark.

    Runs at WS reconnect and periodically. Makes cloud DB wipes
    invisible — worker is the source of truth.
    """
    if not is_enabled():
        return
    try:
        import db as _db
    except Exception:
        return
    for acc in _db.list_accounts():
        tag = acc["tag"]
        state = _cloud_get(f"/api/sync/state?tag={tag}")
        if state is None:
            continue
        # ---- Sessions sync (replay missing) ----
        session_since = float(state.get("latest_session_started_at") or 0)
        try:
            srows = _db.conn().execute(
                "SELECT * FROM sessions WHERE account_id = ? AND started_at > ? "
                "ORDER BY started_at",
                (acc["id"], session_since),
            ).fetchall()
        except Exception:
            srows = []
        if srows:
            log.info("syncing %d missing sessions for #%s (since=%.0f)",
                     len(srows), tag, session_since)
        for s in srows:
            try:
                _post("/api/sync/session_start", {
                    "tag": tag, "brawler": s["brawler"],
                    "target_trophies": s["target_trophies"],
                    "start_trophies": s["start_trophies"],
                    "started_at": s["started_at"],
                })
            except Exception:
                pass
        # ---- Matches sync (replay missing) ----
        match_since = float(state.get("latest_match_ts") or 0)
        try:
            mrows = _db.conn().execute(
                "SELECT * FROM matches WHERE account_id = ? AND timestamp > ? "
                "ORDER BY timestamp",
                (acc["id"], match_since),
            ).fetchall()
        except Exception:
            log.exception("matches query failed for %s", tag)
            continue
        if mrows:
            log.info("syncing %d missing matches for #%s (since=%.0f)",
                     len(mrows), tag, match_since)
        for r in mrows:
            try:
                _post("/api/sync/match", {
                    "tag": tag, "session_id": None,
                    "brawler": r["brawler"], "result": r["result"],
                    "trophies_before": r["trophies_before"],
                    "trophies_after": r["trophies_after"],
                    "account_trophies_after": r["account_trophies_after"],
                    "timestamp": r["timestamp"],
                })
            except Exception:
                pass


def push_brawlers(tag: str, brawlers: list[dict]) -> None:
    """Push the worker's current brawler list to the cloud DB."""
    _post("/api/sync/brawlers", {"tag": tag, "brawlers": brawlers})


def refresh_and_push_brawlers() -> None:
    """Fetch this account's brawlers via the cloud's flaresolverr proxy
    and push the result back. Worker owns the schedule + decision; the
    cloud is just an infrastructure service.
    """
    if not is_enabled():
        return
    try:
        import db as _db
    except Exception:
        return
    accs = _db.list_accounts()
    if not accs:
        return
    cfg = _load_cfg()
    url = cfg["url"].rstrip("/") + "/api/util/brawler_profile/"
    token = cfg["token"]
    for acc in accs:
        tag = acc["tag"]
        try:
            r = requests.get(url + tag, timeout=80,
                             headers={"Authorization": f"Bearer {token}"})
            if r.status_code != 200:
                log.warning("brawler fetch HTTP %d for %s", r.status_code, tag)
                continue
            profile = r.json()
            brawlers = profile.get("brawlers") or []
            if brawlers:
                push_brawlers(tag, brawlers)
                log.info("brawler refresh ok: %s -> cloud (%d)", tag, len(brawlers))
        except Exception as exc:
            log.warning("brawler refresh failed for %s: %s", tag, exc)


def start_brawlers_refresh_loop(interval_s: float = 3600.0) -> None:
    """Background thread: refreshes brawlers every interval_s (default 1h).

    The worker drives this — the cloud is passive. First tick fires
    ~30s after start so initial heartbeat lands first.
    """
    if not is_enabled():
        return
    def loop():
        time.sleep(30)
        while True:
            try:
                refresh_and_push_brawlers()
            except Exception:
                log.exception("brawlers refresh iteration")
            time.sleep(interval_s)
    threading.Thread(target=loop, daemon=True, name="cloud-brawlers-refresh").start()
    log.info("brawlers refresh loop started (every %ds)", interval_s)


def start_history_sync_loop(interval_s: float = 120.0) -> None:
    """Background thread: periodically re-sync local matches to cloud."""
    if not is_enabled():
        return
    def loop():
        time.sleep(15)  # let initial heartbeat finish
        while True:
            try:
                sync_history_to_cloud()
            except Exception:
                log.exception("history sync iteration")
            time.sleep(interval_s)
    threading.Thread(target=loop, daemon=True, name="cloud-history-sync").start()
    log.info("history sync loop started (every %ds)", interval_s)


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
