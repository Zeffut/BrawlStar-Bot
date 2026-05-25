import os.path
import sys
import asyncio
import time
import cv2
import numpy as np
import requests

from state_finder.main import get_state
from trophy_observer import TrophyObserver
# Added resource_path to imports
from utils import find_template_center, extract_text_and_positions, load_toml_as_dict, async_notify_user, \
    save_brawler_data, resource_path

user_id = load_toml_as_dict("cfg/general_config.toml")['discord_id']
user_webhook = load_toml_as_dict("cfg/general_config.toml")['personal_webhook']

def notify_user(message_type):
    message_data = {
        'content': f"<@{user_id}> Pyla Bot has completed all it's targets !"
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
            'trophy_reward': lambda: self.window_controller.press_key("Q")
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

    def click_star_drop(self):
        if self.long_press_star_drop == "yes":
            self.window_controller.press_key("Q", 10)
        else:
            self.window_controller.press_key("Q")

    def end_game(self):
        screenshot = self.window_controller.screenshot()
        if screenshot is None: return
        
        found_game_result = False
        current_state = get_state(screenshot)
        max_end_attempts = 30
        end_attempts = 0
        while current_state == "end" and end_attempts < max_end_attempts:
            if not found_game_result and time.time() - self.time_since_last_stat_change > 10:
                found_game_result = self.Trophy_observer.find_game_result(screenshot, current_brawler=self.brawlers_pick_data[0]['brawler'])
                self.time_since_last_stat_change = time.time()
                save_brawler_data(self.brawlers_pick_data)

            self.window_controller.press_key("Q")
            time.sleep(3)
            screenshot = self.window_controller.screenshot()
            current_state = get_state(screenshot)
            end_attempts += 1

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
            try:
                self.states[state](data)
            except TypeError:
                self.states[state]()