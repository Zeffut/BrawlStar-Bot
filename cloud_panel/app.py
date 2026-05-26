"""Cloud panel — receives pushed events from one or more bot workers,
exposes an aggregated dashboard.

Authentication: every `/api/sync/*` POST requires
`Authorization: Bearer <CLOUD_AUTH_TOKEN>` (env var).
Read endpoints are public on localhost; for prod exposition use Dokploy
+ nginx with basic auth or a reverse-proxy gate.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import db
from ws import HUB, worker_ws_endpoint

STATIC_DIR = Path(__file__).parent / "static"
AUTH_TOKEN = os.environ.get("CLOUD_AUTH_TOKEN", "change-me")

app = FastAPI(title="BrawlStar Bot Cloud Panel")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.on_event("startup")
def _startup() -> None:
    db.init()


def _require_auth(authorization: str | None) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing Bearer token")
    if authorization.removeprefix("Bearer ").strip() != AUTH_TOKEN:
        raise HTTPException(403, "bad token")


# ====================================================================
# WRITE: sync endpoints called by bot workers
# ====================================================================


class HeartbeatPayload(BaseModel):
    instance_id: str
    name: str | None = None
    metadata: dict | None = None


@app.post("/api/sync/heartbeat")
def sync_heartbeat(payload: HeartbeatPayload, authorization: str | None = Header(None)) -> dict:
    _require_auth(authorization)
    inst = db.upsert_instance(payload.instance_id, payload.name, payload.metadata)
    return {"ok": True, "instance_db_id": inst}


class AccountPayload(BaseModel):
    instance_id: str
    tag: str
    name: str | None = None


@app.post("/api/sync/account")
def sync_account(payload: AccountPayload, authorization: str | None = Header(None)) -> dict:
    _require_auth(authorization)
    inst = db.upsert_instance(payload.instance_id)
    acc = db.upsert_account(inst, payload.tag, payload.name)
    return {"ok": True, "account_id": acc}


class SessionStartPayload(BaseModel):
    instance_id: str
    tag: str
    brawler: str
    target_trophies: int
    start_trophies: int | None = None
    started_at: float | None = None


@app.post("/api/sync/session_start")
def sync_session_start(payload: SessionStartPayload, authorization: str | None = Header(None)) -> dict:
    _require_auth(authorization)
    inst = db.upsert_instance(payload.instance_id)
    acc = db.upsert_account(inst, payload.tag, None)
    sid = db.start_session(acc, payload.brawler, payload.target_trophies,
                           payload.start_trophies, payload.started_at)
    return {"ok": True, "session_id": sid}


class SessionEndPayload(BaseModel):
    session_id: int
    status: str = "stopped"
    end_trophies: int | None = None
    ended_at: float | None = None


@app.post("/api/sync/session_end")
def sync_session_end(payload: SessionEndPayload, authorization: str | None = Header(None)) -> dict:
    _require_auth(authorization)
    db.end_session(payload.session_id, payload.status, payload.end_trophies, payload.ended_at)
    return {"ok": True}


class MatchPayload(BaseModel):
    instance_id: str
    tag: str
    session_id: int | None = None
    brawler: str
    result: str
    trophies_before: int | None = None
    trophies_after: int | None = None
    account_trophies_after: int | None = None
    timestamp: float | None = None


@app.post("/api/sync/match")
def sync_match(payload: MatchPayload, authorization: str | None = Header(None)) -> dict:
    _require_auth(authorization)
    inst = db.upsert_instance(payload.instance_id)
    acc = db.upsert_account(inst, payload.tag, None)
    mid = db.log_match(acc, payload.session_id, payload.brawler, payload.result,
                       payload.trophies_before, payload.trophies_after,
                       payload.account_trophies_after, payload.timestamp)
    return {"ok": True, "match_id": mid}


class EventPayload(BaseModel):
    instance_id: str
    tag: str | None = None
    type: str
    payload: dict | None = None
    timestamp: float | None = None


@app.post("/api/sync/event")
def sync_event(payload: EventPayload, authorization: str | None = Header(None)) -> dict:
    _require_auth(authorization)
    inst = db.upsert_instance(payload.instance_id)
    acc = db.upsert_account(inst, payload.tag, None) if payload.tag else None
    db.log_event(inst, payload.type, payload.payload, acc, payload.timestamp)
    return {"ok": True}


# ====================================================================
# READ: dashboard / aggregate
# ====================================================================


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/instances")
def api_instances() -> list[dict]:
    out = []
    for i in db.list_instances():
        accs = db.list_accounts(i["id"])
        i["accounts_count"] = len(accs)
        i["fresh"] = (time.time() - i["last_seen_at"]) < 120
        running = any(db.current_session(a["id"]) for a in accs)
        if running:
            i["status"] = "running"
        elif i["fresh"]:
            i["status"] = "available"
        else:
            i["status"] = "offline"
        out.append(i)
    return out


@app.get("/api/accounts")
def api_accounts(instance_id: int | None = None) -> list[dict]:
    return db.list_accounts(instance_id)


@app.get("/api/accounts/{account_id}")
def api_account(account_id: int) -> dict:
    acc = db.get_account(account_id)
    if not acc:
        raise HTTPException(404, "not found")
    acc["current_session"] = db.current_session(account_id)
    acc["sessions"] = db.list_sessions(account_id, limit=20)
    acc["win_rate_by_brawler"] = db.win_rate_by_brawler(account_id)
    return acc


@app.get("/api/accounts/{account_id}/matches")
def api_account_matches(account_id: int, limit: int = 200) -> list[dict]:
    return db.recent_matches(account_id, limit=limit)


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "ts": time.time()}


# ============================================================
# Device management — WebSocket + commands + streams
# ============================================================


@app.websocket("/ws/worker")
async def ws_worker(ws: WebSocket, token: str = "", instance_id: str = "") -> None:
    """Persistent WS endpoint a worker connects to at startup."""
    await worker_ws_endpoint(ws, token=token, instance_id=instance_id)


def _resolve_instance(instance_id_or_db_id: int | str):
    """Allow both DB id (int) and instance_id (str) in URL paths."""
    if isinstance(instance_id_or_db_id, int) or (
        isinstance(instance_id_or_db_id, str) and instance_id_or_db_id.isdigit()
    ):
        inst = next((i for i in db.list_instances() if i["id"] == int(instance_id_or_db_id)), None)
        return inst["instance_id"] if inst else None
    return instance_id_or_db_id


class CommandPayload(BaseModel):
    name: str
    args: dict | None = None
    timeout_s: float = 15.0


@app.post("/api/instances/{instance_db_id}/cmd")
async def api_instance_cmd(instance_db_id: int, payload: CommandPayload) -> dict:
    """Send a command to the worker connected for this instance."""
    inst_id = _resolve_instance(instance_db_id)
    if not inst_id:
        raise HTTPException(404, "instance not found")
    try:
        data = await HUB.send_command(inst_id, payload.name, payload.args,
                                       timeout_s=payload.timeout_s)
        return {"ok": True, "data": data}
    except TimeoutError as exc:
        return {"ok": False, "error": str(exc)}
    except ConnectionError as exc:
        raise HTTPException(503, str(exc))
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.get("/api/instances/{instance_db_id}/screenshot")
async def api_instance_screenshot(instance_db_id: int, refresh: bool = False) -> dict:
    """Return the last screenshot pushed by the worker.

    When `refresh=true`, ask the worker to capture a NEW frame on
    demand (latency ~300-800 ms instead of waiting up to 15 s for the
    next periodic push).
    """
    inst_id = _resolve_instance(instance_db_id)
    conn = HUB.get(inst_id) if inst_id else None
    if conn is None:
        return {"available": False}
    if refresh:
        try:
            data = await HUB.send_command(inst_id, "screenshot", {}, timeout_s=8)
            if data and data.get("jpeg_b64"):
                return {
                    "available": True,
                    "mime": "image/jpeg",
                    "b64": data["jpeg_b64"],
                    "w": data.get("w"), "h": data.get("h"),
                    "age_s": 0.0,
                }
        except Exception:
            pass  # fall through to cached
    if not conn.last_screenshot_b64:
        return {"available": False}
    return {
        "available": True,
        "mime": conn.last_screenshot_mime,
        "b64": conn.last_screenshot_b64,
        "age_s": time.time() - conn.last_screenshot_at,
    }


@app.get("/api/instances/{instance_db_id}/logs")
def api_instance_logs(instance_db_id: int, limit: int = 100) -> list[dict]:
    inst_id = _resolve_instance(instance_db_id)
    conn = HUB.get(inst_id) if inst_id else None
    if conn is None:
        return []
    items = list(conn.logs)[-limit:]
    return items


class AccountSessionPayload(BaseModel):
    brawler: str | None = None
    target_trophies: int | None = None
    force: bool | None = None


async def _cmd_for_account(account_id: int, name: str, args: dict, timeout_s: float = 15) -> dict:
    """Send a WS command to the worker hosting this account."""
    acc = db.get_account(account_id)
    if not acc:
        raise HTTPException(404, "account not found")
    inst_id = acc.get("instance_uid") or _resolve_instance(acc.get("instance_id"))
    if not inst_id:
        raise HTTPException(404, "instance not found")
    payload = {"tag": acc["tag"], **args}
    try:
        data = await HUB.send_command(inst_id, name, payload, timeout_s=timeout_s)
        return {"ok": True, "data": data}
    except ConnectionError as exc:
        raise HTTPException(503, str(exc))
    except TimeoutError as exc:
        return {"ok": False, "error": str(exc)}


@app.post("/api/accounts/{account_id}/push_max")
async def api_account_push_max(account_id: int) -> dict:
    return await _cmd_for_account(account_id, "session_push_max", {}, timeout_s=20)


@app.post("/api/accounts/{account_id}/start")
async def api_account_start(account_id: int, payload: AccountSessionPayload) -> dict:
    if not payload.brawler or not payload.target_trophies:
        raise HTTPException(400, "brawler and target_trophies required")
    return await _cmd_for_account(account_id, "session_start", {
        "brawler": payload.brawler, "target_trophies": payload.target_trophies,
    }, timeout_s=20)


@app.post("/api/accounts/{account_id}/stop")
async def api_account_stop(account_id: int, payload: AccountSessionPayload | None = None) -> dict:
    force = bool(payload.force) if payload else False
    return await _cmd_for_account(account_id, "session_stop", {"force": force}, timeout_s=15)


@app.get("/api/accounts/{account_id}/session_state")
async def api_account_session_state(account_id: int) -> dict:
    return await _cmd_for_account(account_id, "session_state", {}, timeout_s=8)


@app.get("/api/accounts/{account_id}/brawlers")
async def api_account_brawlers(account_id: int) -> dict:
    return await _cmd_for_account(account_id, "list_brawlers", {}, timeout_s=8)


@app.get("/api/instances/{instance_db_id}/health")
def api_instance_health(instance_db_id: int) -> dict:
    inst_id = _resolve_instance(instance_db_id)
    conn = HUB.get(inst_id) if inst_id else None
    if conn is None:
        return {"connected": False}
    return {
        "connected": True,
        "connected_at": conn.connected_at,
        "uptime_s": time.time() - conn.connected_at,
        **conn.health,
    }
