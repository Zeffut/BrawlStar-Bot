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

import io
import json
import logging
import os
import platform
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
    get_latest_version, load_toml_as_dict, current_wall_model_is_latest,
    api_base_url, save_brawler_data,
)
from time_management import TimeManagement  # noqa: E402
from state_finder.main import get_state  # noqa: E402
from stage_manager import StageManager  # noqa: E402
from play import Play  # noqa: E402
from lobby_automation import LobbyAutomation  # noqa: E402
from account_detect import detect_player_tag, fetch_owned_brawlers, fetch_account_profile, ensure_lobby  # noqa: E402
import db  # noqa: E402
from worker_pool import POOL, BotWorker  # noqa: E402
import alerts  # noqa: E402
import device  # noqa: E402
import cloud_sync  # noqa: E402
from push_max import PushMaxStrategy  # noqa: E402
from logging_setup import setup_logging  # noqa: E402

setup_logging()
log = logging.getLogger("telegram_main")


# ----------------------------------------------------------- bot lifecycle


# Shared singletons created at startup (after host_bootstrap).
# Main reuses these to avoid double-initializing scrcpy/ADB.
_SHARED_RUNTIME: dict = {}


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
        # Running account-wide trophy total (sum across all brawlers).
        # Seeded from brawlace at session start, then updated by deltas
        # from each match. Used by the panel for the progression chart.
        self._account_trophies: int = 0
        self._target_reached_notified: bool = False

    def is_running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self, brawler: str, trophies: int, wins: int,
              mode: str = "single",
              owned_brawlers: list[dict] | None = None,
              max_matches: int | None = None,
              target_total_trophies: int | None = None) -> tuple[bool, str]:
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
            if mode == "push_max":
                if not owned_brawlers:
                    return False, "push_max needs the owned-brawlers list."
                self._push_max = PushMaxStrategy.from_owned(owned_brawlers)
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
            self._last_match_at = time.time()
            self._stuck_alerted = False
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

    def _start_stuck_watchdog(self) -> None:
        """Background thread: alerts via Telegram if no match completes for
        too long while a session is active.

        Threshold read from cfg/alerts.toml `[bot_stuck] threshold_minutes`
        (default 8 min). Alert is fired ONCE per stuck episode (cleared on
        any match progress).
        """
        def watch():
            from alerts import _load as _alerts_load
            while self.is_running():
                time.sleep(60)
                try:
                    elapsed = time.time() - self._last_match_at
                    cfg = _alerts_load().get("bot_stuck", {})
                    threshold_min = float(cfg.get("threshold_minutes", 8))
                    if elapsed > threshold_min * 60 and not self._stuck_alerted:
                        log.warning("STUCK detected: no match for %.0f min", elapsed / 60)
                        self._stuck_alerted = True
                        msg = alerts.format_alert(
                            "bot_stuck",
                            minutes=int(elapsed / 60),
                            brawler=(self.brawler_data[0]["brawler"]
                                     if self.brawler_data else "?"),
                            matches=self._match_count,
                        )
                        if msg and self.notify:
                            try: self.notify(msg)
                            except Exception: log.exception("stuck alert send failed")
                        # Best-effort recovery: try to dismiss any popup back to lobby
                        try:
                            import game_api as _ga
                            api = _ga.get()
                            if api is not None:
                                api.goto_lobby(max_attempts=10)
                        except Exception:
                            log.exception("auto-recovery failed")
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
        """
        with self._lock:
            if not self.is_running():
                return False, "Bot is not running."
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
                _self.states_requiring_data = ["lobby"]
                if data[0]['automatically_pick']:
                    _self.lobby_automator.select_brawler(data[0]['brawler'])
                _self.Play.current_brawler = data[0]['brawler']
                _self.no_detections_action_threshold = 60 * 8
                _self.initialize_stage_manager()
                _self.state = None
                try:
                    _self.max_ips = int(load_toml_as_dict("cfg/general_config.toml")['max_ips'])
                except (ValueError, KeyError):
                    _self.max_ips = None
                _self.run_for_minutes = int(load_toml_as_dict("cfg/general_config.toml")['run_for_minutes'])
                _self.start_time = time.time()
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
                        if (time.time() - _self.start_time) / 60 >= _self.run_for_minutes:
                            _self.in_cooldown = True
                            _self.cooldown_start_time = time.time()
                            _self.Stage_manager.states['lobby'] = lambda: 0
                    if _self.in_cooldown:
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
                    cloud_sync.session_end(self._session_id, "stopped", end_trophies)
                except Exception:
                    log.exception("cloud session_end push failed")
                self._session_id = None
            self.main_instance = None
            log.info("Bot run ended")

    def _read_brawler_trophies(self, main_instance) -> int | None:
        """OCR the trophy badge on the current brawler's lobby card.

        Returns the trophy count if found, else None. Tries for ~10s,
        waiting until the bot is back on the lobby screen.
        """
        import numpy as np
        from utils import extract_text_and_positions

        wc = main_instance.window_controller
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                frame = wc.screenshot()
                if frame is None:
                    time.sleep(0.3)
                    continue
                if get_state(frame) != "lobby":
                    time.sleep(0.5)
                    continue
                # Trophy badge on the brawler card: "<cup-icon> NNN" with
                # a circular rank badge to the right. The leading "1" gets
                # confused with the gold cup icon if we OCR the raw crop
                # (returns "761" instead of "161"). Fix: keep only
                # near-white pixels — that drops the cup icon and the
                # rank-circle background, leaving only the white digits.
                img = frame.convert('RGB').resize((1920, 1080))
                crop = np.array(img.crop((900, 150, 1100, 240)))
                mask = (crop[:, :, 0] > 200) & (crop[:, :, 1] > 200) & (crop[:, :, 2] > 200)
                clean = np.zeros_like(crop)
                clean[mask] = [255, 255, 255]
                from PIL import Image as _Image
                up = _Image.fromarray(clean).resize(
                    (clean.shape[1] * 4, clean.shape[0] * 4)
                )
                for key in extract_text_and_positions(np.array(up)).keys():
                    d = ''.join(c for c in key if c.isdigit())
                    if d and 0 < int(d) <= 9999:
                        return int(d)
            except Exception as exc:
                log.warning("_read_brawler_trophies error: %s", exc)
            time.sleep(0.5)
        return None

    def _install_match_hook(self, main_instance) -> None:
        """Wrap Trophy_observer.add_trophies to push a Telegram log line
        after every match (victory/defeat/draw, trophy delta, totals)."""
        observer = main_instance.Stage_manager.Trophy_observer
        self._match_count = self._win_count = self._loss_count = self._draw_count = 0
        self._target_reached_notified = False
        target = self._target_trophies or 0
        brawler = (self.brawler_data[0].get('brawler') if self.brawler_data else '?')
        log.info("_install_match_hook: brawler=%s target=%d", brawler, target)

        # Seed the account-wide trophy total from the current brawlace
        # snapshot. After this, each match delta will update it locally.
        try:
            acc = db.get_account(self._account_id) if self._account_id else None
            if acc:
                from account_detect import fetch_account_profile
                profile = fetch_account_profile(acc["tag"])
                self._account_trophies = sum(
                    b.get("trophies", 0) for b in profile.get("brawlers", [])
                )
                log.info("seeded account trophies: %d (sum of %d brawlers)",
                         self._account_trophies, len(profile.get("brawlers", [])))
        except Exception:
            log.exception("could not seed account trophies; starting at 0")
            self._account_trophies = 0

        # OCR the brawler card to read CURRENT trophies on the lobby card.
        log.debug("starting trophy OCR on lobby card…")
        current = self._read_brawler_trophies(main_instance)
        log.info("trophy OCR result: %s", current)
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
            runner._last_match_at = time.time()
            runner._stuck_alerted = False  # reset on any progress
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
            # Global trophy target (push_max mode): stop when account total
            # reaches the user-set objective.
            if (runner._target_total_trophies is not None
                    and runner._account_trophies >= runner._target_total_trophies):
                log.info("target_total_trophies=%d reached (current=%d) — stopping",
                         runner._target_total_trophies, runner._account_trophies)
                main_instance.time_to_stop = True
                # Edge-trigger the notif on the match that actually crossed
                # the threshold — avoids spamming if the bot bounces around
                # the target across multiple matches.
                if (runner._account_trophies - delta) < runner._target_total_trophies \
                        and not runner._target_reached_notified:
                    runner._target_reached_notified = True
                    tmsg = alerts.format_alert(
                        "target_reached",
                        brawler=current_brawler,
                        trophies=runner._account_trophies,
                        target=runner._target_total_trophies,
                    )
                    if tmsg and runner.notify:
                        try: runner.notify(tmsg)
                        except Exception as exc: log.warning("target notify failed: %s", exc)

            # push_max: record match, swap brawler if current one is exhausted.
            if runner._push_max is not None:
                runner._push_max.record_match(current_brawler, game_result, after)
                if runner._push_max.all_done():
                    log.info("push_max: all brawlers exhausted — stopping bot")
                    main_instance.time_to_stop = True
                else:
                    cur = runner._push_max.brawlers.get(current_brawler)
                    if cur and cur.exhausted:
                        nxt = runner._push_max.pick_next()
                        if nxt is not None and nxt.name != current_brawler:
                            log.info("push_max: scheduling brawler swap %s → %s",
                                     current_brawler, nxt.name)
                            main_instance.Stage_manager._pending_swap = nxt.name
                            # Tell the new brawler's trophies to the
                            # observer so subsequent matches log correctly.
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
                tmsg = alerts.format_alert(
                    "target_reached", brawler=brawler, trophies=after, target=target,
                )
                if tmsg and runner.notify:
                    try: runner.notify(tmsg)
                    except Exception: pass
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
        # Send match results / target-reached events as Telegram messages.
        self.runner.notify = lambda text: self.send(text)
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
    def _phase2_init():
        try:
            import host_bootstrap
            if not host_bootstrap.bootstrap_host():
                log.warning("host_bootstrap reported a problem; continuing")
        except Exception:
            log.exception("host_bootstrap raised")
        try:
            import game_api
            from window_controller import WindowController
            from lobby_automation import LobbyAutomation
            _shared_wc = WindowController()
            _shared_la = LobbyAutomation(_shared_wc)
            _SHARED_RUNTIME["wc"] = _shared_wc
            _SHARED_RUNTIME["la"] = _shared_la
            game_api.init(_shared_wc, _shared_la).set_runner(bot.runner)
            log.info("Phase 2 ok — GameAPI ready, instance fully online")
            # Flip the heartbeat metadata to "ready" so the cloud
            # transitions us out of 'preparing'.
            try:
                cloud_sync.heartbeat(metadata={"preparing": False, "ready": True})
            except Exception:
                pass
        except Exception:
            log.exception("phase 2 (GameAPI) failed — remote control degraded")
    threading.Thread(target=_phase2_init, daemon=True, name="phase2-init").start()
    # Best-effort: detect the connected account at startup so the panel
    # has something to show before the user runs /start. Silently skips
    # if the game isn't on the lobby screen.
    threading.Thread(target=_bootstrap_account, args=(bot,),
                     daemon=True, name="bootstrap-account").start()
    signal.signal(signal.SIGINT, lambda *a: sys.exit(0))
    bot.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
