import atexit
import math
import threading
import time
import cv2
import os
from PIL import Image
from typing import List

import scrcpy
from adbutils import AdbClient
# Added resource_path to find the bundled jar
from utils import resource_path

# --- Configuration ---
brawl_stars_width, brawl_stars_height = 1920, 1080
BRAWL_STARS_PACKAGE = "com.supercell.brawlstars"

key_coords_dict = {
    "H": (1400, 990), "G": (1640, 990), "M": (1725, 800),
    "Q": (1740, 1000), "E": (1510, 880), "F": (1360, 920),
}

directions_xy_deltas_dict = {
    "w": (0, -150), "a": (-150, 0), "s": (0, 150), "d": (150, 0),
}

class WindowController:
    def __init__(self):
        self.width, self.height = None, None
        self.width_ratio, self.height_ratio = None, None
        self.scale_factor = 1.0
        self.joystick_x, self.joystick_y = None, None
        
        self.last_frame = None
        self.last_frame_time = 0.0
        self.FRAME_STALE_TIMEOUT = 5.0
        self.frame_lock = threading.Lock()

        try:
            self.adb_client = AdbClient(host="127.0.0.1", port=5037)
            try: self.adb_client.server_start()
            except: pass

            device_list = self.adb_client.device_list()
            
            if not device_list:
                for port in [5555, 55555, 5605, 5615]:
                    try: 
                        self.adb_client.connect(f"127.0.0.1:{port}")
                        time.sleep(0.5)
                        device_list = self.adb_client.device_list()
                        if device_list: break
                    except: pass

            if not device_list:
                raise ConnectionError("No ADB devices found. Is your emulator/phone connected and with adb enabled?")

            self.device = device_list[0]
            for dev in device_list:
                if ":" in dev.serial:
                    self.device = dev
                    break

            # --- fix for pyinstaller ---
            jar_path = resource_path("scrcpy/scrcpy-server.jar")
            scrcpy.SCRCPY_SERVER_PATH = jar_path

            self.scrcpy_client = scrcpy.Client(
                device=self.device, 
                max_width=0, 
                bitrate=0
            )
            
            def on_frame(frame):
                if frame is not None:
                    with self.frame_lock:
                        self.last_frame = frame
                        self.last_frame_time = time.time()

            self.scrcpy_client.add_listener(scrcpy.EVENT_FRAME, on_frame)
            self.scrcpy_client.start(threaded=True)

            # --- linux socket fix: Wait for control attribute to initialize
            timeout = time.time() + 5
            while not hasattr(self.scrcpy_client, 'control') or self.scrcpy_client.control is None:
                if time.time() > timeout:
                    print("Warning: Touch control failed to bind. Check 'USB Debugging (Security Settings)'.")
                    break
                time.sleep(0.1)

            atexit.register(self.close)

        except Exception as e:
            raise ConnectionError(f"Hardware init failed: {e}")

        self.are_we_moving = False
        self.PID_JOYSTICK, self.PID_ATTACK = 1, 2

    # restart brawl stars
    def restart_brawl_stars(self):
        """
        Kills the Brawl Stars process and relaunches it via the Android Launcher.
        """
        if self.device:
            print(f"Restarting {BRAWL_STARS_PACKAGE}...")
            # Force stop the app
            self.device.shell(f"am force-stop {BRAWL_STARS_PACKAGE}")
            time.sleep(2)
            # Start the app using the monkey tool (most reliable way via ADB)
            self.device.shell(f"monkey -p {BRAWL_STARS_PACKAGE} -c android.intent.category.LAUNCHER 1")
            time.sleep(5) # Give it time to load before capturing frames
        else:
            print("cannot restart: No ADB device connected, manual restart is required")

    def screenshot(self, array=False):
        deadline = time.time() + 10
        while self.last_frame is None:
            if time.time() > deadline: raise ConnectionError("Frame capture timeout.")
            time.sleep(0.1)

        with self.frame_lock:
            frame = self.last_frame.copy()
        
        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        
        if not self.width or not self.height:
            self.width, self.height = frame.shape[1], frame.shape[0]
            self.width_ratio = self.width / brawl_stars_width
            self.height_ratio = self.height / brawl_stars_height
            self.scale_factor = self.width_ratio 
            self.joystick_x, self.joystick_y = 220 * self.width_ratio, 870 * self.height_ratio

        return frame_rgb if array else Image.fromarray(frame_rgb)

    def touch_down(self, x, y, pointer_id=0):
        if self.scrcpy_client.control:
            self.scrcpy_client.control.touch(int(x), int(y), scrcpy.ACTION_DOWN, pointer_id)

    def touch_move(self, x, y, pointer_id=0):
        if self.scrcpy_client.control:
            self.scrcpy_client.control.touch(int(x), int(y), scrcpy.ACTION_MOVE, pointer_id)

    def touch_up(self, x, y, pointer_id=0):
        if self.scrcpy_client.control:
            self.scrcpy_client.control.touch(int(x), int(y), scrcpy.ACTION_UP, pointer_id)

    # --- new swipe methood ---
    def swipe(self, start_x, start_y, end_x, end_y, duration=0.5, steps=20):
        """
        Simulates a swipe gesture from (start_x, start_y) to (end_x, end_y).
        """
        if not self.scrcpy_client.control:
            return

        self.touch_down(start_x, start_y, pointer_id=self.PID_ATTACK)
        
        for i in range(1, steps + 1):
            curr_x = start_x + (end_x - start_x) * (i / steps)
            curr_y = start_y + (end_y - start_y) * (i / steps)
            self.touch_move(curr_x, curr_y, pointer_id=self.PID_ATTACK)
            time.sleep(duration / steps)
            
        self.touch_up(end_x, end_y, pointer_id=self.PID_ATTACK)

    def keys_down(self, keys: List[str]):
        delta_x, delta_y = 0, 0
        for key in keys:
            if key in directions_xy_deltas_dict:
                dx, dy = directions_xy_deltas_dict[key]
                delta_x, delta_y = delta_x + dx, delta_y + dy

        if not self.are_we_moving:
            self.touch_down(self.joystick_x, self.joystick_y, pointer_id=self.PID_JOYSTICK)
            self.are_we_moving = True

        self.touch_move(self.joystick_x + delta_x, self.joystick_y + delta_y, pointer_id=self.PID_JOYSTICK)

    def keys_up(self, keys: List[str]):
        if "".join(keys).lower() == "wasd":
            self.touch_up(self.joystick_x, self.joystick_y, pointer_id=self.PID_JOYSTICK)
            self.are_we_moving = False

    def click(self, x, y, delay=0.05, already_include_ratio=True):
        if not already_include_ratio:
            x, y = x * self.width_ratio, y * self.height_ratio
        self.touch_down(x, y, pointer_id=self.PID_ATTACK)
        time.sleep(delay)
        self.touch_up(x, y, pointer_id=self.PID_ATTACK)

    def press_key(self, key, delay=0.05):
        if key in key_coords_dict:
            x, y = key_coords_dict[key]
            self.click(x * self.width_ratio, y * self.height_ratio, delay)

    def close(self):
        if hasattr(self, 'scrcpy_client'):
            self.scrcpy_client.stop()