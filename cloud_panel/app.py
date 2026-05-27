"""Cloud panel — receives pushed events from one or more bot workers,
exposes an aggregated dashboard.

Authentication: every `/api/sync/*` POST requires
`Authorization: Bearer <CLOUD_AUTH_TOKEN>` (env var).
Read endpoints are public on localhost; for prod exposition use Dokploy
+ nginx with basic auth or a reverse-proxy gate.
"""
from __future__ import annotations

import logging
import os
import time

log = logging.getLogger(__name__)
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import db
from ws import HUB, BUS, worker_ws_endpoint
from fastapi.responses import StreamingResponse
import asyncio as _asyncio
import json as _json

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
    # If we have no brawlers cached for this account, kick off an immediate
    # fetch (don't wait the 60s for the next refresher tick).
    brawlers, _ = db.get_account_brawlers(acc)
    if not brawlers:
        try:
            profile = _fetch_profile_from_brawlace(payload.tag)
            if profile.get("brawlers"):
                db.set_account_brawlers(acc, profile["brawlers"])
                BUS.publish({
                    "type": "brawlers_refreshed",
                    "account_id": acc, "tag": payload.tag,
                    "count": len(profile["brawlers"]),
                })
        except Exception:
            log.exception("eager brawler fetch failed for %s", payload.tag)
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
    # Broadcast to SSE subscribers (live activity feed).
    delta = None
    if payload.trophies_before is not None and payload.trophies_after is not None:
        delta = payload.trophies_after - payload.trophies_before
    BUS.publish({
        "type": "match",
        "instance_id": payload.instance_id,
        "tag": payload.tag,
        "brawler": payload.brawler,
        "result": payload.result,
        "delta": delta,
        "timestamp": payload.timestamp or time.time(),
    })
    return {"ok": True, "match_id": mid}


class SyncStatePayload(BaseModel):
    tag: str


@app.get("/api/sync/state")
def sync_state(tag: str, authorization: str | None = Header(None)) -> dict:
    """Tell the worker what we already have for this account.

    Worker uses this to push only the gap (matches/sessions newer than
    what's already in the cloud DB). Makes cloud DB wipes invisible to
    the user — worker auto-replays missing history on next sync tick.
    """
    _require_auth(authorization)
    rows = db.conn().execute(
        "SELECT a.id FROM accounts a WHERE a.tag = ? LIMIT 1", (tag,),
    ).fetchall()
    if not rows:
        return {"ok": True, "known": False, "latest_match_ts": 0}
    aid = rows[0]["id"]
    r = db.conn().execute(
        "SELECT MAX(timestamp) AS ts FROM matches WHERE account_id = ?", (aid,),
    ).fetchone()
    s = db.conn().execute(
        "SELECT MAX(started_at) AS ts FROM sessions WHERE account_id = ?", (aid,),
    ).fetchone()
    return {
        "ok": True, "known": True,
        "latest_match_ts": (r["ts"] if r and r["ts"] else 0),
        "latest_session_started_at": (s["ts"] if s and s["ts"] else 0),
    }


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
    now = time.time()
    for i in db.list_instances():
        accs = db.list_accounts(i["id"])
        i["accounts_count"] = len(accs)
        i["fresh"] = (now - i["last_seen_at"]) < 120
        # Snapshot freshness — flags hung bots that are still heartbeating
        # but whose game-state push has died (Play loop crashed, etc).
        conn = HUB.get(i["instance_id"])
        snap_age = None
        if conn and conn.last_snapshot:
            pushed = conn.last_snapshot.get("_pushed_at", 0)
            if pushed:
                snap_age = round(now - pushed, 1)
        i["snapshot_age_s"] = snap_age
        i["snapshot_stale"] = snap_age is not None and snap_age > 120
        running = any(db.current_session(a["id"]) for a in accs)
        if running:
            i["status"] = "running"
        elif i["snapshot_stale"]:
            i["status"] = "stale"
        elif i["fresh"]:
            i["status"] = "available"
        else:
            i["status"] = "offline"
        out.append(i)
    return out


