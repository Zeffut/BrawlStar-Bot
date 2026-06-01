import sys
import asyncio
import logging
import time
import cv2
import numpy as np
import requests

log = logging.getLogger(__name__)

from state_finder.main import get_state
from trophy_observer import TrophyObserver
# Added resource_path to imports
import device
from utils import find_template_center, extract_text_and_positions, load_toml_as_dict, async_notify_user, \
    save_brawler_data, resource_path

user_id = load_toml_as_dict("cfg/general_config.toml")['discord_id']
user_webhook = load_toml_as_dict("cfg/general_config.toml")['personal_webhook']

def notify_user(message_type):
    message_data = {
        'content': f"<@{user_id}> Bot has completed all its targets!"
    }
    response = requests.post(user_webhook, json=message_data)
    if response.status_code != 204:
        print(f'Failed to send message. Status code: {response.status_code}')

def load_image(image_path, scale_factor):
    # Fix: Wrap image_path with resource_path for PyInstaller
    image = cv2.imread(resource_path(image_path))
    if image is None:
        print(f"Could not load image: {image_path}")
        return None
    orig_height, orig_width = image.shape[:2]
    new_width = int(orig_width * scale_factor)
    new_height = int(orig_height * scale_factor)
    resized_image = cv2.resize(image, (new_width, new_height))
    return resized_image

