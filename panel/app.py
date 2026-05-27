"""FastAPI dashboard for the bot — multi-account ready.

Mounts on localhost:8000. Serves a single-page HTML at `/` and JSON
APIs under `/api/`. The bot itself runs in the same process so we can
expose start/stop controls without IPC.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import db
from worker_pool import POOL, BotWorker
import alerts as alerts_mod

log = logging.getLogger(__name__)


def _get_or_create_worker(account_id: int):
    """Return the worker for this account, lazily creating one if the
    bootstrap thread hasn't registered it yet.

    For now there's a single shared BotRunner accessible through the
    Telegram bot — we grab a reference via the module-level cache below.
    Multi-account support will need a per-account runner here.
    """
    w = POOL.get(account_id)
    if w is not None:
        return w
    acc = db.get_account(account_id)
    if acc is None:
        return None
    runner = _SHARED_RUNNER
    if runner is None:
        log.warning("no shared runner yet — cannot lazy-register worker")
        return None
    runner._account_id = account_id
    w = BotWorker(account_id, acc.get("device_serial") or "emulator-5554", runner)
    POOL.register(account_id, w)
    log.info("lazy-registered worker for account=%d (#%s)", account_id, acc["tag"])
    return w


# Set by telegram_main on startup.
_SHARED_RUNNER = None


def set_shared_runner(runner) -> None:
    global _SHARED_RUNNER
    _SHARED_RUNNER = runner

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="BrawlStar Bot Panel")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


# ----------------------------------------------------------------- API


@app.get("/api/accounts")
def api_accounts() -> list[dict]:
    accounts = db.list_accounts()
    for a in accounts:
        worker = POOL.get(a["id"])
        if worker:
            a["worker"] = worker.status()
        else:
            a["worker"] = None
        a["current_session"] = db.current_session(a["id"])
    return accounts


@app.get("/api/accounts/{account_id}")
def api_account(account_id: int) -> dict:
    acc = db.get_account(account_id)
    if not acc:
        raise HTTPException(404, "account not found")
    worker = POOL.get(account_id)
    acc["worker"] = worker.status() if worker else None
    acc["current_session"] = db.current_session(account_id)
    acc["sessions"] = db.list_sessions(account_id, limit=20)
    acc["win_rate_by_brawler"] = db.win_rate_by_brawler(account_id)
    return acc


@app.get("/api/accounts/{account_id}/brawlers")
def api_account_brawlers(account_id: int) -> list[dict]:
    """Live-scrape the owned brawlers for the account (with portrait URLs).

    Brawlace exposes portraits at a predictable URL keyed by the
    title-cased brawler name (with spaces removed).
    """
    from account_detect import fetch_account_profile
    acc = db.get_account(account_id)
    if not acc:
        raise HTTPException(404, "account not found")
    profile = fetch_account_profile(acc["tag"])
    out = []
    for b in profile["brawlers"]:
        # Brawlace image naming: "el primo" -> "El-Primo.png"; "8bit" -> "8-Bit.png"
        slug = "-".join(w.capitalize() for w in b["name"].split())
        out.append({
            **b,
            "image_url": f"https://brawlace.com/assets/images/brawlstars/brawlers/{slug}.png",
        })
    return out


@app.get("/api/accounts/{account_id}/matches")
def api_account_matches(account_id: int, limit: int = 200) -> list[dict]:
    return db.recent_matches(account_id, limit=limit)


@app.get("/api/accounts/{account_id}/events")
def api_account_events(account_id: int, limit: int = 100) -> list[dict]:
    return db.recent_events(account_id, limit=limit)


# ------------------------------------------------------------- controls


class StartPayload(BaseModel):
    brawler: str
    target: int


@app.post("/api/accounts/{account_id}/start")
def api_start(account_id: int, payload: StartPayload) -> dict:
    log.info("PANEL start account=%d brawler=%s target=%d",
             account_id, payload.brawler, payload.target)
    worker = POOL.get(account_id)
    if not worker:
        log.warning("start: no worker registered for account=%d", account_id)
        raise HTTPException(404, "no worker for that account")
    ok, msg = worker.start(payload.brawler, payload.target)
    log.info("start result: ok=%s msg=%s", ok, msg)
    return {"ok": ok, "msg": msg}


class PushMaxPayload(BaseModel):
    target_total_trophies: int | None = None


@app.post("/api/accounts/{account_id}/push_max")
def api_push_max(account_id: int, payload: PushMaxPayload | None = None) -> dict:
    """Start the smart-rotation push-max mode for this account.

    Optional `target_total_trophies` stops the bot once the account
    total reaches the goal (or when all brawlers exhausted).
    """
    log.info("PANEL push_max account=%d target=%s", account_id,
             payload.target_total_trophies if payload else None)
    worker = _get_or_create_worker(account_id)
    if not worker:
        raise HTTPException(404, "no worker for that account")
    acc = db.get_account(account_id)
    from account_detect import fetch_account_profile
    profile = fetch_account_profile(acc["tag"])
    if not profile["brawlers"]:
        raise HTTPException(503, "Could not fetch brawler list from brawlace")
    target = payload.target_total_trophies if payload else None
    ok, msg = worker.runner.start(
        brawler=profile["brawlers"][0]["name"],  # placeholder, strategy overrides
        trophies=99999, wins=0,
        mode="push_max", owned_brawlers=profile["brawlers"],
        target_total_trophies=target,
    )
    log.info("push_max start result: ok=%s msg=%s", ok, msg)
    return {"ok": ok, "msg": msg, "target_total_trophies": target}


@app.get("/api/accounts/{account_id}/push_max_state")
def api_push_max_state(account_id: int) -> dict:
    """Return the current push-max strategy state (or None if not active)."""
    worker = POOL.get(account_id)
    if not worker or worker.runner._push_max is None:
        return {"active": False}
    s = worker.runner._push_max
    return {
        "active": True,
        "summary": s.summary(),
        "target_total_trophies": worker.runner._target_total_trophies,
        "current_total_trophies": worker.runner._account_trophies,
        "brawlers": [
            {"name": b.name, "trophies": b.trophies,
             "defeat_streak": b.defeat_streak,
             "matches_played": b.matches_played,
             "exhausted": b.exhausted}
            for b in s.brawlers.values()
        ],
    }


@app.post("/api/accounts/{account_id}/stop")
def api_stop(account_id: int) -> dict:
    log.info("PANEL stop account=%d", account_id)
    worker = _get_or_create_worker(account_id)
    if not worker:
        raise HTTPException(404, "no worker for that account")
    ok, msg = worker.stop()
    log.info("stop result: ok=%s msg=%s", ok, msg)
    return {"ok": ok, "msg": msg}


@app.post("/api/accounts/{account_id}/forcestop")
def api_forcestop(account_id: int) -> dict:
    log.info("PANEL force_stop account=%d", account_id)
    worker = _get_or_create_worker(account_id)
    if not worker:
        raise HTTPException(404, "no worker for that account")
    ok, msg = worker.force_stop()
    log.info("force_stop result: ok=%s msg=%s", ok, msg)
    return {"ok": ok, "msg": msg}


# ---------------------------------------------------------------- alerts


@app.get("/api/alerts")
def api_get_alerts() -> dict:
    """Return the current alerts config (re-read from disk)."""
    return alerts_mod._load()


class AlertUpdate(BaseModel):
    enabled: bool | None = None
    template: str | None = None
    filter: dict[str, bool] | None = None


@app.put("/api/alerts/{event}")
def api_put_alert(event: str, payload: AlertUpdate) -> dict:
    """Update a single alert (enabled / template / filter).

    Writes back to cfg/alerts.toml. The alerts module re-reads it on
    next send so the change takes effect immediately.
    """
    import tomllib
    try:
        with alerts_mod.CFG_PATH.open("rb") as f:
            cfg = tomllib.load(f)
    except FileNotFoundError:
        cfg = {}
    entry = cfg.get(event, {})
    if payload.enabled is not None:
        entry["enabled"] = payload.enabled
    if payload.template is not None:
        entry["template"] = payload.template
    if payload.filter is not None:
        entry.setdefault("filter", {}).update(payload.filter)
    cfg[event] = entry
    # Write TOML manually — stdlib has no writer. Simple format: top-level
    # tables only, multi-line strings escaped.
    _write_alerts_toml(cfg)
    return {"ok": True, "event": event, "config": entry}


def _write_alerts_toml(cfg: dict) -> None:
    lines: list[str] = []
    for event, entry in cfg.items():
        lines.append(f"[{event}]")
        for k, v in entry.items():
            if isinstance(v, dict):
                continue  # handled below as subtable
            if isinstance(v, bool):
                lines.append(f"{k} = {'true' if v else 'false'}")
            elif isinstance(v, str):
                if "\n" in v:
                    lines.append(f'{k} = """{v}"""')
                else:
                    esc = v.replace('"', '\\"')
                    lines.append(f'{k} = "{esc}"')
            else:
                lines.append(f"{k} = {v}")
        # subtables (e.g. [match.filter])
        for k, v in entry.items():
            if isinstance(v, dict):
                lines.append("")
                lines.append(f"[{event}.{k}]")
                for sk, sv in v.items():
                    if isinstance(sv, bool):
                        lines.append(f"{sk} = {'true' if sv else 'false'}")
                    else:
                        lines.append(f"{sk} = {sv}")
        lines.append("")
    alerts_mod.CFG_PATH.write_text("\n".join(lines))


# Fleet view — aggregate across all accounts.
@app.get("/api/fleet")
def api_fleet() -> dict:
    workers = [w.status() for w in POOL.all()]
    return {
        "workers": workers,
        "total_accounts": len(db.list_accounts()),
        "running_workers": sum(1 for w in workers if w.get("running")),
    }


# ============================================================
# Game API — low-level primitives over the running Brawl Stars
# ============================================================

import asyncio
import game_api as game_api_mod


def _game():
    api = game_api_mod.get()
    if api is None:
        raise HTTPException(503, "game API not initialized (bot still booting)")
    return api


@app.get("/api/game/state")
async def game_state() -> dict:
    return await asyncio.get_event_loop().run_in_executor(None, lambda: {"state": _game().state()})


@app.get("/api/game/snapshot")
async def game_snapshot() -> dict:
    return await asyncio.get_event_loop().run_in_executor(None, lambda: _game().snapshot())


@app.get("/api/game/diag")
def game_diag() -> dict:
    """Diagnostic: shows whether scrcpy is feeding fresh frames."""
    import time as _t
    g = _game()
    wc = g.wc
    age = round(_t.time() - getattr(wc, "last_frame_time", 0), 2) if getattr(wc, "last_frame_time", 0) else None
    return {
        "scrcpy_frame_age_s": age,
        "scrcpy_alive": age is not None and age < 5,
        "resolution": [wc.width, wc.height] if wc.width else None,
    }


@app.get("/api/game/screenshot")
async def game_screenshot() -> dict:
    return await asyncio.get_event_loop().run_in_executor(None, lambda: _game().screenshot_jpeg())


@app.get("/api/game/trophies")
async def game_trophies() -> dict:
    val = await asyncio.get_event_loop().run_in_executor(None, lambda: _game().read_trophies())
    return {"trophies": val}


@app.get("/api/game/current_brawler")
async def game_current_brawler() -> dict:
    val = await asyncio.get_event_loop().run_in_executor(None, lambda: _game().read_current_brawler())
    return {"brawler": val}


@app.get("/api/game/brawlers")
async def game_brawlers(force: bool = False) -> dict:
    val = await asyncio.get_event_loop().run_in_executor(None, lambda: _game().list_brawlers(force_refresh=force))
    return {"brawlers": val}


class SelectBrawlerPayload(BaseModel):
    name: str


@app.post("/api/game/select_brawler")
async def game_select_brawler(payload: SelectBrawlerPayload) -> dict:
    return await asyncio.get_event_loop().run_in_executor(
        None, lambda: _game().select_brawler(payload.name))


class TapPayload(BaseModel):
    x_ratio: float
    y_ratio: float


@app.post("/api/game/tap")
async def game_tap(payload: TapPayload) -> dict:
    return await asyncio.get_event_loop().run_in_executor(
        None, lambda: _game().tap(payload.x_ratio, payload.y_ratio))


@app.post("/api/game/goto_lobby")
async def game_goto_lobby() -> dict:
    ok = await asyncio.get_event_loop().run_in_executor(None, lambda: _game().goto_lobby())
    return {"ok": ok}


class PlayMatchPayload(BaseModel):
    brawler: str | None = None
    timeout_s: float = 420
    required_mode: str | None = None


@app.post("/api/game/play_one_match")
async def game_play_one_match(payload: PlayMatchPayload) -> dict:
    return await asyncio.get_event_loop().run_in_executor(
        None, lambda: _game().play_one_match(
            brawler=payload.brawler, timeout_s=payload.timeout_s,
            required_mode=payload.required_mode))


@app.get("/api/game/current_mode")
async def game_current_mode() -> dict:
    return await asyncio.get_event_loop().run_in_executor(
        None, lambda: {"mode": _game().read_current_mode()})
