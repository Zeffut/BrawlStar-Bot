"""Clean primitives layer over the running Brawl Stars game.

Wraps WindowController + state_finder + lobby_automation into a small
API that the cloud orchestrator can drive over HTTP.

The local HTTP panel (panel/app.py) exposes these methods under
`/api/game/*`. The cloud panel proxies them through the WebSocket link
in worker_link.py.

All methods are *blocking* — caller should run them in a thread or
async executor when latency matters.

Screenshots are taken via `adb exec-out screencap -p` for reliability.
The scrcpy stream that WindowController also exposes is incompatible
with modern PyAV (>=10) on Python 3.12, so we don't depend on it here.
"""
from __future__ import annotations

import base64
import io
import logging
import subprocess
import threading
import time
from typing import Any

import numpy as np
from PIL import Image

import device
from state_finder.main import get_state
from utils import extract_text_and_positions

log = logging.getLogger(__name__)

_API: "GameAPI | None" = None
_LOCK = threading.Lock()

# Battery gate thresholds (configurable later via cfg).
BATTERY_LOW_PCT = 30      # stop grinding below this (post-match gate)
BATTERY_CRITICAL_PCT = 20 # force-pause even mid-session below this
BATTERY_RESUME_PCT = 75   # resume grinding once above this


def _is_brawlball_label(s: str) -> bool:
    """Fuzzy match for the BRAWL BALL tile label.

    EasyOCR commonly mis-reads it as 'brawibal', 'brawhball', 'brawll ball',
    etc. We accept anything starting with 'braw' and ending with 'bal'/'ball'
    (the second L is often dropped).
    """
    k = s.lower().replace(" ", "").replace("-", "").replace("_", "")
    if not k.startswith("braw"):
        return False
    # Must contain 'bal' somewhere after 'braw' to disambiguate from 'brawler'.
    rest = k[4:]
    if "bal" not in rest:
        return False
    # Length sanity: BRAWL BALL is 9 chars normally; allow OCR fuzz.
    return 7 <= len(k) <= 12


def _ocr_trophies(arr) -> int | None:
    """Extract account trophy count from a lobby screenshot.

    The current trophy count is displayed in the top-LEFT next to the
    player avatar (NOT the top-right which shows season max / pass).
    """
    try:
        h, w = arr.shape[:2]
        # Top-left trophy pill: y 0.02-0.12, x 0.10-0.22 in landscape.
        crop = arr[int(h * 0.02):int(h * 0.12), int(w * 0.10):int(w * 0.22)]
        text = extract_text_and_positions(crop)
        # Pick the first plausible digit value.
        for key in text.keys():
            cleaned = "".join(c for c in key if c.isdigit())
            if cleaned and 50 <= int(cleaned) <= 200000:
                return int(cleaned)
        return None
    except Exception:
        return None


def _load_lockscreen_pin() -> str | None:
    """Read the screen-unlock PIN from cfg/lockscreen.toml.

    The file is gitignored — secrets must never enter the repo.
    Format:
        pin = "1234"
    """
    try:
        import tomllib
        from pathlib import Path
        p = Path(__file__).resolve().parent / "cfg" / "lockscreen.toml"
        if not p.exists():
            return None
        with p.open("rb") as f:
            cfg = tomllib.load(f)
        pin = cfg.get("pin")
        return str(pin) if pin else None
    except Exception:
        log.debug("could not read lockscreen pin", exc_info=True)
        return None


def _adb_screencap() -> Image.Image:
    """Capture a screenshot via `adb exec-out screencap -p`.

    Returns a PIL RGB image. ~300-500 ms on USB.
    """
    serial = device.adb_serial()
    out = subprocess.run(
        ["adb", "-s", serial, "exec-out", "screencap", "-p"],
        capture_output=True, timeout=10, check=True,
    )
    img = Image.open(io.BytesIO(out.stdout)).convert("RGB")
    return img


