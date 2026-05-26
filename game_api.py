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


def _ocr_trophies(arr) -> int | None:
    """Extract account trophy count from a lobby screenshot.

    Crops a narrow strip around the trophy icon in the top-right HUD
    (left-most of the trophies/coins/gems triplet) and returns the
    leftmost digit value to avoid picking up coins/gems.
    """
    try:
        h, w = arr.shape[:2]
        # Tight crop: trophy icon sits roughly at x = 0.62-0.72 of width
        # and y = 0.02-0.10 in landscape orientation.
        crop = arr[int(h * 0.02):int(h * 0.10), int(w * 0.60):int(w * 0.72)]
        text = extract_text_and_positions(crop)
        best = None  # (x_pos, value)
        for key, val in text.items():
            cleaned = "".join(c for c in key if c.isdigit())
            if not cleaned:
                continue
            n = int(cleaned)
            if not (50 <= n <= 200000):
                continue
            try:
                xpos = val.get("center", [0, 0])[0]
            except Exception:
                xpos = 0
            if best is None or xpos < best[0]:
                best = (xpos, n)
        return best[1] if best else None
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
        """Return a fresh PIL screenshot via adb (always real-time)."""
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

    def goto_lobby(self, max_attempts: int = 6) -> bool:
        """Close popups / dialogs and try to reach the lobby."""
        for _ in range(max_attempts):
            st = self.state()
            if st == "lobby":
                return True
            if st in ("popup", "shop", "brawler_selection"):
                self._tap_back()
                time.sleep(0.8)
                continue
            if st in ("star_drop", "trophy_reward", "end"):
                # Tap center-bottom CONTINUE.
                self.tap(0.5, 0.92)
                time.sleep(1.0)
                continue
            time.sleep(0.6)
        return self.state() == "lobby"

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

    def play_one_match(self, brawler: str | None = None, timeout_s: float = 420) -> dict:
        """Play one match end-to-end, return result.

        Delegates to the running BotRunner with mode='single' and
        max_matches=1.
        """
        if self._runner is None:
            return {"ok": False, "error": "runner not bound"}
        if self._runner.is_running():
            return {"ok": False, "error": "a session is already running"}
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
