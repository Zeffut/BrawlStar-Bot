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

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import db

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
        i["accounts_count"] = len(db.list_accounts(i["id"]))
        i["fresh"] = (time.time() - i["last_seen_at"]) < 120
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
