import atexit
import threading
import time
import cv2
from PIL import Image
from typing import List

import numpy as np
import scrcpy
from adbutils import AdbClient
# Added resource_path to find the bundled jar
from utils import resource_path


def np_from_pil(pil_img):
    return np.array(pil_img)

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
            except Exception: pass

            device_list = self.adb_client.device_list()
            
            if not device_list:
                for port in [5555, 55555, 5605, 5615]:
                    try: 
                        self.adb_client.connect(f"127.0.0.1:{port}")
                        time.sleep(0.5)
                        device_list = self.adb_client.device_list()
                        if device_list: break
                    except Exception: pass

            if not device_list:
                raise ConnectionError("No ADB devices found. Is your emulator/phone connected and with adb enabled?")

            self.device = device_list[0]
            for dev in device_list:
                if ":" in dev.serial:
                    self.device = dev
                    break

            # scrcpy init is optional — emulators like BlueStacks don't
            # implement enough of the ADB sync protocol for `scrcpy
            # server.jar` to land. When that happens we degrade
            # gracefully: capture goes through ScreenRecorder (adb
            # exec-out screenrecord) and input goes through `adb shell
            # input tap/swipe`. None of the bot's required features
            # depend on the scrcpy touch socket — only legacy
            # joystick/attack helpers do.
            self.scrcpy_client = None
            try:
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

                # Wait briefly for the control socket to initialize.
                timeout = time.time() + 5
                while not hasattr(self.scrcpy_client, 'control') or self.scrcpy_client.control is None:
                    if time.time() > timeout:
                        print("Warning: Touch control failed to bind. Check 'USB Debugging (Security Settings)'.")
                        break
                    time.sleep(0.1)
            except Exception as scrcpy_exc:
                print(f"Warning: scrcpy init failed ({scrcpy_exc}); falling back to ADB-only mode.")
                self.scrcpy_client = None

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
        # Capture priority (each step is much faster than the next):
        #   1. ScreenRecorder H264 pipe (~30 fps, hot frame in memory)
        #   2. wc.last_frame if <2s old (legacy scrcpy or recent capture)
        #   3. adb screencap -p (PNG, ~600ms, last resort)
        frame = None
        # 1. Continuous H264 pipe (lazy-init on first call).
        try:
            import screen_capture as _sc
            rec = _sc.get()
            if rec is not None:
                f = rec.get_frame()
                age = rec.get_frame_age()
                if f is not None and age is not None and age < 1.0:
                    frame = f
                    with self.frame_lock:
                        self.last_frame = f
                        self.last_frame_time = rec.last_frame_time
        except Exception:
            pass
        # 2. Cached frame from previous capture (any source).
        if frame is None:
            with self.frame_lock:
                if self.last_frame is not None and (time.time() - self.last_frame_time) < 2.0:
                    frame = self.last_frame.copy()
        # 3. adb screencap -p fallback.
        if frame is None:
            import subprocess, io as _io
            import device as _device
            try:
                out = subprocess.run(
                    ["adb", "-s", _device.adb_serial(), "exec-out", "screencap", "-p"],
                    capture_output=True, timeout=8, check=True,
                )
                pil = Image.open(_io.BytesIO(out.stdout)).convert("RGB")
                frame_rgb = cv2.cvtColor(np_from_pil(pil), cv2.COLOR_RGB2BGR)
                with self.frame_lock:
                    self.last_frame = frame_rgb
                    self.last_frame_time = time.time()
                frame = frame_rgb
            except Exception as exc:
                raise ConnectionError(f"adb screencap failed: {exc}")

        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

        # Always sync width/height with the CURRENT frame size — sources
        # may swap mid-session (screencap=2340x1080 → screenrec=1280x576).
        # If we kept the first-frame dims, click() would rescale wrong.
        fw, fh = frame.shape[1], frame.shape[0]
        if self.width != fw or self.height != fh:
            self.width, self.height = fw, fh
            self.width_ratio = self.width / brawl_stars_width
            self.height_ratio = self.height / brawl_stars_height
            self.scale_factor = self.width_ratio
            self.joystick_x = 220 * self.width_ratio
            self.joystick_y = 870 * self.height_ratio

        return frame_rgb if array else Image.fromarray(frame_rgb)

    def _scrcpy_ok(self) -> bool:
        return (self.scrcpy_client is not None
                and getattr(self.scrcpy_client, "control", None) is not None)

    def touch_down(self, x, y, pointer_id=0):
        if self._scrcpy_ok():
            self.scrcpy_client.control.touch(int(x), int(y), scrcpy.ACTION_DOWN, pointer_id)

    def touch_move(self, x, y, pointer_id=0):
        if self._scrcpy_ok():
            self.scrcpy_client.control.touch(int(x), int(y), scrcpy.ACTION_MOVE, pointer_id)

    def touch_up(self, x, y, pointer_id=0):
        if self._scrcpy_ok():
            self.scrcpy_client.control.touch(int(x), int(y), scrcpy.ACTION_UP, pointer_id)

    # --- new swipe methood ---
    def swipe(self, start_x, start_y, end_x, end_y, duration=0.5, steps=20):
        """
        Simulates a swipe gesture from (start_x, start_y) to (end_x, end_y).
        Falls back to `adb shell input swipe` when scrcpy is not bound.
        """
        if not self._scrcpy_ok():
            # ADB fallback: native swipe gesture on the device.
            try:
                import subprocess
                import device as _device
                serial = _device.adb_serial()
                ms = max(int(duration * 1000), 1)
                subprocess.run(
                    ["adb", "-s", serial, "shell", "input", "swipe",
                     str(int(start_x)), str(int(start_y)),
                     str(int(end_x)), str(int(end_y)), str(ms)],
                    timeout=max(duration + 3, 5), check=False,
                )
            except Exception:
                pass
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
        # IMPORTANT: x/y here are in FRAME coordinates (recorded resolution).
        # ADB input takes DEVICE coordinates. Rescale.
        import subprocess
        import device as _device
        serial = _device.adb_serial()
        try:
            dw, dh = _device.device_size()
            fw, fh = self.width or dw, self.height or dh
            if fw and fh and (fw != dw or fh != dh):
                x = x * dw / fw
                y = y * dh / fh
        except Exception:
            pass
        try:
            if delay > 0.1:
                ms = int(delay * 1000)
                subprocess.run(
                    ["adb", "-s", serial, "shell", "input", "swipe",
                     str(int(x)), str(int(y)), str(int(x)), str(int(y)), str(ms)],
                    timeout=delay + 3, check=False,
                )
            else:
                subprocess.run(
                    ["adb", "-s", serial, "shell", "input", "tap",
                     str(int(x)), str(int(y))],
                    timeout=3, check=False,
                )
            return
        except Exception:
            pass
        # Fallback to scrcpy if adb fails (e.g. device unplugged).
        self.touch_down(x, y, pointer_id=self.PID_ATTACK)
        time.sleep(delay)
        self.touch_up(x, y, pointer_id=self.PID_ATTACK)

    def press_key(self, key, delay=0.05):
        if key in key_coords_dict:
            x, y = key_coords_dict[key]
            self.click(x * self.width_ratio, y * self.height_ratio, delay)

    def close(self):
        if getattr(self, "scrcpy_client", None) is not None:
            try: self.scrcpy_client.stop()
            except Exception: pass