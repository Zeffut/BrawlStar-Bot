"""Telegram-controlled PylaAI bot.

Replaces the Tkinter GUI (login + select_brawler) with a Telegram bot
interface. The PylaAI core (Play, StageManager, etc.) is started in a
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
import tkinter as tk

# Same prelude as main.py — silence noisy logs.
os.environ.setdefault("ORT_LOGGING_LEVEL", "3")
os.environ.setdefault("ONNXRUNTIME_LOGGING_LEVEL", "3")
warnings.filterwarnings("ignore")
logging.disable(logging.CRITICAL)

# Patch Tk to avoid background-thread destruction errors (PylaAI uses Tk
# internally for some helpers; we don't show any GUI but keep the lib happy).
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

# Re-enable our own logger (we silenced everything globally above).
log = logging.getLogger("telegram_main")
log.disabled = False
log.setLevel(logging.INFO)
_h = logging.StreamHandler()
_h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
log.addHandler(_h)


# ----------------------------------------------------------- bot lifecycle


class BotRunner:
    """Manages the PylaAI bot in a background thread."""

    def __init__(self) -> None:
        self.thread: threading.Thread | None = None
        self.main_instance = None  # PylaAI Main() instance (when running)
        self.brawler_data: list[dict] | None = None
        self.started_at: float = 0.0
        self.stop_flag = threading.Event()
        self._lock = threading.Lock()

    def is_running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self, brawler: str, trophies: int, wins: int) -> tuple[bool, str]:
        with self._lock:
            if self.is_running():
                return False, "Bot already running. Use /stop first."
            data = [{
                "brawler": brawler,
                "trophies": trophies,
                "wins": wins,
                "win_streak": 0,
                "automatically_pick": True,
                "type": "trophies",
                "push_until": trophies + 100,  # placeholder
            }]
            try:
                save_brawler_data(data)
            except Exception as exc:
                log.warning("save_brawler_data failed: %s", exc)
            self.brawler_data = data
            self.stop_flag.clear()
            self.thread = threading.Thread(target=self._run, args=(data,), daemon=True)
            self.thread.start()
            self.started_at = time.time()
            return True, f"Started bot for {brawler} (target {trophies} trophies)."

    def stop(self) -> tuple[bool, str]:
        with self._lock:
            if not self.is_running():
                return False, "Bot is not running."
            # PylaAI's Main loop checks `self.time_to_stop` — we trigger it.
            try:
                if self.main_instance is not None:
                    self.main_instance.time_to_stop = True
                    self.main_instance.in_cooldown = True
                    self.main_instance.cooldown_start_time = time.time()
            except Exception as exc:
                log.warning("could not set stop flag on Main: %s", exc)
            self.stop_flag.set()
            # Give the loop ~3 seconds to exit gracefully.
            if self.thread:
                self.thread.join(timeout=3.0)
            self.main_instance = None
            return True, "Bot stopping."

    def _run(self, data: list[dict]) -> None:
        # This mirrors pyla_main() in main.py but exposes the Main instance
        # so /stop and /status can reach into it.
        class Main:
            def __init__(_self):
                _self.window_controller = WindowController()
                _self.Play = Play(*_self.load_models(), _self.window_controller)
                _self.Time_management = TimeManagement()
                _self.lobby_automator = LobbyAutomation(_self.window_controller)
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
            self.main_instance.main()
        except Exception:
            log.exception("Bot crashed")
        finally:
            self.main_instance = None
            log.info("Bot run ended")

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
                lines.append(f"Current trophies: {m.Stage_manager.Trophy_observer.current_trophies}")
                lines.append(f"Wins: {m.Stage_manager.Trophy_observer.current_wins}")
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
    def __init__(self, token: str, chat_id: int, poll_timeout_s: int = 25):
        self.token = token
        self.chat_id = chat_id
        self.poll_timeout = poll_timeout_s
        self.api = f"https://api.telegram.org/bot{token}"
        self.runner = BotRunner()
        self.offset: int | None = None

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

    def send(self, text: str) -> None:
        self._post("sendMessage", chat_id=self.chat_id, text=text)

    def send_photo(self, jpeg_bytes: bytes, caption: str = "") -> None:
        self._post_file(
            "sendPhoto",
            files={"photo": ("screen.png", jpeg_bytes, "image/png")},
            chat_id=self.chat_id, caption=caption,
        )

    # --- command dispatch ---
    HELP_TEXT = (
        "Commands:\n"
        "/start <brawler> <trophies> <wins> — launch bot\n"
        "/stop — stop the bot\n"
        "/status — current stats\n"
        "/screenshot — send a live screenshot\n"
        "/help — this message"
    )

    def handle(self, text: str) -> None:
        parts = text.strip().split()
        if not parts:
            return
        cmd = parts[0].lower()
        args = parts[1:]
        if cmd in ("/help", "/start_help", "help"):
            self.send(self.HELP_TEXT)
        elif cmd == "/start":
            if len(args) < 3:
                self.send("Usage: /start <brawler> <trophies> <wins>\nEx: /start colt 600 0")
                return
            brawler = args[0].lower()
            try:
                trophies = int(args[1])
                wins = int(args[2])
            except ValueError:
                self.send("trophies and wins must be integers")
                return
            ok, msg = self.runner.start(brawler, trophies, wins)
            self.send(msg)
        elif cmd == "/stop":
            ok, msg = self.runner.stop()
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
            self.send(f"Unknown command. {self.HELP_TEXT}")

    # --- main loop ---
    def run(self) -> None:
        self.send("Bot interface online. Send /help for commands.")
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
                msg = upd.get("message") or upd.get("edited_message")
                if not msg:
                    continue
                chat_id = msg.get("chat", {}).get("id")
                if chat_id != self.chat_id:
                    continue  # ignore messages from other chats
                text = msg.get("text", "")
                if not text:
                    continue
                log.info("RX: %s", text)
                try:
                    self.handle(text)
                except Exception:
                    log.exception("handler error")
                    self.send("Error handling command — check logs.")


def main() -> int:
    cfg_path = BASE / "cfg" / "telegram.toml"
    with cfg_path.open("rb") as f:
        cfg = tomllib.load(f)
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
    signal.signal(signal.SIGINT, lambda *a: sys.exit(0))
    bot.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
