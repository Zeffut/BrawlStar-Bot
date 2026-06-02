"""Cloud-side Telegram bot — the single interactive controller for the whole
fleet, locked to ONE chat id (the owner).

Why it lives on the cloud panel and not the worker:
  - Telegram allows exactly ONE update consumer per token. With several
    workers each polling getUpdates we'd get 409 conflicts. The panel is the
    single place that already sees every instance (via the WS HUB) and the
    full DB, so it's the natural home.
  - We use a WEBHOOK (not long-polling): Telegram POSTs updates to
    /api/telegram/webhook. Registering the webhook also makes any leftover
    worker getUpdates loop start failing harmlessly (409) → the cloud bot
    cleanly takes over.

Security: every update is checked against the configured chat id; anything from
another chat is ignored. The webhook endpoint also validates a secret header.
"""
from __future__ import annotations

import base64
import html
import json as _json
import logging
import os
import urllib.parse
import urllib.request

import db
from ws import HUB

log = logging.getLogger("telegram_bot")

_API = "https://api.telegram.org/bot{token}/{method}"
WEBHOOK_SECRET = os.environ.get("TG_WEBHOOK_SECRET", "brawlfleet-tg-hook-v1")
PUBLIC_URL = os.environ.get("PANEL_PUBLIC_URL", "https://brawlpanel.zeffut.fr")
DEFAULT_CEILING = 750


# ------------------------------------------------------------- config + HTTP


def _conf() -> tuple[str, str] | None:
    """(token, chat_id) from the panel notif config, or None if not set up."""
    try:
        import notif
        cfg = notif.get_config().get("telegram", {}) or {}
    except Exception:
        return None
    token = (cfg.get("bot_token") or "").strip()
    chat = str(cfg.get("chat_id") or "").strip()
    if not token or not chat:
        return None
    return token, chat


def is_configured() -> bool:
    return _conf() is not None


def _post(method: str, **payload) -> dict:
    """POST urlencoded form data to the Telegram API (stdlib urllib — the cloud
    image has no `requests`)."""
    c = _conf()
    if not c:
        return {}
    token, _ = c
    try:
        data = urllib.parse.urlencode(
            {k: v for k, v in payload.items() if v is not None}).encode()
        req = urllib.request.Request(
            _API.format(token=token, method=method), data=data, method="POST")
        with urllib.request.urlopen(req, timeout=20) as resp:
            return _json.loads(resp.read().decode())
    except Exception as exc:
        log.warning("telegram %s failed: %s", method, exc)
        return {}