class StageManager:
    def __init__(self, brawlers_data, lobby_automator, window_controller):
        self.states = {
            'shop': self.quit_shop,
            'brawler_selection': self.quit_shop,
            'popup': self.close_pop_up,
            'match': lambda: 0,
            'end': self.end_game,
            'lobby': self.start_game,
            'play_store': self.click_brawl_stars,
            'star_drop': self.click_star_drop,
            'trophy_reward': self.click_trophy_reward,
        }
        self.Lobby_automation = lobby_automator
        self.lobby_config = load_toml_as_dict("cfg/lobby_config.toml")
        self.brawl_stars_icon = None
        self.close_popup_icon = None
        self.brawlers_pick_data = brawlers_data
        brawler_list = [brawler["brawler"] for brawler in brawlers_data]
        self.Trophy_observer = TrophyObserver(brawler_list)
        self.time_since_last_stat_change = time.time()
        self.long_press_star_drop = load_toml_as_dict("cfg/general_config.toml")["long_press_star_drop"]
        self.window_controller = window_controller

    def start_brawl_stars(self, frame):
        if frame is None: return
        data = extract_text_and_positions(np.array(frame))
        for key in list(data.keys()):
            if key.replace(" ", "") in ["brawl", "brawlstars", "stars"]:
                x, y = data[key]['center']
                self.window_controller.click(x, y)
                return
        brawl_stars_icon_coords = self.lobby_config['lobby'].get('brawl_stars_icon', [960, 540])
        x, y = brawl_stars_icon_coords[0]*self.window_controller.width_ratio, brawl_stars_icon_coords[1]*self.window_controller.height_ratio
        self.window_controller.click(x, y)

    @staticmethod
    def validate_trophies(trophies_string):
        trophies_string = trophies_string.lower()
        while "s" in trophies_string:
            trophies_string = trophies_string.replace("s", "5")
        numbers = ''.join(filter(str.isdigit, trophies_string))
        if not numbers: return False
        return int(numbers)

    def start_game(self, data):
        # push_max: a previous match's hook may have scheduled a brawler
        # swap. Execute it now (we're back on the lobby screen).
        pending = getattr(self, "_pending_swap", None)
        if pending:
            log.info("start_game: executing pending brawler swap → %s", pending)
            self._pending_swap = None
            try:
                try:
                    import game_api
                    game_api.set_activity(f"Swapping to {pending.strip().title()}")
                except Exception:
                    pass
                self.Lobby_automation.select_brawler(pending)
                # Update brawlers_pick_data so subsequent logic uses the new brawler.
                self.brawlers_pick_data[0]['brawler'] = pending
            except Exception:
                log.exception("brawler swap to %s failed", pending)
                # The menu OCR can't find some brawlers (name fused with the
                # trophy badge, e.g. nita→"n1o8"). Record it so push_max marks
                # it exhausted and moves to a brawler it CAN select, instead
                # of looping forever on the same unselectable target.
                if not hasattr(self, "_unselectable_brawlers"):
                    self._unselectable_brawlers = set()
                self._unselectable_brawlers.add(pending)
        print("state is lobby, starting game")
        values = {
            "trophies": self.Trophy_observer.current_trophies,
            "wins": self.Trophy_observer.current_wins
        }
        type_of_push = self.brawlers_pick_data[0]['type']
        if type_of_push not in values: type_of_push = "trophies"
        value = values[type_of_push]
        if value == "" and type_of_push == "wins": value = 0
        push_current_brawler_till = self.brawlers_pick_data[0]['push_until']
        
        if value >= push_current_brawler_till:
            if len(self.brawlers_pick_data) <= 1:
                print("Bot targets completed.")
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    screenshot = self.window_controller.screenshot()
                    loop.run_until_complete(async_notify_user("bot_is_stuck", screenshot))
                finally:
                    loop.close()
                self.window_controller.keys_up(list("wasd"))
                self.window_controller.close()
                sys.exit(0)
            
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                screenshot = self.window_controller.screenshot()
                loop.run_until_complete(async_notify_user(self.brawlers_pick_data[0]["brawler"], screenshot))
            finally:
                loop.close()
            self.brawlers_pick_data.pop(0)
            self.Trophy_observer.change_trophies(self.brawlers_pick_data[0]['trophies'])
            self.Trophy_observer.current_wins = self.brawlers_pick_data[0]['wins'] if self.brawlers_pick_data[0]['wins'] != "" else 0
            self.Trophy_observer.win_streak = self.brawlers_pick_data[0]['win_streak']
            next_brawler_name = self.brawlers_pick_data[0]['brawler']
            if self.brawlers_pick_data[0]["automatically_pick"]:
                try:
                    import game_api
                    game_api.set_activity(f"Selecting {next_brawler_name.strip().title()}")
                except Exception:
                    pass
                self.Lobby_automation.select_brawler(next_brawler_name)

        self.window_controller.keys_up(list("wasd"))
        self.window_controller.press_key("Q")

    def click_brawl_stars(self, frame):
        # Fix: if it isnt reciving a frame it dosent just crash the bot
        if frame is None:
            print("Scrcpy frame not received yet...")
            return

        screenshot = frame.crop((50, 4, 900, 31))
        if self.brawl_stars_icon is None:
            self.brawl_stars_icon = load_image("state_finder/images_to_detect/brawl_stars_icon.png",
                                               self.window_controller.scale_factor)
        
        detection = find_template_center(screenshot, self.brawl_stars_icon)
        if detection:
            x, y = detection
            self.window_controller.click(x=x + 50, y=y)

    def click_trophy_reward(self):
        """Tap CONTINUE button at bottom-center of screen (covers both the
        old 'GO' trophy reward popup AND the new POWER/COINS/etc. result
        screens that show a single CONTINUE button)."""
        log.info("state=trophy_reward → tapping CONTINUE")
        import subprocess
        sw = self.window_controller.width or 1920
        sh = self.window_controller.height or 1080
        # CONTINUE button is around 90-95% of screen height, centered.
        cx, cy = sw // 2, int(sh * 0.92)
        serial = getattr(self.window_controller, "device_serial", None) or device.adb_serial()
        try:
            subprocess.run(
                ["adb", "-s", serial, "shell", "input", "tap", str(cx), str(cy)],
                timeout=3, check=False,
            )
        except Exception:
            self.window_controller.click(cx, cy, already_include_ratio=True)

    def _restart_brawlstars(self, reason: str):
        """Force-stop + relaunch Brawl Stars to bail out of a screen the bot
        can't dismiss (e.g. an unopenable star drop). The grind resumes at
        the lobby; any pending reward stays claimable manually."""
        import subprocess
        serial = getattr(self.window_controller, "device_serial", None) or device.adb_serial()
        log.warning("restarting Brawl Stars (%s)", reason)
        try:
            subprocess.run(["adb", "-s", serial, "shell", "am", "force-stop",
                            "com.supercell.brawlstars"], timeout=5, check=False)
            time.sleep(2)
            subprocess.run(["adb", "-s", serial, "shell", "am", "start", "-n",
                            "com.supercell.brawlstars/.GameApp"], timeout=10, check=False)
            time.sleep(8)
        except Exception as exc:
            log.warning("Brawl Stars restart failed: %s", exc)

    def click_star_drop(self):
        # "TAP AND HOLD" / "TOUCHEZ ET MAINTENEZ". A perfectly static
        # synthesized hold (input swipe X Y X Y) is ignored by BS on a phone;
        # try a slow DRAG instead (a held touch *with* slight motion, like a
        # real finger jitter). Use real DISPLAY coords (the old code used the
        # downscaled capture size, tapping the wrong spot). If it still won't
        # open after a couple tries, bail out by restarting BS so the grind
        # continues (star drop stays pending, claimable manually).
        import subprocess
        try:
            dw, dh = device.device_size()           # (2340,1080) landscape
        except Exception:
            dw, dh = 2340, 1080
        cx, cy = dw // 2, dh // 2
        serial = getattr(self.window_controller, "device_serial", None) or device.adb_serial()
        self._star_drop_attempts = getattr(self, "_star_drop_attempts", 0) + 1
        if self._star_drop_attempts >= 3:
            self._star_drop_attempts = 0
            self._restart_brawlstars("star drop won't open (synthetic hold filtered)")
            return
        log.info("state=star_drop → drag-hold center %dx%d (try %d)",
                 cx, cy, self._star_drop_attempts)
        try:
            # Slow 3.5s drag over a small distance = a continuous held touch
            # that moves slightly. One swipe call.
            subprocess.run(
                ["adb", "-s", serial, "shell", "input", "swipe",
                 str(cx), str(cy), str(cx + 18), str(cy + 18), "3500"],
                timeout=8, check=False,
            )
        except Exception as exc:
            log.warning("click_star_drop drag-hold failed: %s", exc)

    def end_game(self):
        screenshot = self.window_controller.screenshot()
        if screenshot is None: return
        
        found_game_result = False
        current_state = get_state(screenshot)
        max_end_attempts = 7
        end_attempts = 0
        while current_state == "end" and end_attempts < max_end_attempts:
            if not found_game_result and time.time() - self.time_since_last_stat_change > 10:
                found_game_result = self.Trophy_observer.find_game_result(screenshot, current_brawler=self.brawlers_pick_data[0]['brawler'])
                self.time_since_last_stat_change = time.time()
                save_brawler_data(self.brawlers_pick_data)
            # Once the result is logged and we're still on the end screen,
            # it's a star drop we can't open — stop hammering Q and bail.
            if found_game_result and end_attempts >= 2:
                break
            self.window_controller.press_key("Q")
            time.sleep(2)
            screenshot = self.window_controller.screenshot()
            current_state = get_state(screenshot)
            end_attempts += 1
        # Still stuck on the post-match screen — almost always an unopenable
        # star drop ("TOUCHEZ ET MAINTENEZ") that Unity won't accept a
        # synthesized hold for on a physical phone. Bail out by restarting
        # Brawl Stars so the grind resumes at the lobby.
        if current_state in ("end", "star_drop"):
            self._restart_brawlstars(f"stuck on {current_state} after {end_attempts} tries")

    def quit_shop(self):
        self.window_controller.click(100*self.window_controller.width_ratio, 60*self.window_controller.height_ratio)

    def close_pop_up(self):
        screenshot = self.window_controller.screenshot()
        if screenshot is None: return
        if self.close_popup_icon is None:
            self.close_popup_icon = load_image("state_finder/images_to_detect/close_popup.png", self.window_controller.scale_factor)
        popup_location = find_template_center(screenshot, self.close_popup_icon)
        if popup_location:
            self.window_controller.click(*popup_location)

    def do_state(self, state, data=None):
        if state in self.states:
            log.debug("do_state: %s", state)
            if state != "star_drop":
                self._star_drop_attempts = 0
            try:
                self.states[state](data)
            except TypeError:
                self.states[state]()
        else:
            log.warning("do_state: unknown state %r (skipped)", state)