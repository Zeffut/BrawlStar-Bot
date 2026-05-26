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
    serial = _adb_serial()
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


def _cmd_bot_restart(args: dict) -> dict:
    """Exit the bot — systemd will respawn it on Linux."""
    log.info("bot_restart requested by cloud")
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
    out["brawlstars_pid"] = ps.strip() if rc == 0 and ps.strip() else None
    return out


def _cmd_health(args: dict) -> dict:
    return _collect_health()


# ---- bot session control (proxy to local panel API) --------------

LOCAL_PANEL = "http://127.0.0.1:8000"


def _local_get(path: str) -> dict:
    import requests
    r = requests.get(f"{LOCAL_PANEL}{path}", timeout=10)
    r.raise_for_status()
    return r.json()


def _local_post(path: str, body: dict | None = None) -> dict:
    import requests
    r = requests.post(f"{LOCAL_PANEL}{path}", json=body or {}, timeout=10)
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
    return _local_post(f"/api/accounts/{aid}/push_max")


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
    return _local_post("/api/game/goto_lobby")


def _cmd_game_play_one_match(args: dict) -> dict:
    return _local_post("/api/game/play_one_match", {
        "brawler": args.get("brawler"),
        "timeout_s": float(args.get("timeout_s", 420)),
    })


# ---- dispatch table ----------------------------------------------

COMMANDS: dict[str, Callable[[dict], dict]] = {
    "screenshot":         _cmd_screenshot,
    "brawlstars_start":   _cmd_brawlstars_start,
    "brawlstars_stop":    _cmd_brawlstars_stop,
    "brawlstars_restart": _cmd_brawlstars_restart,
    "phone_reboot":       _cmd_phone_reboot,
    "adb_reconnect":      _cmd_adb_reconnect,
    "bot_restart":        _cmd_bot_restart,
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
    last_screenshot_at = 0.0
    last_health_at = 0.0

    while True:  # outer reconnect loop
        try:
            log.info("worker_link: connecting to %s", ws_url.split("token=")[0] + "token=…")
            async with websockets.connect(ws_url, ping_interval=20, ping_timeout=20,
                                          max_size=10 * 1024 * 1024) as ws:
                log.info("worker_link: connected")
                await ws.send(json.dumps({"type": "hello", "instance_id": instance_id}))

                async def sender_loop():
                    nonlocal last_log_offset, last_screenshot_at, last_health_at
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

                sender_task = asyncio.create_task(sender_loop())

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
                        try:
                            data = await asyncio.get_running_loop().run_in_executor(
                                None, handler, args)
                            await ws.send(json.dumps({
                                "type": "cmd_result", "id": cmd_id,
                                "ok": True, "data": data,
                            }))
                        except Exception as exc:
                            log.exception("command %s failed", name)
                            await ws.send(json.dumps({
                                "type": "cmd_result", "id": cmd_id,
                                "ok": False, "error": str(exc),
                            }))
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
