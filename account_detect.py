"""Auto-detect the connected Brawl Stars account.

Flow:
1. From the lobby, tap the avatar (top-left) to open the profile screen.
2. OCR the player tag (e.g. "#PYLV98LG9").
3. Press BACK to return to the lobby.
4. Scrape https://brawlace.com/players/<tag> to list owned brawlers
   with their current trophies (no auth, no rate limit).

No dependency on the running bot — uses raw ADB so it works while the
bot is stopped.
"""
from __future__ import annotations

import io
import logging
import re
import subprocess
import time
from typing import List, Optional, Tuple

import numpy as np
import requests
from PIL import Image

from state_finder.main import get_state
import device
from utils import extract_text_and_positions

log = logging.getLogger(__name__)

# Supercell tag charset (uppercase). Used to validate OCR output.
TAG_CHARS = set("0289PYLQGRJCUV")

# Avatar tap point in *native* device coordinates (works on the user's
# BlueStacks at 2560x1440). The OS scales input.tap to device coords.
AVATAR_TAP_XY = (200, 90)


def _adb(*args, serial: str | None = None, timeout: int = 5) -> bytes:
    if serial is None: serial = device.adb_serial()
    return subprocess.check_output(
        ["adb", "-s", serial, *args], timeout=timeout
    )


def _screencap(serial: str | None = None) -> Image.Image:
    if serial is None: serial = device.adb_serial()
    raw = _adb("exec-out", "screencap", "-p", serial=serial, timeout=8)
    return Image.open(io.BytesIO(raw)).convert("RGB")


def _wait_state(target: str, serial: str, timeout_s: float = 8.0) -> bool:
    end = time.time() + timeout_s
    while time.time() < end:
        try:
            if get_state(_screencap(serial)) == target:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def ensure_lobby(serial: str | None = None, max_attempts: int = 12) -> bool:
    if serial is None: serial = device.adb_serial()
    """Bring the game to the LOBBY screen if it's anywhere else.

    Strategy:
      * popup / shop / brawler_selection / trophy_reward / star_drop /
        play_store / disconnect → press BACK
      * match / end → wait (game will resolve itself)
      * anything else → press BACK
    Returns True if we ended up on the lobby.
    """
    log.info("ensure_lobby start (serial=%s)", serial)
    for attempt in range(max_attempts):
        try:
            state = get_state(_screencap(serial))
        except Exception as exc:
            log.warning("ensure_lobby screencap failed: %s", exc)
            time.sleep(1)
            continue
        log.debug("ensure_lobby attempt %d: state=%s", attempt + 1, state)
        if state == "lobby":
            log.info("ensure_lobby OK after %d attempts", attempt + 1)
            return True
        if state in ("match", "end"):
            # Don't interrupt a match — wait it out.
            log.debug("in match/end, waiting…")
            time.sleep(3)
            continue
        if state == "star_drop":
            # Star drop needs a long-press, not BACK. Tap CONTINUE area.
            _adb("shell", "input", "swipe", "1280", "720", "1280", "720", "4000",
                 serial=serial, timeout=8)
            time.sleep(2)
            continue
        if state == "trophy_reward":
            _adb("shell", "input", "tap", "1280", "1320", serial=serial)
            time.sleep(1.5)
            continue
        # popup / shop / brawler_selection / play_store / etc. → BACK
        _adb("shell", "input", "keyevent", "4", serial=serial)
        time.sleep(1.5)
    log.warning("ensure_lobby gave up after %d attempts", max_attempts)
    return False


