"""Telegram-controlled Brawl Stars bot.

Replaces the legacy Tkinter GUI with a Telegram bot + web panel. The
match-playing core (Play, StageManager, etc.) is started in a
background thread on /start.

Commands:
    /start <brawler> <trophies> <wins>  Launch the bot pushing the brawler
                                        until that trophy/wins count.
    /stop                               Stop the running bot.
    /status                             Stats (state, trophies, win rate, IPS).
    /screenshot                         Send a live screenshot of the game.
    /help                               Show commands.

Examples:
    /start colt 600 0
    /start shelly 500 25
"""
from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import warnings
from pathlib import Path

import requests
import tomllib
try:
    import tkinter as tk
except ImportError:
    tk = None  # tkinter is optional — only needed on macOS for legacy patch

# Same prelude as main.py — silence noisy 3rd-party stdout. Our own
# logging is reconfigured via logging_setup below.
os.environ.setdefault("ORT_LOGGING_LEVEL", "3")
os.environ.setdefault("ONNXRUNTIME_LOGGING_LEVEL", "3")
warnings.filterwarnings("ignore")
# Force UTF-8 stdout/stderr on Windows so progress bars from third-party
# libs (EasyOCR uses U+2588 block chars) don't crash with cp1252 errors.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Patch Tk to avoid background-thread destruction errors (some legacy
# helpers still touch Tk at import time; we don't show any GUI but keep
# the lib happy).
if tk is not None:
    def _safe_tk_del(self):
        try:
            if self._tk.getboolean(self._tk.call("info", "exists", self._name)):
                self._tk.globalgetvar(self._name)
        except Exception:
            pass
    tk.Variable.__del__ = _safe_tk_del

# Make project importable regardless of CWD.
BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from window_controller import WindowController  # noqa: E402
from utils import (  # noqa: E402
    get_brawler_list, update_missing_brawlers_info, check_version,
    update_wall_model_classes, get_latest_wall_model_file,
    load_toml_as_dict, current_wall_model_is_latest,
    api_base_url, save_brawler_data,
)
from time_management import TimeManagement  # noqa: E402
from state_finder.main import get_state  # noqa: E402
from stage_manager import StageManager  # noqa: E402
from play import Play  # noqa: E402
from lobby_automation import LobbyAutomation  # noqa: E402
from account_detect import detect_player_tag, fetch_account_profile, ensure_lobby  # noqa: E402
import db  # noqa: E402
from worker_pool import POOL, BotWorker  # noqa: E402
import alerts  # noqa: E402
import device  # noqa: E402
import cloud_sync  # noqa: E402
from push_max import PushMaxStrategy, EFFICIENCY_CEILING  # noqa: E402
import play_schedule  # noqa: E402  (humane play schedule — no 24/7 grind)
from logging_setup import setup_logging  # noqa: E402

setup_logging()
log = logging.getLogger("telegram_main")


# ----------------------------------------------------------- bot lifecycle

# Minimum wall-clock gap between two brawlace trophy re-syncs (seconds). The
# re-sync is the "source of truth" catch-up for account + brawler trophies; we
# attempt it after every match but skip if the previous one was more recent
# than this. Tighter than the old "every 3rd match" → less trophy lag, while
# the wall-clock floor stops fast match streaks from saturating flaresolverr.
BRAWLACE_SYNC_MIN_INTERVAL = 90.0

# A brawler the menu OCR fails to select is marked "unselectable" so push_max
# stops churning on it. That used to be a PERMANENT ban (a single transient
# OCR miss excluded a brawler forever) — the ratchet that ended up banning the
# best brawlers (maisie/carl/jessie/colt…) and starved the rotation. Now the
# ban EXPIRES after this TTL: the brawler gets another chance on the next
# session start/resume past the window (the menu is flaky, not always-failing —
# these same brawlers select fine most of the time).
UNSELECTABLE_TTL_S = 2 * 3600     # 2 h


def _load_unselectable(raw, now: "float | None" = None) -> dict:
    """Normalize a persisted unselectable record into {name: ts}, dropping
    entries older than UNSELECTABLE_TTL_S (expired bans get a fresh chance).

    Accepts the legacy LIST format (names with no timestamp): those carry no
    age, so we treat them as expired and clear them — which also one-shot
    unbans whatever the old permanent ratchet had accumulated."""
    now = now if now is not None else time.time()
    if isinstance(raw, dict):
        out = {}
        for n, ts in raw.items():
            try:
                if now - float(ts) < UNSELECTABLE_TTL_S:
                    out[n] = float(ts)
            except (TypeError, ValueError):
                continue
        return out
    return {}


# Shared singletons created at startup (after host_bootstrap).
# Main reuses these to avoid double-initializing scrcpy/ADB.
_SHARED_RUNTIME: dict = {}


# ----------------------------- Activity status ---------------------
# Publish a short human-readable "what is the bot doing right now" string
# to game_api (snapshot() picks it up → cloud panel). Best-effort: never
# let a status update break the grind.
def _set_activity(text: "str | None") -> None:
    try:
        import game_api as _gapi
        _gapi.set_activity(text)
    except Exception:
        pass


def _title(name: "str | None") -> str:
    return (name or "").strip().title()


# Maps a detected screen state (+ the brawler being ground) to a friendly
# activity phrase. Returns None for states with no meaningful label so the
# previous activity is kept (avoids flicker on transient OCR misreads).
def _activity_for_state(state: "str | None", brawler: "str | None") -> "str | None":
    bw = _title(brawler)
    if state == "match":
        return f"Playing as {bw}" if bw else "Playing a match"
    if state == "lobby":
        # The runner only publishes activity while a grind is active, and an
        # active grind taps PLAY the instant it's back at the lobby — so
        # "lobby" here means "queuing for the next match", not idling. (Idle /
        # manual lobby falls back to the STATE_LABELS "In lobby" on the panel.)
        return f"Searching for match — {bw}" if bw else "Searching for match"
    if state == "brawler_selection":
        return f"Selecting {bw}" if bw else "Selecting brawler"
    if state == "end":
        return "Match ended — reading result"
    if state == "star_drop":
        return "Opening Star Drop"
    if state == "trophy_reward":
        return "Collecting trophy reward"
    if state == "shop":
        return "Closing shop"
    if state == "popup":
        return "Dismissing popup"
    if state == "play_store":
        return "Reopening Brawl Stars"
    return None


# ----------------------------- Resume-state persistence ------------
# When a session is active and the bot restarts (self-update, crash,
# reboot…), we want to resume the same task automatically. The state
# now lives entirely on the cloud panel — workers are stateless so a
# disk wipe / fresh deploy / different machine all pick up the same
# session if the user had one running.
# Don't auto-resume a session whose state hasn't been touched in this long.
# The match hook refreshes `started_at` on activity, so an actively-grinding
# session never hits this — it only discards a genuinely abandoned one.
_RESUME_MAX_AGE_S = 24 * 3600


def _save_resume_state(state: dict) -> None:
    try:
        cloud_sync.set_instance_state(state)
        log.info("resume state saved (cloud): mode=%s brawler=%s target=%s",
                 state.get("mode"), state.get("brawler"),
                 state.get("target_total_trophies"))
    except Exception:
        log.exception("save_resume_state failed")


def _load_resume_state() -> dict | None:
    try:
        state = cloud_sync.get_instance_state()
    except Exception:
        log.exception("load_resume_state failed")
        return None
    if not state:
        return None
    age = time.time() - state.get("started_at", 0)
    if age > _RESUME_MAX_AGE_S:
        log.info("resume state too old (%.0fs) — discarding", age)
        _clear_resume_state()
        return None
    return state


def _clear_resume_state() -> None:
    try:
        cloud_sync.clear_instance_state()
        log.info("resume state cleared (cloud)")
    except Exception:
        log.exception("clear_resume_state failed")


def _manage_schedule_powersave(bot) -> None:
    """Close Brawl Stars during ANY schedule pause (sleep window, daily cap, OR
    a between-blocks break) and reopen it on resume — runs from the keepalive
    EVERY tick, independent of whether there's a session to resume (the bug: a
    pause left the game open with the screen on).

    Breaks are 20–70 min, far too long to keep the screen on idling "for a quick
    resume" — relaunching BS on resume costs ~15–30 s, negligible against the
    battery a lit idle screen burns. So breaks power-save too.

    Does nothing while the bot is actively running (a block in progress)."""
    try:
        if bot.runner.is_running():
            return
        st = play_schedule.get().state()
    except Exception:
        return
    import game_api as _ga
    api = _ga.get()
    if api is None:
        return
    if st == "play":
        # Window reopened / break over / new day: reopen the game if we'd closed
        # it (wake screen, unlock, relaunch BS). The actual resume is done right
        # after by _try_resume_session.
        if bot.runner._power_saved:
            try:
                api.exit_power_save()
                log.info("play schedule: waking — power-save exited (Brawl Stars relaunching)")
            except Exception:
                log.exception("schedule exit_power_save failed")
            bot.runner._power_saved = False
        return

    # Sale-ready: PREP the account (buy max hypercharges + upgrade brawlers toward
    # P11) BEFORE power-saving or marking it ready. The prep drives the device for a
    # few minutes (in a background thread), so while it runs we must NOT close BS.
    if st == "sale_ready":
        try:
            import sale_prep
            sched = play_schedule.get()
            target = int(getattr(sched, "sale_target", 0) or 0)
            tag = None
            aid = getattr(bot.runner, "_account_id", None)
            if aid is not None:
                acc = db.get_account(aid)
                if acc:
                    tag = acc.get("tag")
            if tag and target and not sale_prep.completed(tag, target):
                bot.runner._power_saved = False  # re-close BS once prep finishes
                sale_prep.maybe_start(bot, tag, target, bot.send)
            if tag and sale_prep.is_in_progress(tag):
                try:
                    _set_activity("⚙️ Prépa vente en cours…")
                except Exception:
                    pass
                return  # keep BS open for the prep; skip power-save this tick
        except Exception:
            log.warning("sale-ready prep trigger failed", exc_info=True)

    # Paused (sleep / daily cap / between-blocks break) → close the game and
    # turn the screen off. A break is long enough (20–70 min) to be worth it.
    # Any non-"play" state is a pause → close the game + screen off. Generalized
    # from an explicit allowlist so new schedule states (pause windows, day off)
    # close the game automatically.
    if st != "play" and not bot.runner._power_saved:
        try:
            api.enter_power_save()
            bot.runner._power_saved = True
            log.info("play schedule: %s → power-save (Brawl Stars closed, screen off)", st)
        except Exception:
            log.exception("schedule enter_power_save failed")
    label = {"sleep": "sommeil", "cap": "quota du jour", "break": "pause",
             "pause": "pause", "dayoff": "jour de repos",
             "sale_ready": "prêt à vendre"}.get(st, st)
    try:
        _set_activity(f"💤 Pause — {label}")
    except Exception:
        pass

    # (sale-ready prep + notify is handled above, before power-save — see sale_prep)


