import difflib
import logging
import subprocess
import time

import numpy as np

from stage_manager import load_image
from typization import BrawlerName
import device
from utils import extract_text_and_positions, count_hsv_pixels, load_toml_as_dict, find_template_center

debug = load_toml_as_dict("cfg/general_config.toml")['super_debug'] == "yes"
log = logging.getLogger(__name__)

class LobbyAutomation:

    def __init__(self, window_controller):
        self.coords_cfg = load_toml_as_dict("./cfg/lobby_config.toml")
        self.window_controller = window_controller

    def check_for_idle(self, frame):
        screenshot = frame
        wr = self.window_controller.width_ratio
        hr = self.window_controller.height_ratio
        screenshot = screenshot.crop(
            (int(400 * wr), int(380 * hr), int(1500 * wr), int(700 * hr)))
        gray_pixels = count_hsv_pixels(screenshot, (0, 0, 55), (10, 15, 77))
        if debug: print("gray pixels (if > 1000 then bot will try to unidle) :", gray_pixels)
        if gray_pixels > 1000:
            self.window_controller.click(int(535 * wr), int(615 * hr))

    def select_brawler(self, brawler, max_attempts: int = 3):
        """Select a brawler by name. Resilient: if the first attempt fails
        (menu didn't open, scroll missed, OCR was off), close the menu
        with BACK and retry up to `max_attempts` times.
        """
        log.info("select_brawler: target=%r (max_attempts=%d)", brawler, max_attempts)
        last_exc: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            log.info("select_brawler attempt %d/%d", attempt, max_attempts)
            try:
                self._select_brawler_once(brawler)
                log.info("select_brawler: SUCCESS on attempt %d", attempt)
                return
            except Exception as exc:
                log.warning("select_brawler attempt %d failed: %s", attempt, exc)
                last_exc = exc
                # Reset: BACK key to close any open menu, give the game
                # 2s to settle before next attempt.
                self._reset_to_lobby()
        # All attempts exhausted.
        log.error("select_brawler: ALL %d attempts failed; last error: %s",
                  max_attempts, last_exc)
        raise last_exc or ValueError(
            f"Brawler '{brawler}' could not be selected after {max_attempts} attempts."
        )

    @staticmethod
    def _fuzzy_match(target: str, candidates, min_ratio: float = 0.7) -> str | None:
        """Return the candidate whose name is the closest fuzzy match to
        `target` (Ratcliff/Obershelp ratio ≥ min_ratio), or None.

        Also accepts an exact substring match — useful when OCR adds
        leading/trailing chars (e.g. `?colt`).
        """
        target = target.lower().strip()
        best: tuple[float, str] | None = None
        for c in candidates:
            c_norm = c.lower().strip()
            if not c_norm:
                continue
            # Skip purely-numeric strings (trophy counts).
            if c_norm.replace(".", "").isdigit():
                continue
            # Substring match wins immediately.
            if target in c_norm or c_norm in target:
                return c
            ratio = difflib.SequenceMatcher(None, target, c_norm).ratio()
            if ratio >= min_ratio and (best is None or ratio > best[0]):
                best = (ratio, c)
        return best[1] if best else None

    def _reset_to_lobby(self) -> None:
        """Press BACK a couple of times to close any open menus."""
        try:
            serial = getattr(self.window_controller, "device_serial", None) or device.adb_serial()
            for _ in range(2):
                subprocess.run(
                    ["adb", "-s", serial, "shell", "input", "keyevent", "4"],
                    timeout=3, check=False,
                )
                time.sleep(0.6)
            time.sleep(1.0)
            log.debug("reset_to_lobby: BACK pressed")
        except Exception as exc:
            log.warning("reset_to_lobby failed: %s", exc)

    def _select_brawler_once(self, brawler):
        self.window_controller.screenshot()
        brawler_menu_treshold = 0.8
        found = False
        brawler_menu_btn_coords = None
        while not found:
            brawler_menu_btn_coords = find_template_center(self.window_controller.screenshot(), load_image(
                r'state_finder/images_to_detect/brawler_menu_btn.png', self.window_controller.scale_factor),
                                                           brawler_menu_treshold)
            if brawler_menu_btn_coords:
                found = True
            else:
                log.debug("brawler menu button not found at threshold=%.2f", brawler_menu_treshold)
                brawler_menu_treshold -= 0.1
                time.sleep(1)
            if not found and brawler_menu_treshold < 0.5:
                image = self.window_controller.screenshot()
                image.save(r'brawler_menu_btn_not_found.png')
                raise ValueError("Brawler menu button not found on screen, even at low threshold.")
        x, y = brawler_menu_btn_coords
        log.debug("clicking brawler menu button at (%d,%d)", x, y)
        self.window_controller.click(x, y)
        time.sleep(1.2)  # menu open animation
        wr = self.window_controller.width_ratio
        hr = self.window_controller.height_ratio

        # Two phases: forward scroll (50 swipes down), then a "scroll-back-up"
        # phase (15 swipes up) in case the menu jumped past the target.
        for phase, (swipes, dy_start, dy_step) in enumerate([
            (50, 850, 650),     # forward scroll
            (15, 250, 450),     # reverse — swipe upward to scroll back
        ]):
            log.debug("select_brawler phase %d: %d swipes", phase, swipes)
            for i in range(swipes):
                screenshot = self.window_controller.screenshot()
                screenshot_small = screenshot.resize(
                    (int(screenshot.width * 0.65), int(screenshot.height * 0.65)))
                results = extract_text_and_positions(np.array(screenshot_small))
                reworked_results = {}
                for key in results.keys():
                    orig_key = key
                    for symbol in [' ', '-', '.', "&"]:
                        key = key.replace(symbol, "")
                    key = self.resolve_ocr_typos(key)
                    reworked_results[key] = results[orig_key]
                if i < 3:
                    log.debug("phase %d swipe %d: OCR keys=%s",
                              phase, i, list(reworked_results.keys())[:20])
                # Fuzzy match: EasyOCR mis-reads many brawler names
                # (colt→cowt, shelly→shey, bibi→bbi, …). Pick the OCR
                # key with the closest match to the target, accepting
                # any with similarity ≥ 0.7 (Ratcliff/Obershelp).
                match_key = self._fuzzy_match(brawler, reworked_results.keys())
                if match_key is not None:
                    bx, by = reworked_results[match_key]['center']
                    real_x, real_y = int(bx * 1.5385), int(by * 1.5385)
                    log.info("FOUND brawler %r (OCR=%r) at (%d,%d) — clicking",
                             brawler, match_key, real_x, real_y)
                    self.window_controller.click(real_x, real_y)
                    time.sleep(1.5)
                    # Find the EQUIP/SELECT button via OCR — its position
                    # differs between BlueStacks (1920x1080) and phones,
                    # and the hardcoded select_btn coords were wrong on Mi 9T
                    # → bot tapped outside the popup, kept previous brawler.
                    if not self._find_and_tap_equip_button():
                        log.warning("EQUIP button not found via OCR — falling back "
                                    "to legacy select_btn coords")
                        select_x = self.coords_cfg['lobby']['select_btn'][0]
                        select_y = self.coords_cfg['lobby']['select_btn'][1]
                        self.window_controller.click(select_x, select_y, already_include_ratio=False)
                    time.sleep(1.5)
                    log.info("brawler %r selection completed", brawler)
                    return
                # Swipe to scroll within the menu.
                start_y = 900 if phase == 0 else dy_start
                end_y = dy_start if phase == 0 else dy_step
                self.window_controller.swipe(
                    int(1700 * wr), int(start_y * hr),
                    int(1700 * wr), int(end_y * hr),
                    duration=0.8,
                )
                time.sleep(0.6)

        # Not found anywhere — fail this attempt; outer loop will retry.
        raise ValueError(f"Brawler '{brawler}' not found in menu OCR.")

    # OCR variants of the equip/select button text.
    # Modern BS (2026) uses CHOISIR in French ("CHOOSE") — older builds
    # used ÉQUIPER. Cover both.
    _EQUIP_KEYWORDS = (
        "choisir", "choose",
        "equiper", "équiper", "equip", "équipé", "equipé",
        "select", "sélectionner", "selectionner", "selected", "sélection",
    )

    def _find_and_tap_equip_button(self) -> bool:
        """OCR the screen for the EQUIP/SELECT button and tap it.

        After tapping a brawler row, BS shows a popup with the brawler
        details + an EQUIP button. Hardcoded coords were calibrated for
        BlueStacks; on phones they miss the button and the popup closes
        without selecting (= previous brawler stays equipped).
        """
        try:
            from utils import extract_text_and_positions
            import numpy as np
            screenshot = self.window_controller.screenshot()
            arr = np.array(screenshot)
            text = extract_text_and_positions(arr)
            for key, val in text.items():
                k = key.lower().strip().replace(" ", "")
                if any(kw in k for kw in self._EQUIP_KEYWORDS):
                    cx, cy = val.get("center", [0, 0])
                    if cx > 0 and cy > 0:
                        log.info("EQUIP button OCR'd as %r at (%d,%d) — tapping",
                                 key, cx, cy)
                        # cx/cy are in frame coords; wc.click rescales to device.
                        self.window_controller.click(cx, cy)
                        return True
        except Exception:
            log.exception("_find_and_tap_equip_button OCR failed")
        return False

    @staticmethod
    def resolve_ocr_typos(potential_brawler_name: str) -> str:
        """
        Matches well known 'typos' from OCR to the correct brawler's name
        or returns the original string
        """

        matched_typo: str | None = {
            'shey': BrawlerName.Shelly.value,
            'shlly': BrawlerName.Shelly.value,
            'larryslawrie': BrawlerName.Larry.value,
        }.get(potential_brawler_name, None)

        return matched_typo or potential_brawler_name