def _ocr_player_tag(profile_img: Image.Image) -> Optional[str]:
    """Find a `#XXXXXXX` tag in the profile screen by scanning the top-left
    region. Returns the cleaned tag (uppercase, no `#`) or None."""
    # Tag sits under the avatar at native y=400-450 (2560x1440 screenshots).
    # Scan a small vertical band to be robust to layout shifts.
    candidates: List[str] = []
    for y0 in (390, 410, 430):
        crop = profile_img.crop((20, y0, 600, y0 + 70))
        for key in extract_text_and_positions(np.array(crop)).keys():
            m = re.search(r"#?([A-Za-z0-9]{5,12})", key)
            if not m:
                continue
            raw = m.group(1).upper()
            # OCR sometimes reads B as 8 or vice versa. Try both swaps.
            for variant in {raw, raw.replace("B", "8"), raw.replace("8", "B")}:
                if all(c in TAG_CHARS for c in variant):
                    candidates.append(variant)
    if not candidates:
        return None
    # OCR sometimes drops a letter (PYLV98LG9 → PYV98LG9). Among
    # plausible candidates, prefer the LONGEST one first, then the most
    # common at that length. Brawl Stars tags are 8-9 chars typically.
    from collections import Counter
    max_len = max(len(c) for c in candidates)
    longest = [c for c in candidates if len(c) == max_len]
    return Counter(longest).most_common(1)[0][0]


def detect_player_tag(serial: str | None = None) -> Optional[str]:
    if serial is None: serial = device.adb_serial()
    """Open the profile from the lobby, OCR the tag, return to the lobby.

    Requires the game to be on the LOBBY screen when called. Returns
    None on any failure (caller should fall back to manual config).
    """
    log.info("detect_player_tag start (serial=%s)", serial)
    # 1. Bring the game to the lobby if it isn't already.
    if not ensure_lobby(serial):
        log.warning("detect_player_tag aborted: couldn't reach lobby")
        return None
    # 2. Tap avatar.
    log.debug("tapping avatar at %s", AVATAR_TAP_XY)
    _adb("shell", "input", "tap", str(AVATAR_TAP_XY[0]), str(AVATAR_TAP_XY[1]),
         serial=serial)
    # 3. Wait for profile screen, then OCR.
    time.sleep(2.0)
    img = _screencap(serial)
    tag = _ocr_player_tag(img)
    log.info("OCR'd tag: %s", tag)
    # 4. Always try to return to lobby (BACK key).
    _adb("shell", "input", "keyevent", "4", serial=serial)
    time.sleep(1.0)
    return tag


# --------------------------- brawlace scraping ---------------------------

# Row layout (one brawler per <tr>):
#   <td>...alt='NAME' ... NAME</td>
#   <td>POWER</td>
#   <td data-order='N'>...tier img...</td>
#   <td>TROPHIES</td>
#   <td>HIGHEST</td>
#   ...
_ROW_RE = re.compile(
    r"/brawlers/([A-Za-z0-9_\-\.]+)\.png[^>]*/>\s*([A-Z0-9 \.\-&!]+?)</td>"
    r"<td>(\d+)</td>"
    r"<td[^>]*>.*?/tiers/\d+\.png.*?</td>"
    r"<td>(\d+)</td>",
    re.DOTALL,
)


_NAME_RE = re.compile(
    r'<meta name="description" content="([^"]+?) Brawl Stars Stats',
    re.IGNORECASE,
)


def fetch_account_profile(tag: str, timeout: float = 8.0) -> dict:
    """Return {"name": "zeffut2.0", "brawlers": [...]} for the account.

    `brawlers` is a list of {"name", "power", "trophies"}. Empty list /
    None on failure.
    """
    tag = tag.lstrip("#").upper()
    url = f"https://brawlace.com/players/{tag}"
    log.info("fetching brawlace profile: %s", url)
    try:
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"},
                            timeout=timeout)
    except requests.RequestException as exc:
        log.warning("brawlace request failed: %s", exc)
        return {"name": None, "brawlers": []}
    if resp.status_code != 200:
        log.warning("brawlace returned HTTP %d", resp.status_code)
        return {"name": None, "brawlers": []}
    name_match = _NAME_RE.search(resp.text)
    name = name_match.group(1).strip() if name_match else None
    brawlers: List[dict] = []
    for _img_name, display_name, power, trophies in _ROW_RE.findall(resp.text):
        brawlers.append({
            "name": display_name.strip().lower(),
            "power": int(power),
            "trophies": int(trophies),
        })
    log.info("brawlace profile parsed: name=%r brawlers=%d", name, len(brawlers))
    return {"name": name, "brawlers": brawlers}


def fetch_owned_brawlers(tag: str, timeout: float = 8.0) -> List[dict]:
    """Legacy helper — returns just the brawlers list."""
    return fetch_account_profile(tag, timeout)["brawlers"]