def _resolve_account_id(accounts: "list[dict]", serial: "str | None") -> "int | None":
    """Resolve which account id this worker should grind under.

    Prefer an exact device-serial match; otherwise, on a single-account worker
    (exactly one row), use that sole account. Returns None when the choice is
    ambiguous (0 or >1 accounts and no serial match) — the caller must then
    DEFER rather than grind un-persisted.
    """
    match = next((a for a in accounts if a.get("device_serial") == serial), None)
    if match is None and len(accounts) == 1:
        match = accounts[0]
    return match["id"] if match else None


def _try_resume_session(bot) -> None:
    """Re-launch the last task if the bot was interrupted mid-session."""
    state = _load_resume_state()
    if state is None:
        return
    if bot.runner.is_running():
        log.info("resume: bot already running, skipping")
        return
    # Humane schedule gate: don't auto-resume during the sleep window, an active
    # break, or once the daily match cap is hit. The resume state is KEPT so the
    # next keepalive tick (inside the active window, break over) picks it up.
    # Closing/reopening the game during these pauses is handled separately by
    # _manage_schedule_powersave (runs from the keepalive even when there's no
    # session to resume), so it isn't gated behind this resume flow.
    try:
        if play_schedule.get().state() != "play":
            log.debug("resume: held by play schedule")
            return
    except Exception:
        pass
    # Bind the account BEFORE building the session. _bootstrap_account runs in a
    # separate thread and can lose the cold-boot race against this resume (e.g.
    # the HP reboots and the WiFi-ADB / brawlace fetch lags), leaving
    # runner._account_id None — which SILENTLY disables match persistence AND
    # the data-driven pick order (db.start_session, brawler_efficiency and
    # log_match are ALL gated on _account_id), so the bot grinds for hours
    # recording nothing and on tier-prior order. Resolve it here from the local
    # DB (populated by any prior run); defer the resume if we can't yet.
    if bot.runner._account_id is None:
        try:
            _serial = device.adb_serial()
        except Exception:
            _serial = None
        _aid = _resolve_account_id(db.list_accounts(), _serial)
        if _aid is None:
            log.warning("resume: account not yet bound and not resolvable from "
                        "local DB — deferring (retried next keepalive tick)")
            return
        bot.runner._account_id = _aid
        log.info("resume: bound account id=%s before start (bootstrap-race fix)", _aid)
    mode = state.get("mode") or "single"
    log.info("RESUMING %s session (started %.0fs ago): brawler=%s target_total=%s",
             mode, time.time() - state.get("started_at", 0),
             state.get("brawler"), state.get("target_total_trophies"))
    # The persisted `owned_brawlers` snapshot is STALE (captured when the
    # session first started). push_max's efficiency ceiling works on these
    # trophy counts, so a stale-low value makes a 1000+ brawler look "easy"
    # and get picked (the shelly/bartaba bug). Re-fetch fresh trophies from
    # brawlace at resume; fall back to the stale snapshot only if it fails.
    owned = state.get("owned_brawlers")
    if mode == "push_max":
        try:
            from account_detect import fetch_account_profile
            accs = db.list_accounts()
            if accs:
                prof = fetch_account_profile(accs[0]["tag"])
                fresh = prof.get("brawlers")
                if fresh:
                    owned = fresh
                    log.info("resume: refreshed %d owned brawlers from brawlace "
                             "(stale snapshot discarded)", len(fresh))
        except Exception:
            log.exception("resume: brawler refresh failed — using stale snapshot")
    try:
        ok, msg = bot.runner.start(
            brawler=state.get("brawler") or "shelly",
            trophies=state.get("trophies", 99999),
            wins=state.get("wins", 0),
            mode=mode,
            owned_brawlers=owned,
            max_matches=state.get("max_matches"),
            target_total_trophies=state.get("target_total_trophies"),
            per_brawler_max_trophies=state.get("per_brawler_max_trophies"),
            efficiency_ceiling=state.get("efficiency_ceiling"),
            last_equipped=state.get("last_equipped") or state.get("brawler"),
            resume_account_trophies=state.get("account_trophies"),
            unselectable=state.get("unselectable"),
        )
        log.info("resume result: ok=%s msg=%s", ok, msg)
        if not ok:
            # If we can't resume now (battery, lobby, etc.) keep the
            # state so the next restart can try again.
            log.warning("resume failed; keeping state for retry")
    except Exception:
        log.exception("resume crashed")


