import sys
import os
import platform
import asyncio
import time
import logging
import warnings
import tkinter as tk

# OS Detection Status
print(f"{platform.system()} detected")

# silent the console a bit from warnings
os.environ['ORT_LOGGING_LEVEL'] = '3'
os.environ['ONNXRUNTIME_LOGGING_LEVEL'] = '3' # Catch-all for all ORT versions
warnings.filterwarnings("ignore")      # Silence Python Deprecation warnings
logging.disable(logging.CRITICAL)      # Muzzle CustomTkinter/PIL spam

def safe_tk_del(self):
    try:
        if self._tk.getboolean(self._tk.call("info", "exists", self._name)):
            self._tk.globalgetvar(self._name)
    except:
        pass
tk.Variable.__del__ = safe_tk_del

def silent_exception_handler(exc_type, exc_value, tb):
    """
    Kills the 'RuntimeError: main thread is not in main loop' spam 
    while keeping real crashes visible.
    """
    if issubclass(exc_type, (RuntimeError, AttributeError)) and "main loop" in str(exc_value):
        return
    sys.__excepthook__(exc_type, exc_value, tb)

sys.excepthook = silent_exception_handler
# =======================================================

# checks if its running as compiled build or source code so it knows from whare to read data
if getattr(sys, 'frozen', False):
    # Running as compiled bundle
    bundle_dir = sys._MEIPASS 
    internal_dir = os.path.join(bundle_dir, "_internal")
    if os.path.exists(internal_dir):
        sys.path.insert(0, internal_dir)
    sys.path.insert(0, bundle_dir)
else:
    # Running as a script
    bundle_dir = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, bundle_dir)

# =======================================================

from window_controller import WindowController
from utils import (get_brawler_list, update_missing_brawlers_info, check_version, 
                   async_notify_user, update_wall_model_classes, get_latest_wall_model_file, 
                   get_latest_version, cprint, load_toml_as_dict, current_wall_model_is_latest, 
                   api_base_url)
from time_management import TimeManagement
from state_finder.main import get_state
from stage_manager import StageManager
from play import Play
from lobby_automation import LobbyAutomation
from gui.select_brawler import SelectBrawler
from gui.main import App
from gui.login import login
from gui.hub import Hub
import window_controller

IS_LINUX = platform.system() == "Linux"
IS_MAC = platform.system() == "Darwin"

# Set App ID for Windows taskbar
if not IS_LINUX and not IS_MAC:
    import ctypes
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('pyla.ai.bot.v1')
    except:
        pass

pyla_version = load_toml_as_dict("./cfg/general_config.toml")['pyla_version']