@app.get("/api/accounts")
def api_accounts(instance_id: int | None = None) -> list[dict]:
    accs = db.list_accounts(instance_id)
    # Enrich with running flag + cached trophy total so sidebar can show
    # live state without per-account roundtrips.
    for a in accs:
        a["session_running"] = bool(db.current_session(a["id"]))
        brawlers, _ = db.get_account_brawlers(a["id"])
        a["total_trophies"] = sum(b.get("trophies", 0) for b in brawlers) if brawlers else None
    return accs


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
    return {"ok": True, "ts": time.time(), "sse_subscribers": BUS.count}


@app.get("/api/activity/recent")
def activity_recent(limit: int = 30) -> list[dict]:
    """Recent match events across all accounts (newest first)."""
    rows = db.conn().execute(
        "SELECT m.brawler, m.result, m.trophies_before, m.trophies_after, "
        "       m.timestamp, a.tag, a.name AS account_name, i.instance_id "
        "FROM matches m "
        "JOIN accounts a ON m.account_id = a.id "
        "JOIN instances i ON a.instance_id = i.id "
        "ORDER BY m.timestamp DESC LIMIT ?",
        (limit,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if d["trophies_before"] is not None and d["trophies_after"] is not None:
            d["delta"] = d["trophies_after"] - d["trophies_before"]
        else:
            d["delta"] = None
        out.append(d)
    return out


@app.get("/api/fleet/overview")
def fleet_overview() -> dict:
    """One-shot dashboard data: instances breakdown + today's activity."""
    now = time.time()
    today_start = now - (now % 86400)
    insts = api_instances()  # reuses status logic
    breakdown = {"running": 0, "available": 0, "stale": 0, "offline": 0}
    for i in insts:
        breakdown[i["status"]] = breakdown.get(i["status"], 0) + 1
    accs = db.list_accounts()
    total_trophies = 0
    for a in accs:
        brawlers, _ = db.get_account_brawlers(a["id"])
        total_trophies += sum(b.get("trophies", 0) for b in brawlers)
    # Today's matches across all accounts.
    today_matches = db.conn().execute(
        "SELECT result, COUNT(*) AS n FROM matches WHERE timestamp >= ? GROUP BY result",
        (today_start,),
    ).fetchall()
    today = {"victory": 0, "defeat": 0, "draw": 0}
    for r in today_matches:
        today[r["result"]] = r["n"]
    total_today = sum(today.values())
    wr_today = round(today["victory"] / total_today * 100) if total_today else None
    # Active sessions count.
    active_sessions = db.conn().execute(
        "SELECT COUNT(*) AS n FROM sessions WHERE ended_at IS NULL",
    ).fetchone()["n"]
    return {
        "instances_total": len(insts),
        "instances_by_status": breakdown,
        "accounts_total": len(accs),
        "total_trophies": total_trophies,
        "active_sessions": active_sessions,
        "today": {**today, "total": total_today, "win_rate_pct": wr_today},
        "ts": now,
    }


# ============================================================
# GitHub webhook — auto-deploy workers on push to main
# ============================================================


import hmac as _hmac
import hashlib as _hashlib

GITHUB_WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "")


@app.post("/api/github/webhook")
async def github_webhook(request: Request) -> dict:
    """Receive GitHub `push` events and trigger `git_update` on every worker.

    Validates X-Hub-Signature-256 (HMAC-SHA256 of body) if a secret is set.
    """
    body = await request.body()
    sig = request.headers.get("X-Hub-Signature-256", "")
    if GITHUB_WEBHOOK_SECRET:
        expected = "sha256=" + _hmac.new(
            GITHUB_WEBHOOK_SECRET.encode(), body, _hashlib.sha256
        ).hexdigest()
        if not _hmac.compare_digest(sig, expected):
            raise HTTPException(403, "bad signature")
    event = request.headers.get("X-GitHub-Event", "")
    if event != "push":
        return {"ok": True, "ignored": event}
    try:
        payload = _json.loads(body)
    except Exception:
        raise HTTPException(400, "bad json")
    ref = payload.get("ref", "")
    if ref != "refs/heads/main":
        return {"ok": True, "ignored_ref": ref}
    head = payload.get("head_commit", {})
    commit_sha = head.get("id", "")[:7]
    commit_msg = (head.get("message", "") or "").splitlines()[0][:120]
    log.info("github webhook: push to main %s '%s' → triggering workers", commit_sha, commit_msg)
    # Fan-out: send git_update to every connected worker (parallel).
    results = {}
    for conn in HUB.list():
        try:
            r = await HUB.send_command(conn.instance_id, "git_update",
                                        {"sha": commit_sha, "msg": commit_msg},
                                        timeout_s=90)
            results[conn.instance_id] = {"ok": True, "data": r}
        except Exception as exc:
            results[conn.instance_id] = {"ok": False, "error": str(exc)}
    # Broadcast UI event so the panel reflects deploy activity.
    BUS.publish({"type": "git_update", "sha": commit_sha, "msg": commit_msg, "results": results})
    return {"ok": True, "sha": commit_sha, "workers": results}


# ============================================================
# Server-Sent Events — continuous push from workers to browsers
# ============================================================


@app.get("/api/events")
async def events_stream(request: Request) -> StreamingResponse:
    """SSE stream — every worker snapshot is forwarded to all subscribers.

    Resumable: send `Last-Event-ID` header to pick up where the previous
    connection left off (events buffered for ~200 most recent).
    Format: `id: <n>\ndata: {json}\n\n`. Keepalive comment every 25s.
    """
    # Parse Last-Event-ID for resume.
    try:
        last_id = int(request.headers.get("Last-Event-ID", "0"))
    except ValueError:
        last_id = 0

    q = await BUS.subscribe()

    async def gen():
        # 1. Replay any missed events since client's last seen id.
        if last_id > 0:
            for ev in BUS.replay_since(last_id):
                yield f"id: {ev['_id']}\ndata: " + _json.dumps(ev) + "\n\n"
        # 2. Initial snapshot per instance (idempotent for new clients).
        for conn in HUB.list():
            if conn.last_snapshot:
                yield "data: " + _json.dumps({
                    "type": "snapshot",
                    "instance_id": conn.instance_id,
                    **{k: v for k, v in conn.last_snapshot.items() if k != "_pushed_at"},
                }) + "\n\n"
        yield "data: " + _json.dumps({"type": "ready"}) + "\n\n"
        # 3. Live stream.
        try:
            while True:
                try:
                    ev = await _asyncio.wait_for(q.get(), timeout=25)
                    eid = ev.get("_id", "")
                    line = (f"id: {eid}\n" if eid else "") + "data: " + _json.dumps(ev) + "\n\n"
                    yield line
                except _asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            BUS.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                              headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/instances/{instance_db_id}/snapshot")
def api_instance_snapshot(instance_db_id: int) -> dict:
    """Return the last cached snapshot (state, trophies, account_tag, ts)."""
    inst_id = _resolve_instance(instance_db_id)
    conn = HUB.get(inst_id) if inst_id else None
    if conn is None or not conn.last_snapshot:
        return {"available": False}
    return {"available": True, **conn.last_snapshot}


# ====================================================================
# Public scraping helpers — used by workers to fetch external data
# (brawlace.com is Cloudflare-protected; we proxy through flaresolverr).
# ====================================================================


from brawlace_parse import BRAWLACE_ROW_RE as _BRAWLACE_ROW_RE
from brawlace_parse import BRAWLACE_NAME_RE as _BRAWLACE_NAME_RE
from brawlace_parse import parse_profile as _parse_brawlace_profile
_FLARESOLVERR_URL = os.environ.get("FLARESOLVERR_URL", "http://flaresolverr:8191/v1")

def _fetch_profile_from_brawlace(tag: str) -> dict:
    """Raw fetch via flaresolverr. Returns {name, brawlers:[...]}.

    Raises HTTPException on upstream failure.
    """
    import urllib.request, json as _json
    tag = tag.lstrip("#").upper()
    payload = _json.dumps({
        "cmd": "request.get",
        "url": f"https://brawlace.com/players/{tag}",
        "maxTimeout": 60000,
    }).encode()
    req = urllib.request.Request(
        _FLARESOLVERR_URL, data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=75) as resp:
            data = _json.loads(resp.read())
    except Exception as exc:
        raise HTTPException(502, f"flaresolverr error: {exc}")
    sol = data.get("solution", {})
    if data.get("status") != "ok" or sol.get("status") != 200:
        raise HTTPException(502, f"upstream status: {data.get('status')} / {sol.get('status')}")
    html = sol.get("response", "")
    name_match = _BRAWLACE_NAME_RE.search(html)
    name = name_match.group(1).strip() if name_match else None
    brawlers: list[dict] = []
    for _img, display, power, trophies in _BRAWLACE_ROW_RE.findall(html):
        brawlers.append({
            "name": display.strip().lower(),
            "power": int(power),
            "trophies": int(trophies),
        })
    return {"name": name, "brawlers": brawlers}


@app.get("/api/util/brawler_profile/{tag}")
def util_brawler_profile(tag: str) -> dict:
    """Direct scrape (used only by the worker on tag validation).

    The browser should NOT call this — it reads from the DB via
    /api/accounts/{id}/brawlers.
    """
    return {"ok": True, **_fetch_profile_from_brawlace(tag)}


# ---- DB-backed account brawlers (preferred path) ------------------


@app.get("/api/accounts/{account_id}/brawlers")
def api_account_brawlers(account_id: int) -> dict:
    """Return brawlers cached in the cloud DB. Instant, no upstream call.

    Includes the age of the data so the UI can show staleness.
    Also returns the total trophies (sum of all brawlers) as the
    authoritative trophy count for the account.
    """
    brawlers, refreshed_at = db.get_account_brawlers(account_id)
    total_trophies = sum(b.get("trophies", 0) for b in brawlers) if brawlers else None
    return {
        "brawlers": brawlers,
        "total_trophies": total_trophies,
        "refreshed_at": refreshed_at,
        "age_s": (time.time() - refreshed_at) if refreshed_at else None,
    }


@app.post("/api/accounts/{account_id}/brawlers/refresh")
def api_account_brawlers_refresh(account_id: int) -> dict:
    """Manual refresh — pulls brawlace via flaresolverr, persists, returns."""
    acc = db.get_account(account_id)
    if not acc:
        raise HTTPException(404, "account not found")
    profile = _fetch_profile_from_brawlace(acc["tag"])
    if profile.get("brawlers"):
        db.set_account_brawlers(account_id, profile["brawlers"])
    return {"ok": True, "brawlers": profile.get("brawlers", []), "refreshed_at": time.time()}


# ---- background refresher -----------------------------------------

import asyncio as _asyncio2  # already imported as asyncio earlier, alias to avoid shadowing
import logging as _logging
_refresh_log = _logging.getLogger("brawler_refresher")

REFRESH_INTERVAL_S = 3600       # rescan each account at most once per hour
REFRESH_STALE_AFTER_S = 3600    # consider data stale after 1h
REFRESH_BATCH_PAUSE_S = 5       # gap between requests so flaresolverr isn't hammered


async def _brawlers_refresh_loop():
    await _asyncio2.sleep(20)  # give workers time to register
    while True:
        try:
            stale = db.accounts_needing_refresh(REFRESH_STALE_AFTER_S)
            for acc in stale:
                tag = acc["tag"]
                try:
                    profile = await _asyncio2.get_running_loop().run_in_executor(
                        None, _fetch_profile_from_brawlace, tag)
                    if profile.get("brawlers"):
                        db.set_account_brawlers(acc["id"], profile["brawlers"])
                        _refresh_log.info("refreshed brawlers for #%s (%d)",
                                          tag, len(profile["brawlers"]))
                        # Push event to live SSE subscribers.
                        BUS.publish({
                            "type": "brawlers_refreshed",
                            "account_id": acc["id"],
                            "tag": tag,
                            "count": len(profile["brawlers"]),
                        })
                except Exception as exc:
                    _refresh_log.warning("refresh #%s failed: %s", tag, exc)
                await _asyncio2.sleep(REFRESH_BATCH_PAUSE_S)
        except Exception:
            _refresh_log.exception("refresh loop iteration crashed")
        await _asyncio2.sleep(60)  # check every minute for staleness


@app.on_event("startup")
async def _start_brawlers_refresher() -> None:
    _asyncio2.create_task(_brawlers_refresh_loop())


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


class PushMaxBody(BaseModel):
    target_total_trophies: int | None = None


@app.post("/api/accounts/{account_id}/push_max")
async def api_account_push_max(account_id: int, payload: PushMaxBody | None = None) -> dict:
    args = {}
    if payload and payload.target_total_trophies is not None:
        args["target_total_trophies"] = payload.target_total_trophies
    return await _cmd_for_account(account_id, "session_push_max", args, timeout_s=20)


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


# -------- Game API proxies (per-account, fine-grained) ---------


@app.get("/api/accounts/{account_id}/game/state")
async def api_account_game_state(account_id: int) -> dict:
    return await _cmd_for_account(account_id, "game_state", {}, timeout_s=8)


@app.get("/api/accounts/{account_id}/game/screenshot")
async def api_account_game_screenshot(account_id: int) -> dict:
    return await _cmd_for_account(account_id, "game_screenshot", {}, timeout_s=10)


@app.get("/api/accounts/{account_id}/game/trophies")
async def api_account_game_trophies(account_id: int) -> dict:
    return await _cmd_for_account(account_id, "game_trophies", {}, timeout_s=10)


@app.get("/api/accounts/{account_id}/game/current_brawler")
async def api_account_game_current_brawler(account_id: int) -> dict:
    return await _cmd_for_account(account_id, "game_current_brawler", {}, timeout_s=10)


@app.get("/api/accounts/{account_id}/game/brawlers")
async def api_account_game_brawlers(account_id: int, force: bool = False) -> dict:
    return await _cmd_for_account(account_id, "game_brawlers", {"force": force}, timeout_s=180)


class GameTapPayload(BaseModel):
    x_ratio: float
    y_ratio: float


@app.post("/api/accounts/{account_id}/game/tap")
async def api_account_game_tap(account_id: int, payload: GameTapPayload) -> dict:
    return await _cmd_for_account(account_id, "game_tap",
                                   {"x_ratio": payload.x_ratio, "y_ratio": payload.y_ratio},
                                   timeout_s=8)


class GameSelectBrawlerPayload(BaseModel):
    name: str


@app.post("/api/accounts/{account_id}/game/select_brawler")
async def api_account_game_select(account_id: int, payload: GameSelectBrawlerPayload) -> dict:
    return await _cmd_for_account(account_id, "game_select_brawler",
                                   {"name": payload.name}, timeout_s=60)


@app.post("/api/accounts/{account_id}/game/goto_lobby")
async def api_account_game_goto_lobby(account_id: int) -> dict:
    return await _cmd_for_account(account_id, "game_goto_lobby", {}, timeout_s=30)


class GamePlayOneMatchPayload(BaseModel):
    brawler: str | None = None
    timeout_s: float = 420
    required_mode: str | None = None


@app.post("/api/accounts/{account_id}/game/play_one_match")
async def api_account_game_play_one_match(account_id: int, payload: GamePlayOneMatchPayload) -> dict:
    # WS timeout = match timeout + 30s slack
    return await _cmd_for_account(account_id, "game_play_one_match",
                                   {"brawler": payload.brawler, "timeout_s": payload.timeout_s,
                                    "required_mode": payload.required_mode},
                                   timeout_s=payload.timeout_s + 30)


@app.get("/api/accounts/{account_id}/game/current_mode")
async def api_account_game_current_mode(account_id: int) -> dict:
    return await _cmd_for_account(account_id, "game_current_mode", {}, timeout_s=15)


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
