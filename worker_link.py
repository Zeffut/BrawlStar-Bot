"""WebSocket link between this worker bot and the cloud panel.

Opens a persistent WS connection (with auto-reconnect) and:
  • Streams logs (tail of logs/bot.log) every few seconds
  • Streams health stats (battery / RAM / storage / CPU) every 30 s
  • Streams a fresh screenshot every 5 s
  • Handles inbound commands sent from the cloud (start/stop Brawl Stars,
    reboot phone, restart bot, etc.)

Spawned by `telegram_main.py` at startup. Fails silently if cfg/cloud.toml
is missing/disabled — heartbeat-only push still works through cloud_sync.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import re
import subprocess
import threading
import time
import tomllib
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

CFG_PATH = Path(__file__).resolve().parent / "cfg" / "cloud.toml"
BS_PACKAGE = "com.supercell.brawlstars"

# ----------------------------------------------------------- config


def _cfg() -> dict | None:
    if not CFG_PATH.exists():
        return None
    try:
        with CFG_PATH.open("rb") as f:
            return tomllib.load(f)
    except Exception:
        log.exception("cloud.toml parse failed")
        return None


# ----------------------------------------------------- command handlers


def _adb_serial() -> str:
    import device
    return device.adb_serial()


def _adb(*args, timeout: float = 10.0) -> tuple[int, str]:
    """Run an adb command targeted at the resolved device serial."""
    try:
        serial = _adb_serial()
    except Exception as exc:
        # Phone unplugged / device.toml override unreachable. Fail
        # cleanly rather than crashing the WS command handler.
        return -1, f"NO_DEVICE: {exc}"
    cmd = ["adb", "-s", serial, *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, errors="replace")
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return -1, "TIMEOUT"


# ---- screenshot --------------------------------------------------

def _cmd_screenshot(args: dict) -> dict:
    """Capture phone screen, downscale + JPEG-compress to keep frames
    small and fast over the WebSocket.

    Args (optional):
      max_width : int   target width in px (default 960, original aspect ratio)
      quality   : int   JPEG quality 1-95 (default 70)
    """
    serial = _adb_serial()
    raw = subprocess.check_output(
        ["adb", "-s", serial, "exec-out", "screencap", "-p"],
        timeout=8,
    )
    # Convert raw PNG → downscaled JPEG
    from PIL import Image
    img = Image.open(io.BytesIO(raw)).convert("RGB")
    max_w = int(args.get("max_width", 960))
    if img.width > max_w:
        new_h = int(img.height * max_w / img.width)
        img = img.resize((max_w, new_h), Image.LANCZOS)
    quality = int(args.get("quality", 70))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return {
        "jpeg_b64": base64.b64encode(buf.getvalue()).decode("ascii"),
        "w": img.width, "h": img.height,
        "bytes": buf.tell(),
    }


# ---- Brawl Stars control -----------------------------------------

def _cmd_brawlstars_start(args: dict) -> dict:
    code, out = _adb("shell", "am", "start", "-n", f"{BS_PACKAGE}/.GameApp", timeout=15)
    return {"ok": code == 0, "output": out.strip()[:500]}


def _cmd_brawlstars_stop(args: dict) -> dict:
    code, out = _adb("shell", "am", "force-stop", BS_PACKAGE, timeout=15)
    return {"ok": code == 0, "output": out.strip()[:500]}


def _cmd_brawlstars_restart(args: dict) -> dict:
    _cmd_brawlstars_stop({})
    time.sleep(1.5)
    return _cmd_brawlstars_start({})


# ---- Device control ----------------------------------------------

def _cmd_phone_reboot(args: dict) -> dict:
    code, out = _adb("reboot", timeout=8)
    return {"ok": code == 0, "output": out.strip()[:500]}


def _cmd_adb_reconnect(args: dict) -> dict:
    subprocess.run(["adb", "kill-server"], timeout=5)
    time.sleep(1)
    subprocess.run(["adb", "start-server"], timeout=5)
    time.sleep(1)
    code, out = _adb("devices", timeout=5)
    return {"ok": code == 0, "output": out.strip()[:500]}


def _cmd_battery_gate_set(args: dict) -> dict:
    """Enable or disable the charging-triggered power-save loop.

    args = {"enabled": bool}. Persisted to disk so the choice survives
    restarts. The power-saver loop reads the flag every iteration.
    """
    try:
        import game_api as _ga
        enabled = bool(args.get("enabled", True))
        _ga.set_battery_gate(enabled)
        return {"ok": True, "enabled": enabled}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _cmd_battery_gate_get(args: dict) -> dict:
    try:
        import game_api as _ga
        return {"ok": True, "enabled": _ga.battery_gate_enabled()}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _check_and_self_update() -> None:
    """If local HEAD is behind origin/main, trigger an update.

    Called on every WS (re)connect so the worker recovers even when a
    GitHub push landed during a cloud panel redeploy and missed the
    broadcast.
    """
    bot_dir = str(Path(__file__).resolve().parent)
    try:
        # Fetch quietly so we know origin's HEAD without touching working tree.
        f = subprocess.run(["git", "fetch", "--quiet", "origin", "main"],
                            cwd=bot_dir, capture_output=True, text=True, timeout=20)
        if f.returncode != 0:
            return
        local = subprocess.run(["git", "rev-parse", "HEAD"],
                                cwd=bot_dir, capture_output=True, text=True, timeout=5).stdout.strip()
        remote = subprocess.run(["git", "rev-parse", "origin/main"],
                                 cwd=bot_dir, capture_output=True, text=True, timeout=5).stdout.strip()
        if local and remote and local != remote:
            log.info("self-update: behind origin (%s -> %s), pulling+restarting",
                     local[:7], remote[:7])
            _cmd_git_update({"sha": remote[:7], "msg": "self-heal on WS reconnect"})
    except Exception:
        log.debug("self-update check raised", exc_info=True)


# ---- Deferred restart -------------------------------------------
# Restarts mid-match destroy the player's grind (current match aborted,
# trophies lost, brawler swap state forgotten). When a session is
# active we mark the intent and let the match loop drain it from the
# post-match hook — at that point the bot is at the lobby and any
# resume-state has just been persisted to the cloud.
_PENDING_RESTART: dict | None = None
_PENDING_LOCK = threading.Lock()


def _session_active() -> bool:
    try:
        import game_api as _ga
        api = _ga.get()
        if api is None:
            return False
        r = getattr(api, "_runner", None)
        return r is not None and r.is_running()
    except Exception:
        return False


def is_restart_pending() -> bool:
    with _PENDING_LOCK:
        return _PENDING_RESTART is not None


def drain_pending_restart() -> None:
    """Execute the deferred restart if one was queued.

    Called by the bot's post-match hook between matches. Idempotent.
    """
    global _PENDING_RESTART
    with _PENDING_LOCK:
        pending = _PENDING_RESTART
        _PENDING_RESTART = None
    if pending is None:
        return
    log.info("draining pending restart: %s", pending.get("action"))
    action = pending.get("action")
    args = pending.get("args", {})
    if action == "git_update":
        _do_git_update(args)
    else:
        _do_bot_restart(args)


def _cmd_git_update(args: dict) -> dict:
    """Pull latest from origin/main and restart the bot service.

    Triggered by GitHub webhook → cloud → all workers. If a session is
    active we defer until the next post-match hook so we don't kill a
    match mid-flight (loses trophies + corrupts the grind state).
    The git pull auto-stashes local-only files (cfg/telegram.toml,
    cfg/device.toml, cfg/cloud.toml) so they survive the update.
    """
    global _PENDING_RESTART
    if _session_active() and not args.get("immediate"):
        with _PENDING_LOCK:
            _PENDING_RESTART = {"action": "git_update", "args": args}
        log.info("git_update queued — will fire at next end of match")
        return {"ok": True, "deferred": True, "sha": args.get("sha"),
                "msg": args.get("msg")}
    return _do_git_update(args)


def _do_git_update(args: dict) -> dict:
    bot_dir = str(Path(__file__).resolve().parent)
    try:
        # Stash anything locally modified (e.g. cfg/telegram.toml with token).
        stash = subprocess.run(["git", "stash", "push", "-u", "-m", "auto-deploy"],
                                cwd=bot_dir, capture_output=True, text=True, timeout=10)
        had_stash = "No local changes" not in stash.stdout
        # Pull.
        pull = subprocess.run(["git", "pull", "--ff-only"],
                               cwd=bot_dir, capture_output=True, text=True, timeout=30)
        if pull.returncode != 0:
            # Try to restore stash on failure.
            if had_stash:
                subprocess.run(["git", "stash", "pop"], cwd=bot_dir, timeout=10)
            return {"ok": False, "error": f"git pull failed: {pull.stderr[:300]}"}
        # Restore stashed local config files.
        if had_stash:
            subprocess.run(["git", "stash", "pop"], cwd=bot_dir, timeout=10)
        # Restart the bot via systemd (requires NOPASSWD sudoers rule for
        # `systemctl restart brawlbot`). The current process WILL be killed
        # so we reply quickly first; we use Popen + detached.
        subprocess.Popen(
            ["sudo", "-n", "systemctl", "restart", "brawlbot"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return {"ok": True, "sha": args.get("sha"), "msg": args.get("msg"),
                "pull_stdout": pull.stdout[-300:]}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _cmd_bot_restart(args: dict) -> dict:
    """Exit the bot — systemd (Linux) / NSSM (Windows) will respawn it.

    Deferred until end of match if a session is active, unless the
    caller passes `immediate=True` (e.g. emergency operator restart).
    """
    global _PENDING_RESTART
    if _session_active() and not args.get("immediate"):
        with _PENDING_LOCK:
            _PENDING_RESTART = {"action": "bot_restart", "args": args}
        log.info("bot_restart queued — will fire at next end of match")
        return {"ok": True, "deferred": True}
    return _do_bot_restart(args)


def _do_bot_restart(args: dict) -> dict:
    log.info("bot_restart firing now")
    threading.Timer(0.5, lambda: os._exit(0)).start()
    return {"ok": True, "output": "exiting in 0.5s"}


# ---- Health --------------------------------------------------------

_HEALTH_RE = {
    "battery": re.compile(r"level: (\d+)"),
    "battery_status": re.compile(r"status: (\d+)"),  # 2=charging, 3=discharging
    "battery_temp": re.compile(r"temperature: (\d+)"),  # 10ths of °C
}


def _collect_health() -> dict:
    """Combine adb queries for battery + storage + memory + model."""
    out: dict[str, Any] = {}
    # Phone model + Android version
    rc, m = _adb("shell", "getprop", "ro.product.model", timeout=4)
    if rc == 0: out["model"] = m.strip()
    rc, v = _adb("shell", "getprop", "ro.build.version.release", timeout=4)
    if rc == 0: out["android"] = v.strip()
    # Battery
    rc, b = _adb("shell", "dumpsys", "battery", timeout=4)
    if rc == 0:
        for k, rx in _HEALTH_RE.items():
            m_ = rx.search(b)
            if m_: out[k] = int(m_.group(1))
        if "battery_temp" in out:
            out["battery_temp_c"] = out.pop("battery_temp") / 10.0
    # Storage (free MB on /data)
    rc, d = _adb("shell", "df", "/data", timeout=4)
    if rc == 0:
        lines = d.strip().splitlines()
        if len(lines) >= 2:
            parts = lines[-1].split()
            if len(parts) >= 4:
                try:
                    avail_kb = int(parts[3])
                    out["storage_free_mb"] = avail_kb // 1024
                except Exception:
                    pass
    # RAM
    rc, mem = _adb("shell", "cat", "/proc/meminfo", timeout=4)
    if rc == 0:
        for line in mem.splitlines():
            if line.startswith("MemAvailable:"):
                kb = int(line.split()[1])
                out["ram_free_mb"] = kb // 1024
                break
            if line.startswith("MemTotal:"):
                kb = int(line.split()[1])
                out["ram_total_mb"] = kb // 1024
    # Brawl Stars running?
    rc, ps = _adb("shell", "pidof", BS_PACKAGE, timeout=4)
    bs_pid = ps.strip() if rc == 0 and ps.strip() else None
    out["brawlstars_pid"] = bs_pid
    # Foreground/lock detection: a running PID doesn't mean the user
    # actually sees BS — the phone may be locked or the user may have
    # swiped to another app. Determine the actual foreground state.
    foreground = False
    locked = False
    try:
        rc2, win = _adb("shell", "dumpsys", "window", "windows", timeout=5)
        if rc2 == 0:
            # mCurrentFocus / mFocusedApp lines tell us what's on top.
            focus_text = ""
            for line in win.splitlines():
                if "mCurrentFocus" in line or "mFocusedApp" in line:
                    focus_text += line + "\n"
            lf = focus_text.lower()
            if "keyguard" in lf or "statusbar" in lf or "lockscreen" in lf:
                locked = True
            elif BS_PACKAGE.lower() in lf:
                foreground = True
    except Exception:
        pass
    # Screen state via dumpsys display.
    screen_on = None
    try:
        rc3, disp = _adb("shell", "dumpsys", "display", timeout=4)
        if rc3 == 0:
            if "mScreenState=ON" in disp:
                screen_on = True
            elif "mScreenState=OFF" in disp:
                screen_on = False
                locked = True  # screen off implies user can't interact
    except Exception:
        pass
    out["brawlstars_foreground"] = bool(bs_pid) and foreground and not locked
    out["screen_locked"] = locked
    out["screen_on"] = screen_on
    return out


def _cmd_health(args: dict) -> dict:
    out = _collect_health()
    # Surface the battery-gate toggle state so the panel can render the
    # control without an extra round-trip.
    try:
        import game_api as _ga
        out["battery_gate_enabled"] = _ga.battery_gate_enabled()
    except Exception:
        pass
    return out


# ---- bot session control (proxy to local panel API) --------------

LOCAL_PANEL = "http://127.0.0.1:8000"


def _local_get(path: str, timeout: float = 10) -> dict:
    import requests
    r = requests.get(f"{LOCAL_PANEL}{path}", timeout=timeout)
    r.raise_for_status()
    return r.json()


def _local_post(path: str, body: dict | None = None, timeout: float = 10) -> dict:
    import requests
    r = requests.post(f"{LOCAL_PANEL}{path}", json=body or {}, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _resolve_local_account_id(tag: str) -> int | None:
    """Find the local DB account_id matching this cloud tag."""
    try:
        accs = _local_get("/api/accounts")
        for a in accs:
            if a.get("tag") == tag:
                return a.get("id")
    except Exception as e:
        log.warning("could not list local accounts: %s", e)
    return None


def _cmd_session_push_max(args: dict) -> dict:
    tag = args.get("tag")
    if not tag:
        return {"ok": False, "error": "missing tag"}
    aid = _resolve_local_account_id(tag)
    if aid is None:
        return {"ok": False, "error": f"account {tag} not found locally"}
    body = {}
    if args.get("target_total_trophies") is not None:
        body["target_total_trophies"] = int(args["target_total_trophies"])
    # Local endpoint calls fetch_account_profile → cloud → flaresolverr,
    # which can take 12-15s on a cold call. Allow up to 90s.
    return _local_post(f"/api/accounts/{aid}/push_max", body, timeout=90)


def _cmd_session_start(args: dict) -> dict:
    """Start a single-brawler session: args = {tag, brawler, target_trophies}"""
    tag = args.get("tag")
    brawler = args.get("brawler")
    target = args.get("target_trophies")
    if not (tag and brawler and target):
        return {"ok": False, "error": "missing tag/brawler/target_trophies"}
    aid = _resolve_local_account_id(tag)
    if aid is None:
        return {"ok": False, "error": f"account {tag} not found locally"}
    return _local_post(f"/api/accounts/{aid}/start",
                       {"brawler": brawler, "target_trophies": int(target)})


def _cmd_session_stop(args: dict) -> dict:
    tag = args.get("tag")
    force = args.get("force", False)
    if not tag:
        return {"ok": False, "error": "missing tag"}
    aid = _resolve_local_account_id(tag)
    if aid is None:
        return {"ok": False, "error": f"account {tag} not found locally"}
    endpoint = "forcestop" if force else "stop"
    return _local_post(f"/api/accounts/{aid}/{endpoint}")


def _cmd_session_state(args: dict) -> dict:
    tag = args.get("tag")
    if not tag:
        return {"ok": False, "error": "missing tag"}
    aid = _resolve_local_account_id(tag)
    if aid is None:
        return {"ok": False, "error": "account not found locally"}
    try:
        st = _local_get(f"/api/accounts/{aid}/push_max_state")
        return {"ok": True, "state": st}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _cmd_list_brawlers(args: dict) -> dict:
    tag = args.get("tag")
    if not tag:
        return {"ok": False, "error": "missing tag"}
    aid = _resolve_local_account_id(tag)
    if aid is None:
        return {"ok": False, "error": "account not found locally"}
    try:
        data = _local_get(f"/api/accounts/{aid}/brawlers")
        return {"ok": True, "brawlers": data}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---- game API proxies (low-level primitives) ----------------------

def _cmd_game_state(args: dict) -> dict:
    return _local_get("/api/game/state")


def _cmd_game_screenshot(args: dict) -> dict:
    return _local_get("/api/game/screenshot")


def _cmd_game_trophies(args: dict) -> dict:
    return _local_get("/api/game/trophies")


def _cmd_game_current_brawler(args: dict) -> dict:
    return _local_get("/api/game/current_brawler")


def _cmd_game_brawlers(args: dict) -> dict:
    force = "?force=true" if args.get("force") else ""
    return _local_get(f"/api/game/brawlers{force}")


def _cmd_game_select_brawler(args: dict) -> dict:
    return _local_post("/api/game/select_brawler", {"name": args.get("name", "")})


def _cmd_game_tap(args: dict) -> dict:
    return _local_post("/api/game/tap", {
        "x_ratio": float(args.get("x_ratio", 0.5)),
        "y_ratio": float(args.get("y_ratio", 0.5)),
    })


def _cmd_game_goto_lobby(args: dict) -> dict:
    # goto_lobby loops up to 20 iterations * ~5s each = 100s worst case
    return _local_post("/api/game/goto_lobby", timeout=140)


def _cmd_game_play_one_match(args: dict) -> dict:
    timeout_s = float(args.get("timeout_s", 420))
    return _local_post("/api/game/play_one_match", {
        "brawler": args.get("brawler"),
        "timeout_s": timeout_s,
        "required_mode": args.get("required_mode"),
    }, timeout=timeout_s + 30)


def _cmd_game_current_mode(args: dict) -> dict:
    return _local_get("/api/game/current_mode")


def _cmd_alerts_get(args: dict) -> dict:
    return _local_get("/api/alerts")


def _cmd_alerts_update(args: dict) -> dict:
    event = args.get("event")
    if not event:
        return {"ok": False, "error": "missing event"}
    body = {k: v for k, v in args.items() if k in ("enabled", "template", "filter")}
    # Use _local_put (we don't have one — inline)
    import requests
    r = requests.put(f"{LOCAL_PANEL}/api/alerts/{event}", json=body, timeout=10)
    r.raise_for_status()
    return r.json()


def _local_snapshot() -> dict | None:
    """Fetch a lightweight snapshot (state + trophies + tag + session)."""
    try:
        snap = _local_get("/api/game/snapshot")
    except Exception:
        return None
    # Add account tag + session active if any session running.
    try:
        accs = _local_get("/api/accounts")
        snap["account_tag"] = accs[0]["tag"] if accs else None
    except Exception:
        snap["account_tag"] = None
    return snap


# ---- dispatch table ----------------------------------------------

COMMANDS: dict[str, Callable[[dict], dict]] = {
    "screenshot":         _cmd_screenshot,
    "brawlstars_start":   _cmd_brawlstars_start,
    "brawlstars_stop":    _cmd_brawlstars_stop,
    "brawlstars_restart": _cmd_brawlstars_restart,
    "phone_reboot":       _cmd_phone_reboot,
    "adb_reconnect":      _cmd_adb_reconnect,
    "battery_gate_set":   _cmd_battery_gate_set,
    "battery_gate_get":   _cmd_battery_gate_get,
    "bot_restart":        _cmd_bot_restart,
    "git_update":         _cmd_git_update,
    "health":             _cmd_health,
    # bot session control
    "session_push_max":   _cmd_session_push_max,
    "session_start":      _cmd_session_start,
    "session_stop":       _cmd_session_stop,
    "session_state":      _cmd_session_state,
    "list_brawlers":      _cmd_list_brawlers,
    # game API primitives (low-level)
    "game_state":            _cmd_game_state,
    "game_screenshot":       _cmd_game_screenshot,
    "game_trophies":         _cmd_game_trophies,
    "game_current_brawler":  _cmd_game_current_brawler,
    "game_brawlers":         _cmd_game_brawlers,
    "game_select_brawler":   _cmd_game_select_brawler,
    "game_tap":              _cmd_game_tap,
    "game_goto_lobby":       _cmd_game_goto_lobby,
    "game_play_one_match":   _cmd_game_play_one_match,
    "game_current_mode":     _cmd_game_current_mode,
    # Telegram alerts config
    "alerts_get":            _cmd_alerts_get,
    "alerts_update":         _cmd_alerts_update,
}


# ------------------------------------------------------ ws client


async def _run_ws_client():
    cfg = _cfg()
    if not cfg or not cfg.get("enabled"):
        log.info("worker_link: cloud disabled, not starting WS")
        return
    url = cfg["url"].rstrip("/")
    # Convert https:// to wss://
    if url.startswith("https://"):
        ws_url = "wss://" + url[len("https://"):]
    elif url.startswith("http://"):
        ws_url = "ws://" + url[len("http://"):]
    else:
        ws_url = url
    instance_id = cfg["instance_id"]
    token = cfg["token"]
    ws_url = f"{ws_url}/ws/worker?token={token}&instance_id={instance_id}"

    # Lazy import so the worker_link only requires `websockets` when enabled.
    import websockets

    last_log_offset = 0
    last_health_at = 0.0
    last_snapshot_at = 0.0

    while True:  # outer reconnect loop
        try:
            log.info("worker_link: connecting to %s", ws_url.split("token=")[0] + "token=…")
            async with websockets.connect(ws_url, ping_interval=20, ping_timeout=20,
                                          max_size=10 * 1024 * 1024) as ws:
                log.info("worker_link: connected")
                await ws.send(json.dumps({"type": "hello", "instance_id": instance_id}))
                # Immediate snapshot so the UI populates without waiting for
                # the periodic 10s tick.
                try:
                    snap = await asyncio.get_running_loop().run_in_executor(None, _local_snapshot)
                    if snap:
                        await ws.send(json.dumps({"type": "snapshot", "data": snap}))
                        last_snapshot_at = time.time()
                except Exception:
                    pass
                # Self-heal: check if we're behind origin/main and self-update.
                # This catches the case where the cloud panel was redeploying
                # while a GitHub push happened so the broadcast was missed.
                try:
                    await asyncio.get_running_loop().run_in_executor(None, _check_and_self_update)
                except Exception:
                    log.debug("self-update check failed", exc_info=True)
                # Re-sync local match history to cloud (in case cloud DB was
                # wiped by a Dokploy redeploy while we were disconnected).
                try:
                    import cloud_sync as _cs
                    await asyncio.get_running_loop().run_in_executor(None, _cs.sync_history_to_cloud)
                    # Also refresh brawlers so the panel shows fresh trophies
                    # right after reconnect (no waiting for the 1h tick).
                    await asyncio.get_running_loop().run_in_executor(None, _cs.refresh_and_push_brawlers)
                except Exception:
                    log.debug("history/brawlers sync on reconnect failed", exc_info=True)

                async def sender_loop():
                    nonlocal last_log_offset, last_health_at, last_snapshot_at
                    log_path = Path(__file__).resolve().parent / "logs" / "bot.log"
                    while True:
                        await asyncio.sleep(2.0)
                        # log tail (delta)
                        try:
                            if log_path.exists():
                                size = log_path.stat().st_size
                                if size < last_log_offset:
                                    last_log_offset = 0   # log rotated
                                if size > last_log_offset:
                                    with log_path.open("rb") as f:
                                        f.seek(last_log_offset)
                                        chunk = f.read(64 * 1024).decode("utf-8", errors="replace")
                                        last_log_offset = f.tell()
                                    new_lines = [l for l in chunk.splitlines() if l.strip()]
                                    if new_lines:
                                        await ws.send(json.dumps({"type": "log", "lines": new_lines}))
                        except websockets.exceptions.ConnectionClosed:
                            return  # outer loop will reconnect
                        except Exception:
                            log.exception("log tail failed")
                        # screenshots are 100% on-demand (user clicks → ws cmd).
                        # No periodic push — saves CPU during matches and keeps
                        # the bot's vision loop unaffected.
                        now = time.time()
                        # health every 30s
                        if now - last_health_at >= 30:
                            try:
                                data = await asyncio.get_running_loop().run_in_executor(
                                    None, _collect_health)
                                await ws.send(json.dumps({"type": "health", "data": data}))
                                last_health_at = now
                            except websockets.exceptions.ConnectionClosed:
                                return
                            except Exception:
                                log.debug("health push failed")
                        # game snapshot every 10s (state + trophies, no image)
                        if now - last_snapshot_at >= 10:
                            try:
                                snap = await asyncio.get_running_loop().run_in_executor(
                                    None, _local_snapshot)
                                if snap:
                                    await ws.send(json.dumps({"type": "snapshot", "data": snap}))
                                last_snapshot_at = now
                            except websockets.exceptions.ConnectionClosed:
                                return
                            except Exception:
                                log.debug("snapshot push failed")

                sender_task = asyncio.create_task(sender_loop())

                async def _run_cmd(cmd_id: str, name: str, handler, args: dict) -> None:
                    """Run a single command in the thread pool and reply.

                    Fired as a task so long commands (play_one_match,
                    list_brawlers) don't block screenshot / state / etc.
                    """
                    try:
                        data = await asyncio.get_running_loop().run_in_executor(
                            None, handler, args)
                        await ws.send(json.dumps({
                            "type": "cmd_result", "id": cmd_id,
                            "ok": True, "data": data,
                        }))
                    except websockets.exceptions.ConnectionClosed:
                        pass
                    except Exception as exc:
                        log.exception("command %s failed", name)
                        try:
                            await ws.send(json.dumps({
                                "type": "cmd_result", "id": cmd_id,
                                "ok": False, "error": str(exc),
                            }))
                        except Exception:
                            pass

                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    if msg.get("type") == "cmd":
                        cmd_id = msg.get("id")
                        name = msg.get("name")
                        args = msg.get("args") or {}
                        handler = COMMANDS.get(name)
                        if handler is None:
                            await ws.send(json.dumps({
                                "type": "cmd_result", "id": cmd_id,
                                "ok": False, "error": f"unknown command: {name}",
                            }))
                            continue
                        # Fire-and-forget so this command can run in
                        # parallel with others (e.g. a 5 min play_one_match
                        # must not block screenshot requests).
                        asyncio.create_task(_run_cmd(cmd_id, name, handler, args))
                sender_task.cancel()
        except Exception as exc:
            log.warning("worker_link disconnected: %s", exc)
        # exponential-ish backoff before reconnecting
        await asyncio.sleep(5.0)


def start() -> None:
    """Spawn the WS client in a background thread with its own asyncio loop."""
    cfg = _cfg()
    if not cfg or not cfg.get("enabled"):
        log.info("worker_link disabled")
        return

    def runner():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_run_ws_client())
        except Exception:
            log.exception("worker_link crashed terminally")

    t = threading.Thread(target=runner, daemon=True, name="worker_link")
    t.start()
    log.info("worker_link thread started")
