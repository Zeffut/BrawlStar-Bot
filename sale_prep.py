"""Auto-prep an account for sale when it reaches the sale target.

When `play_schedule` reports "sale_ready" (the account hit `sale_target_trophies`),
this runs — BEFORE the account is marked ready-to-sell — to maximise its value:
  1. buy the max hypercharges affordable (deterministic 5000 gold/HC, primary value),
  2. upgrade as many brawlers as possible toward power 11 with the remaining gold,
then notifies + marks the account ready.

SAFETY: gated by `sale_prep_mode` (cfg/general_config.toml):
  - "off"  : no prep — just notify + mark ready (legacy behaviour).
  - "plan" : DRY-RUN — compute & notify what it WOULD buy/upgrade, spend NOTHING.
             (default; safe while the shop_actions live-spend tap geometry is not
             yet calibrated.)
  - "live" : actually spend (enable only AFTER a supervised calibration pass on an
             account that has a buyable hypercharge).

Runs in a background thread (the prep drives the device for several minutes) and is
idempotent per (tag, target) via cfg/sale_prep_state.json. The notification goes
through cloud_sync.event (the working channel — the worker's own Telegram token is
revoked), and the legacy sale_report is still sent best-effort.
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

log = logging.getLogger("sale_prep")

_STATE_PATH = Path("cfg/sale_prep_state.json")
_VALID_MODES = ("off", "plan", "live")
_in_progress: "set[str]" = set()
_lock = threading.Lock()


def mode() -> str:
    """Read sale_prep_mode from cfg/general_config.toml (default 'plan').
    Uses `toml` directly (not utils) to avoid utils' heavy module-level imports."""
    try:
        import os
        import toml
        path = "cfg/general_config.toml"
        if os.path.exists(path):
            cfg = toml.load(path) or {}
            m = str(cfg.get("sale_prep_mode", "plan")).strip().lower()
            return m if m in _VALID_MODES else "plan"
    except Exception:
        pass
    return "plan"


def completed(tag: str, target: int) -> bool:
    """True if this tag was already prepped at >= this target."""
    try:
        state = json.loads(_STATE_PATH.read_text())
        return int(state.get(tag, 0)) >= int(target)
    except Exception:
        return False


def mark_done(tag: str, target: int) -> None:
    try:
        state = {}
        if _STATE_PATH.exists():
            state = json.loads(_STATE_PATH.read_text())
        state[tag] = int(target)
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _STATE_PATH.write_text(json.dumps(state))
    except Exception:
        log.warning("could not persist sale_prep state", exc_info=True)


def is_in_progress(tag: str) -> bool:
    with _lock:
        return tag in _in_progress


def _set_in_progress(tag: str) -> bool:
    """Atomically claim the in-progress slot. Returns False if already claimed."""
    with _lock:
        if tag in _in_progress:
            return False
        _in_progress.add(tag)
        return True


def _clear_in_progress(tag: str) -> None:
    with _lock:
        _in_progress.discard(tag)


def run(tag: str, target: int, serial: str) -> dict:
    """Core orchestration (synchronous — the caller runs it in a thread).
    Returns a summary dict. HC first (deterministic value), then power upgrades."""
    m = mode()
    if m == "off":
        return {"mode": "off", "live": False, "skipped": True,
                "hc_count": 0, "upgrade_count": 0,
                "coins_before": None, "coins_after": None}
    live = (m == "live")
    from revente.shop_actions import ShopActionEngine
    eng = ShopActionEngine(serial, dry_run=not live)
    buy = eng.buy_hypercharges(confirm=live)
    up = eng.upgrade_power(target_level=11, scope="walk", confirm=live, max_brawlers=12)
    hc_count = len([a for a in buy.planned if a.kind == "buy_hypercharge"])
    upgrade_count = len(up.planned)
    return {
        "mode": m, "live": live, "skipped": False,
        "hc_count": hc_count, "upgrade_count": upgrade_count,
        "coins_before": buy.coins_before, "coins_after": up.coins_after,
        "hc": buy.as_dict(), "upgrades": up.as_dict(),
    }


def format_summary(tag: str, target: int, summary: dict) -> str:
    """Short FR notification message describing what was done / planned."""
    m = summary.get("mode")
    hc = summary.get("hc_count", 0)
    up = summary.get("upgrade_count", 0)
    flag = "\U0001F3C1"  # 🏁
    if m == "off":
        return f"{flag} Compte {tag} prêt à vendre ({target} tr). Prépa désactivée (mode 'off')."
    if not summary.get("live"):
        return (f"{flag} Compte {tag} prêt à vendre ({target} tr).\n"
                f"PLAN (dry-run, rien dépensé) : achèterait {hc} hypercharge(s) "
                f"+ {up} amélioration(s) de power.\n"
                f"Passe sale_prep_mode='live' (après calibration) pour exécuter pour de vrai.")
    cb = summary.get("coins_before")
    ca = summary.get("coins_after")
    return (f"{flag} Compte {tag} prêt à vendre ({target} tr).\n"
            f"Prépa LIVE : {hc} hypercharge(s) achetée(s), {up} amélioration(s) de power. "
            f"Or {cb}→{ca}.")


def maybe_start(bot, tag: str, target: int, send_fn) -> None:
    """Launch the prep (once) in a background thread, then notify + mark ready.
    No-op if already completed or in progress."""
    if completed(tag, target):
        return
    if not _set_in_progress(tag):
        return
    threading.Thread(target=_run_and_finish,
                     args=(bot, tag, target, send_fn), daemon=True).start()


def _run_and_finish(bot, tag: str, target: int, send_fn) -> None:
    # Suppress game_api's idle watchdog / goto_lobby while the prep drives the device
    # (otherwise game_api fights us — it would keep forcing the lobby). goto_lobby and
    # the watchdog stand down when _power_save_active is set. Restored in `finally`.
    api = None
    try:
        import game_api
        api = game_api.get()
        if api is not None:
            api._power_save_active = True
    except Exception:
        api = None
    try:
        import device
        serial = device.adb_serial()
        summary = run(tag, target, serial)
        text = format_summary(tag, target, summary)
        try:
            import cloud_sync
            cloud_sync.event("sale_ready", {
                "tag": tag, "target": target, "text": text,
                "mode": summary.get("mode"),
                "hc_count": summary.get("hc_count", 0),
                "upgrade_count": summary.get("upgrade_count", 0),
            }, tag=tag)
        except Exception:
            log.warning("sale_prep cloud notif failed", exc_info=True)
        try:
            import sale_report
            sale_report.build_and_send(tag, target, send_fn)  # legacy, best-effort
            sale_report.mark_notified(tag, target)
        except Exception:
            log.warning("sale_prep legacy report failed", exc_info=True)
        mark_done(tag, target)
        log.info("sale_prep done for %s: %s", tag, text.replace("\n", " | "))
    except Exception:
        log.exception("sale_prep failed for %s", tag)
    finally:
        if api is not None:
            try:
                api._power_save_active = False
            except Exception:
                pass
        _clear_in_progress(tag)
