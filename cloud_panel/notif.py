"""Panel-side notification dispatcher.

The panel is now the single source of truth for notifications. Workers
emit raw events (match, battery_low, …) via WS/HTTP; this module reads
the global config and decides what to send through which channel.

Channels supported:
  - Telegram: bot_token + chat_id
  - Discord:  webhook_url

Config is stored in the `config` SQLite table under the key `notif`,
shape:

    {
      "telegram": {"enabled": bool, "bot_token": str, "chat_id": str},
      "discord":  {"enabled": bool, "webhook_url": str},
      "events":   {
        "<event_type>": {"telegram": bool, "discord": bool}
      }
    }

Event types: match, target_reached, battery_low, battery_resumed,
bot_stuck, instance_connected, instance_disconnected, session_ended.
"""
from __future__ import annotations

import logging
import os
import threading
import urllib.parse
import urllib.request

import db

log = logging.getLogger("notif")

DEFAULT_CONFIG: dict = {
    "telegram": {"enabled": False, "bot_token": "", "chat_id": ""},
    "discord":  {"enabled": False, "webhook_url": ""},
    "events": {
        "match":                 {"telegram": False, "discord": False},
        "target_reached":        {"telegram": True,  "discord": True},
        "sale_ready":            {"telegram": True,  "discord": True},
        "battery_low":           {"telegram": True,  "discord": True},
        "battery_resumed":       {"telegram": False, "discord": False},
        "bot_stuck":             {"telegram": True,  "discord": True},
        # instance_connected removed — too noisy on self-updates.
        "instance_disconnected": {"telegram": True,  "discord": True},
        "session_ended":         {"telegram": False, "discord": False},
    },
}

# Known event types — used by the UI to render checkboxes consistently
# even when the stored config is missing some.
KNOWN_EVENTS = tuple(DEFAULT_CONFIG["events"].keys())


def _env_seed() -> dict:
    """Bootstrap config from env vars.

    Used when the SQLite DB is empty (fresh deploy or wiped volume on
    Dokploy redeploy). Vars survive redeploys via the platform's env
    config, so the panel doesn't lose Telegram/Discord setup whenever
    the container is recreated.
    """
    seed: dict = {}
    tg_token = os.environ.get("NOTIF_TELEGRAM_BOT_TOKEN", "").strip()
    tg_chat = os.environ.get("NOTIF_TELEGRAM_CHAT_ID", "").strip()
    if tg_token and tg_chat:
        seed["telegram"] = {
            "enabled": True, "bot_token": tg_token, "chat_id": tg_chat,
        }
    dc_url = os.environ.get("NOTIF_DISCORD_WEBHOOK_URL", "").strip()
    if dc_url:
        seed["discord"] = {"enabled": True, "webhook_url": dc_url}
    return seed


def get_config() -> dict:
    """Return the current config merged onto defaults.

    Layering: DEFAULT_CONFIG ← env seed (NOTIF_* vars) ← stored DB row.
    Stored UI edits always win; env vars only fill the gap when the
    user has never saved anything (or the DB was wiped).
    """
    stored = db.get_config("notif")
    base = _merge(DEFAULT_CONFIG, _env_seed())
    return _merge(base, stored)


def set_config(payload: dict) -> dict:
    """Validate + persist config. Returns the merged effective config.

    Merges onto the CURRENT effective config (not bare defaults) so a partial
    update — e.g. toggling one event's telegram flag — preserves everything
    else (bot_token, other events, …)."""
    merged = _merge(get_config(), payload)
    db.set_config("notif", merged)
    return merged


def _merge(default: dict, override: dict) -> dict:
    """Deep-merge override into a copy of default (override wins)."""
    out = {}
    for k, v in default.items():
        if k in override:
            if isinstance(v, dict) and isinstance(override[k], dict):
                out[k] = _merge(v, override[k])
            else:
                out[k] = override[k]
        else:
            out[k] = v.copy() if isinstance(v, dict) else v
    # Carry over keys only present in override.
    for k, v in override.items():
        if k not in out:
            out[k] = v
    return out