class GameAPI:
    """All game-level primitives. Single instance per worker process."""

    def __init__(self, window_controller, lobby_automation):
        self.wc = window_controller
        self.la = lobby_automation
        self._brawler_cache: list[dict] = []
        self._brawler_cache_at: float = 0.0
        self._runner = None  # set by set_runner()

    # ---- lifecycle ------------------------------------------------

    def set_runner(self, runner) -> None:
        """Inject the bot runner so we can delegate play_match to it."""
        self._runner = runner

    # ---- observation ---------------------------------------------

    def _grab(self) -> Image.Image:
        """Return a fresh PIL screenshot.

        Priority order:
        1. ScreenRecorder H264 pipe (~30fps, frame age <1s in normal ops)
        2. wc.last_frame if recent (<15s)
        3. adb screencap (~600ms PNG fallback)
        4. wc.last_frame at any age → last resort
        """
        import cv2
        wc = self.wc
        # 1. ScreenRecorder pipe.
        try:
            import screen_capture as _sc
            rec = _sc.get()
            if rec is not None:
                f = rec.get_frame()
                age = rec.get_frame_age()
                if f is not None and age is not None and age < 2.0:
                    rgb = cv2.cvtColor(f, cv2.COLOR_BGR2RGB)
                    return Image.fromarray(rgb)
        except Exception:
            log.debug("screenrec grab failed", exc_info=True)
        # 2. Shared frame.
        try:
            if wc is not None and getattr(wc, "last_frame", None) is not None:
                age = time.time() - getattr(wc, "last_frame_time", 0)
                if age < 15.0:
                    rgb = cv2.cvtColor(wc.last_frame, cv2.COLOR_BGR2RGB)
                    return Image.fromarray(rgb)
        except Exception:
            log.debug("piggyback failed", exc_info=True)
        # 2. Own adb call — also writes back into wc.last_frame so the
        #    snapshot loop AND subsequent capture clicks read from cache.
        try:
            img = _adb_screencap()
            if wc is not None:
                try:
                    bgr = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
                    if hasattr(wc, "frame_lock"):
                        with wc.frame_lock:
                            wc.last_frame = bgr
                            wc.last_frame_time = time.time()
                    else:
                        wc.last_frame = bgr
                        wc.last_frame_time = time.time()
                except Exception:
                    pass
            return img
        except Exception as exc:
            log.warning("adb screencap failed: %s", exc)
        # 3. Last resort: stale wc.last_frame.
        if wc is not None and getattr(wc, "last_frame", None) is not None:
            try:
                rgb = cv2.cvtColor(wc.last_frame, cv2.COLOR_BGR2RGB)
                return Image.fromarray(rgb)
            except Exception:
                pass
        raise RuntimeError("no screenshot available (wc.last_frame=None and adb failed)")

    def state(self) -> str:
        try:
            img = self._grab()
            return get_state(img)
        except Exception as exc:
            log.warning("state(): %s", exc)
            return "unknown"

    def screenshot_jpeg(self, max_width: int = 960, quality: int = 75) -> dict:
        t0 = time.time()
        img = self._grab()
        capture_ms = round((time.time() - t0) * 1000, 1)
        if img.width > max_width:
            ratio = max_width / img.width
            img = img.resize((max_width, int(img.height * ratio)))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        return {
            "b64": base64.b64encode(buf.getvalue()).decode(),
            "mime": "image/jpeg",
            "w": img.width, "h": img.height,
            "capture_ms": capture_ms,
            "captured_at": round(time.time(), 2),
        }

    def read_trophies(self) -> int | None:
        """OCR the trophy counter from the lobby screen.

        Returns None if not on the lobby (any other screen has different
        numbers at the same position).
        """
        try:
            img = self._grab()
            arr = np.array(img)
            if get_state(img) != "lobby":
                return None
            return _ocr_trophies(arr)
        except Exception as exc:
            log.warning("read_trophies(): %s", exc)
        return None

    def battery_status(self) -> dict:
        """Read battery level + charging state via adb dumpsys battery."""
        try:
            out = subprocess.run(
                ["adb", "-s", device.adb_serial(), "shell", "dumpsys", "battery"],
                capture_output=True, text=True, timeout=5, check=False,
            ).stdout
            level = None
            charging = None
            for line in out.splitlines():
                line = line.strip()
                if line.startswith("level:"):
                    try: level = int(line.split(":", 1)[1].strip())
                    except Exception: pass
                elif line.startswith("status:"):
                    # 1=unknown 2=charging 3=discharging 4=not-charging 5=full
                    try: charging = int(line.split(":", 1)[1].strip()) in (2, 5)
                    except Exception: pass
            return {"level": level, "charging": charging}
        except Exception as exc:
            log.warning("battery_status: %s", exc)
            return {"level": None, "charging": None}

    def can_play(self) -> tuple[bool, str]:
        """Return (ok, reason) — refuses to play if battery is too low.

        Rule: if level < LOW_PCT and not charging, refuse.
              Once paused, only resume when level >= RESUME_PCT.
        """
        bat = self.battery_status()
        lvl, chg = bat.get("level"), bat.get("charging")
        if lvl is None:
            return True, "battery level unknown — proceeding"
        if chg and lvl < BATTERY_RESUME_PCT and getattr(self, "_battery_paused", False):
            return False, f"charging ({lvl}%, will resume at {BATTERY_RESUME_PCT}%)"
        if lvl < BATTERY_LOW_PCT and not chg:
            self._battery_paused = True
            return False, f"battery too low ({lvl}%) — plug in, will resume at {BATTERY_RESUME_PCT}%"
        if lvl >= BATTERY_RESUME_PCT:
            self._battery_paused = False
        return True, f"battery OK ({lvl}%{', charging' if chg else ''})"

    # ---- Power saver (idle low-battery handling) -----------------

    def enter_power_save(self) -> None:
        """Force-stop Brawl Stars and turn off the phone screen.

        Used when battery is too low to keep playing. Saves drain and
        lets the phone recharge faster (Brawl Stars idle still drains
        notably, and the OLED screen is the biggest culprit).
        """
        serial = device.adb_serial()
        try:
            subprocess.run(["adb", "-s", serial, "shell", "am", "force-stop",
                            "com.supercell.brawlstars"], timeout=5, check=False)
            # Screen off via POWER keyevent (only toggles if currently on).
            # Use dumpsys to check state first.
            ds = subprocess.run(["adb", "-s", serial, "shell", "dumpsys", "display"],
                                capture_output=True, text=True, timeout=5, check=False).stdout
            if "mScreenState=ON" in ds:
                subprocess.run(["adb", "-s", serial, "shell", "input", "keyevent", "26"],
                                timeout=5, check=False)
            log.info("power-save: Brawl Stars stopped + screen off")
        except Exception:
            log.exception("enter_power_save failed")

    def exit_power_save(self) -> None:
        """Wake screen, unlock (PIN if configured), relaunch Brawl Stars.

        PIN is read from cfg/lockscreen.toml (gitignored).
        """
        serial = device.adb_serial()
        try:
            # 1. Wake the screen.
            ds = subprocess.run(["adb", "-s", serial, "shell", "dumpsys", "display"],
                                capture_output=True, text=True, timeout=5, check=False).stdout
            if "mScreenState=OFF" in ds:
                subprocess.run(["adb", "-s", serial, "shell", "input", "keyevent", "26"],
                                timeout=5, check=False)
                time.sleep(0.6)
            # 2. Swipe up to reveal the lock screen PIN entry.
            subprocess.run(["adb", "-s", serial, "shell", "input", "swipe",
                            "540", "1500", "540", "500", "200"],
                            timeout=5, check=False)
            time.sleep(0.6)
            # 3. Type PIN if configured.
            pin = _load_lockscreen_pin()
            if pin:
                # `input text` types the digits in one shot.
                subprocess.run(["adb", "-s", serial, "shell", "input", "text", pin],
                                timeout=5, check=False)
                time.sleep(0.3)
                # Confirm via ENTER keyevent (works with most launchers).
                subprocess.run(["adb", "-s", serial, "shell", "input", "keyevent", "66"],
                                timeout=5, check=False)
                time.sleep(0.6)
            else:
                # No PIN: try dismiss-keyguard as a generic unlock.
                subprocess.run(["adb", "-s", serial, "shell", "wm", "dismiss-keyguard"],
                                timeout=5, check=False)
            # 4. Relaunch Brawl Stars.
            subprocess.run(["adb", "-s", serial, "shell", "am", "start", "-n",
                            "com.supercell.brawlstars/.GameApp"],
                            timeout=10, check=False)
            log.info("power-save exited: screen on + unlocked + game launched")
        except Exception:
            log.exception("exit_power_save failed")

    def wait_for_battery(self, max_wait_s: float = 3600, poll_s: float = 60) -> bool:
        """Block until battery is OK to play (or max_wait_s passes).

        Used by grind loops (push_max, repeated play_one_match) to pause
        when battery drops too low and auto-resume when it recovers.
        """
        ok, reason = self.can_play()
        if ok:
            return True
        log.info("battery gate: %s — pausing up to %ds", reason, max_wait_s)
        deadline = time.time() + max_wait_s
        while time.time() < deadline:
            time.sleep(poll_s)
            ok, reason = self.can_play()
            log.info("battery gate check: %s", reason)
            if ok:
                return True
        return False

    def snapshot(self) -> dict:
        """Return a lightweight observation snapshot (no screenshot)."""
        try:
            img = self._grab()
            st = get_state(img)
        except Exception:
            st = "unknown"
        # OCR trophies ONLY on the lobby screen — on other screens (end
        # of match, brawler menu, etc) the top-left corner contains a
        # totally different number (daily wins, brawler trophies, etc).
        trophies = None
        try:
            if st == "lobby":
                trophies = _ocr_trophies(np.array(img))
        except Exception:
            pass
        bat = self.battery_status()
        return {
            "state": st,
            "trophies": trophies,
            "battery_pct": bat.get("level"),
            "battery_charging": bat.get("charging"),
            "battery_paused": getattr(self, "_battery_paused", False),
            "ts": round(time.time(), 2),
        }

    def switch_to_brawlball(self, max_attempts: int = 3) -> bool:
        """Open the mode selection menu and pick Brawl Ball.

        From lobby:
          1. Tap the mode banner (HORS-JEU/etc) at ~(0.67, 0.93) → opens picker
          2. OCR for the Brawl Ball tile (tolerates 'brawibal' / 'brawllball'
             OCR variants) and tap it
          3. Verify mode is now brawlball.
        """
        for attempt in range(max_attempts):
            log.info("switch_to_brawlball attempt %d/%d", attempt + 1, max_attempts)
            # 1. Ensure we're on lobby first.
            self.goto_lobby(max_attempts=5)
            # 2. Tap the mode banner (right-of-center, just above PLAY).
            self.tap(0.67, 0.93)
            time.sleep(1.8)
            # 3. We should be on the mode picker. OCR for Brawl Ball tile.
            try:
                img = self._grab()
                arr = np.array(img)
                text = extract_text_and_positions(arr)
                target = None
                target_key = None
                for key, val in text.items():
                    if _is_brawlball_label(key):
                        target = val.get("center")
                        target_key = key
                        break
                if target:
                    h, w = arr.shape[:2]
                    cx, cy = target
                    log.info("found Brawl Ball tile via OCR=%r at (%d,%d)",
                             target_key, cx, cy)
                    self.tap(cx / w, cy / h)
                    time.sleep(1.5)
                    # 4. The tile click may open a map sub-picker; tap a
                    #    'JOUER' / 'PLAY' button or just BACK to return to lobby.
                    self.goto_lobby(max_attempts=8)
                    mode = self.read_current_mode()
                    if mode == "brawlball":
                        return True
                    log.warning("post-switch mode is %r, retrying", mode)
                else:
                    log.warning("BRAWL BALL tile not found in mode picker OCR "
                                "(keys=%s)", list(text.keys())[:15])
                    self._tap_back()
                    time.sleep(0.8)
            except Exception as exc:
                log.warning("switch_to_brawlball OCR failed: %s", exc)
                self._tap_back()
                time.sleep(0.8)
        return False

    def read_current_mode(self) -> str | None:
        """OCR the current game mode shown at the bottom-center of the lobby.

        Returns a normalized mode name (e.g. "brawlball", "showdown",
        "gemgrab"). Returns None if not detectable.
        """
        try:
            img = self._grab()
            arr = np.array(img)
            h, w = arr.shape[:2]
            # Mode banner sits at the bottom-center, just above the PLAY button.
            crop = arr[int(h * 0.84):int(h * 0.96), int(w * 0.40):int(w * 0.70)]
            text = extract_text_and_positions(crop)
            joined = " ".join(text.keys()).lower()
            # Common French / English mode keywords.
            modes = {
                "brawlball": ["brawlball", "brawl ball", "brawl-ball", "bal de"],
                "showdown": ["showdown", "survie"],
                "gemgrab": ["gem grab", "rafle de gemmes", "razzia"],
                "bounty": ["bounty", "prime"],
                "heist": ["heist", "braquage"],
                "knockout": ["knockout", "ko"],
                "duels": ["duel"],
                "hotzone": ["hot zone", "zone"],
            }
            for canon, kws in modes.items():
                if any(k in joined for k in kws):
                    return canon
        except Exception as exc:
            log.warning("read_current_mode(): %s", exc)
        return None

    def read_current_brawler(self) -> str | None:
        """OCR the brawler name shown above the play button in lobby."""
        try:
            img = self._grab()
            arr = np.array(img)
            h, w = arr.shape[:2]
            # The current brawler name is shown center-bottom under the avatar.
            crop = arr[int(h * 0.72):int(h * 0.82), int(w * 0.35):int(w * 0.65)]
            text = extract_text_and_positions(crop)
            for key in text.keys():
                key = key.strip()
                if key.isalpha() and 3 <= len(key) <= 16:
                    return key.lower()
        except Exception as exc:
            log.warning("read_current_brawler(): %s", exc)
        return None

    # ---- navigation ----------------------------------------------

    def goto_lobby(self, max_attempts: int = 30) -> bool:
        """Aggressively close everything and reach the lobby.

        Strategy: shotgun. Try every known dismissal in sequence per
        iteration. One of them will work regardless of what screen we're
        actually on. Logs every action so we can debug what worked.
        """
        last_state = None
        same_state_count = 0
        for i in range(max_attempts):
            st = self.state()
            log.info("goto_lobby[%d] state=%s", i, st)
            if st == "lobby":
                if self._dismiss_team_invite():
                    time.sleep(1.0)
                    continue
                return True
            if self._dismiss_team_invite():
                log.info("goto_lobby[%d]: dismissed team invite", i)
                time.sleep(1.0)
                continue
            # OCR-based detection of reward / star-drop screens.
            ocr_action = self._dismiss_via_ocr()
            if ocr_action:
                log.info("goto_lobby[%d]: OCR action: %s", i, ocr_action)
                time.sleep(1.5)
                continue
            # Track stuck.
            if st == last_state:
                same_state_count += 1
            else:
                same_state_count = 0
                last_state = st
            # SHOTGUN: try multi-action dismiss for unknown / stuck states.
            log.info("goto_lobby[%d]: SHOTGUN dismiss (state=%s, stuck=%d)",
                     i, st, same_state_count)
            # 1. Long-press center 5s — star drop "TOUCHEZ ET MAINTENEZ"
            #    needs a full hold; anything <4s gets dismissed as a tap.
            #    Try multiple slightly different positions to handle the
            #    case where Android caps swipe duration on first try.
            self._long_press(0.5, 0.5, 5000)
            time.sleep(0.3)
            # Some MIUI builds cap swipe duration; chain two presses if so.
            self._long_press(0.5, 0.5, 4000)
            time.sleep(0.4)
            # 2. Tap-anywhere center (covers most reward dismisses).
            self.tap(0.5, 0.5)
            time.sleep(0.4)
            # 3. CONTINUER bottom-right (Brawl Stars French).
            self.tap(0.92, 0.94)
            time.sleep(0.3)
            # 4. CONTINUE bottom-center.
            self.tap(0.5, 0.93)
            time.sleep(0.3)
            # 5. Close-X top-right (popups with X icon).
            self.tap(0.96, 0.05)
            time.sleep(0.3)
            # 6. BACK key (menus).
            self._tap_back()
            time.sleep(1.2)
        log.warning("goto_lobby gave up after %d attempts (still on %s)",
                    max_attempts, self.state())
        return self.state() == "lobby"

    # Keywords we recognize on post-match reward screens.
    _OCR_HOLD_KEYWORDS = (
        "touchez et maintenez", "touchez maintenez", "tap and hold",
        "appuyez et maintenez", "tap & hold", "appuyez maintenez",
    )
    _OCR_CONTINUE_KEYWORDS = (
        "continuer", "continue", "ok",
    )
    _OCR_REWARD_TITLES = (
        "credits", "crédits", "victoires du jour", "puissance",
        "pieces", "pièces", "coins", "power points", "points de pouvoir",
        "star drop", "star drops",
    )

    def _dismiss_via_ocr(self) -> str | None:
        """OCR-based fallback for unhandled reward / star-drop screens.

        Returns a short label of the action taken, or None if no relevant
        UI was found.
        """
        try:
            img = self._grab()
            arr = np.array(img)
            text = extract_text_and_positions(arr)
            joined = " ".join(text.keys()).lower()
            h, w = arr.shape[:2]
            # 1. "TOUCHEZ ET MAINTENEZ" / "TAP AND HOLD" → long-press center
            if any(k in joined for k in self._OCR_HOLD_KEYWORDS):
                self._long_press(0.5, 0.5, 5000)
                return "long-press (HOLD keyword)"
            # 2. CONTINUER button — find its position and tap precisely.
            for key, val in text.items():
                if any(k == key.lower().strip() for k in self._OCR_CONTINUE_KEYWORDS):
                    cx, cy = val.get("center", [0, 0])
                    if cx > 0 and cy > 0:
                        self.tap(cx / w, cy / h)
                        return f"tap CONTINUE at OCR {key!r}"
            # 3. Reward title detected (CRÉDITS, etc.) but no CONTINUE text.
            #    Star drop reward screens dismiss on tap-anywhere; tap
            #    center first (universal), then canonical bottom positions
            #    as fallback for other reward layouts.
            if any(t in joined for t in self._OCR_REWARD_TITLES):
                self.tap(0.5, 0.5)        # center — works for star-drop rewards
                time.sleep(0.4)
                self.tap(0.92, 0.94)      # bottom-right CONTINUER
                time.sleep(0.3)
                self.tap(0.5, 0.93)       # bottom-center CONTINUE
                return "tap multi (reward title)"
        except Exception:
            log.debug("_dismiss_via_ocr failed", exc_info=True)
        return None

    def _dismiss_team_invite(self) -> bool:
        """Detect 'INVITATION D'ÉQUIPE' popup and tap REFUSER.

        Returns True if an invite popup was found and dismissed.
        """
        try:
            img = self._grab()
            arr = np.array(img)
            text = extract_text_and_positions(arr)
            keys_lower = {k.lower() for k in text.keys()}
            # Detection heuristic: either the title or the REFUSER button.
            has_invite = any(
                ("invitation" in k and ("equipe" in k or "team" in k or "equip" in k))
                or "invitation dequipe" in k
                for k in keys_lower
            )
            refuser_pos = None
            for k, v in text.items():
                kl = k.lower()
                if "refuser" in kl or "decline" in kl or "refus" == kl:
                    refuser_pos = v.get("center")
                    break
            if has_invite and refuser_pos:
                h, w = arr.shape[:2]
                cx, cy = refuser_pos
                log.info("team invite detected -> tapping REFUSER at (%d,%d)", cx, cy)
                self.tap(cx / w, cy / h)
                return True
            # Fallback: invite-shaped layout but OCR missed REFUSER text;
            # tap the canonical position (left button of pair).
            if has_invite:
                log.info("team invite detected (REFUSER text missed) -> tapping (0.42, 0.63)")
                self.tap(0.42, 0.63)
                return True
        except Exception:
            log.debug("_dismiss_team_invite failed", exc_info=True)
        return False

    def _long_press(self, x_ratio: float, y_ratio: float, duration_ms: int) -> None:
        """Reliable long-press via `input touchscreen swipe`.

        `input swipe` is sometimes treated as a tap when start==end.
        `input touchscreen swipe` is explicit about the input source
        and handles long durations correctly on modern Android.
        We add a small movement (2px) so the gesture is unambiguous.
        """
        dw, dh = device.device_size()
        x = int(x_ratio * dw)
        y = int(y_ratio * dh)
        serial = device.adb_serial()
        try:
            subprocess.run(
                ["adb", "-s", serial, "shell", "input", "touchscreen", "swipe",
                 str(x), str(y), str(x + 2), str(y + 2), str(duration_ms)],
                timeout=duration_ms / 1000 + 5, check=False,
            )
        except Exception:
            log.exception("long_press failed")

    def _tap_back(self) -> None:
        serial = device.adb_serial()
        try:
            subprocess.run(
                ["adb", "-s", serial, "shell", "input", "keyevent", "4"],
                timeout=3, check=False,
            )
        except Exception:
            pass

    def select_brawler(self, name: str) -> dict:
        try:
            self.la.select_brawler(name)
            return {"ok": True, "selected": name}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def list_brawlers(self, force_refresh: bool = False, tag: str | None = None) -> list[dict]:
        """Return owned brawlers.

        Source priority:
          1. Cloud panel `/api/util/brawler_profile/<tag>` (proxies brawlace
             through flaresolverr) — fast (~3-5 s first hit, instant on cache).
          2. Local DB tag if `tag` not provided.
        """
        if not force_refresh and self._brawler_cache and (time.time() - self._brawler_cache_at) < 300:
            return self._brawler_cache
        # Resolve account tag from local DB if not given.
        if tag is None:
            try:
                import db as _db
                accs = _db.list_accounts()
                if accs:
                    tag = accs[0]["tag"]
            except Exception:
                pass
        if not tag:
            log.warning("list_brawlers: no tag available; returning empty")
            return []
        # Pull cloud config to know where to ask.
        cloud_url = self._cloud_url()
        if not cloud_url:
            log.warning("list_brawlers: cloud panel URL not configured")
            return []
        try:
            import requests as _r
            resp = _r.get(f"{cloud_url.rstrip('/')}/api/util/brawler_profile/{tag}",
                          timeout=80)
            resp.raise_for_status()
            j = resp.json()
            brawlers = j.get("brawlers", [])
            if brawlers:
                self._brawler_cache = brawlers
                self._brawler_cache_at = time.time()
            return brawlers
        except Exception as exc:
            log.warning("list_brawlers via cloud failed: %s", exc)
            return []

    @staticmethod
    def _cloud_url() -> str | None:
        try:
            import tomllib
            from pathlib import Path
            p = Path(__file__).resolve().parent / "cfg" / "cloud.toml"
            with p.open("rb") as f:
                return tomllib.load(f).get("url")
        except Exception:
            return None

    # ---- raw input ------------------------------------------------

    def tap(self, x_ratio: float, y_ratio: float) -> dict:
        # ADB input tap takes DEVICE pixel coordinates, not frame coords.
        # screenrec downscales to 1280x720 — using wc.width would tap at
        # half the intended position on a 2336x1080 device.
        dw, dh = device.device_size()
        x = int(x_ratio * dw)
        y = int(y_ratio * dh)
        serial = device.adb_serial()
        try:
            subprocess.run(["adb", "-s", serial, "shell", "input", "tap", str(x), str(y)],
                           timeout=3, check=False)
            return {"ok": True, "x": x, "y": y}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    # ---- match playing -------------------------------------------

    def play_one_match(self, brawler: str | None = None, timeout_s: float = 420,
                        required_mode: str | None = None) -> dict:
        """Play one match end-to-end, return result.

        If `required_mode` is set, verify the lobby is on that mode and
        abort if it isn't (no auto-switch yet — the user can change mode
        on the phone manually).
        Delegates to the running BotRunner with mode='single' and max_matches=1.
        """
        if self._runner is None:
            return {"ok": False, "error": "runner not bound"}
        if self._runner.is_running():
            return {"ok": False, "error": "a session is already running"}
        # Battery gate — refuse to play if too low (plug in to resume).
        ok_bat, bat_reason = self.can_play()
        if not ok_bat:
            return {"ok": False, "error": bat_reason}
        # Ensure we're on the lobby before checking mode (dismiss popups).
        self.goto_lobby(max_attempts=8)
        if required_mode:
            cur_mode = self.read_current_mode()
            log.info("mode check: current=%r required=%r", cur_mode, required_mode)
            if cur_mode != required_mode.lower():
                # Currently only Brawl Ball is supported for auto-switch.
                if required_mode.lower() == "brawlball":
                    log.info("not on brawlball — auto-switching")
                    if not self.switch_to_brawlball():
                        return {"ok": False, "error": "could not switch to Brawl Ball automatically — change mode on phone and retry"}
                else:
                    return {"ok": False, "error": f"mode '{required_mode}' auto-switch not implemented yet (only brawlball is)"}
        if brawler is None:
            brawler = self.read_current_brawler() or "shelly"
        # Bind the local account_id so post-match hooks persist results to
        # both the local DB and the cloud panel.
        try:
            import db as _db
            accs = _db.list_accounts()
            if accs and not self._runner._account_id:
                self._runner._account_id = accs[0]["id"]
                log.info("play_one_match: bound runner.account_id = %d", accs[0]["id"])
        except Exception:
            log.exception("could not bind account_id")
        ok, msg = self._runner.start(
            brawler=brawler,
            trophies=999999,
            wins=0,
            mode="single",
            max_matches=1,
        )
        if not ok:
            return {"ok": False, "error": msg}
        # Block until runner stops or timeout.
        t0 = time.time()
        while self._runner.is_running() and (time.time() - t0) < timeout_s:
            time.sleep(2.0)
        if self._runner.is_running():
            self._runner.force_stop()
            return {"ok": False, "error": "match timeout"}
        # Force return to lobby — dismiss any reward / star drop / credits
        # screens that the runner left behind.
        try:
            self.goto_lobby(max_attempts=20)
        except Exception:
            log.exception("post-match goto_lobby failed")
        # Read last match result from runner stats.
        return {
            "ok": True,
            "brawler": brawler,
            "matches": self._runner._match_count,
            "wins": self._runner._win_count,
            "losses": self._runner._loss_count,
            "draws": self._runner._draw_count,
            "duration_s": round(time.time() - t0, 1),
        }


