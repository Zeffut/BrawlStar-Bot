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

        Priority: piggyback on the bot's shared `wc.last_frame` if it's
        recent (<3s old) to avoid ADB contention while the bot is playing.
        Otherwise spawn our own adb screencap.
        """
        try:
            wc = self.wc
            if wc is not None and getattr(wc, "last_frame", None) is not None:
                age = time.time() - getattr(wc, "last_frame_time", 0)
                if age < 3.0:
                    import cv2
                    # last_frame is BGR np.ndarray (from scrcpy/adb path).
                    rgb = cv2.cvtColor(wc.last_frame, cv2.COLOR_BGR2RGB)
                    return Image.fromarray(rgb)
        except Exception:
            pass
        return _adb_screencap()

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
        """OCR the top-right trophy counter from the lobby screen."""
        try:
            img = self._grab()
            return _ocr_trophies(np.array(img))
        except Exception as exc:
            log.warning("read_trophies(): %s", exc)
        return None

    def snapshot(self) -> dict:
        """Return a lightweight observation snapshot (no screenshot)."""
        try:
            img = self._grab()
            st = get_state(img)
        except Exception:
            st = "unknown"
        # Re-use the already-captured frame for OCR (avoid double adb).
        trophies = None
        try:
            if st != "unknown":
                trophies = _ocr_trophies(np.array(img))
        except Exception:
            pass
        return {
            "state": st,
            "trophies": trophies,
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

    def goto_lobby(self, max_attempts: int = 20) -> bool:
        """Aggressively close everything and reach the lobby.

        Tries multiple dismissal strategies per attempt: tap likely
        CONTINUE buttons (center-bottom, bottom-right), long-press for
        star drops, BACK key, OK button. Loops until lobby or max_attempts.
        """
        last_state = None
        same_state_count = 0
        for i in range(max_attempts):
            st = self.state()
            if st == "lobby":
                return True
            # Track how long we've been stuck on the same screen.
            if st == last_state:
                same_state_count += 1
            else:
                same_state_count = 0
                last_state = st
            # State-specific dismiss strategies.
            if st in ("popup", "shop", "brawler_selection"):
                self._tap_back()
            elif st == "star_drop":
                # TOUCHEZ ET MAINTENEZ: long-press center for 4s.
                self._long_press(0.5, 0.5, 4000)
            elif st in ("end", "trophy_reward"):
                # Try several candidate locations for CONTINUE button.
                self.tap(0.92, 0.94)   # bottom-right (Continuer)
                time.sleep(0.4)
                self.tap(0.5, 0.93)    # center-bottom (Continue)
            else:
                # Unknown state: try the universal dismiss sequence.
                self.tap(0.92, 0.94)
                time.sleep(0.3)
                self.tap(0.5, 0.93)
                if same_state_count >= 2:
                    self._tap_back()
                if same_state_count >= 4:
                    # Long-press in case there's a hidden tap-and-hold.
                    self._long_press(0.5, 0.5, 4000)
            time.sleep(1.2)
        return self.state() == "lobby"

    def _long_press(self, x_ratio: float, y_ratio: float, duration_ms: int) -> None:
        if not self.wc.width or not self.wc.height:
            self.wc.screenshot()
        x = int(x_ratio * self.wc.width)
        y = int(y_ratio * self.wc.height)
        try:
            subprocess.run(
                ["adb", "-s", device.adb_serial(), "shell", "input", "swipe",
                 str(x), str(y), str(x), str(y), str(duration_ms)],
                timeout=duration_ms / 1000 + 3, check=False,
            )
        except Exception:
            pass

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
        if not self.wc.width or not self.wc.height:
            self.wc.screenshot()  # ensure dims known
        x = int(x_ratio * self.wc.width)
        y = int(y_ratio * self.wc.height)
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
    """Initialize the global GameAPI (called once at bot startup)."""
    global _API
    with _LOCK:
        if _API is None:
            _API = GameAPI(window_controller, lobby_automation)
            log.info("GameAPI initialized")
    return _API
