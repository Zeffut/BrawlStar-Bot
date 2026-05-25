import os
import sys
import hashlib
import io
from io import BytesIO
import ctypes
import json
import aiohttp
import google_play_scraper
import requests
import toml
from PIL import Image
from discord import Webhook
import discord
import cv2
import numpy as np
from packaging import version
import time
import easyocr
from contextlib import contextmanager # Added for silencer

# make the console stop all the warnings that are not important
@contextmanager
def suppress_stdout_stderr():
    """Forcefully mutes the console for the duration of the 'with' block."""
    with open(os.devnull, "w") as devnull:
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        sys.stdout = devnull
        sys.stderr = devnull
        try:
            yield
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr

def resource_path(relative_path):
    """ Get absolute path to resource, works for dev and for PyInstaller """
    try:
        # PyInstaller creates a temp folder and stores path in _MEIPASS
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")

    return os.path.join(base_path, relative_path)

# Function to safely get path for JSON/TOML files
def get_json_path(relative_path):
    if getattr(sys, 'frozen', False):
        exe_path = os.path.join(os.path.dirname(sys.executable), relative_path)
        if os.path.exists(exe_path):
            return exe_path
    return resource_path(relative_path)
# ---------------------------------------------------------

def extract_text_and_positions(image_path):
    results = reader.readtext(image_path)
    text_details = {}
    for (bbox, text, prob) in results:
        top_left, top_right, bottom_right, bottom_left = bbox
        cx = (top_left[0] + top_right[0] + bottom_right[0] + bottom_left[0]) / 4
        cy = (top_left[1] + top_right[1] + bottom_right[1] + bottom_left[1]) / 4
        center = (cx, cy)
        formatted_bbox = {
            'top_left': top_left,
            'top_right': top_right,
            'bottom_right': bottom_right,
            'bottom_left': bottom_left,
            'center': center
        }

        text_details[text.lower()] = formatted_bbox

    return text_details

class DefaultEasyOCR:
    def __init__(self):
        # OCR initialized with GPU disabled for compatibility across providers
        self.reader = easyocr.Reader(['en'], gpu=False)

    def readtext(self, image_input):
        return self.reader.readtext(image_input)

def load_toml_as_dict(file_path):
    path = get_json_path(file_path)
    if os.path.exists(path):
        try:
            with open(path, 'r') as f:
                return toml.load(f)
        except:
            return {}
    return {}

reader = DefaultEasyOCR()
api_base_url = "localhost"

brawlers_info_file_path = get_json_path("cfg/brawlers_info.json")

def count_hsv_pixels(pil_image, low_hsv, high_hsv):
    opencv_image = cv2.cvtColor(np.array(pil_image), cv2.COLOR_RGB2BGR)
    hsv_image = cv2.cvtColor(opencv_image, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv_image, np.array(low_hsv), np.array(high_hsv))
    pixel_count = np.count_nonzero(mask)
    return pixel_count

def save_brawler_data(data):
    save_dir = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else "."
    save_path = os.path.join(save_dir, "latest_brawler_data.json")
    with open(save_path, 'w') as f:
        json.dump(data, f, indent=4)