# ---- singleton ----------------------------------------------------


def get() -> GameAPI | None:
    """Return the global GameAPI (None if not yet initialized)."""
    return _API


def init(window_controller, lobby_automation) -> GameAPI:
    """Initialize the global GameAPI (called once at bot startup).

    Triggers a first screenshot to populate `wc.last_frame` so that the
    cloud panel can serve captures immediately, without waiting for the
    bot's main loop to do its first frame.
    Also starts the power-saver background monitor.
    """
    global _API
    with _LOCK:
        if _API is None:
            _API = GameAPI(window_controller, lobby_automation)
            # Warm up the shared frame buffer.
            try:
                _adb_screencap()  # not assigned; the wc capture happens via wc
                window_controller.screenshot()  # populates wc.last_frame
                log.info("GameAPI initialized (wc.last_frame warmed)")
            except Exception:
                log.info("GameAPI initialized (warm-up failed, will fetch on demand)")
            # Start the background loops.
            _start_power_saver(_API)
            _start_idle_watchdog(_API)
    return _API


def _start_idle_watchdog(api: "GameAPI") -> None:
    """Background loop: keep the bot at the lobby when idle.

    Every 30s, if no session is active AND the game is not on the lobby,
    try goto_lobby. If that fails 3 times in a row, force-restart
    Brawl Stars. Makes the worker self-healing — the user shouldn't
    have to click 'goto lobby' from the panel.
    """
    def loop():
        time.sleep(60)  # let phase 2 init finish
        consecutive_failures = 0
        while True:
            try:
                time.sleep(30)
                # Skip if a session is running — runner owns the screen.
                if api._runner is not None and api._runner.is_running():
                    consecutive_failures = 0
                    continue
                st = api.state()
                if st == "lobby":
                    consecutive_failures = 0
                    continue
                # Not at lobby + no session → try to recover.
                log.info("idle watchdog: state=%s, attempting goto_lobby", st)
                ok = api.goto_lobby(max_attempts=20)
                if ok:
                    log.info("idle watchdog: back to lobby")
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1
                    log.warning("idle watchdog: goto_lobby failed (%d/3)",
                                consecutive_failures)
                    if consecutive_failures >= 3:
                        log.warning("idle watchdog: force-restarting Brawl Stars")
                        try:
                            serial = device.adb_serial()
                            subprocess.run(["adb", "-s", serial, "shell",
                                            "am", "force-stop",
                                            "com.supercell.brawlstars"],
                                            timeout=5, check=False)
                            time.sleep(2)
                            subprocess.run(["adb", "-s", serial, "shell",
                                            "am", "start", "-n",
                                            "com.supercell.brawlstars/.GameApp"],
                                            timeout=10, check=False)
                            consecutive_failures = 0
                        except Exception:
                            log.exception("Brawl Stars restart failed")
            except Exception:
                log.exception("idle watchdog iteration crashed")
    threading.Thread(target=loop, daemon=True, name="idle-watchdog").start()
    log.info("idle watchdog armed (checks every 30s)")


