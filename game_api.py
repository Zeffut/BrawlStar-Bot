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
from pathlib import Path

import numpy as np
from PIL import Image

import device
from state_finder.main import get_state
from utils import extract_text_and_positions

log = logging.getLogger(__name__)

_API: "GameAPI | None" = None
_LOCK = threading.Lock()

# --- Bot activity status ----------------------------------------------------
# A short human-readable description of what the worker is *currently doing*
# ("Selecting Brock", "Playing as Shelly", "Swapping to Bartaba", …).
# Published by the bot runner as it moves through phases (telegram_main.py,
# stage_manager.py) and surfaced in snapshot() so the cloud panel can show a
# rich status instead of the raw screen-state string. Falls back to the
# detected screen state when no activity is published (idle / manual play).
#
# The runner refreshes it continuously while the main loop spins, so a stale
# value (> TTL) means the bot stopped/crashed → we drop it and let the panel
# fall back to the screen state.
_ACTIVITY: dict = {"text": None, "ts": 0.0}
_ACTIVITY_LOCK = threading.Lock()
_ACTIVITY_TTL_S = 150.0


def set_activity(text: "str | None") -> None:
    """Publish (or clear, with None) the worker's current activity."""
    with _ACTIVITY_LOCK:
        _ACTIVITY["text"] = text or None
        _ACTIVITY["ts"] = time.time()


def get_activity() -> "str | None":
    """Current activity, or None if never set / gone stale (bot stopped)."""
    with _ACTIVITY_LOCK:
        text = _ACTIVITY["text"]
        ts = _ACTIVITY["ts"]
    if not text:
        return None
    if time.time() - ts > _ACTIVITY_TTL_S:
        return None
    return text

def _is_emulator_serial() -> bool:
    """Detect whether cfg/device.toml points at an emulator.

    Reading the file directly (instead of going through
    device.adb_serial()) means this works even when the emulator's
    ADB is temporarily unreachable — XAPK install, BlueStacks boot,
    etc. Used by every code path that should skip the battery /
    pause logic for emulators.
    """
    try:
        import tomllib as _t
        from pathlib import Path as _P
        p = _P(__file__).resolve().parent / "cfg" / "device.toml"
        if not p.exists():
            return False
        with p.open("rb") as f:
            s = (_t.load(f).get("serial") or "").strip()
        return s.startswith("emulator-") or s.startswith("127.0.0.1:")
    except Exception:
        return False


# Battery gate — single hysteresis: pause when level drops below LOW,
# stay paused until level reaches RESUME. No intermediate threshold.
# Stops the yo-yo of pausing/resuming around a single boundary while
# the bot's own activity briefly nudges the level back and forth.
BATTERY_LOW_PCT = 30          # pause grinding when level drops below this
BATTERY_CRITICAL_PCT = 20     # background safety net: force-stop active session
BATTERY_RESUME_PCT = 80       # release the pause once level reaches this

# Flag file path: presence == battery gate DISABLED. Default (absent) == enabled.
# Lets the user (via the panel toggle) opt out of the whole charging-based
# auto-pause without restarting the bot. The CRITICAL hardware safety net
# (< BATTERY_CRITICAL_PCT) still applies regardless.
_BATTERY_GATE_DISABLED_FLAG = (
    Path(__file__).resolve().parent / "data" / "battery_gate_disabled.flag"
)


def battery_gate_enabled() -> bool:
    """Return True if the battery gate is currently enabled (default)."""
    return not _BATTERY_GATE_DISABLED_FLAG.exists()