def format_event(event_type: str, payload: dict) -> str:
    """Build a human-readable message for an event payload."""
    e = event_type
    if e == "match":
        emo = {"victory": "🏆", "defeat": "💀", "draw": "🤝"}.get(payload.get("result"), "•")
        d = payload.get("delta")
        sign = "+" if d is not None and d >= 0 else ""
        delta_txt = f" ({sign}{d} 🏆)" if d is not None else ""
        return (
            f"{emo} {payload.get('result', '?').upper()} — {payload.get('brawler', '?')}"
            f"{delta_txt} • {payload.get('account_trophies_after', '?')} total"
        )
    if e == "target_reached":
        return (
            f"🎯 Cible atteinte : {payload.get('account_trophies', '?')}/{payload.get('target', '?')} 🏆"
            f" (dernier brawler : {payload.get('brawler', '?')})"
        )
    if e == "battery_low":
        return f"🪫 Batterie faible ({payload.get('level', '?')}%) — bot en pause."
    if e == "battery_resumed":
        return f"⚡ Batterie OK ({payload.get('level', '?')}%) — bot reprend."
    if e == "bot_stuck":
        return (
            f"⚠️ Bot semble bloqué — aucun match depuis {payload.get('minutes', '?')} min.\n"
            f"Brawler : {payload.get('brawler', '?')}"
        )
    if e == "instance_connected":
        return f"🟢 Instance connectée : {payload.get('instance_id', '?')}"
    if e == "instance_disconnected":
        return f"🔴 Instance déconnectée : {payload.get('instance_id', '?')}"
    if e == "session_ended":
        return (
            f"🏁 Session terminée — {payload.get('matches', 0)} matches, "
            f"W/L/D {payload.get('wins', 0)}/{payload.get('losses', 0)}/{payload.get('draws', 0)}"
        )
    if e == "sale_ready":
        return payload.get("text") or (
            f"🏁 Compte prêt à vendre : {payload.get('tag', '?')} "
            f"({payload.get('target', '?')} 🏆)"
        )
    return f"{e}: {payload}"


def dispatch(event_type: str, payload: dict) -> None:
    """Decide + fire-and-forget any enabled notifications."""
    cfg = get_config()
    routing = cfg.get("events", {}).get(event_type, {})
    if not routing:
        return
    message = format_event(event_type, payload)
    if routing.get("telegram") and cfg["telegram"].get("enabled"):
        _send_telegram(cfg["telegram"], message)
    if routing.get("discord") and cfg["discord"].get("enabled"):
        _send_discord(cfg["discord"], message)


def _send_telegram(tg_cfg: dict, message: str) -> None:
    token = tg_cfg.get("bot_token", "").strip()
    chat_id = tg_cfg.get("chat_id", "").strip()
    if not token or not chat_id:
        log.warning("telegram notif: missing token or chat_id")
        return

    def _send():
        try:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            data = urllib.parse.urlencode({
                "chat_id": chat_id, "text": message, "parse_mode": "HTML",
            }).encode()
            req = urllib.request.Request(url, data=data, method="POST")
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status != 200:
                    log.warning("telegram notif: HTTP %d", resp.status)
        except Exception as exc:
            log.warning("telegram notif failed: %s", exc)

    threading.Thread(target=_send, daemon=True, name="notif-telegram").start()


def _send_discord(dc_cfg: dict, message: str) -> None:
    webhook = dc_cfg.get("webhook_url", "").strip()
    if not webhook:
        log.warning("discord notif: missing webhook_url")
        return

    def _send():
        try:
            import json as _json
            req = urllib.request.Request(
                webhook,
                data=_json.dumps({"content": message}).encode(),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status not in (200, 204):
                    log.warning("discord notif: HTTP %d", resp.status)
        except Exception as exc:
            log.warning("discord notif failed: %s", exc)

    threading.Thread(target=_send, daemon=True, name="notif-discord").start()


def send_test(channel: str) -> tuple[bool, str]:
    """Send a 'test' message to confirm credentials. Returns (ok, msg)."""
    cfg = get_config()
    if channel == "telegram":
        if not cfg["telegram"].get("enabled"):
            return False, "Telegram is disabled"
        _send_telegram(cfg["telegram"], "🧪 Test notification from BrawlStar-Bot panel.")
        return True, "Sent (check Telegram)"
    if channel == "discord":
        if not cfg["discord"].get("enabled"):
            return False, "Discord is disabled"
        _send_discord(cfg["discord"], "🧪 Test notification from BrawlStar-Bot panel.")
        return True, "Sent (check Discord)"
    return False, f"unknown channel: {channel}"