class BotRunner:
    """Manages the bot worker in a background thread."""

    def __init__(self) -> None:
        self.thread: threading.Thread | None = None
        self.main_instance = None  # Main() instance (when running)
        self.brawler_data: list[dict] | None = None
        self.started_at: float = 0.0
        self.stop_flag = threading.Event()
        self._lock = threading.Lock()
        # Set by TelegramBot; called with a plain-text status line whenever
        # the bot finishes a match, hits the trophy target, etc.
        self.notify: "callable | None" = None
        # Cached starting values so we can report deltas in /status and on stop.
        self._initial_trophies: int = 0
        self._match_count: int = 0
        self._win_count: int = 0
        self._loss_count: int = 0
        self._draw_count: int = 0
        self._target_trophies: int = 0
        self._max_matches: int | None = None
        self._target_total_trophies: int | None = None
        # Timestamp of the last match-end detection. Used by the stuck-watchdog.
        self._last_match_at: float = 0.0
        self._stuck_alerted: bool = False
        self._account_id: int | None = None
        self._session_id: int | None = None
        # When non-None we're in "push max" mode and this controls
        # brawler rotation between matches.
        self._push_max: PushMaxStrategy | None = None
        self._resume_state: dict | None = None
        # Brawlers the in-game menu OCR can never select (e.g. 8-bit/ARCADE,
        # short names like "bo"). Persisted in the resume state so a restart
        # doesn't re-pick + re-churn them every session (which left the bot
        # stuck cycling selection → no match → BS restart loop).
        self._unselectable: dict[str, float] = {}   # {name: ts marked}, TTL'd
        # Running account-wide trophy total (sum across all brawlers).
        # Seeded from brawlace at session start, then updated by deltas
        # from each match. Used by the panel for the progression chart.
        self._account_trophies: int = 0
        self._target_reached_notified: bool = False
        # Name of the brawler the bot last PLAYED a match with → the one Brawl
        # Stars still has equipped (BS persists the equipped brawler across app
        # restarts). On resume we use this to SKIP re-opening the brawler menu
        # when the target is already equipped — otherwise a restart re-selects
        # the same brawler via the menu (2+ min of OCR/scroll), and for menu-
        # unselectable brawlers (8-bit/Arcade, tick) it fails outright. This is
        # deterministic (no OCR) so it works where _is_already_equipped doesn't.
        self._last_equipped: str | None = None
        self._resume_account_trophies: int | None = None
        # True while a background brawlace re-sync is in flight — prevents two
        # concurrent fetches (a slow/504 fetch > BRAWLACE_SYNC_MIN_INTERVAL would
        # otherwise let a second one start and race the first on shared state).
        self._resync_inflight: bool = False
        # True while the phone is in power-save FOR THE SCHEDULE (Brawl Stars
        # force-stopped + screen off during the sleep window / daily cap). Lets
        # the keepalive close the game once and reopen it on wake.
        self._power_saved: bool = False
        # Wall-clock of the last brawlace re-sync ATTEMPT (set when we launch
        # the background fetch). The resync is throttled to one per
        # BRAWLACE_SYNC_MIN_INTERVAL seconds — this both REDUCES trophy lag
        # (sync ~every match in normal play instead of every 3rd) and CAPS the
        # API rate during fast draw/loss streaks (never saturate flaresolverr).
        self._last_brawlace_sync: float = 0.0

    def is_running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self, brawler: str, trophies: int, wins: int,
              mode: str = "single",
              owned_brawlers: list[dict] | None = None,
              max_matches: int | None = None,
              target_total_trophies: int | None = None,
              per_brawler_max_trophies: int | None = None,
              efficiency_ceiling: int | None = None,
              last_equipped: str | None = None,
              resume_account_trophies: int | None = None,
              unselectable: list[str] | None = None) -> tuple[bool, str]:
        """Start a cycle.

        mode = "single"   → push one brawler to a fixed target (current behaviour)
        mode = "push_max" → smart rotation across all owned brawlers,
                             ignoring `trophies` (target). Requires
                             `owned_brawlers` to seed the strategy.
        """
        log.info("BotRunner.start(brawler=%r, target=%d, mode=%s, wins=%d)",
                 brawler, trophies, mode, wins)
        with self._lock:
            if self.is_running():
                log.warning("start denied: bot already running")
                return False, "Bot already running. Use /stop first."
            # Pre-session battery gate: refuse to start a grind on a
            # phone that's already low. Prevents the bot from draining
            # the last 5% by launching at 28%.
            try:
                import game_api as _gapi
                api = _gapi.get()
                if api is not None:
                    ok_bat, bat_reason = api.can_play()
                    if not ok_bat:
                        log.warning("start denied: %s", bat_reason)
                        return False, f"Battery gate: {bat_reason}"
            except Exception:
                log.exception("pre-session battery check failed")
            # Make sure Brawl Stars is open and we're at the lobby
            # before launching the runner thread. Relaunches BS if
            # it's not running.
            try:
                if api is not None:
                    ok_lobby, lobby_reason = api.ensure_brawlstars_at_lobby()
                    if not ok_lobby:
                        log.warning("start denied: %s", lobby_reason)
                        return False, f"Lobby check: {lobby_reason}"
            except Exception:
                log.exception("pre-session lobby check failed")
            # Restore the persisted unselectable record (brawlers the menu OCR
            # can't pick — 8-bit/ARCADE, "bo", …) so we don't re-churn them this
            # session. Now a {name: ts} dict with a TTL: bans older than 2 h are
            # dropped here, so the best brawlers get periodically retried instead
            # of being banned forever (the old set ratchet starved the pool).
            self._unselectable = _load_unselectable(unselectable)
            if self._unselectable:
                log.info("push_max: %d unselectable still within TTL: %s",
                         len(self._unselectable), sorted(self._unselectable))
            if mode == "push_max":
                if not owned_brawlers:
                    return False, "push_max needs the owned-brawlers list."
                # Data-driven pick order: rank brawlers by their realized
                # net/match from this account's local match history (shrunk
                # toward the tier prior for small samples). Recomputed at every
                # start/resume so it always reflects the latest performance.
                brawler_stats = {}
                if self._account_id is not None:
                    try:
                        brawler_stats = db.brawler_efficiency(self._account_id)
                    except Exception:
                        log.exception("push_max: brawler_efficiency lookup failed "
                                      "— falling back to tier-prior order")
                # no_swap=True (2026-06-15): grind the EQUIPPED brawler, never open
                # the collection menu. On fresh accounts the menu OCR can't select
                # the 0-trophy FR-named brawlers (e.g. ELIZ@) → the bot got trapped
                # on the brawler grid. Mono-brawler grinding is reliable (no menu);
                # rotation can be re-enabled once menu selection is hardened.
                self._push_max = PushMaxStrategy.from_owned(
                    owned_brawlers, brawler_max_trophies=per_brawler_max_trophies,
                    efficiency_ceiling=(efficiency_ceiling or EFFICIENCY_CEILING),
                    no_swap=True, brawler_stats=brawler_stats)
                # Pre-exhaust known-unselectable brawlers BEFORE picking the
                # starter, so the bot doesn't open the session churning on one
                # it can never select.
                for _name in self._unselectable:
                    _bs = self._push_max.brawlers.get(_name)
                    if _bs:
                        _bs.exhausted = True
                if self._unselectable:
                    log.info("push_max: %d persisted-unselectable brawlers skipped: %s",
                             len(self._unselectable), sorted(self._unselectable))
                # Pick the starter brawler from the strategy.
                first = self._push_max.pick_next()
                if first is None:
                    return False, "No eligible brawler in push_max."
                brawler = first.name
                trophies = 99999  # unused in push_max, but the core checks it
                log.info("push_max starting brawler: %s", brawler)
            else:
                self._push_max = None
            data = [{
                "brawler": brawler,
                # Placeholder for current trophies; overridden by OCR in
                # _install_match_hook once we're on the lobby screen.
                "trophies": 0,
                "wins": wins,
                "win_streak": 0,
                "automatically_pick": True,
                "type": "trophies",
                "push_until": trophies,
            }]
            self._target_trophies = trophies
            self._max_matches = max_matches
            self._target_total_trophies = target_total_trophies
            self._last_brawlace_sync = 0.0   # force a sync on the first match
            self._last_equipped = last_equipped
            # On resume, carry the delta-tracked account total forward instead of
            # re-anchoring DOWN to brawlace (which lags reality) at every block.
            self._resume_account_trophies = resume_account_trophies
            self._last_match_at = time.monotonic()   # watchdog (clock-jump safe)
            self._stuck_alerted = False
            # Persist the resume state so a restart can pick up where
            # we left off (self-update, crash, reboot…). Stored on the runner
            # so the match hook can re-save it with a fresh `started_at` — an
            # actively-grinding session must never be discarded as "too old".
            self._resume_state = {
                "mode": mode,
                "brawler": brawler,
                "wins": wins,
                "trophies": trophies,
                "max_matches": max_matches,
                "target_total_trophies": target_total_trophies,
                "per_brawler_max_trophies": per_brawler_max_trophies,
                "efficiency_ceiling": efficiency_ceiling,
                "last_equipped": last_equipped,
                "account_trophies": resume_account_trophies,
                "owned_brawlers": owned_brawlers,
                "unselectable": dict(self._unselectable),
                "started_at": time.time(),
            }
            _save_resume_state(self._resume_state)
            try:
                save_brawler_data(data)
            except Exception as exc:
                log.warning("save_brawler_data failed: %s", exc)
            self.brawler_data = data
            self.stop_flag.clear()
            # Make sure the game is on the lobby screen before the worker
            # spins up (it calls select_brawler immediately on init).
            if not ensure_lobby():
                log.warning("start: ensure_lobby failed — launching anyway")
            self.thread = threading.Thread(target=self._run, args=(data,), daemon=True)
            self.thread.start()
            self.started_at = time.time()
            # Start the stuck-watchdog (one per session, exits when runner stops).
            self._start_stuck_watchdog()
            return True, f"Started bot for {brawler} (target {trophies} trophies)."

    # Stuck thresholds (minutes of no completed match). The legitimate gap
    # between two match-ends maxes out around 12 min (a 3-5 min match + a
    # ~4.5 min brawler-swap selection + post-match screens), so recovery only
    # kicks in well past that to avoid disrupting a slow-but-healthy cycle.
    STUCK_RECOVERY_MIN = 12      # start active recovery (goto_lobby, retried)
    STUCK_HARD_RESTART_MIN = 18  # still stuck → hard-restart the worker

    def _start_stuck_watchdog(self) -> None:
        """Background thread: detect a wedged session (no match completing for
        too long) and recover with ESCALATION, not just a one-shot alert.

        - alert (once) at cfg/alerts.toml `[bot_stuck] threshold_minutes` (8);
        - from STUCK_RECOVERY_MIN: call goto_lobby every tick — it dismisses
          star drops / team invites / reward popups and restarts Brawl Stars
          if it's wedged on a frozen screen;
        - from STUCK_HARD_RESTART_MIN: goto_lobby couldn't fix it → the worker
          itself is wedged, so hard-exit. systemd (Restart=always) relaunches
          and the cloud resume-state brings the grind straight back.
        `_stuck_alerted` is reset on any match progress (see the match hook).
        """
        def watch():
            import os as _os
            from alerts import _load as _alerts_load
            hard_restart_done = False
            while self.is_running():
                time.sleep(60)
                try:
                    # MONOTONIC: a wall-clock jump (DST +1h, NTP correction)
                    # must NOT make `elapsed` leap past the hard-restart threshold
                    # and trigger an unjustified _os._exit(1) on a healthy session.
                    elapsed = time.monotonic() - self._last_match_at
                    cfg = _alerts_load().get("bot_stuck", {})
                    alert_min = float(cfg.get("threshold_minutes", 13))
                    cur_brawler = (self.brawler_data[0]["brawler"]
                                   if self.brawler_data else "?")
                    # Alert once when first stuck (informational).
                    if elapsed > alert_min * 60 and not self._stuck_alerted:
                        log.warning("STUCK detected: no match for %.0f min", elapsed / 60)
                        self._stuck_alerted = True
                        msg = alerts.format_alert("bot_stuck", minutes=int(elapsed / 60),
                                                  brawler=cur_brawler, matches=self._match_count)
                        if msg and self.notify:
                            try: self.notify(msg)
                            except Exception: log.exception("stuck alert send failed")
                        try:
                            cloud_sync.event("bot_stuck", {"minutes": int(elapsed / 60),
                                "brawler": cur_brawler, "matches": self._match_count})
                        except Exception:
                            log.exception("cloud_sync.event(bot_stuck) failed")
                    # Escalating recovery.
                    if elapsed > self.STUCK_HARD_RESTART_MIN * 60:
                        if not hard_restart_done:
                            hard_restart_done = True
                            log.warning("STUCK %.0f min — goto_lobby didn't recover; "
                                        "hard-restarting worker (resume will relaunch)",
                                        elapsed / 60)
                            try:
                                cloud_sync.event("bot_stuck", {"recovery": "worker_restart",
                                                               "brawler": cur_brawler})
                            except Exception:
                                pass
                            time.sleep(1)
                            _os._exit(1)   # systemd Restart=always + cloud resume
                    elif elapsed > self.STUCK_RECOVERY_MIN * 60:
                        log.warning("STUCK %.0f min — recovery: goto_lobby", elapsed / 60)
                        _set_activity(f"⚠ Recovering (stuck {int(elapsed / 60)}m)")
                        try:
                            import game_api as _ga
                            api = _ga.get()
                            if api is not None:
                                api.goto_lobby(max_attempts=15)
                        except Exception:
                            log.exception("stuck recovery goto_lobby failed")
                except Exception:
                    log.exception("stuck watchdog iteration crashed")
        t = threading.Thread(target=watch, daemon=True, name="stuck-watchdog")
        t.start()
        log.info("stuck watchdog armed")

    def stop(self) -> tuple[bool, str]:
        log.info("BotRunner.stop() called (soft)")
        """Soft stop: finish the current match, then stop (no new matches).

        The Main loop honors `in_cooldown`: it replaces the lobby
        handler with a no-op so the bot won't tap PLAY again. After the
        cooldown_duration (default 3 min) it breaks out of the loop.
        Also clears the resume state so a future restart doesn't
        auto-relaunch the session.
        """
        with self._lock:
            if not self.is_running():
                return False, "Bot is not running."
            _clear_resume_state()  # user-initiated stop, no auto-resume
            try:
                if self.main_instance is not None:
                    self.main_instance.in_cooldown = True
                    self.main_instance.cooldown_start_time = time.time()
                    # Prevent starting new matches (the Main loop also
                    # does this when in_cooldown is set; force the override).
                    self.main_instance.Stage_manager.states['lobby'] = lambda: 0
            except Exception as exc:
                log.warning("could not set cooldown on Main: %s", exc)
            return True, (
                "Bot will finish the current match (max ~3 min), "
                "then stop. Use /forcestop for immediate."
            )

    def force_stop(self) -> tuple[bool, str]:
        log.info("BotRunner.force_stop() called (hard)")
        """Hard stop: kill the loop right now, abandoning the current match.

        If called during the init phase (model loading, select_brawler),
        the stop_flag prevents the main loop from starting at all once init
        finishes — so no match is launched.
        """
        with self._lock:
            if not self.is_running():
                return False, "Bot is not running."
            _clear_resume_state()  # user-initiated stop
            # Always set stop_flag first — _run checks it after Main() init
            # and before entering the main loop, so even if init is still
            # in progress the match loop will be skipped.
            self.stop_flag.set()
            try:
                if self.main_instance is not None:
                    self.main_instance.time_to_stop = True
                    self.main_instance.in_cooldown = True
                    self.main_instance.cooldown_start_time = time.time()
                    msg = "Bot force-stopped (current match abandoned)."
                else:
                    msg = (
                        "Stop requested during init — bot will exit as soon "
                        "as initialization finishes (no match will start)."
                    )
            except Exception as exc:
                log.warning("could not set stop flag on Main: %s", exc)
                msg = "Stop requested."
            # Don't null main_instance here — the run thread owns its
            # lifecycle and clears it in its finally block.
            return True, msg

    def _run(self, data: list[dict]) -> None:
        # Inline Main class — exposes the instance via self.main_instance
        # so /stop and /status can reach into it.
        class Main:
            def __init__(_self):
                _self.window_controller = _SHARED_RUNTIME.get("wc") or WindowController()
                _self.Play = Play(*_self.load_models(), _self.window_controller)
                _self.Time_management = TimeManagement()
                _self.lobby_automator = _SHARED_RUNTIME.get("la") or LobbyAutomation(_self.window_controller)
                _self.Stage_manager = StageManager(data, _self.lobby_automator, _self.window_controller)
                # Give the stage manager the owned-brawler roster so start_game
                # can reconcile the recorded brawler against what's actually
                # equipped (canonical-name mapping needs the candidate list).
                if self._push_max is not None:
                    _self.Stage_manager._owned_brawler_names = list(self._push_max.brawlers.keys())
                _self.states_requiring_data = ["lobby"]
                _no_swap = bool(self._push_max and self._push_max.no_swap)
                if _no_swap:
                    # Stay-on-equipped push_max: do NOT navigate the brawler
                    # menu (its OCR/scroll over 102 cards is too unreliable).
                    # Grind whatever brawler is already equipped; its real name
                    # is detected + recorded in _install_match_hook.
                    log.info("push_max no_swap: skipping menu selection, "
                             "grinding the equipped brawler")
                elif (self._last_equipped
                      and data[0]['brawler'].strip().lower()
                          == self._last_equipped.strip().lower()):
                    # The target is the brawler we were last playing → BS still
                    # has it equipped (it persists across app restarts). Skip the
                    # menu entirely: re-selecting it would waste 2+ min of OCR/
                    # scroll, and for menu-unselectable brawlers (8-bit/Arcade,
                    # tick) it fails outright then churns through the roster.
                    # This is the "re-selects Arcade after already playing it"
                    # bug on every git_update/restart.
                    log.info("initial selection: %s already equipped (resumed) "
                             "— skipping menu", data[0]['brawler'])
                    if self._push_max is not None:
                        self._push_max.current = data[0]['brawler']
                elif data[0]['automatically_pick']:
                    # Initial brawler selection. If the menu OCR can't find it
                    # (locked, or name fused with the trophy badge → unreadable),
                    # mark it exhausted in push_max and try the next eligible
                    # brawler instead of crashing the cycle on one bad target.
                    while True:
                        try:
                            _set_activity(f"Selecting {_title(data[0]['brawler'])}")
                            _self.lobby_automator.select_brawler(data[0]['brawler'])
                            break
                        except Exception:
                            log.exception("initial select_brawler(%s) failed",
                                          data[0]['brawler'])
                            if self._push_max is None:
                                break
                            bad = self._push_max.brawlers.get(data[0]['brawler'])
                            if bad:
                                bad.exhausted = True
                            # Persist as unselectable (timestamped → expires
                            # after the TTL) so restarts don't re-churn it now.
                            self._unselectable[data[0]['brawler']] = time.time()
                            if self._resume_state is not None:
                                self._resume_state["unselectable"] = dict(self._unselectable)
                                try: _save_resume_state(self._resume_state)
                                except Exception: pass
                            nxt = self._push_max.pick_next()
                            if nxt is None or nxt.name == data[0]['brawler']:
                                log.warning("no other selectable brawler — "
                                            "continuing with the equipped one")
                                break
                            log.info("initial selection: %s unselectable → "
                                     "trying %s", data[0]['brawler'], nxt.name)
                            data[0]['brawler'] = nxt.name
                _self.Play.current_brawler = data[0]['brawler']
                _self.no_detections_action_threshold = 60 * 8
                _self.initialize_stage_manager()
                _self.state = None
                try:
                    _self.max_ips = int(load_toml_as_dict("cfg/general_config.toml")['max_ips'])
                except (ValueError, KeyError):
                    _self.max_ips = None
                _self.run_for_minutes = int(load_toml_as_dict("cfg/general_config.toml")['run_for_minutes'])
                # Humane schedule: bound THIS block to a randomized length
                # (40–85 min) instead of a 10h marathon, so the bot stops often
                # and the keepalive only resumes after a break / inside the
                # active window (see play_schedule + _try_resume_session).
                _sched = play_schedule.get()
                if _sched.enabled:
                    _self.run_for_minutes = _sched.block_minutes()
                    log.info("play schedule: this block = %d min", _self.run_for_minutes)
                _self.start_time = time.monotonic()   # block duration (clock-jump safe)
                _self.time_to_stop = False
                _self.in_cooldown = False
                _self.cooldown_start_time = 0
                _self.cooldown_duration = 3 * 60

            def initialize_stage_manager(_self):
                _self.Stage_manager.Trophy_observer.win_streak = data[0]['win_streak']
                _self.Stage_manager.Trophy_observer.current_trophies = data[0]['trophies']
                _self.Stage_manager.Trophy_observer.current_wins = data[0]['wins'] if data[0]['wins'] != "" else 0

            @staticmethod
            def load_models():
                folder_path = "./models/"
                model_names = ['mainInGameModel.onnx', 'tileDetector.onnx']
                return [os.path.join(folder_path, name) for name in model_names]

            def restart_brawl_stars(_self):
                _self.window_controller.restart_brawl_stars()

            def manage_time_tasks(_self, frame):
                if _self.Time_management.state_check():
                    state = get_state(frame)
                    _self.state = state
                    if state != "match":
                        _self.Play.time_since_last_proceeding = time.time()
                    _self.Stage_manager.do_state(state, None)
                # Refresh the published activity every tick (cheap) so it never
                # goes stale while the loop spins. Uses the last known state +
                # the brawler currently being ground. Done OUTSIDE the
                # state_check gate on purpose: keeps the panel status live even
                # between state polls.
                try:
                    cur_brawler = _self.Stage_manager.brawlers_pick_data[0]['brawler']
                except Exception:
                    cur_brawler = None
                act = _activity_for_state(_self.state, cur_brawler)
                if act is not None:
                    _set_activity(act)
                if _self.Time_management.no_detections_check():
                    frame_data = _self.Play.time_since_detections
                    for key, value in frame_data.items():
                        if time.time() - value > _self.no_detections_action_threshold:
                            _self.restart_brawl_stars()
                if _self.Time_management.idle_check():
                    _self.lobby_automator.check_for_idle(frame)

            def main(_self):
                s_time = time.time()
                c = 0
                while not _self.time_to_stop:
                    if _self.max_ips:
                        frame_start = time.perf_counter()
                    if _self.run_for_minutes > 0 and not _self.in_cooldown:
                        if (time.monotonic() - _self.start_time) / 60 >= _self.run_for_minutes:
                            _self.in_cooldown = True
                            _self.cooldown_start_time = time.time()
                            _self.Stage_manager.states['lobby'] = lambda: 0
                            # Block elapsed → schedule a (randomized) break so the
                            # keepalive won't resume immediately. Human cadence.
                            try: play_schedule.get().start_break()
                            except Exception: log.debug("start_break failed", exc_info=True)
                    # Schedule pause mid-block: sleep window opened or the daily
                    # match cap was hit → finish the current match then stop.
                    if not _self.in_cooldown:
                        try:
                            _ok, _why = play_schedule.get().should_play_now()
                        except Exception:
                            _ok, _why = True, ""
                        if not _ok:
                            log.info("play schedule: pausing grind (%s)", _why)
                            _self.in_cooldown = True
                            _self.cooldown_start_time = time.time()
                            _self.Stage_manager.states['lobby'] = lambda: 0
                    if _self.in_cooldown:
                        # Stop requested. Exit as soon as we're NOT in a match
                        # (nothing to finish) — keeps a soft stop near-instant
                        # at the lobby instead of idling for the full cooldown.
                        # If a match is in progress, finish it, then the next
                        # non-match iteration breaks (cooldown_duration is just
                        # a hard safety cap).
                        if _self.state != "match":
                            break
                        if time.time() - _self.cooldown_start_time >= _self.cooldown_duration:
                            break
                    if abs(s_time - time.time()) > 1:
                        s_time = time.time()
                        c = 0
                    frame = _self.window_controller.screenshot()
                    last_ft = _self.window_controller.last_frame_time
                    if last_ft > 0 and (time.time() - last_ft) > _self.window_controller.FRAME_STALE_TIMEOUT:
                        _self.Play.window_controller.keys_up(list("wasd"))
                        time.sleep(1)
                        continue
                    _self.manage_time_tasks(frame)
                    brawler = _self.Stage_manager.brawlers_pick_data[0]['brawler']
                    _self.Play.main(frame, brawler)
                    c += 1
                    if _self.max_ips:
                        target_period = 1 / _self.max_ips
                        work_time = time.perf_counter() - frame_start
                        if work_time < target_period:
                            time.sleep(target_period - work_time)
                # graceful exit
                try:
                    _self.window_controller.keys_up(list("wasd"))
                    _self.window_controller.close()
                except Exception:
                    pass

        try:
            _set_activity("Starting up — loading models")
            self.main_instance = Main()
            if self.stop_flag.is_set():
                log.info("force_stop received during init — skipping main loop")
                msg = alerts.format_alert("stop_during_init")
                if msg and self.notify:
                    try: self.notify(msg)
                    except Exception: pass
                return
            self._install_match_hook(self.main_instance)
            # _install_match_hook can take ~10s for trophy OCR; honor stops
            # that come in during that window too.
            if self.stop_flag.is_set():
                log.info("force_stop received before main loop — exiting")
                return
            self.main_instance.main()
        except Exception:
            log.exception("Bot crashed")
        finally:
            # Close any open DB session row.
            if self._session_id is not None:
                try:
                    end_trophies = None
                    if self.main_instance is not None:
                        try:
                            end_trophies = self.main_instance.Stage_manager.Trophy_observer.current_trophies
                        except Exception:
                            pass
                    db.end_session(self._session_id, status="stopped",
                                   end_trophies=end_trophies)
                except Exception:
                    pass
                try:
                    _tag = None
                    try:
                        if self._account_id is not None:
                            _acc = db.get_account(self._account_id)
                            _tag = _acc["tag"] if _acc else None
                    except Exception:
                        pass
                    cloud_sync.session_end(self._session_id, "stopped",
                                           end_trophies, tag=_tag)
                except Exception:
                    log.exception("cloud session_end push failed")
                self._session_id = None
            self.main_instance = None
            # Drop the push-max strategy so the panel's push_max_state reports
            # inactive once the run thread exits. Without this, a session that
            # ends on its own (all brawlers hit the ceiling) leaves _push_max
            # set → the panel keeps showing "Push Max running" + the Stop button
            # forever (the "task doesn't get removed" bug).
            self._push_max = None
            self._target_total_trophies = None
            # Clear the published activity so the panel falls back to the raw
            # screen state (idle / manual) instead of showing a frozen phase.
            _set_activity(None)
            log.info("Bot run ended")


    def _install_match_hook(self, main_instance) -> None:
        """Wrap Trophy_observer.add_trophies to push a Telegram log line
        after every match (victory/defeat/draw, trophy delta, totals)."""
        observer = main_instance.Stage_manager.Trophy_observer
        self._match_count = self._win_count = self._loss_count = self._draw_count = 0
        self._target_reached_notified = False
        target = self._target_trophies or 0
        brawler = (self.brawler_data[0].get('brawler') if self.brawler_data else '?')
        log.info("_install_match_hook: brawler=%s target=%d", brawler, target)

        # Stay-on-equipped push_max: grind the brawler actually equipped on the
        # lobby card (we skipped the menu). Read it via OCR and fuzzy-match to
        # the owned names so the label + trophy tracking are correct. Falls
        # back to the placeholder pick if the read is unusable.
        if self._push_max is not None and self._push_max.no_swap:
            try:
                import game_api as _gapi
                from difflib import SequenceMatcher
                api = _gapi.get()
                eq = api.read_current_brawler() if api else None
                if eq:
                    owned_names = list(self._push_max.brawlers.keys())
                    best = max(owned_names,
                               key=lambda n: SequenceMatcher(None, eq, n.lower()).ratio(),
                               default=None)
                    if best and SequenceMatcher(None, eq, best.lower()).ratio() >= 0.6:
                        brawler = best
                    log.info("push_max no_swap: equipped OCR=%r → grinding %r",
                             eq, brawler)
                self._push_max.current = brawler
                if self.brawler_data:
                    self.brawler_data[0]['brawler'] = brawler
            except Exception:
                log.exception("equipped-brawler detection failed; using %s", brawler)

        # Seed trophy totals from the brawlace profile (reliable), not the
        # on-screen OCR (which reads 0 / stray digits and, once it seeds the
        # session wrong, makes record_match overwrite each brawler's real
        # count with an estimate built from 0 — the "trophies stuck at 0"
        # bug). After seeding, each match delta updates the totals locally.
        profile = None
        try:
            acc = db.get_account(self._account_id) if self._account_id else None
            if acc:
                from account_detect import fetch_account_profile
                profile = fetch_account_profile(acc["tag"])
                self._account_trophies = sum(
                    b.get("trophies", 0) for b in profile.get("brawlers", [])
                )
                # On RESUME, don't re-anchor DOWN to brawlace: it lags reality by
                # minutes-to-hours, so re-seeding at every 40-85 min block snapped
                # the panel total back to brawlace's stale value and discarded the
                # real gains since (the "~1000 trophy deficit"). Carry the
                # delta-tracked total forward; brawlace stays an UPWARD floor (the
                # post-match catch-up still raises us if we genuinely missed gains).
                _carry = self._resume_account_trophies
                if _carry and _carry > self._account_trophies:
                    log.info("carry-forward account trophies: brawlace=%d < tracked=%d → keep tracked",
                             self._account_trophies, _carry)
                    self._account_trophies = int(_carry)
                log.info("seeded account trophies: %d (sum of %d brawlers)",
                         self._account_trophies, len(profile.get("brawlers", [])))
        except Exception:
            log.exception("could not seed account trophies; starting at 0")
            self._account_trophies = 0

        # Current brawler's trophy count: from the brawlace API ONLY (no OCR).
        # Trophy counting uses the API + per-match deltas exclusively — the
        # on-screen OCR was dropped (it caused ~100-trophy drift).
        current = None
        if profile:
            for b in profile.get("brawlers", []):
                if b.get("name", "").lower().strip() == brawler.lower().strip():
                    current = b.get("trophies")
                    break
        if current is not None:
            log.info("seed current-brawler '%s' trophies from brawlace: %d",
                     brawler, current)

        if current is not None:
            observer.current_trophies = current
            self._initial_trophies = current
            # Also update brawler_data so the push_until logic is correct.
            if self.brawler_data:
                self.brawler_data[0]['trophies'] = current
            msg = alerts.format_alert(
                "cycle_started",
                brawler=brawler, current=current, target=target,
                needed=max(0, target - current),
            )
            if msg and self.notify:
                try: self.notify(msg)
                except Exception: pass
        else:
            self._initial_trophies = observer.current_trophies or 0
            msg = alerts.format_alert(
                "cycle_started_no_ocr", brawler=brawler, target=target,
            )
            if msg and self.notify:
                try: self.notify(msg)
                except Exception: pass

        # Open / refresh the DB session row tied to this run.
        if self._account_id is not None:
            # Make the humane daily match cap restart-proof: seed the schedule's
            # counter from matches already played today (DB), not an in-memory 0
            # that resets on every worker restart (which let it hit 300+/day).
            try:
                _aid = self._account_id
                play_schedule.set_match_count_provider(lambda: db.count_matches_today(_aid))
                play_schedule.set_trophy_total_provider(lambda: db.latest_account_trophies(_aid))
            except Exception:
                log.debug("set schedule providers failed", exc_info=True)
            self._session_id = db.start_session(
                self._account_id, brawler, target,
                start_trophies=self._initial_trophies or None,
            )
            log.info("DB session opened: id=%d account=%d brawler=%s target=%d start_trophies=%s",
                     self._session_id, self._account_id, brawler, target,
                     self._initial_trophies)
            try:
                acc = db.get_account(self._account_id)
                if acc:
                    cloud_sync.session_start(acc["tag"], brawler, target,
                                             self._initial_trophies, self._session_id)
            except Exception:
                log.exception("cloud session_start push failed")
        else:
            log.warning("No account_id bound; matches won't be persisted to DB")

        original = observer.add_trophies
        runner = self

        emojis = {"victory": "🏆", "defeat": "💀", "draw": "🤝"}

        def wrapped(game_result, current_brawler):
            before = observer.current_trophies or 0
            log.info("match ended: result=%s brawler=%s trophies_before=%d",
                     game_result, current_brawler, before)
            ret = original(game_result, current_brawler)
            after = observer.current_trophies or 0
            delta = after - before
            log.info("match logged: delta=%+d trophies_after=%d", delta, after)
            # Update the running account-wide trophy total.
            runner._account_trophies += delta
            # Persist to DB before counters update.
            if runner._session_id is not None and runner._account_id is not None:
                try:
                    db.log_match(runner._session_id, runner._account_id,
                                 current_brawler, game_result, before, after,
                                 account_trophies_after=runner._account_trophies)
                except Exception as exc:
                    log.warning("db.log_match failed: %s", exc)
                # Push to cloud panel (fire-and-forget).
                try:
                    acc = db.get_account(runner._account_id)
                    if acc:
                        cloud_sync.match(acc["tag"], runner._session_id,
                                         current_brawler, game_result,
                                         before, after, runner._account_trophies)
                except Exception:
                    log.exception("cloud match push failed")
            runner._match_count += 1
            runner._last_match_at = time.monotonic()   # watchdog (clock-jump safe)
            runner._stuck_alerted = False  # reset on any progress
            # Feed the humane schedule's daily match counter.
            try: play_schedule.get().record_match()
            except Exception: log.debug("schedule record_match failed", exc_info=True)
            # We just played this brawler → it's the one BS has equipped. Record
            # it so a resume skips re-selecting it through the menu.
            runner._last_equipped = current_brawler
            # Keep the resume state fresh: refresh its timestamp + current
            # brawler each match so an actively-grinding session is NEVER
            # discarded as "too old" after a restart (the bug that left the
            # bot idle at the lobby, not grinding). Cheap PUT to our cloud.
            if runner._resume_state is not None:
                runner._resume_state["started_at"] = time.time()
                runner._resume_state["brawler"] = current_brawler
                runner._resume_state["last_equipped"] = current_brawler
                # Persist the running total so a block restart carries it forward
                # instead of snapping back to laggy brawlace.
                runner._resume_state["account_trophies"] = runner._account_trophies
                try:
                    _save_resume_state(runner._resume_state)
                except Exception:
                    log.debug("resume-state refresh failed", exc_info=True)
            if game_result == "victory":
                runner._win_count += 1
            elif game_result == "defeat":
                runner._loss_count += 1
            elif game_result == "draw":
                runner._draw_count += 1

            # max_matches cap: stop the runner cleanly after N matches.
            if runner._max_matches is not None and runner._match_count >= runner._max_matches:
                log.info("max_matches=%d reached — stopping bot", runner._max_matches)
                main_instance.time_to_stop = True
                _clear_resume_state()
            # Global trophy target (push_max mode): stop when account total
            # reaches the user-set objective.
            if (runner._target_total_trophies is not None
                    and runner._account_trophies >= runner._target_total_trophies):
                log.info("target_total_trophies=%d reached (current=%d) — stopping",
                         runner._target_total_trophies, runner._account_trophies)
                main_instance.time_to_stop = True
                _clear_resume_state()
                # Edge-trigger the notif on the match that actually crossed
                # the threshold — avoids spamming if the bot bounces around
                # the target across multiple matches.
                if (runner._account_trophies - delta) < runner._target_total_trophies \
                        and not runner._target_reached_notified:
                    runner._target_reached_notified = True
                    try:
                        cloud_sync.event("target_reached", {
                            "brawler": current_brawler,
                            "account_trophies": runner._account_trophies,
                            "target": runner._target_total_trophies,
                        })
                    except Exception:
                        log.exception("cloud_sync.event(target_reached) failed")

            # push_max: record match; swap brawler if current is exhausted
            # (swap mode only — no_swap grinds the equipped brawler).
            if runner._push_max is not None:
                runner._push_max.record_match(current_brawler, game_result, after)
                # Re-sync REAL trophies from brawlace (source of truth). The
                # seed at session start (and the resume snapshot) can be
                # stale-LOW, so push_max's tracked values lag reality and the
                # efficiency ceiling never fires — that's how shelly/barley got
                # picked at 1000+. We correct ALL brawlers, not just the current
                # one: a stale-low NON-current brawler (e.g. nita tracked 490 but
                # really 798) otherwise stays in the "easy" pool and keeps being
                # picked. Run on match #1 (close the stale-seed window fast) then
                # after every match, THROTTLED to one fetch per
                # BRAWLACE_SYNC_MIN_INTERVAL seconds: normal play (matches
                # ~2-3 min) syncs roughly every match → ~3× less trophy lag than
                # the old "every 3rd match", while the wall-clock floor caps the
                # API rate during fast draw/loss streaks (no flaresolverr
                # saturation). BACKGROUND thread: a slow/504 brawlace fetch in
                # the hot path would stall the grind (looks stuck).
                _now = time.monotonic()   # MONOTONIC: a clock rewind must not
                # leave _last_brawlace_sync in the future and block resync ~1h.
                _due = (runner._match_count == 1
                        or _now - runner._last_brawlace_sync >= BRAWLACE_SYNC_MIN_INTERVAL)
                if (_due and runner._account_id is not None
                        and not runner._resync_inflight):
                    # Stamp BEFORE launching: the throttle is on attempts, so two
                    # quick matches can't both fire a fetch. Capture the session id
                    # so a fetch that outlives a session restart can't write stale
                    # values onto the new session.
                    runner._last_brawlace_sync = _now
                    runner._resync_inflight = True
                    _sid = runner._session_id
                    def _resync(acc_id, brawler_name, pm, obs, sid):
                        try:
                            acc = db.get_account(acc_id)
                            if not acc:
                                return
                            from account_detect import fetch_account_profile
                            prof = fetch_account_profile(acc["tag"])
                            brawlers = prof.get("brawlers", [])
                            if not brawlers:
                                return
                            # If the session was restarted while this (slow) fetch
                            # ran, the runner now owns a DIFFERENT push_max/observer
                            # and a freshly-seeded _account_trophies — writing our
                            # stale values would make the total jump backward and
                            # mutate abandoned objects. Bail out.
                            if runner._session_id != sid:
                                log.info("resync: session changed mid-fetch — discarding stale result")
                                return
                            # Piggyback: push these fresh per-brawler trophies to
                            # the cloud right now. The panel's per-brawler display
                            # (win/loss chart, account detail) reads brawlers_json,
                            # which was otherwise only refreshed once an HOUR — the
                            # "retard sur les trophées du brawler". This reuses the
                            # fetch we just made → zero extra brawlace/API calls.
                            try:
                                cloud_sync.push_brawlers(acc["tag"], brawlers)
                            except Exception:
                                log.debug("resync push_brawlers failed", exc_info=True)
                            # Account total: driven by EXACT per-match deltas
                            # (read off the result screen) for smoothness, with
                            # an UPWARD-ONLY catch-up to the brawlace sum. We
                            # never anchor DOWNWARD: brawlace lags per-brawler (a
                            # value can read 501 long after the bot pushed it to
                            # 1075), and anchoring both ways made the counter jump
                            # erratically (losses showing +30). Upward-only with a
                            # margin fixes the opposite failure — missed match
                            # deltas slowly under-counting the real total (the
                            # "déficit vs réalité"). brawlace lag only ever makes
                            # the sum LOW (< tracked), so it can't trigger a false
                            # catch-up; a sum ABOVE tracked means we genuinely
                            # missed gains → catch up to reality.
                            real_total = sum(b.get("trophies", 0) for b in brawlers)
                            if real_total > runner._account_trophies + 15:
                                log.info("brawlace catch-up: account total %d → %d "
                                         "(recovered missed deltas)",
                                         runner._account_trophies, real_total)
                                runner._account_trophies = real_total
                            #
                            # Per-brawler trophies are corrected UPWARD ONLY (for
                            # the efficiency-ceiling decision). Never downward:
                            # brawlace lag would otherwise drop a just-pushed
                            # brawler back below the ceiling and it'd be re-picked
                            # at 1000+ (the shelly bug).
                            real_by_name = {
                                (b.get("name") or "").lower(): (b.get("trophies") or 0)
                                for b in brawlers
                            }
                            is_current = (brawler_name or "").lower()
                            for bname, bs in pm.brawlers.items():
                                real = real_by_name.get(bname.lower())
                                if real is None or real <= bs.trophies:
                                    continue
                                log.info("brawlace sync (up): %s %d → %d",
                                         bname, bs.trophies, real)
                                bs.trophies = real
                                if bname.lower() == is_current:
                                    obs.current_trophies = real
                        except Exception:
                            log.exception("brawlace trophy re-sync failed")
                        finally:
                            runner._resync_inflight = False
                    threading.Thread(target=_resync, daemon=True,
                                     args=(runner._account_id, current_brawler,
                                           runner._push_max, observer, _sid),
                                     name="brawlace-resync").start()
                if runner._push_max.no_swap:
                    # Stay-on-equipped: never swap. Only the per-brawler cap
                    # stops us here (the global target is handled above).
                    cur = runner._push_max.brawlers.get(current_brawler)
                    if cur and cur.exhausted:
                        log.info("push_max no_swap: %s hit its cap — stopping bot",
                                 current_brawler)
                        main_instance.time_to_stop = True
                        _clear_resume_state()
                else:
                    # Brawlers that failed selection (menu OCR couldn't find
                    # them) get marked exhausted so push_max stops trying to
                    # swap to them and grinds one it CAN select instead.
                    unsel = getattr(main_instance.Stage_manager,
                                    "_unselectable_brawlers", None)
                    if unsel:
                        for bad in list(unsel):
                            b = runner._push_max.brawlers.get(bad)
                            if b and not b.exhausted:
                                b.exhausted = True
                                log.warning("push_max: %s unselectable (menu OCR)"
                                            " — marking exhausted", bad)
                            runner._unselectable[bad] = time.time()
                        unsel.clear()
                        # Persist (timestamped, TTL'd) so a restart doesn't
                        # re-churn these now — but they're retried after the TTL
                        # (was a permanent ban → starved pool).
                        if runner._resume_state is not None:
                            runner._resume_state["unselectable"] = dict(runner._unselectable)
                            try:
                                _save_resume_state(runner._resume_state)
                            except Exception:
                                log.debug("unselectable persist failed", exc_info=True)
                    # Keep the grind ALIVE: when everything looks exhausted
                    # (stagnation + failed selections), don't stop — revive the
                    # still-grindable brawlers so the session runs to the global
                    # target. Only stop when there's genuinely nothing left
                    # (all capped or locked).
                    if runner._push_max.all_done():
                        revived = runner._push_max.revive_grindable()
                        # revive_grindable() un-exhausts everything below the cap,
                        # INCLUDING brawlers the menu OCR can't select. Re-mark
                        # those so we don't re-pick + re-fail them every revive
                        # cycle (in-session selection churn). They stay revived
                        # only if they're genuinely the last resort below.
                        for _bad in runner._unselectable:
                            _b = runner._push_max.brawlers.get(_bad)
                            if _b:
                                _b.exhausted = True
                        still = [b for b in runner._push_max.brawlers.values() if not b.exhausted]
                        if revived and still:
                            log.info("push_max: all exhausted — revived %d grindable "
                                     "brawlers, continuing toward target", len(still))
                        else:
                            log.info("push_max: nothing selectable+grindable left "
                                     "— stopping bot")
                            main_instance.time_to_stop = True
                            _clear_resume_state()
                    cur = runner._push_max.brawlers.get(current_brawler)
                    # Swap when the current brawler is exhausted OR has climbed
                    # past the efficiency ceiling — don't keep pushing it to
                    # e.g. 950; move to an easier brawler with better gains.
                    if (not main_instance.time_to_stop and cur
                            and (cur.exhausted or cur.trophies >= runner._push_max.efficiency_ceiling)):
                        nxt = runner._push_max.pick_next()
                        if nxt is None:
                            # Nothing below the efficiency ceiling left to grind.
                            # STOP rather than keep playing a 1000+ brawler that
                            # nets ~0 (the "why is it grinding bartaba at 1000+"
                            # complaint). The global target may not be reached —
                            # that's intended: efficiency over a futile grind.
                            log.info("push_max: current %s at %d ≥ ceiling and no "
                                     "efficient brawler left — stopping session",
                                     current_brawler, cur.trophies)
                            main_instance.time_to_stop = True
                            _clear_resume_state()
                            try:
                                cloud_sync.event("push_max_done", {
                                    "reason": "no_brawler_below_ceiling",
                                    "account_trophies": runner._account_trophies,
                                })
                            except Exception:
                                pass
                        elif nxt.name != current_brawler:
                            log.info("push_max: swap %s → %s (exhausted=%s, trophies=%d)",
                                     current_brawler, nxt.name, cur.exhausted, cur.trophies)
                            main_instance.Stage_manager._pending_swap = nxt.name
                            # Tell the new brawler's trophies to the observer so
                            # subsequent matches log correctly.
                            main_instance.Stage_manager.Trophy_observer.current_trophies = nxt.trophies

            sign = "+" if delta >= 0 else ""
            session_delta = after - runner._initial_trophies
            session_sign = "+" if session_delta >= 0 else ""
            wr = (runner._win_count / runner._match_count * 100) if runner._match_count else 0
            msg = alerts.format_alert(
                "match",
                emoji=emojis.get(game_result, "•"),
                result=game_result, result_upper=game_result.upper(),
                brawler=current_brawler,
                before=before, after=after, sign=sign, delta=delta,
                match_n=runner._match_count, wins=runner._win_count,
                losses=runner._loss_count, draws=runner._draw_count, wr=wr,
                session_sign=session_sign, session_delta=session_delta,
                target=target,
            )
            if msg and runner.notify:
                try: runner.notify(msg)
                except Exception as exc: log.warning("notify failed: %s", exc)
            # Target-reached notification (single-brawler mode only).
            if runner._push_max is None and after >= target > 0 and before < target:
                try:
                    cloud_sync.event("target_reached", {
                        "brawler": brawler, "account_trophies": after, "target": target,
                    })
                except Exception:
                    log.exception("cloud_sync.event(target_reached) failed")
            # Battery gate: post-match is the only safe place to pause —
            # we're between matches with the bot at the lobby. If battery
            # is too low, force-stop BS + screen off and block here until
            # it recharges past the resume threshold.
            try:
                import game_api as _gapi
                api = _gapi.get()
                if api is not None and not main_instance.time_to_stop:
                    ok_bat, reason = api.can_play()
                    if not ok_bat:
                        log.info("battery gate hit post-match: %s — entering power save", reason)
                        try:
                            bat = api.battery_status()
                            cloud_sync.event("battery_low", {"level": bat.get("level")})
                        except Exception:
                            log.exception("cloud_sync.event(battery_low) failed")
                        api.enter_power_save()
                        # Block until battery recovers (or 2h cap).
                        recovered = api.wait_for_battery(max_wait_s=7200, poll_s=60)
                        api.exit_power_save()
                        if recovered:
                            log.info("battery recovered — resuming grind")
                            try:
                                bat = api.battery_status()
                                cloud_sync.event("battery_resumed", {"level": bat.get("level")})
                            except Exception:
                                log.exception("cloud_sync.event(battery_resumed) failed")
                        else:
                            log.warning("battery did not recover within 2h — stopping bot")
                            main_instance.time_to_stop = True
            except Exception:
                log.exception("post-match battery gate failed")
            # Deferred restart drain: if a self-update or operator
            # restart was queued during the match, fire it now — we're
            # at the lobby and resume-state has been persisted, so
            # _try_resume_session() on relaunch will pick the grind
            # back up where it left off.
            try:
                import worker_link as _wl
                if _wl.is_restart_pending():
                    log.info("pending restart detected at end of match — firing now")
                    _wl.drain_pending_restart()
            except Exception:
                log.exception("pending restart drain failed")
            return ret

        observer.add_trophies = wrapped

    def status_summary(self) -> str:
        if not self.is_running():
            return "Bot: STOPPED"
        elapsed = time.time() - self.started_at
        lines = [f"Bot: RUNNING ({elapsed:.0f}s uptime)"]
        if self.brawler_data:
            d = self.brawler_data[0]
            lines.append(f"Brawler: {d.get('brawler')}  →  target {d.get('trophies')} trophies")
        m = self.main_instance
        if m is not None:
            try:
                lines.append(f"State: {m.state}")
                cur = m.Stage_manager.Trophy_observer.current_trophies
                lines.append(f"Current trophies: {cur}")
                if self._initial_trophies:
                    sd = (cur or 0) - self._initial_trophies
                    lines.append(f"Session: {sd:+d} trophies since start")
                lines.append(f"Wins: {m.Stage_manager.Trophy_observer.current_wins}")
                if self._match_count:
                    wr = self._win_count / self._match_count * 100
                    lines.append(
                        f"Matches: {self._match_count} "
                        f"(W{self._win_count}/L{self._loss_count}/D{self._draw_count}, {wr:.0f}% WR)"
                    )
            except Exception:
                pass
        return "\n".join(lines)

    def take_screenshot(self) -> bytes | None:
        """Capture phone screen via ADB. Works whether bot is running or not."""
        try:
            return subprocess.check_output(
                ["adb", "exec-out", "screencap", "-p"], timeout=5
            )
        except Exception as exc:
            log.warning("screenshot failed: %s", exc)
            return None