def set_battery_gate(enabled: bool) -> None:
    """Persist the battery gate state to disk."""
    _BATTERY_GATE_DISABLED_FLAG.parent.mkdir(parents=True, exist_ok=True)
    if enabled:
        try:
            _BATTERY_GATE_DISABLED_FLAG.unlink()
        except FileNotFoundError:
            pass
    else:
        _BATTERY_GATE_DISABLED_FLAG.touch()


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
        # Collect all plausible digit values, then pick the LONGEST digit
        # run (tie -> largest). The account total is the longest number in
        # the pill; returning the first token sometimes caught a stray "75"
        # fragment from the avatar badge instead of e.g. "2475".
        cands = []
        for key in text.keys():
            cleaned = "".join(c for c in key if c.isdigit())
            if cleaned and 50 <= int(cleaned) <= 200000:
                cands.append(cleaned)
        if cands:
            return int(max(cands, key=lambda s: (len(s), int(s))))
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
        # True while the phone is DELIBERATELY powered down (schedule sleep/cap/
        # break, or the battery saver). The idle watchdog stands down while set
        # so it doesn't fight the power-save by relaunching BS / waking the
        # screen. Set by enter_power_save(), cleared by exit_power_save().
        self._power_save_active = False

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

        def _pil(bgr):
            return Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))

        # 1. ScreenRecorder pipe.
        try:
            import screen_capture as _sc
            rec = _sc.get()
            if rec is not None:
                f = rec.get_frame()
                age = rec.get_frame_age()
                if f is not None and age is not None and age < 2.0:
                    return _pil(f)
        except Exception:
            log.debug("screenrec grab failed", exc_info=True)
        # 2. Shared frame.
        try:
            if wc is not None and getattr(wc, "last_frame", None) is not None:
                age = time.time() - getattr(wc, "last_frame_time", 0)
                if age < 15.0:
                    return _pil(wc.last_frame)
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
                return _pil(wc.last_frame)
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

    def battery_status(self, retries: int = 3) -> dict:
        """Read battery level + charging state via adb dumpsys battery.

        Emulators (BlueStacks, AVD, …) don't have a real battery and
        often refuse dumpsys battery. We detect those from the
        configured serial in cfg/device.toml — independent of whether
        the device is currently reachable — and short-circuit to
        {level: 100, charging: True} so the battery gate never
        applies. Without this, a transient ADB hiccup during XAPK
        install would flip the bot into 'paused' mode.

        For real phones: retries on ADB transient errors. Returns
        level=None only after all retries fail — callers must treat
        unknown level as a danger signal (assume LOW), never as OK
        to play.
        """
        # Emulator detection FIRST, from config (doesn't depend on a
        # live ADB connection).
        if _is_emulator_serial():
            return {"level": 100, "charging": True}
        try:
            serial = device.adb_serial()
        except Exception:
            return {"level": None, "charging": None}
        last_exc = None
        for attempt in range(retries):
            try:
                out = subprocess.run(
                    ["adb", "-s", serial, "shell", "dumpsys", "battery"],
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
                if level is not None:
                    return {"level": level, "charging": charging}
            except Exception as exc:
                last_exc = exc
            if attempt < retries - 1:
                time.sleep(1.5)
        log.warning("battery_status: all %d retries failed (last: %s)", retries, last_exc)
        return {"level": None, "charging": None}

    def _battery_pause_update(self, lvl: "int | None") -> bool:
        """Single hysteresis gate (30% / 80%) — the one place the pause flag moves.

        - Drop below LOW_PCT (30%) → paused.
        - Once paused, stay paused until level reaches RESUME_PCT (80%).
        - In between (e.g. 50% climbing) the bot keeps doing whatever
          it was doing: paused if it was paused, playing if it was
          playing. No mid-level toggle.
        """
        was_paused = getattr(self, "_battery_paused", False)
        self._battery_paused = (lvl is None or lvl < BATTERY_LOW_PCT
                                or (was_paused and lvl < BATTERY_RESUME_PCT))
        return self._battery_paused

    def can_play(self) -> tuple[bool, str]:
        """Return (ok, reason) from the battery hysteresis gate."""
        bat = self.battery_status()
        lvl = bat.get("level")
        if self._battery_pause_update(lvl):
            if lvl is None:
                return False, "battery level unknown (ADB failure) — refusing to play"
            if lvl < BATTERY_LOW_PCT:
                return False, f"battery {lvl}% — will resume at {BATTERY_RESUME_PCT}%"
            return False, f"recharging ({lvl}%) — will resume at {BATTERY_RESUME_PCT}%"
        return True, f"battery OK ({lvl}%)"

    # ---- Power saver (idle low-battery handling) -----------------

    def enter_power_save(self) -> None:
        """Force-stop Brawl Stars and turn off the phone screen.

        Used when battery is too low to keep playing. Saves drain and
        lets the phone recharge faster (Brawl Stars idle still drains
        notably, and the OLED screen is the biggest culprit).
        """
        serial = device.adb_serial()
        # Mark BEFORE touching the device so the idle watchdog (30s loop)
        # can't sneak a goto_lobby/relaunch in between the force-stop and the
        # screen-off (the flap we saw: break power-save undone within ~10s).
        self._power_save_active = True
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
        finally:
            # Re-arm the idle watchdog only after the game is (re)launched, so
            # it resumes its keep-at-lobby job for the active window.
            self._power_save_active = False

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
        # Trophy counting no longer uses OCR (it drifted ~1000). The account
        # total comes from the brawlace API + per-match deltas, pushed
        # separately (account_trophies_after). The snapshot reports no trophy
        # number so nothing OCR-derived reaches the panel.
        trophies = None
        bat = self.battery_status()
        lvl = bat.get("level")
        chg = bat.get("charging")
        paused = self._battery_pause_update(lvl)
        # Rich "what am I doing" status. Battery pause overrides any published
        # activity (it's the real reason the bot is idle), and works even when
        # no session is running. Otherwise use the runner-published activity,
        # which is None when idle → the panel falls back to the screen state.
        if paused:
            activity = (f"Waiting — charging ({lvl}%)" if lvl is not None
                        else "Waiting — battery gate")
        else:
            activity = get_activity()
        return {
            "state": st,
            "activity": activity,
            "trophies": trophies,
            "battery_pct": lvl,
            "battery_charging": chg,
            "battery_paused": paused,
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

    def _restart_brawlstars(self, reason: str = "") -> None:
        """Force-stop + relaunch Brawl Stars to bail out of a screen we can't
        dismiss (an unopenable star drop). The drop stays claimable; the lobby
        becomes reachable again.

        COOLDOWN (2026-06-15): a cold BS load to the lobby takes ~20-40s. Two
        concurrent goto_lobby loops (the grind + the idle watchdog) could each
        hit the stuck-bailout and force-stop BS every ~12s — BS never finished
        loading → perpetual restart loop. Suppress a restart if one happened
        <45s ago so BS actually reaches the lobby."""
        now = time.time()
        last = getattr(self, "_last_restart_t", 0.0)
        if now - last < 45:
            log.info("restart suppressed (BS restarted %.0fs ago, still loading) — %s",
                     now - last, reason)
            time.sleep(5)
            return
        self._last_restart_t = now
        serial = device.adb_serial()
        log.warning("restarting Brawl Stars (%s)", reason)
        try:
            subprocess.run(["adb", "-s", serial, "shell", "am", "force-stop",
                            "com.supercell.brawlstars"], timeout=5, check=False)
            time.sleep(2)
            subprocess.run(["adb", "-s", serial, "shell", "am", "start", "-n",
                            "com.supercell.brawlstars/.GameApp"], timeout=10, check=False)
            time.sleep(8)
        except Exception:
            log.exception("Brawl Stars restart failed")

    def _drag_hold_center(self, duration_ms: int = 3500) -> None:
        """Star-drop open gesture: a slow drag over a tiny distance = a held
        touch that moves slightly (like a real finger). A static synthesized
        hold (swipe X Y X Y) is filtered out by Brawl Stars on a phone."""
        try:
            dw, dh = device.device_size()
        except Exception:
            dw, dh = 2340, 1080
        cx, cy = dw // 2, dh // 2
        serial = device.adb_serial()
        try:
            subprocess.run(["adb", "-s", serial, "shell", "input", "swipe",
                            str(cx), str(cy), str(cx + 18), str(cy + 18),
                            str(duration_ms)],
                           timeout=duration_ms / 1000 + 5, check=False)
        except Exception:
            log.exception("drag-hold failed")

    def goto_lobby(self, max_attempts: int = 30) -> bool:
        """Aggressively close everything and reach the lobby.

        Strategy: shotgun. Try every known dismissal in sequence per
        iteration. One of them will work regardless of what screen we're
        actually on. Logs every action so we can debug what worked.

        First checks the screen-lock state — no point dismissing popups
        if the phone is locked, the wallpaper would just keep being
        misclassified as 'match' by the state detector.
        """
        last_state = None
        same_state_count = 0
        star_drop_tries = 0
        for i in range(max_attempts):
            # A DELIBERATE power-save is in effect (schedule break/sleep/cap or
            # battery saver): the screen is off / BS closed ON PURPOSE — abort
            # instead of waking it. Checked every iteration so an in-flight
            # goto_lobby that started just BEFORE the power-save bails out the
            # moment the flag flips, before its lock-check would wake the phone
            # (the residual flap: power-save undone ~30s later by this loop).
            if self._power_save_active:
                log.info("goto_lobby[%d]: power-save active → aborting "
                         "(device deliberately off)", i)
                return False
            # Lock check — wake + PIN if screen is off or keyguard is up.
            if self._screen_is_locked():
                log.warning("goto_lobby[%d]: screen locked → wake + unlock", i)
                self.exit_power_save()
                time.sleep(2.5)
                continue
            # BS can be ALIVE but backgrounded (MIUI steals focus to its
            # launcher / minus-one screen). state() then misreads it as 'match'
            # and the shotgun can't bring BS back, so goto_lobby loops forever —
            # worse with two concurrent goto_lobby calls (grind + idle watchdog),
            # whose interleaved taps keep resetting each other's stuck counter
            # below the relaunch threshold. Bring BS to the front directly.
            # LIVE-FIX 2026-06-15 (zeffut2.0 got stuck on the MIUI minus-one screen).
            if not self._foreground_is_bs():
                log.warning("goto_lobby[%d]: Brawl Stars not foreground → "
                            "bringing it to front", i)
                try:
                    subprocess.run(
                        ["adb", "-s", device.adb_serial(), "shell", "am", "start",
                         "-n", "com.supercell.brawlstars/.GameApp"],
                        timeout=10, check=False)
                except Exception:
                    log.debug("am start (bring BS to front) failed", exc_info=True)
                time.sleep(3)
                continue
            # "Quitter Brawl Stars ?" exit dialog (a stray BACK on the lobby
            # opens it) — tap Annuler to return to the lobby, never O.K. (quit).
            if self._dismiss_quit_dialog():
                log.info("goto_lobby[%d]: cancelled 'Quitter Brawl Stars?'", i)
                time.sleep(1.0)
                continue
            st = self.state()
            log.info("goto_lobby[%d] state=%s", i, st)
            if st == "lobby":
                if self._dismiss_team_invite():
                    time.sleep(1.0)
                    continue
                return True
            # Star drop: try the drag-hold open gesture; if it won't open
            # after a few tries, bail out by restarting BS instead of looping
            # the shotgun for minutes (the old failure mode: stuck on a star
            # drop the whole boot, watchdog only restarting after ~9 min).
            if st == "star_drop":
                star_drop_tries += 1
                if star_drop_tries <= 3:
                    log.info("goto_lobby[%d]: star_drop → drag-hold (try %d)",
                             i, star_drop_tries)
                    self._drag_hold_center()
                    time.sleep(1.5)
                    continue
                log.warning("goto_lobby[%d]: star drop won't open after %d "
                            "tries — restarting Brawl Stars", i, star_drop_tries)
                self._restart_brawlstars("unopenable star drop blocking lobby")
                star_drop_tries = 0
                continue
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
            # Generic stuck bail-out: stuck on the SAME non-lobby state for
            # several iterations means the shotgun isn't working (frozen match
            # frame, unhandled popup…). Restart BS instead of looping until
            # max_attempts — and, crucially, this also unblocks the deferred
            # self-update, which only fires once we're back at the lobby.
            if same_state_count >= 5:
                log.warning("goto_lobby[%d]: stuck on %s for %d iters — "
                            "restarting Brawl Stars", i, st, same_state_count)
                self._restart_brawlstars(f"stuck on {st} during goto_lobby")
                same_state_count = 0
                last_state = None
                continue
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
            # (BACK removed 2026-06-15: ignored by BS on its own screens, and on
            #  the lobby it opens the "Quitter Brawl Stars ?" dialog which the
            #  shotgun couldn't reliably cancel — trapping the bot. That dialog
            #  is now handled at the top of the loop via _dismiss_quit_dialog.)
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
            # Match on the JOINED text, not per-token: "INVITATION" and
            # "D'ÉQUIPE" come back as SEPARATE OCR tokens, so the old
            # per-token "invitation AND equipe" check never fired and the
            # popup blocked the lobby forever. The REFUSER+ACCEPTER button
            # pair is also a dead giveaway of an invite popup.
            joined = " ".join(k.lower() for k in text.keys())
            has_invite = (
                ("invitation" in joined
                 and ("equipe" in joined or "team" in joined or "equip" in joined))
                or ("refuser" in joined and "accepter" in joined)
                or ("decline" in joined and "accept" in joined)
            )
            if not has_invite:
                return False
            # Prefer tapping the OCR'd REFUSER button precisely.
            h, w = arr.shape[:2]
            for k, v in text.items():
                kl = k.lower()
                if "refuser" in kl or "decline" in kl or "refus" in kl:
                    cx, cy = v.get("center", [0, 0])
                    if cx > 0 and cy > 0:
                        log.info("team invite -> tapping REFUSER at (%d,%d)", cx, cy)
                        self.tap(cx / w, cy / h)
                        return True
            # Fallback: REFUSER text missed — tap the red (left) button of
            # the invite's button pair (≈38% width, ≈76% height).
            log.info("team invite detected (REFUSER text missed) -> tap (0.38,0.76)")
            self.tap(0.38, 0.76)
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

    def is_brawlstars_running(self) -> bool:
        """Check via adb pidof whether Brawl Stars is currently running."""
        try:
            out = subprocess.run(
                ["adb", "-s", device.adb_serial(), "shell", "pidof",
                 "com.supercell.brawlstars"],
                capture_output=True, text=True, timeout=5, check=False,
            )
            return bool(out.stdout.strip())
        except Exception:
            return False

    def _screen_is_locked(self) -> bool:
        """Return True if the screen is off OR the keyguard is up."""
        serial = device.adb_serial()
        try:
            disp = subprocess.run(
                ["adb", "-s", serial, "shell", "dumpsys", "display"],
                capture_output=True, text=True, timeout=5, check=False,
            ).stdout
            if "mScreenState=OFF" in disp:
                return True
            win = subprocess.run(
                ["adb", "-s", serial, "shell", "dumpsys", "window", "windows"],
                capture_output=True, text=True, timeout=5, check=False,
            ).stdout.lower()
            for line in win.splitlines():
                if "mcurrentfocus" in line or "mfocusedapp" in line:
                    if "keyguard" in line or "lockscreen" in line:
                        return True
        except Exception:
            pass
        return False

    def _foreground_is_bs(self) -> bool:
        """True if Brawl Stars is the FOCUSED app. BS can be running yet
        backgrounded (MIUI steals focus to its launcher / minus-one / App Vault
        screen) — then goto_lobby's state() misreads it as 'match' and the
        shotgun can never bring BS back. Returns True on an adb hiccup so a
        transient read doesn't trigger a needless relaunch."""
        serial = device.adb_serial()
        try:
            win = subprocess.run(
                ["adb", "-s", serial, "shell", "dumpsys", "window"],
                capture_output=True, text=True, timeout=5, check=False,
            ).stdout
            for line in win.splitlines():
                if "mCurrentFocus" in line or "mFocusedApp" in line:
                    return "com.supercell.brawlstars" in line
        except Exception:
            pass
        return True

    def _dismiss_quit_dialog(self) -> bool:
        """Detect the 'Quitter Brawl Stars ?' exit dialog and tap Annuler (cancel)
        to stay in the game. It's opened by a stray BACK on the lobby; if the
        shotgun later taps O.K. it would quit BS entirely (→ launcher → the
        foreground-steal loop). Returns True if it acted. LIVE-FIX 2026-06-15."""
        try:
            arr = np.array(self._grab())
            text = extract_text_and_positions(arr)
            joined = " ".join(text.keys()).lower()
            if "quitter" not in joined or "brawl" not in joined:
                return False
            h, w = arr.shape[:2]
            for key, val in text.items():
                kl = key.lower()
                if "annul" in kl or "cancel" in kl:
                    cx, cy = val.get("center", [0, 0])
                    if cx and cy:
                        self.tap(cx / w, cy / h)
                        return True
            self.tap(0.40, 0.74)  # fallback: Annuler is the left button
            return True
        except Exception:
            log.debug("_dismiss_quit_dialog failed", exc_info=True)
            return False

    def ensure_brawlstars_at_lobby(self, max_wait_s: float = 120) -> tuple[bool, str]:
        """Pre-flight for any task launch.

        1. If the screen is locked / off, wake + unlock (PIN).
        2. If Brawl Stars isn't running OR isn't on top, force-start it.
        3. Wait until state=lobby (dismissing popups via goto_lobby).
        Returns (ok, reason).
        """
        serial = device.adb_serial()
        deadline = time.time() + max_wait_s
        # 1. Wake & unlock if needed — exit_power_save handles wake + PIN.
        if self._screen_is_locked():
            log.info("ensure_at_lobby: screen locked → wake + unlock")
            self.exit_power_save()
            time.sleep(2)
        if not self.is_brawlstars_running():
            log.info("ensure_at_lobby: Brawl Stars not running → launching")
            try:
                subprocess.run(
                    ["adb", "-s", serial, "shell", "am", "start", "-n",
                     "com.supercell.brawlstars/.GameApp"],
                    timeout=15, check=False,
                )
            except Exception as exc:
                return False, f"failed to start Brawl Stars: {exc}"
            # Give the splash/loading screen time to advance before we
            # start dismissing popups.
            time.sleep(8)
        # Drive popups → lobby. goto_lobby returns True when it sees lobby.
        try:
            reached = self.goto_lobby(max_attempts=30)
            while not reached and time.time() < deadline:
                if not self.is_brawlstars_running():
                    log.warning("ensure_at_lobby: BS process died, relaunching")
                    subprocess.run(
                        ["adb", "-s", serial, "shell", "am", "start", "-n",
                         "com.supercell.brawlstars/.GameApp"],
                        timeout=15, check=False,
                    )
                    time.sleep(8)
                reached = self.goto_lobby(max_attempts=15)
            if not reached:
                return False, "could not reach lobby within timeout"
            log.info("ensure_at_lobby: lobby reached ✓")
            return True, "lobby reached"
        except Exception as exc:
            log.exception("ensure_at_lobby failed")
            return False, f"ensure_at_lobby crashed: {exc}"

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
        # Make sure Brawl Stars is open and we're at the lobby before
        # doing anything. Relaunches the app if it's not running.
        ok_lobby, lobby_reason = self.ensure_brawlstars_at_lobby()
        if not ok_lobby:
            return {"ok": False, "error": lobby_reason}
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
            _start_adb_reconnect_loop()
    return _API


def _start_adb_reconnect_loop() -> None:
    """Background loop: try to recover from a phone disconnect.

    Polls `adb devices` every 15 s. If no authorized device is
    present, runs `adb reconnect offline` then re-tries to detect a
    device. Logs once per state change to avoid spamming the journal.
    """
    def loop():
        last_state = None  # "connected" | "missing"
        while True:
            try:
                time.sleep(15)
                try:
                    serial = device.adb_serial()
                    state = "connected"
                    if state != last_state:
                        log.info("adb reconnect loop: device online (%s)", serial)
                        last_state = state
                except device.DeviceNotConnected:
                    if last_state != "missing":
                        log.warning("adb reconnect loop: no device — trying recovery")
                        last_state = "missing"
                    # Best-effort recovery sequence.
                    try:
                        subprocess.run(["adb", "reconnect", "offline"],
                                       timeout=5, check=False,
                                       stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL)
                    except Exception:
                        pass
                    # Also try `adb start-server` in case the daemon died.
                    try:
                        subprocess.run(["adb", "start-server"],
                                       timeout=5, check=False,
                                       stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL)
                    except Exception:
                        pass
            except Exception:
                log.exception("adb reconnect loop iter crashed")
                time.sleep(10)
    threading.Thread(target=loop, daemon=True, name="adb-reconnect").start()
    log.info("adb reconnect loop started")


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
                # Stand down during a DELIBERATE power-save (schedule sleep/cap/
                # break or battery saver): the screen is off / BS closed ON
                # PURPOSE, so don't "recover" it by waking + relaunching (that
                # flapped the break power-save off within ~10s).
                if getattr(api, "_power_save_active", False):
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

    Rules (when the battery gate is enabled — see battery_gate_enabled()):
      - As soon as the phone reports charging=True: enter power save
        (force-stop Brawl Stars + screen off) so the phone charges as
        fast as possible without the OLED + game drain.
      - As soon as charging flips back to False (user unplugged): exit
        power save (wake + unlock + relaunch Brawl Stars).
      - Hardware safety net: if battery < CRITICAL_PCT (20%) we still
        enter power save even when not charging, regardless of toggle.
    When the gate is disabled and we were in power save, we cleanly exit
    so the user isn't stranded with a locked phone.
    Runs every 60s, fully self-contained.
    """
    def loop():
        in_power_save = False
        critical_notified = False
        # Cache the last `charging` reading we acted on so a single
        # toggle (plug in / unplug) only triggers enter/exit once,
        # never repeatedly across iterations.
        last_charging_acted = None
        while True:
            try:
                bat = api.battery_status()
                lvl = bat.get("level")
                chg = bat.get("charging")
                gate_on = battery_gate_enabled()

                # Gate disabled: exit any active power save and idle.
                if not gate_on:
                    if in_power_save:
                        log.info("power-saver: gate disabled by user → "
                                 "exiting power save")
                        try: api.exit_power_save()
                        except Exception: log.exception("exit_power_save failed")
                        in_power_save = False
                        api._battery_paused = False
                        critical_notified = False
                        last_charging_acted = None
                    time.sleep(60)
                    continue

                # Adaptive poll: tighter when battery is at risk.
                poll_interval = 20 if (lvl is not None and lvl < BATTERY_LOW_PCT + 5) else 60
                session_active = (api._runner is not None and api._runner.is_running())

                # 1) Critical hardware safety net (lvl < CRITICAL).
                if lvl is not None and lvl < BATTERY_CRITICAL_PCT and not in_power_save:
                    log.warning("power-saver: battery=%d%% CRITICAL → "
                                "force-stopping session + entering power save "
                                "(charging=%s)", lvl, chg)
                    if session_active and api._runner is not None:
                        try: api._runner.force_stop()
                        except Exception: log.exception("force_stop failed")
                    try: api.enter_power_save()
                    except Exception: log.exception("enter_power_save failed")
                    api._battery_paused = True
                    in_power_save = True
                    last_charging_acted = chg
                    if not critical_notified:
                        try:
                            import cloud_sync as _cs
                            _cs.event("battery_low", {"level": lvl, "critical": True})
                            critical_notified = True
                        except Exception:
                            log.exception("cloud_sync.event(battery_low) failed")
                    time.sleep(poll_interval)
                    continue

                # 2) ADB battery read failed: be safe — pause if we
                # weren't already, but don't relaunch on recovery.
                if lvl is None and chg is None:
                    if not in_power_save:
                        log.warning("power-saver: battery unknown (ADB down?) — "
                                    "force-stopping session as safety net")
                        if session_active and api._runner is not None:
                            try: api._runner.force_stop()
                            except Exception: log.exception("force_stop failed")
                        try: api.enter_power_save()
                        except Exception: log.exception("enter_power_save failed")
                        api._battery_paused = True
                        in_power_save = True
                    time.sleep(30)
                    continue

                # 3) Charging-state edge detection (the new behavior).
                # `chg` can be None when only the level is readable; in
                # that case keep prior state — don't enter/exit on noise.
                if chg is True and not in_power_save and last_charging_acted is not True:
                    log.info("power-saver: charging=True → entering power save "
                             "(battery=%s%%, session_active=%s)", lvl, session_active)
                    if session_active and api._runner is not None:
                        try: api._runner.force_stop()
                        except Exception: log.exception("force_stop failed")
                    try: api.enter_power_save()
                    except Exception: log.exception("enter_power_save failed")
                    api._battery_paused = True
                    in_power_save = True
                    last_charging_acted = True
                elif chg is False and in_power_save and last_charging_acted is not False:
                    log.info("power-saver: charging=False → exiting power save "
                             "(battery=%s%%)", lvl)
                    try: api.exit_power_save()
                    except Exception: log.exception("exit_power_save failed")
                    api._battery_paused = False
                    in_power_save = False
                    critical_notified = False
                    last_charging_acted = False
                elif chg is True:
                    last_charging_acted = True
                elif chg is False:
                    last_charging_acted = False

                time.sleep(poll_interval)
            except Exception:
                log.exception("power-saver iteration crashed")
                time.sleep(30)
    threading.Thread(target=loop, daemon=True, name="power-saver").start()
    log.info("power-saver loop started")