def _start_power_saver(api: "GameAPI") -> None:
    """Background loop: enter/exit power-save mode based on battery state.

    Rules:
      - If idle (no runner session) AND battery < LOW_PCT and not charging:
        enter power save (force-stop game + screen off)
      - If in power-save AND battery >= RESUME_PCT:
        exit (wake + relaunch game)
    Runs every 60s, fully self-contained.
    """
    def loop():
        in_power_save = False
        while True:
            try:
                time.sleep(60)
                bat = api.battery_status()
                lvl = bat.get("level")
                chg = bat.get("charging")
                if lvl is None:
                    continue
                session_active = (api._runner is not None and api._runner.is_running())
                if not in_power_save:
                    # Critical: force-pause even an active session — the
                    # post-match gate is too late if the bot is stuck
                    # mid-match or on a reward screen.
                    if lvl < BATTERY_CRITICAL_PCT and not chg:
                        log.warning("power-saver: battery=%d%% CRITICAL → "
                                    "force-stopping session + entering power save", lvl)
                        if session_active and api._runner is not None:
                            try: api._runner.force_stop()
                            except Exception: log.exception("force_stop failed")
                        api.enter_power_save()
                        in_power_save = True
                    elif not session_active and lvl < BATTERY_LOW_PCT and not chg:
                        log.info("power-saver: battery=%d%% idle → entering power save", lvl)
                        api.enter_power_save()
                        in_power_save = True
                else:
                    if lvl >= BATTERY_RESUME_PCT:
                        log.info("power-saver: battery=%d%% → exiting power save", lvl)
                        api.exit_power_save()
                        in_power_save = False
            except Exception:
                log.exception("power-saver iteration crashed")
    threading.Thread(target=loop, daemon=True, name="power-saver").start()
    log.info("power-saver loop started")