# ----------------------------------------------------------- Telegram client


class TelegramBot:
    # Popular brawlers shown first; user can /list to see the rest.
    POPULAR_BRAWLERS = [
        "shelly", "colt", "brock", "bull", "rico", "piper", "8bit", "bea",
        "nita", "jessie", "spike", "leon",
    ]
    # Trophy step buttons.
    TROPHY_STEPS = [100, 250, 500, 1000]

    def __init__(self, token: str, chat_id: int, poll_timeout_s: int = 25):
        self.token = token
        self.chat_id = chat_id
        self.poll_timeout = poll_timeout_s
        self.api = f"https://api.telegram.org/bot{token}"
        self.runner = BotRunner()
        # Worker-side Telegram notifs disabled — panel owns notifications.
        # The runner emits events via cloud_sync.event() which the panel
        # dispatches based on its global config. The TelegramBot here is
        # kept for legacy /start /stop /status commands only.
        self.runner.notify = None
        self.offset: int | None = None
        # Conversation state: stores partial /start args between button taps.
        # Key = chat_id (only one user, so single entry).
        self._wizard: dict = {}
        # Cached account info: {"tag": "PYLV98LG9", "brawlers": [{...}]}.
        # Populated by /start (re-detects every time so the user can switch
        # accounts between sessions).
        self._account: dict | None = None

    # --- HTTP helpers ---
    def _post(self, method: str, **payload):
        try:
            return requests.post(f"{self.api}/{method}", data=payload, timeout=30).json()
        except Exception as exc:
            log.warning("Telegram %s failed: %s", method, exc)
            return {}

    def _post_file(self, method: str, files, **payload):
        try:
            return requests.post(f"{self.api}/{method}", data=payload, files=files, timeout=60).json()
        except Exception as exc:
            log.warning("Telegram %s failed: %s", method, exc)
            return {}

    def send(self, text: str, keyboard=None) -> dict:
        payload = {"chat_id": self.chat_id, "text": text}
        if keyboard is not None:
            payload["reply_markup"] = json.dumps({"inline_keyboard": keyboard})
        return self._post("sendMessage", **payload)

    def edit(self, message_id: int, text: str, keyboard=None) -> dict:
        payload = {"chat_id": self.chat_id, "message_id": message_id, "text": text}
        if keyboard is not None:
            payload["reply_markup"] = json.dumps({"inline_keyboard": keyboard})
        return self._post("editMessageText", **payload)

    def send_photo(self, jpeg_bytes: bytes, caption: str = "") -> None:
        self._post_file(
            "sendPhoto",
            files={"photo": ("screen.png", jpeg_bytes, "image/png")},
            chat_id=self.chat_id, caption=caption,
        )

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self._post("answerCallbackQuery", callback_query_id=callback_id, text=text)

    # --- keyboard builders ---
    def _brawler_keyboard(self, page: int = 0):
        per_page = 12
        # Prefer the account-scraped list (owned brawlers + current
        # trophies). Falls back to the full brawler list.
        if self._account and self._account.get("brawlers"):
            owned = self._account["brawlers"]
            # Sort by current trophies desc (most-pushed first).
            owned = sorted(owned, key=lambda b: -b.get("trophies", 0))
            ordered_items = [
                (b["name"], f"{b['name'].capitalize()} ({b.get('trophies', '?')})")
                for b in owned
            ]
        else:
            try:
                all_brawlers = sorted(get_brawler_list())
            except Exception:
                all_brawlers = self.POPULAR_BRAWLERS.copy()
            ordered = self.POPULAR_BRAWLERS + [b for b in all_brawlers if b not in self.POPULAR_BRAWLERS]
            ordered_items = [(b, b.capitalize()) for b in ordered]
        start = page * per_page
        page_items = ordered_items[start:start + per_page]
        ordered = ordered_items  # alias for pagination logic below
        rows = []
        row = []
        for key, label in page_items:
            row.append({"text": label, "callback_data": f"brawler:{key}"})
            if len(row) == 3:
                rows.append(row); row = []
        if row:
            rows.append(row)
        # Pagination
        nav = []
        if start > 0:
            nav.append({"text": "◀ Prev", "callback_data": f"page:{page-1}"})
        if start + per_page < len(ordered):
            nav.append({"text": "Next ▶", "callback_data": f"page:{page+1}"})
        if nav:
            rows.append(nav)
        rows.append([{"text": "✖ Cancel", "callback_data": "cancel"}])
        return rows

    def _cancel_keyboard(self):
        return [[
            {"text": "◀ Back", "callback_data": "back_to_brawlers"},
            {"text": "✖ Cancel", "callback_data": "cancel"},
        ]]

    # --- command dispatch ---
    HELP_TEXT = (
        "Commands:\n"
        "/start — choose brawler & trophy target (interactive)\n"
        "/stop — finish current match, then stop (soft)\n"
        "/forcestop — stop NOW, abandon current match\n"
        "/status — current stats\n"
        "/screenshot — send a live screenshot\n"
        "/help — this message"
    )

    def handle(self, text: str) -> None:
        parts = text.strip().split()
        if not parts:
            return
        # Wizard waits for a numeric trophy target — eat the message here
        # so /commands still pass through when "/" is the first char.
        if self._wizard.get("awaiting_target") and not text.startswith("/"):
            try:
                target = int(parts[0])
            except ValueError:
                self.send("That doesn't look like a number. Send the target trophy count (e.g. 700).")
                return
            brawler = self._wizard.get("brawler")
            if not brawler:
                self.send("Session lost. /start again.")
                self._wizard.clear()
                return
            ok, msg = self.runner.start(brawler, target, 0)
            self.send(f"{msg}\nTarget: {target} 🏆")
            self._wizard.clear()
            return
        cmd = parts[0].lower()
        if cmd in ("/help", "help"):
            self.send(self.HELP_TEXT)
        elif cmd == "/start":
            if self.runner.is_running():
                self.send("Bot already running. Use /stop first.")
                return
            self._wizard.clear()
            # Detect the current account so the keyboard only shows owned
            # brawlers (with their current trophies as labels).
            self.send("🔍 Detecting account…")
            tag = detect_player_tag()
            if tag:
                profile = fetch_account_profile(tag)
                brawlers = profile["brawlers"]
                if brawlers:
                    self._account = {"tag": tag, "name": profile.get("name"),
                                     "brawlers": brawlers}
                    # Register the account in the DB + worker pool so the
                    # panel can see it and so matches get persisted.
                    account_id = db.upsert_account(
                        tag, name=profile.get("name"),
                        device_serial=device.adb_serial(),
                        telegram_chat_id=self.chat_id,
                    )
                    self.runner._account_id = account_id
                    POOL.register(account_id, BotWorker(
                        account_id, device.adb_serial(), self.runner,
                    ))
                    db.log_event("account_detected",
                                 {"tag": tag, "brawlers_owned": len(brawlers)},
                                 account_id=account_id)
                    label = profile.get("name") or f"#{tag}"
                    self.send(
                        f"Account: {label} (#{tag})\n"
                        f"{len(brawlers)} brawlers owned.\n"
                        f"Pick one:",
                        keyboard=self._brawler_keyboard(0),
                    )
                    return
                self.send(
                    f"⚠️ Got tag #{tag} but brawlace.com returned no data — "
                    "falling back to full list."
                )
            else:
                self.send(
                    "⚠️ Couldn't detect account tag (is the lobby visible?). "
                    "Falling back to full list."
                )
            self._account = None
            self.send("Pick a brawler:", keyboard=self._brawler_keyboard(0))
        elif cmd == "/stop":
            _ok, msg = self.runner.stop()
            self.send(msg)
        elif cmd == "/forcestop":
            _ok, msg = self.runner.force_stop()
            self.send(msg)
        elif cmd == "/status":
            self.send(self.runner.status_summary())
        elif cmd == "/screenshot":
            img = self.runner.take_screenshot()
            if img is None:
                self.send("Could not capture screen.")
            else:
                self.send_photo(img, caption=f"State: {getattr(self.runner.main_instance, 'state', '?')}")
        else:
            self.send(f"Unknown command.\n{self.HELP_TEXT}")

    def handle_callback(self, data: str, message_id: int, callback_id: str) -> None:
        """Handle inline-button callbacks (the /start wizard)."""
        self.answer_callback(callback_id)
        if data == "cancel":
            self._wizard.clear()
            self.edit(message_id, "Cancelled.")
            return
        if data.startswith("page:"):
            page = int(data.split(":", 1)[1])
            self.edit(message_id, "Pick a brawler:", keyboard=self._brawler_keyboard(page))
            return
        if data == "back_to_brawlers":
            self.edit(message_id, "Pick a brawler:", keyboard=self._brawler_keyboard(0))
            return
        if data.startswith("brawler:"):
            brawler = data.split(":", 1)[1]
            self._wizard["brawler"] = brawler
            self._wizard["awaiting_target"] = True
            self.edit(
                message_id,
                f"Brawler: {brawler.capitalize()}\n"
                f"Send the TARGET trophy count for this brawler "
                f"(e.g. 700 = push until 700 🏆).",
                keyboard=self._cancel_keyboard(),
            )
            return
        self.edit(message_id, f"Unknown action: {data}")

    # --- main loop ---
    def run(self) -> None:
        # The interactive Telegram bot now lives on the CLOUD panel (single
        # webhook consumer of the token — see cloud_panel/telegram_bot.py).
        # Workers must NOT also poll getUpdates: Telegram allows one consumer
        # per token, so two pollers (or a poller + the cloud webhook) collide
        # with 409s. Default = don't poll; just keep the process alive (the WS
        # link, game loop and session-keepalive run in daemon threads). Set
        # WORKER_TELEGRAM_POLL=1 to restore local polling (legacy/standalone).
        if os.environ.get("WORKER_TELEGRAM_POLL", "0") != "1":
            log.info("worker Telegram polling disabled — cloud panel owns the "
                     "bot via webhook. Idling to keep the process alive.")
            while True:
                time.sleep(3600)
        while True:
            params = {"timeout": self.poll_timeout}
            if self.offset is not None:
                params["offset"] = self.offset
            try:
                r = requests.get(f"{self.api}/getUpdates", params=params, timeout=self.poll_timeout + 10)
                data = r.json()
            except Exception as exc:
                log.warning("getUpdates failed: %s", exc)
                time.sleep(2)
                continue
            for upd in data.get("result", []):
                self.offset = upd["update_id"] + 1
                # 1. Inline-button callbacks (wizard flow)
                cq = upd.get("callback_query")
                if cq:
                    cb_chat = cq.get("message", {}).get("chat", {}).get("id")
                    if cb_chat != self.chat_id:
                        continue
                    payload = cq.get("data", "")
                    msg_id = cq["message"]["message_id"]
                    cb_id = cq["id"]
                    log.info("CB: %s", payload)
                    try:
                        self.handle_callback(payload, msg_id, cb_id)
                    except Exception:
                        log.exception("callback error")
                        self.send("Error handling button — check logs.")
                    continue
                # 2. Text commands
                msg = upd.get("message") or upd.get("edited_message")
                if not msg:
                    continue
                chat_id = msg.get("chat", {}).get("id")
                if chat_id != self.chat_id:
                    continue
                text = msg.get("text", "")
                if not text:
                    continue
                log.info("RX: %s", text)
                try:
                    self.handle(text)
                except Exception:
                    log.exception("handler error")
                    self.send("Error handling command — check logs.")


