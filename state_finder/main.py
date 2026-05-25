import os
import sys
import cv2
import numpy as np
from difflib import SequenceMatcher
from PIL import Image

# Use the resource_path from utils to find bundled files
from utils import reader, extract_text_and_positions, count_hsv_pixels, load_toml_as_dict, resource_path

orig_screen_width, orig_screen_height = 1920, 1080

# Fix the base path for PyInstaller
# This ensures it looks INSIDE the internal bundle for the images
path = resource_path("state_finder/images_to_detect/")
images_with_star_drop = []

# Ensure the directory exists before listing
if os.path.exists(path):
    for file in os.listdir(path):
        if "star_drop" in file:
            images_with_star_drop.append(file)

region_data = load_toml_as_dict("cfg/lobby_config.toml")['template_matching']
super_debug = load_toml_as_dict("cfg/general_config.toml")['super_debug'] == "yes"

if super_debug:
    debug_folder = resource_path("debug_frames/")
    if not os.path.exists(debug_folder):
        os.makedirs(debug_folder)

def is_template_in_region(image, template_path, region):
    current_height, current_width = image.shape[:2]
    orig_x, orig_y, orig_width, orig_height = region
    width_ratio, height_ratio = current_width / orig_screen_width, current_height / orig_screen_height

    new_x, new_y = int(orig_x * width_ratio), int(orig_y * height_ratio)
    new_width, new_height = int(orig_width * width_ratio), int(orig_height * height_ratio)
    cropped_image = image[new_y:new_y + new_height, new_x:new_x + new_width]
    
    # Pass the fixed path to load_template
    loaded_template = load_template(template_path, current_width, current_height)
    
    if loaded_template is None:
        return False

    result = cv2.matchTemplate(cropped_image, loaded_template, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, _ = cv2.minMaxLoc(result)
    return max_val > 0.7

def load_template(image_path, width, height):
    current_width_ratio, current_height_ratio = width / orig_screen_width, height / orig_screen_height
    
    # Wrap the path for pyinstaller
    # This makes sure it looks in _internal/state_finder/ cuse it has some probelms finding it
    full_path = resource_path(image_path)
    image = cv2.imread(full_path)
    
    if image is None:
        # Note: In production, this warning is mostly suppressed by our logger
        # but kept here for debugging if things go south
        return None
        
    orig_height, orig_width = image.shape[:2]
    resized_image = cv2.resize(image, (int(orig_width * current_width_ratio), int(orig_height * current_height_ratio)))
    return resized_image

# Use resource_path for config as well
crop_region = load_toml_as_dict("cfg/lobby_config.toml")['lobby']['trophy_observer']

def rework_game_result(res_string):
    res_string = res_string.lower()
    if res_string in ["victory", "defeat", "draw"]:
        return res_string, 1.0

    ratios = {
        "victory": SequenceMatcher(None, res_string, 'victory').ratio(),
        "defeat": SequenceMatcher(None, res_string, 'defeat').ratio(),
        "draw": SequenceMatcher(None, res_string, "draw").ratio()
    }
    highest_ratio_string = max(ratios, key=ratios.get)
    return highest_ratio_string, ratios[highest_ratio_string]

def find_game_result(screenshot):
    if not isinstance(screenshot, np.ndarray):
        raise TypeError("Expected a numpy.ndarray, but got {}".format(type(screenshot)))

    x1, y1, x2, y2 = crop_region
    screenshot = screenshot[y1:y2, x1:x2]

    result = reader.readtext(screenshot)
    if len(result) == 0:
        return False

    _, text, _ = result[0]
    game_result, ratio = rework_game_result(text)
    if ratio < 0.55:
        return False
    return True

def get_in_game_state(image):
    if is_in_end_of_a_match(image): return "end"
    if is_in_shop(image): return "shop"
    if is_in_offer_popup(image): return "popup"
    if is_in_lobby(image): return "lobby"
    if is_in_brawler_selection(image):
        return "brawler_selection"

    if count_hsv_pixels(Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB)), (0, 0, 240), (180, 20, 255)) > 300000:
        return "play_store"

    if is_in_brawl_pass(image) or is_in_star_road(image):
        return "shop"

    if is_in_star_drop(image):
        return "star_drop"

    if is_in_trophy_reward(image):
        return "trophy_reward"

    return "match"

def is_in_shop(image) -> bool:
    return is_template_in_region(image, os.path.join('state_finder', 'images_to_detect', 'powerpoint.png'), region_data["powerpoint"])

def is_in_brawler_selection(image) -> bool:
    return is_template_in_region(image, os.path.join('state_finder', 'images_to_detect', 'brawler_menu_task.png'), region_data["brawler_menu_task"])

def is_in_offer_popup(image) -> bool:
    return is_template_in_region(image, os.path.join('state_finder', 'images_to_detect', 'close_popup.png'), region_data["close_popup"])

def is_in_lobby(image) -> bool:
    return is_template_in_region(image, os.path.join('state_finder', 'images_to_detect', 'lobby_menu.png'), region_data["lobby_menu"])

def is_in_end_of_a_match(image):
    return find_game_result(image)

def is_in_trophy_reward(image):
    image = np.array(image)
    starting_x = int(image.shape[1] * 0.75)
    starting_y = int(image.shape[0] * 0.75)
    image = image[starting_y:, starting_x:]
    all_text = (" ".join(extract_text_and_positions(image).keys())).lower().replace("'", "")
    return "go" in all_text

def is_in_brawl_pass(image):
    # Note: PNG must match actual file extension on disk
    return is_template_in_region(image, os.path.join('state_finder', 'images_to_detect', 'brawl_pass_house.PNG'),
                                  region_data['brawl_pass_house'])

def is_in_star_road(image):
    return is_template_in_region(image, os.path.join('state_finder', 'images_to_detect', 'go_back_arrow.png'), region_data['go_back_arrow'])

def is_in_star_drop(image):
    for image_filename in images_with_star_drop:
        if is_template_in_region(image, os.path.join('state_finder', 'images_to_detect', image_filename), region_data['star_drop']):
            return True
    return False

def get_state(screenshot):
    if super_debug:
        screenshot_path = resource_path(f"debug_frames/state_screenshot_{len(os.listdir(resource_path('debug_frames')))}.png")
        screenshot.save(screenshot_path)
    screenshot_arr = np.array(screenshot)
    screenshot_bgr = cv2.cvtColor(screenshot_arr, cv2.COLOR_RGB2BGR)
    state = get_in_game_state(screenshot_bgr)
    # Status print kept for user info
    print(f"State: {state}")
    return state