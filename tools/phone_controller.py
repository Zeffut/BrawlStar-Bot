"""High-level Phone Controller — wraps ADB screencap + input + state detection
into a single API designed for autonomous test / calibration scripts.

Example usage:

    from phone_controller import PhoneController

    p = PhoneController()
    p.tap(2115, 930)                  # press JOUER
    state = p.wait_for_state("match", timeout_s=30)
    if state:
        p.capture_template("match", "joystick", crop=(50, 600, 700, 1050))
    p.run_until_state("end", on_tick=my_strategy_fn, timeout_s=180)

Designed so Claude (or any orchestration code) can drive the phone without
human intervention for calibration + experiments.
"""
from __future__ import annotations

import io
import logging
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

import cv2
import numpy as np
from PIL import Image

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from bsbot.vision.state_finder import StateFinder  # noqa: E402

logger = logging.getLogger("phone_controller")
TEMPLATES_DIR = REPO_ROOT / "src" / "bsbot" / "data" / "state_templates"
DEBUG_DIR = REPO_ROOT / "debug"
DEBUG_DIR.mkdir(exist_ok=True)


class PhoneController:
    """Synchronous, single-device controller.

    Not thread-safe — designed to be driven by ONE orchestrator at a time
    (your tests / Claude session).
    """

    def __init__(self, serial: str | None = None, threshold: float = 0.85):
        self.serial = serial
        self.adb_prefix = ["adb"] if serial is None else ["adb", "-s", serial]
        self.state_finder = StateFinder(TEMPLATES_DIR, threshold=threshold)
        self._verify_device()

    # ------------------------------------------------------------------- ADB

    def _verify_device(self) -> None:
        out = subprocess.check_output(["adb", "devices"], text=True)
        lines = [l.strip() for l in out.splitlines()[1:] if l.strip()]
        devices = [l.split()[0] for l in lines if "device" in l and "unauthorized" not in l]
        if not devices:
            raise RuntimeError("No authorized ADB device. Check `adb devices`.")
        if self.serial is None:
            self.serial = devices[0]
            self.adb_prefix = ["adb", "-s", self.serial]
        elif self.serial not in devices:
            raise RuntimeError(f"Device {self.serial} not connected or unauthorized.")
        logger.info("PhoneController connected to %s", self.serial)

    def _shell(self, cmd: str) -> str:
        return subprocess.check_output([*self.adb_prefix, "shell", cmd], text=True)

    # ----------------------------------------------------------------- input

    def tap(self, x: int, y: int, delay_after_s: float = 0.0) -> None:
        self._shell(f"input tap {int(x)} {int(y)}")
        if delay_after_s:
            time.sleep(delay_after_s)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 200) -> None:
        self._shell(f"input swipe {int(x1)} {int(y1)} {int(x2)} {int(y2)} {int(duration_ms)}")

    def key(self, keycode: str | int) -> None:
        self._shell(f"input keyevent {keycode}")

    def back(self) -> None:
        self.key("KEYCODE_BACK")

    # --------------------------------------------------------------- capture

    def screenshot(self, save_to: str | Path | None = None) -> np.ndarray:
        raw = subprocess.check_output([*self.adb_prefix, "exec-out", "screencap", "-p"])
        img = Image.open(io.BytesIO(raw))
        frame = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
        if save_to is not None:
            cv2.imwrite(str(save_to), frame)
        return frame

    # ----------------------------------------------------------- state logic

    def detect_state(self, frame: np.ndarray | None = None) -> tuple[str, float | None]:
        if frame is None:
            frame = self.screenshot()
        match = self.state_finder.detect(frame)
        if match:
            return match.state, match.score
        return "unknown", None

    def wait_for_state(
        self,
        target_state: str,
        timeout_s: float = 30.0,
        poll_interval_s: float = 0.5,
    ) -> bool:
        """Block until `detect_state()` returns `target_state` or timeout."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            state, score = self.detect_state()
            logger.info("wait_for_state(%s): saw %s (%s)", target_state, state, score)
            if state == target_state:
                return True
            time.sleep(poll_interval_s)
        return False

    def wait_for_state_change(
        self,
        from_state: str,
        timeout_s: float = 60.0,
        poll_interval_s: float = 0.5,
    ) -> str:
        """Block until detected state is no longer `from_state`. Returns the new state."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            state, _ = self.detect_state()
            if state != from_state:
                return state
            time.sleep(poll_interval_s)
        return from_state  # timed out, still same state

    def capture_template(
        self,
        state: str,
        name: str,
        crop: tuple[int, int, int, int] | None = None,
        from_frame: np.ndarray | None = None,
        reload_finder: bool = True,
    ) -> Path:
        """Capture a template image and save into data/state_templates/<state>/<name>.png.

        If `from_frame` is provided, use it; otherwise take a fresh screenshot.
        If `crop=(x1,y1,x2,y2)` is provided, crop accordingly. Reloads
        StateFinder by default so the new template is active immediately.
        """
        frame = from_frame if from_frame is not None else self.screenshot()
        if crop:
            x1, y1, x2, y2 = crop
            frame = frame[y1:y2, x1:x2]
        out_dir = TEMPLATES_DIR / state
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{name}.png"
        cv2.imwrite(str(out_path), frame)
        logger.info("Saved template %s/%s.png  (%dx%d)", state, name, frame.shape[1], frame.shape[0])
        if reload_finder:
            self.state_finder.reload()
        return out_path

    def save_debug(self, label: str = "frame") -> Path:
        """Save a timestamped screenshot to debug/ for later inspection."""
        ts = time.strftime("%Y%m%d-%H%M%S")
        path = DEBUG_DIR / f"{ts}_{label}.png"
        frame = self.screenshot(save_to=path)
        logger.info("Saved debug screenshot to %s (%dx%d)", path, frame.shape[1], frame.shape[0])
        return path

    # ---------------------------------------------------------------- helpers

    def run_loop(
        self,
        on_tick: Callable[["PhoneController", str, np.ndarray], bool],
        max_ticks: int = 1000,
        tick_delay_s: float = 0.5,
    ) -> None:
        """Generic orchestration loop. `on_tick(controller, state, frame)` returns
        True to keep going, False to stop."""
        for i in range(max_ticks):
            frame = self.screenshot()
            state, _ = self.detect_state(frame)
            logger.info("tick %d  state=%s", i, state)
            if not on_tick(self, state, frame):
                break
            time.sleep(tick_delay_s)


# --- CLI smoke test ----------------------------------------------------------

def _cli_main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    p = PhoneController()
    state, score = p.detect_state()
    print(f"Current state: {state}  score={score}")
    debug = p.save_debug(label=state)
    print(f"Screenshot saved to {debug}")
    return 0


if __name__ == "__main__":
    sys.exit(_cli_main())