def find_template_center(main_img, template, threshold=0.8):
    main_image_cv = cv2.cvtColor(np.array(main_img), cv2.COLOR_RGB2GRAY)
    template_arr = np.array(template)
    if len(template_arr.shape) == 3 and template_arr.shape[2] == 3:
        template_cv = cv2.cvtColor(template_arr, cv2.COLOR_BGR2GRAY)
    else:
        template_cv = template_arr
    w, h = template_cv.shape[::-1]

    result = cv2.matchTemplate(main_image_cv, template_cv, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(result)

    if max_val >= threshold:
        center_x = max_loc[0] + w // 2
        center_y = max_loc[1] + h // 2
        return center_x, center_y
    else:
        return False

def save_dict_as_toml(data, file_path):
    with open(get_json_path(file_path), 'w') as f:
        toml.dump(data, f)

def update_toml_file(path, new_data):
    with open(get_json_path(path), 'w') as file:
        toml.dump(new_data, file)

def load_brawlers_info():
    path = get_json_path("cfg/brawlers_info.json")
    if os.path.exists(path):
        with open(path, 'r') as f:
            return json.load(f)
    return {}

def update_brawlers_info(brawlers_info):
    path = get_json_path("cfg/brawlers_info.json")
    with open(path, 'w') as f:
        json.dump(brawlers_info, f, indent=4)

def get_brawler_list():
    if api_base_url == "localhost":
        return list(load_brawlers_info().keys())
    url = f'https://{api_base_url}/get_brawler_list'
    response = requests.post(url)
    if response.status_code == 201:
        return response.json().get('brawlers', [])
    return []

def update_missing_brawlers_info(brawlers):
    brawlers_info = load_brawlers_info()
    for brawler in brawlers:
        if brawler not in brawlers_info:
            brawler_info = get_brawler_info(brawler)
            if brawler_info:
                brawlers_info[brawler] = brawler_info
                update_brawlers_info(brawlers_info)
        
        icon_path = get_json_path(f"api/assets/brawler_icons/{brawler}.png")
        if not os.path.exists(icon_path):
            save_brawler_icon(brawler)

def get_brawler_info(brawler_name):
    url = f'https://{api_base_url}/get_brawler_info'
    response = requests.post(url, json={'brawler_name': brawler_name})
    if response.status_code == 200:
        return response.json().get('info', [])
    return None

def save_brawler_icon(brawler_name):
    brawler_name_clean = brawler_name.lower().replace(' ', '').replace('-', '').replace('.', '').replace('&', '')
    brawlers_url = "https://api.brawlapi.com/v1/brawlers"
    response = requests.get(brawlers_url)
    if response.status_code != 200: return
    
    brawlers_data = response.json()['list']
    for brawler_obj in brawlers_data:
        api_name = brawler_obj['name'].lower().replace(' ', '').replace('-', '').replace('.', '').replace('&', '')
        if api_name == brawler_name_clean:
            icon_url = brawler_obj['imageUrl2']
            img_response = requests.get(icon_url)
            if img_response.status_code == 200:
                image = Image.open(BytesIO(img_response.content))
                save_path = get_json_path(f"api/assets/brawler_icons/{brawler_name_clean}.png")
                os.makedirs(os.path.dirname(save_path), exist_ok=True)
                image.save(save_path)
            return

def get_discord_link():
    if api_base_url == "localhost":
        return "https://discord.gg/xUusk3fw4A"
    try:
        url = f'https://{api_base_url}/get_discord_link'
        response = requests.get(url)
        if response.status_code == 200:
            return response.json().get('link', 'https://discord.gg/xUusk3fw4A')
    except:
        pass
    return "https://discord.gg/xUusk3fw4A"

def get_latest_version():
    url = f'https://{api_base_url}/check_version'
    try:
        response = requests.get(url)
        return response.json().get('version', '') if response.status_code == 200 else None
    except: return None

def check_version():
    if api_base_url != "localhost":
        latest = get_latest_version()
        if latest:
            current = load_toml_as_dict("cfg/general_config.toml").get('pyla_version', '')
            if version.parse(current) < version.parse(latest):
                print(f"Update available.")

async def async_notify_user(message_type: str | None = None, screenshot: Image = None) -> None:
    config = load_toml_as_dict("cfg/general_config.toml")
    webhook_url = config.get("personal_webhook")
    if not webhook_url: return

    buffer = io.BytesIO()
    screenshot.save(buffer, format="PNG")
    buffer.seek(0)
    
    async with aiohttp.ClientSession() as session:
        webhook = Webhook.from_url(webhook_url, session=session)
        await webhook.send(file=discord.File(buffer, filename="screenshot.png"))

# --- WALL MODEL FUNCTIONS (Required by main.py) ---
def get_online_wall_model_hash():
    try:
        url = f'https://{api_base_url}/get_wall_model_hash'
        response = requests.get(url)
        return response.json().get('hash', '') if response.status_code == 200 else ""
    except: return ""

def get_latest_wall_model_file():
    try:
        url = f'https://{api_base_url}/get_wall_model_file'
        response = requests.get(url)
        if response.status_code == 200:
            save_path = get_json_path("models/tileDetector.onnx")
            os.makedirs(os.path.dirname(save_path), exist_ok=True)
            with open(save_path, "wb") as file:
                file.write(response.content)
    except:
        pass

def get_latest_wall_model_classes():
    try:
        url = f'https://{api_base_url}/get_wall_model_classes'
        response = requests.get(url)
        return response.json().get('classes', []) if response.status_code == 200 else []
    except: return []

def update_wall_model_classes():
    classes = get_latest_wall_model_classes()
    full_config = load_toml_as_dict("cfg/bot_config.toml")
    if classes and full_config:
        full_config["wall_model_classes"] = classes
        update_toml_file("cfg/bot_config.toml", full_config)

def calculate_sha256(file_path):
    sha256_hash = hashlib.sha256()
    path = get_json_path(file_path)
    if not os.path.exists(path): return ""
    with open(path, "rb") as file:
        for chunk in iter(lambda: file.read(4096), b""):
            sha256_hash.update(chunk)
    return sha256_hash.hexdigest()

def current_wall_model_is_latest() -> bool:
    return calculate_sha256("models/tileDetector.onnx") == get_online_wall_model_hash()

def cprint(text: str, hex_color: str):
    try:
        hex_color = hex_color.lstrip("#")
        r, g, b = tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))
        print(f"\033[38;2;{r};{g};{b}m{text}\033[0m")
    except: print(text)

def get_dpi_scale():
    return 96