def _bootstrap_account(bot: "TelegramBot") -> None:
    """Detect / load the account once at startup so the panel isn't empty.

    Resolution order:
      1. cfg/device.toml override (account_tag + account_name)
      2. Local DB: a previous run already validated this device's account
      3. OCR-based detection via detect_player_tag() (last resort — slow)
    """
    try:
        serial = device.adb_serial()
        # 1. Manual override (cfg/device.toml)
        override_tag, override_name = device.account_override()
        if override_tag:
            log.info("bootstrap: using account override tag=%s name=%s",
                     override_tag, override_name)
            tag = override_tag
            name = override_name
            brawlers: list[dict] = []
            try:
                profile = fetch_account_profile(tag)
                brawlers = profile.get("brawlers") or []
                name = profile.get("name") or name
            except Exception:
                log.warning("brawlace unreachable; using override values as-is")
        else:
            # 2. Fast path: previous run for this device already validated
            #    an account → skip the expensive OCR + flaresolverr loop.
            existing = next((a for a in db.list_accounts()
                             if a.get("device_serial") == serial), None)
            if existing:
                log.info("bootstrap: reusing known account from local DB "
                         "(tag=%s device=%s) — skipping OCR validation",
                         existing["tag"], serial)
                tag = existing["tag"]
                name = existing.get("name")
                brawlers = []  # let the worker's brawler refresh loop fill this
            else:
                # 3. OCR-based detection (slow: 2-3 min in the worst case)
                tag = detect_player_tag()
                if not tag:
                    log.info("bootstrap: no tag detected (game not in lobby?)")
                    return
                profile = fetch_account_profile(tag)
                brawlers = profile.get("brawlers") or []
                name = profile.get("name")
                if not brawlers:
                    log.warning("bootstrap: tag #%s returned no brawlers from brawlace — skipping",
                                tag)
                    return
        account_id = db.upsert_account(
            tag, name=name,
            device_serial=device.adb_serial(),
            telegram_chat_id=bot.chat_id,
        )
        cloud_sync.account(tag, name)
        bot.runner._account_id = account_id
        bot._account = {"tag": tag, "name": name, "brawlers": brawlers}
        POOL.register(account_id, BotWorker(
            account_id, device.adb_serial(), bot.runner,
        ))
        log.info("bootstrap: registered account #%s (%d brawlers known)",
                 tag, len(brawlers))
    except Exception:
        log.exception("bootstrap_account failed")