def _esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def send(text: str, kb=None, chat: str | None = None) -> dict:
    c = _conf()
    if not c:
        return {}
    _, chat_id = c
    payload = {"chat_id": chat or chat_id, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    if kb is not None:
        payload["reply_markup"] = _json_kb(kb)
    return _post("sendMessage", **payload)


def edit(message_id: int, text: str, kb=None, chat: str | None = None) -> dict:
    c = _conf()
    if not c:
        return {}
    _, chat_id = c
    payload = {"chat_id": chat or chat_id, "message_id": message_id,
               "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if kb is not None:
        payload["reply_markup"] = _json_kb(kb)
    return _post("editMessageText", **payload)


def send_photo(jpeg_bytes: bytes, caption: str = "", chat: str | None = None) -> dict:
    """sendPhoto via a hand-built multipart/form-data body (stdlib only)."""
    c = _conf()
    if not c:
        return {}
    token, chat_id = c
    boundary = "----brawlfleetTGboundary7c3f"
    fields = {"chat_id": str(chat or chat_id), "caption": caption[:1000]}
    body = bytearray()
    for k, v in fields.items():
        body += f"--{boundary}\r\n".encode()
        body += f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode()
        body += f"{v}\r\n".encode()
    body += f"--{boundary}\r\n".encode()
    body += b'Content-Disposition: form-data; name="photo"; filename="screen.jpg"\r\n'
    body += b"Content-Type: image/jpeg\r\n\r\n"
    body += jpeg_bytes + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    try:
        req = urllib.request.Request(
            _API.format(token=token, method="sendPhoto"),
            data=bytes(body), method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=40) as resp:
            return _json.loads(resp.read().decode())
    except Exception as exc:
        log.warning("telegram sendPhoto failed: %s", exc)
        return {}


def answer_callback(callback_id: str, text: str = "") -> None:
    _post("answerCallbackQuery", callback_query_id=callback_id, text=text)


def _json_kb(kb) -> str:
    return _json.dumps({"inline_keyboard": kb})


def ensure_webhook(base_url: str | None = None) -> dict:
    """Register the webhook (idempotent). Safe to call on every startup."""
    if not _conf():
        log.info("telegram bot not configured — skipping webhook setup")
        return {"ok": False, "reason": "not configured"}
    url = (base_url or PUBLIC_URL).rstrip("/") + "/api/telegram/webhook"
    r = _post("setWebhook", url=url, secret_token=WEBHOOK_SECRET,
              allowed_updates=_json.dumps(["message", "callback_query"]),
              drop_pending_updates="false")
    log.info("telegram setWebhook(%s) → %s", url, r.get("description") or r.get("ok"))
    return r


def get_webhook_info() -> dict:
    """getWebhookInfo (diagnostics): url, pending count, last error."""
    return _post("getWebhookInfo").get("result", {})


# ------------------------------------------------------------- keyboards


def _main_menu_kb() -> list:
    return [
        [{"text": "📊 Statut flotte", "callback_data": "menu:status"},
         {"text": "📈 Rapport", "callback_data": "menu:report"}],
        [{"text": "👥 Comptes", "callback_data": "menu:accounts"},
         {"text": "⚙️ Réglages", "callback_data": "menu:settings"}],
    ]


def _account_kb(account_id: int, running: bool) -> list:
    aid = account_id
    rows = []
    if running:
        rows.append([{"text": "⏹ Stop", "callback_data": f"act:stop:{aid}"}])
    else:
        rows.append([{"text": "▶ Push Max", "callback_data": f"act:push:{aid}"}])
    rows.append([
        {"text": "📸 Écran", "callback_data": f"act:screen:{aid}"},
        {"text": "📊 Stats", "callback_data": f"act:stats:{aid}"},
        {"text": "⚡ Efficacité", "callback_data": f"act:eff:{aid}"},
    ])
    rows.append([
        {"text": "🔄 Redémarrer le bot", "callback_data": f"act:restart_ask:{aid}"},
    ])
    rows.append([{"text": "◀ Menu", "callback_data": "menu:main"}])
    return rows


def _back_kb() -> list:
    return [[{"text": "◀ Menu", "callback_data": "menu:main"}]]


# ------------------------------------------------------------- data helpers


def _push_ceiling() -> int:
    try:
        v = int(db.get_config("app").get("push_max_ceiling") or DEFAULT_CEILING)
        return v if v > 0 else DEFAULT_CEILING
    except Exception:
        return DEFAULT_CEILING


def _live_snapshot(instance_uid: str | None) -> dict:
    if not instance_uid:
        return {}
    conn = HUB.get(instance_uid)
    if conn and getattr(conn, "last_snapshot", None):
        return conn.last_snapshot or {}
    return {}


def _account_running(account_id: int) -> bool:
    try:
        return bool(db.current_session(account_id))
    except Exception:
        return False


def _fmt_signed(n) -> str:
    n = int(n or 0)
    return f"+{n}" if n > 0 else str(n)


def _fleet_summary() -> str:
    accs = db.list_accounts()
    if not accs:
        return "Aucun compte enregistré."
    lines = ["<b>🛰️ Flotte</b>"]
    tot_today = tot_matches = 0
    for a in accs:
        st = db.account_stats(a["id"])
        snap = _live_snapshot(a.get("instance_uid"))
        activity = snap.get("activity")
        running = _account_running(a["id"]) or bool(activity)
        dot = "🟢" if running else "⚪"
        net = st.get("net_today", 0); tot_today += net
        m = st.get("matches_today", 0); tot_matches += m
        wr = st.get("win_rate_24h")
        tph = st.get("trophies_per_hour")
        act = f" · {_esc(activity)}" if activity else ""
        lines.append(
            f"{dot} <b>{_esc(a.get('name') or a['tag'])}</b>{act}\n"
            f"   auj {_fmt_signed(net)} 🏆 · {m} matchs"
            + (f" · WR {wr}%" if wr is not None else "")
            + (f" · {tph}/h" if tph is not None else "")
        )
    lines.append(f"\n<b>Total aujourd'hui</b> : {_fmt_signed(tot_today)} 🏆 · {tot_matches} matchs")
    lines.append(f"Plafond push max : <b>{_push_ceiling()}</b> 🏆")
    return "\n".join(lines)


def _account_summary(account_id: int) -> str:
    a = db.get_account(account_id)
    if not a:
        return "Compte introuvable."
    st = db.account_stats(account_id)
    snap = _live_snapshot(a.get("instance_uid"))
    running = _account_running(account_id)
    activity = snap.get("activity")
    troph = db.latest_account_trophies(account_id)
    state = "🟢 en cours" if (running or activity) else "⚪ à l'arrêt"
    if activity:
        state += " · " + _esc(activity)
    lines = [
        f"<b>{_esc(a.get('name') or a['tag'])}</b>  <code>#{_esc(a['tag'])}</code>",
        "État : " + state,
    ]
    if troph is not None:
        lines.append(f"Trophées : <b>{troph}</b> 🏆")
    lines.append(
        f"Aujourd'hui : {_fmt_signed(st.get('net_today',0))} 🏆 · "
        f"{st.get('matches_today',0)} matchs"
        + (f" · WR {st['win_rate_24h']}%" if st.get('win_rate_24h') is not None else "")
        + (f" · {st['trophies_per_hour']}/h" if st.get('trophies_per_hour') is not None else "")
    )
    lines.append(
        f"7 j : WR {st.get('win_rate_7d','—')}% · {st.get('matches_7d',0)} matchs · "
        f"net 24h {_fmt_signed(st.get('net_24h',0))} 🏆"
    )
    return "\n".join(lines)


def _brawler_eff_text(account_id: int) -> str:
    a = db.get_account(account_id)
    rows = db.brawler_efficiency(account_id, days=7)
    if not rows:
        return "Pas encore de données d'efficacité (aucun match récent)."
    head = f"<b>⚡ Efficacité — {_esc(a.get('name') or a['tag']) if a else ''}</b> <i>(7 j)</i>\n"
    head += "<code>brawler      🏆/h   net   WR</code>"
    out = [head]
    for b in rows[:15]:
        name = (b["brawler"] or "")[:11].ljust(11)
        tph = b["trophies_per_hour"]
        tph_s = (f"{_fmt_signed(tph)}").rjust(5) if tph is not None else "   —"
        net = _fmt_signed(b["net"]).rjust(5)
        wr = (f"{b['win_rate']}%" if b["win_rate"] is not None else "—").rjust(4)
        out.append(f"<code>{_esc(name)} {tph_s} {net} {wr}</code>")
    return "\n".join(out)


# ------------------------------------------------------------- commands (WS)


async def _send_cmd(account_id: int, name: str, args: dict | None = None,
                    timeout: float = 15) -> tuple[dict | None, str | None]:
    acc = db.get_account(account_id)
    if not acc:
        return None, "compte introuvable"
    inst = acc.get("instance_uid")
    if not inst or HUB.get(inst) is None:
        return None, "instance hors-ligne"
    payload = {"tag": acc["tag"], **(args or {})}
    try:
        data = await HUB.send_command(inst, name, payload, timeout_s=timeout)
        return data, None
    except Exception as exc:
        return None, str(exc)


# ------------------------------------------------------------- update router


async def handle_update(update: dict) -> None:
    """Process one Telegram update. Locked to the configured chat id."""
    c = _conf()
    if not c:
        return
    _, owner = c
    try:
        if "message" in update:
            await _on_message(update["message"], owner)
        elif "callback_query" in update:
            await _on_callback(update["callback_query"], owner)
    except Exception:
        log.exception("telegram handle_update failed")


async def _on_message(msg: dict, owner: str) -> None:
    chat_id = str((msg.get("chat") or {}).get("id"))
    if chat_id != owner:
        log.warning("telegram: ignoring message from chat %s (owner=%s)", chat_id, owner)
        return
    text = (msg.get("text") or "").strip()
    if not text:
        return
    parts = text.split()
    cmd = parts[0].lower().lstrip("/").split("@")[0]
    args = parts[1:]

    if cmd in ("start", "menu", "help"):
        send("🤖 <b>BrawlStar Fleet</b> — contrôle de la flotte.\n"
             "Utilise les boutons ci-dessous ou les commandes :\n"
             "/status · /report · /ceiling [n] · /push · /stop · /screenshot",
             kb=_main_menu_kb())
    elif cmd == "status":
        send(_fleet_summary(), kb=_main_menu_kb())
    elif cmd == "report":
        send("📈 <b>Rapport</b>\n\n" + _fleet_summary(), kb=_back_kb())
    elif cmd == "ceiling":
        if args:
            await _set_ceiling(args[0])
        else:
            send(f"Plafond push max actuel : <b>{_push_ceiling()}</b> 🏆\n"
                 f"Pour changer : <code>/ceiling 800</code>")
    elif cmd in ("push", "stop", "screenshot", "restart", "stats", "eff"):
        # Resolve the target account: single account → implicit; else list.
        accs = db.list_accounts()
        if len(accs) == 1:
            aid = accs[0]["id"]
            await _do_action({"push": "push", "stop": "stop", "screenshot": "screen",
                              "restart": "restart_ask", "stats": "stats",
                              "eff": "eff"}[cmd], aid, None)
        else:
            _send_accounts_list()
    else:
        send("Commande inconnue. /menu pour les options.", kb=_main_menu_kb())


def _send_accounts_list(message_id: int | None = None) -> None:
    accs = db.list_accounts()
    if not accs:
        send("Aucun compte.", kb=_back_kb())
        return
    rows = []
    for a in accs:
        running = _account_running(a["id"]) or bool(_live_snapshot(a.get("instance_uid")).get("activity"))
        dot = "🟢" if running else "⚪"
        rows.append([{"text": f"{dot} {a.get('name') or a['tag']}",
                      "callback_data": f"act:open:{a['id']}"}])
    rows.append([{"text": "◀ Menu", "callback_data": "menu:main"}])
    if message_id:
        edit(message_id, "<b>👥 Comptes</b> — choisis-en un :", kb=rows)
    else:
        send("<b>👥 Comptes</b> — choisis-en un :", kb=rows)


async def _set_ceiling(raw: str) -> None:
    try:
        v = int(raw)
    except ValueError:
        send("Valeur invalide. Ex : <code>/ceiling 800</code>")
        return
    if v < 1 or v > 5000:
        send("Le plafond doit être entre 1 et 5000.")
        return
    cfg = db.get_config("app")
    cfg["push_max_ceiling"] = v
    db.set_config("app", cfg)
    send(f"✅ Plafond push max réglé sur <b>{v}</b> 🏆\n"
         f"<i>S'applique au prochain démarrage de Push Max.</i>")


async def _on_callback(cb: dict, owner: str) -> None:
    frm = str((cb.get("from") or {}).get("id"))
    if frm != owner:
        answer_callback(cb.get("id", ""), "Non autorisé.")
        return
    data = cb.get("data") or ""
    msg = cb.get("message") or {}
    message_id = msg.get("message_id")
    answer_callback(cb.get("id", ""))

    if data == "menu:main":
        edit(message_id, "🤖 <b>BrawlStar Fleet</b> — menu principal :", kb=_main_menu_kb())
    elif data == "menu:status":
        edit(message_id, _fleet_summary(), kb=_main_menu_kb())
    elif data == "menu:report":
        edit(message_id, "📈 <b>Rapport</b>\n\n" + _fleet_summary(), kb=_back_kb())
    elif data == "menu:accounts":
        _send_accounts_list(message_id)
    elif data == "menu:settings":
        edit(message_id,
             f"⚙️ <b>Réglages</b>\nPlafond push max : <b>{_push_ceiling()}</b> 🏆\n"
             f"Change-le avec <code>/ceiling 800</code>.", kb=_back_kb())
    elif data.startswith("act:"):
        _, action, said = data.split(":", 2)
        try:
            aid = int(said)
        except ValueError:
            return
        await _do_action(action, aid, message_id)


async def _do_action(action: str, account_id: int, message_id: int | None) -> None:
    if action == "open":
        running = _account_running(account_id)
        txt = _account_summary(account_id)
        if message_id:
            edit(message_id, txt, kb=_account_kb(account_id, running))
        else:
            send(txt, kb=_account_kb(account_id, running))
        return
    if action == "stats":
        running = _account_running(account_id)
        if message_id:
            edit(message_id, _account_summary(account_id), kb=_account_kb(account_id, running))
        else:
            send(_account_summary(account_id), kb=_account_kb(account_id, running))
        return
    if action == "eff":
        running = _account_running(account_id)
        if message_id:
            edit(message_id, _brawler_eff_text(account_id), kb=_account_kb(account_id, running))
        else:
            send(_brawler_eff_text(account_id), kb=_account_kb(account_id, running))
        return
    if action == "screen":
        send("📸 Capture en cours…")
        data, err = await _send_cmd(account_id, "screenshot", {}, timeout=12)
        if err or not data or not data.get("jpeg_b64"):
            send(f"❌ Capture impossible ({err or 'pas de frame'}).")
            return
        try:
            send_photo(base64.b64decode(data["jpeg_b64"]),
                       caption=_account_summary(account_id).split("\n")[0])
        except Exception:
            send("❌ Décodage de la capture échoué.")
        return
    if action == "push":
        send("▶ Démarrage de Push Max…")
        data, err = await _send_cmd(account_id, "session_push_max",
                                    {"efficiency_ceiling": _push_ceiling()}, timeout=100)
        if err:
            send(f"❌ Échec du démarrage ({err}).")
        else:
            ok = (data or {}).get("ok", True)
            send("✅ Push Max démarré." if ok else f"❌ {(data or {}).get('msg','refusé')}")
        send(_account_summary(account_id), kb=_account_kb(account_id, _account_running(account_id)))
        return
    if action == "stop":
        send("⏹ Arrêt demandé (fin du match en cours)…")
        data, err = await _send_cmd(account_id, "session_stop", {"force": False}, timeout=15)
        send("✅ Arrêt programmé." if not err else f"❌ {err}")
        send(_account_summary(account_id), kb=_account_kb(account_id, _account_running(account_id)))
        return
    if action == "restart_ask":
        kb = [[{"text": "✅ Oui, redémarrer", "callback_data": f"act:restart:{account_id}"},
               {"text": "✖ Annuler", "callback_data": f"act:open:{account_id}"}]]
        if message_id:
            edit(message_id, "⚠️ Redémarrer le bot worker ?\n<i>Il finit le match en cours "
                 "puis redémarre à jour.</i>", kb=kb)
        else:
            send("⚠️ Redémarrer le bot worker ?", kb=kb)
        return
    if action == "restart":
        send("🔄 Redémarrage du bot…")
        data, err = await _send_cmd(account_id, "bot_restart", {}, timeout=20)
        send("✅ Redémarrage demandé." if not err else f"❌ {err}")
        return