def pyla_main(data):
    class Main:
        def __init__(self):
            self.window_controller = WindowController()
            self.Play = Play(*self.load_models(), self.window_controller)
            self.Time_management = TimeManagement()
            self.lobby_automator = LobbyAutomation(self.window_controller)
            self.Stage_manager = StageManager(data, self.lobby_automator, self.window_controller)
            self.states_requiring_data = ["lobby"]
            
            if data[0]['automatically_pick']:
                print(f"Automatically picking brawler: {data[0]['brawler']}")
                self.lobby_automator.select_brawler(data[0]['brawler'])
                
            self.Play.current_brawler = data[0]['brawler']
            self.no_detections_action_threshold = 60 * 8
            self.initialize_stage_manager()
            self.state = None
            
            try:
                self.max_ips = int(load_toml_as_dict("cfg/general_config.toml")['max_ips'])
            except (ValueError, KeyError):
                self.max_ips = None
                
            self.run_for_minutes = int(load_toml_as_dict("cfg/general_config.toml")['run_for_minutes'])
            self.start_time = time.time()
            self.time_to_stop = False
            self.in_cooldown = False
            self.cooldown_start_time = 0
            self.cooldown_duration = 3 * 60

        def initialize_stage_manager(self):
            self.Stage_manager.Trophy_observer.win_streak = data[0]['win_streak']
            self.Stage_manager.Trophy_observer.current_trophies = data[0]['trophies']
            self.Stage_manager.Trophy_observer.current_wins = data[0]['wins'] if data[0]['wins'] != "" else 0

        @staticmethod
        def load_models():
            folder_path = "./models/"
            model_names = ['mainInGameModel.onnx', 'tileDetector.onnx']
            return [os.path.join(folder_path, name) for name in model_names]

        def restart_brawl_stars(self):
            self.window_controller.restart_brawl_stars()
            self.Play.time_since_detections["player"] = time.time()
            self.Play.time_since_detections["enemy"] = time.time()
            if self.window_controller.device.app_current().package != window_controller.BRAWL_STARS_PACKAGE:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    screenshot = self.window_controller.screenshot()
                    loop.run_until_complete(async_notify_user("bot_is_stuck", screenshot))
                finally:
                    loop.close()
                print("Bot got stuck. Shutting down.")
                self.window_controller.keys_up(list("wasd"))
                self.window_controller.close()
                sys.exit(1)

        def manage_time_tasks(self, frame):
            if self.Time_management.state_check():
                state = get_state(frame)
                self.state = state
                if state != "match":
                    self.Play.time_since_last_proceeding = time.time()
                self.Stage_manager.do_state(state, None)

            if self.Time_management.no_detections_check():
                frame_data = self.Play.time_since_detections
                for key, value in frame_data.items():
                    if time.time() - value > self.no_detections_action_threshold:
                        self.restart_brawl_stars()

            if self.Time_management.idle_check():
                self.lobby_automator.check_for_idle(frame)

        def main(self):
            s_time = time.time()
            c = 0
            while True:
                if self.max_ips:
                    frame_start = time.perf_counter()
                
                if self.run_for_minutes > 0 and not self.in_cooldown:
                    elapsed_time = (time.time() - self.start_time) / 60
                    if elapsed_time >= self.run_for_minutes:
                        cprint(f"Session limit reached. Finishing current game...", "#AAE5A4")
                        self.in_cooldown = True
                        self.cooldown_start_time = time.time()
                        self.Stage_manager.states['lobby'] = lambda: 0

                if self.in_cooldown:
                    if time.time() - self.cooldown_start_time >= self.cooldown_duration:
                        cprint("Shutting down bot.", "#AAE5A4")
                        break

                if abs(s_time - time.time()) > 1:
                    elapsed = time.time() - s_time
                    if elapsed > 0:
                        print(f"IPS: {c / elapsed:.2f}")
                    s_time = time.time()
                    c = 0

                frame = self.window_controller.screenshot()
                last_ft = self.window_controller.last_frame_time
                if last_ft > 0 and (time.time() - last_ft) > self.window_controller.FRAME_STALE_TIMEOUT:
                    self.Play.window_controller.keys_up(list("wasd"))
                    print("⌛ Stale frame detected -- waiting for feed...")
                    time.sleep(1)
                    continue

                self.manage_time_tasks(frame)
                brawler = self.Stage_manager.brawlers_pick_data[0]['brawler']
                self.Play.main(frame, brawler)
                c += 1

                if self.max_ips:
                    target_period = 1 / self.max_ips
                    work_time = time.perf_counter() - frame_start
                    if work_time < target_period:
                        time.sleep(target_period - work_time)

    main_instance = Main()
    main_instance.main()

# ---------------------------------------
# STARTUP LOGIC
# ---------------------------------------
all_brawlers = get_brawler_list()
if api_base_url != "localhost":
    update_missing_brawlers_info(all_brawlers)
    check_version()
    update_wall_model_classes()
    if not current_wall_model_is_latest():
        print("Updating Wall Detection model...")
        get_latest_wall_model_file()

app = App(login, SelectBrawler, pyla_main, all_brawlers, Hub)
app.start(pyla_version, get_latest_version)