def _start_panel_thread() -> None:
    """Run the FastAPI panel in a daemon thread, bound to 127.0.0.1:8000."""
    import uvicorn
    from panel.app import app as panel_app

    config = uvicorn.Config(panel_app, host="127.0.0.1", port=8000,
                            log_level="warning")
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True, name="panel")
    t.start()
    log.info("Panel listening on http://127.0.0.1:8000")


def main() -> int:
    cfg_path = BASE / "cfg" / "telegram.toml"
    with cfg_path.open("rb") as f:
        cfg = tomllib.load(f)
    # Init DB up-front so both Telegram and panel see the schema.
    db.init()
    if api_base_url != "localhost":
        try:
            all_brawlers = get_brawler_list()
            update_missing_brawlers_info(all_brawlers)
            check_version()
            update_wall_model_classes()
            if not current_wall_model_is_latest():
                print("Updating wall model...")
                get_latest_wall_model_file()
        except Exception:
            log.exception("non-fatal startup update failed; continuing")
    bot = TelegramBot(cfg["bot_token"], cfg["chat_id"], cfg.get("poll_timeout_s", 25))
    # Share the bot's runner with the panel so panel-side controls can
    # lazy-register workers for accounts seen in the DB.
    from panel import app as panel_module
    panel_module.set_shared_runner(bot.runner)
    _start_panel_thread()
    # ---- Phase 1: bring the instance ONLINE as fast as possible ----
    # Heartbeat + WS connection + account push BEFORE the slow init steps.
    # The user sees "preparing" status until phase 2 completes.
    try:
        if cloud_sync.is_enabled():
            cloud_sync.heartbeat(metadata={"preparing": True})
            cloud_sync.start_heartbeat_loop()
            cloud_sync.start_history_sync_loop()
            cloud_sync.start_brawlers_refresh_loop()
            for acc in db.list_accounts():
                try:
                    cloud_sync.account(acc["tag"], acc.get("name"))
                except Exception: pass
            log.info("Phase 1 ok — instance online (preparing)")
    except Exception:
        log.exception("phase 1 cloud_sync failed (non-fatal)")
    # WS link — open BEFORE host_bootstrap so the cloud panel sees us
    # as connected immediately. Commands targeting GameAPI will return
    # 503 until phase 2 finishes; that's acceptable during preparing.
    try:
        import worker_link
        worker_link.start()
    except Exception:
        log.exception("worker_link failed to start")

    # ---- Phase 2: heavy init (game launch + GameAPI) in background ----
    # Keeps the WS / heartbeat / Telegram loop responsive during boot.
    # Retries every 60s on failure so the bot self-heals when the phone
    # gets plugged back in (or BlueStacks restarted, etc.).
    def _phase2_init():
        import game_api
        attempt = 0
        while game_api.get() is None:
            attempt += 1
            try:
                import host_bootstrap
                if not host_bootstrap.bootstrap_host():
                    log.warning("host_bootstrap reported a problem; continuing")
            except Exception:
                log.exception("host_bootstrap raised")
            try:
                from window_controller import WindowController
                from lobby_automation import LobbyAutomation
                _shared_wc = WindowController()
                _shared_la = LobbyAutomation(_shared_wc)
                _SHARED_RUNTIME["wc"] = _shared_wc
                _SHARED_RUNTIME["la"] = _shared_la
                game_api.init(_shared_wc, _shared_la).set_runner(bot.runner)
                log.info("Phase 2 ok — GameAPI ready, instance fully online (attempt %d)",
                         attempt)
                # Flip the heartbeat metadata to "ready" so the cloud
                # transitions us out of 'preparing'.
                try:
                    cloud_sync.heartbeat(metadata={"preparing": False, "ready": True})
                except Exception:
                    pass
                # Auto-resume an interrupted session if a resume state
                # file exists. Triggered by self-update / crash / reboot.
                _try_resume_session(bot)
                return
            except Exception as exc:
                log.warning("phase 2 (attempt %d) failed: %s — retrying in 60s",
                            attempt, exc)
                time.sleep(60)
    threading.Thread(target=_phase2_init, daemon=True, name="phase2-init").start()
    # Best-effort: detect the connected account at startup so the panel
    # has something to show before the user runs /start. Silently skips
    # if the game isn't on the lobby screen.
    threading.Thread(target=_bootstrap_account, args=(bot,),
                     daemon=True, name="bootstrap-account").start()

    # Always-on session keepalive: if a grind crashes mid-run (e.g. the
    # capture/ADB pipe dies with "Bad file descriptor"), the run ends but
    # the process keeps idling at the lobby. _try_resume_session re-launches
    # the saved session (it no-ops when already running, when there's no
    # resume state — cleared on user-stop/target-reached — or when the state
    # is >2h old). Without this the bot could sit idle for hours.
    def _session_keepalive():
        import game_api as _ga
        while True:
            time.sleep(150)
            try:
                if _ga.get() is not None:
                    # Close/reopen the game per the humane schedule (sleep/cap),
                    # independent of whether there's a session to resume.
                    _manage_schedule_powersave(bot)
                    _try_resume_session(bot)
            except Exception:
                log.exception("session-keepalive iteration crashed")
    threading.Thread(target=_session_keepalive, daemon=True,
                     name="session-keepalive").start()

    signal.signal(signal.SIGINT, lambda *a: sys.exit(0))
    bot.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
