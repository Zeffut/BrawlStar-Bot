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
from typing import List, Optional

import numpy as np
import requests
from PIL import Image

from state_finder.main import get_state
import device
from utils import extract_text_and_positions

log = logging.getLogger(__name__)

# Supercell Brawl Stars tag charset (uppercase). Brawl Stars uses a
# broader set than Clash Royale — includes B, R, M, F, etc.
TAG_CHARS = set("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ")

# Avatar tap point as a RATIO of the screen (works on any resolution).
# The brawler avatar sits at the very top-left of the lobby.
AVATAR_TAP_RATIO = (0.08, 0.085)  # x = 8% of width, y = 8.5% of height


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


def _ocr_player_tag_candidates(profile_img: Image.Image, max_candidates: int = 30) -> List[str]:
    """Return ranked candidate tags from OCR (best first). The caller
    validates them via brawlace.com to pick the real one."""
    w, h = profile_img.size
    raw_candidates: list[tuple[str, bool]] = []
    for ratio in (0.24, 0.27, 0.30, 0.32, 0.35, 0.38, 0.41):
        y0 = int(h * ratio)
        crop = profile_img.crop((int(w * 0.01), y0, int(w * 0.26), y0 + int(h * 0.06)))
        for key in extract_text_and_positions(np.array(crop)).keys():
            has_hash = key.lstrip().startswith("#")
            m = re.search(r"#?([A-Za-z0-9]{5,12})", key)
            if not m:
                continue
            raw = m.group(1).upper()
            # Don't filter on digit/alpha mix — OCR often confuses digits
            # with letters (9→I, 2→Z, 0→O). brawlace.com validation will
            # reject bogus candidates.
            raw_candidates.append((raw, has_hash))
    # Build expanded variants covering common OCR confusions.
    # Important pairs: I↔1, O↔0, Z↔2, B↔8, B↔R, S↔5, G↔6, T↔7
    seen: set[str] = set()
    ranked: list[str] = []
    def _add(t: str, hashed: bool):
        if t in seen or not all(c in TAG_CHARS for c in t):
            return
        seen.add(t)
        ranked.append(t)

    # Ordered by frequency (most common OCR confusions first so the
    # right candidate surfaces sooner).
    PAIRS = [("I", "9"), ("9", "I"), ("I", "1"), ("1", "I"),
             ("O", "0"), ("0", "O"), ("Z", "2"), ("2", "Z"),
             ("R", "B"), ("B", "R"), ("B", "8"), ("8", "B"),
             ("S", "5"), ("5", "S"), ("G", "6"), ("6", "G"),
             ("T", "7"), ("7", "T")]

    # Generate positional variants: for each position in the raw tag,
    # find PAIRS where the current char matches and swap. Then combine
    # up to 3 simultaneous positional swaps to cover the most common
    # OCR misreads.
    def swaps_at(s: str):
        """For each position, list (idx, candidate_char) replacements."""
        out: list[tuple[int, str]] = []
        for i, c in enumerate(s):
            for a, b in PAIRS:
                if c == a:
                    out.append((i, b))
        return out

    def apply_swaps(s: str, swap_set: list[tuple[int, str]]) -> str:
        chars = list(s)
        for i, ch in swap_set:
            chars[i] = ch
        return "".join(chars)

    from itertools import combinations
    for hashed_pass in (True, False):
        for raw, hashed in raw_candidates:
            if hashed != hashed_pass:
                continue
            _add(raw, hashed)
            single_swaps = swaps_at(raw)
            # 1-swap variants
            for sw in single_swaps:
                _add(apply_swaps(raw, [sw]), hashed)
                if len(ranked) >= max_candidates:
                    return ranked
            # 2-swap combinations (at DIFFERENT positions)
            for s1, s2 in combinations(single_swaps, 2):
                if s1[0] == s2[0]:
                    continue
                _add(apply_swaps(raw, [s1, s2]), hashed)
                if len(ranked) >= max_candidates:
                    return ranked
            # 3-swap combinations
            for s1, s2, s3 in combinations(single_swaps, 3):
                positions = {s1[0], s2[0], s3[0]}
                if len(positions) != 3:
                    continue
                _add(apply_swaps(raw, [s1, s2, s3]), hashed)
                if len(ranked) >= max_candidates:
                    return ranked
    return ranked[:max_candidates]


def _ocr_player_tag(profile_img: Image.Image) -> Optional[str]:
    """Find a `#XXXXXXX` tag in the profile screen by scanning the top-left
    region. Returns the cleaned tag (uppercase, no `#`) or None.

    The tag sits in the upper-left of the profile, just under the
    avatar. Resolution-independent: we scan a band at 26%-42% of the
    screen height, covering both BlueStacks (tag at ~30%) and physical
    phones (tag at ~30% too — Brawl Stars UI is aspect-ratio adaptive).
    """
    w, h = profile_img.size
    candidates: List[str] = []
    # Scan a 10-step vertical band from 24% to 44% of height.
    for ratio in (0.24, 0.27, 0.30, 0.32, 0.35, 0.38, 0.41):
        y0 = int(h * ratio)
        crop = profile_img.crop((int(w * 0.01), y0, int(w * 0.26), y0 + int(h * 0.06)))
        for key in extract_text_and_positions(np.array(crop)).keys():
            # Tags ALWAYS start with #. EasyOCR sometimes drops the #, so we
            # accept both, but score #-prefixed candidates higher.
            has_hash = key.lstrip().startswith("#")
            m = re.search(r"#?([A-Za-z0-9]{5,12})", key)
            if not m:
                continue
            raw = m.group(1).upper()
            # Brawl Stars tags are mixed alphanumeric — reject pure-letter
            # OCR noise (e.g. "PODDYOODIS").
            has_digit = any(c.isdigit() for c in raw)
            has_alpha = any(c.isalpha() for c in raw)
            if not (has_digit and has_alpha):
                continue
            # Try common OCR confusions: B↔8, I↔1, O↔0, Z↔2
            base_variants = {raw}
            base_variants.add(raw.replace("B", "8"))
            base_variants.add(raw.replace("I", "1"))
            base_variants.add(raw.replace("O", "0"))
            base_variants.add(raw.replace("Z", "2"))
            for variant in base_variants:
                if all(c in TAG_CHARS for c in variant):
                    candidates.append((variant, has_hash))
    if not candidates:
        return None
    # Rank: prefer #-prefixed, then longer tags, then most common.
    # Group by tag string; track if any source had #.
    by_tag: dict[str, dict] = {}
    for tag, hashed in candidates:
        d = by_tag.setdefault(tag, {"hashed": False, "count": 0})
        d["hashed"] = d["hashed"] or hashed
        d["count"] += 1
    # Sort: hashed first, then by length (longer is better), then count.
    ranked = sorted(by_tag.items(),
                    key=lambda kv: (kv[1]["hashed"], len(kv[0]), kv[1]["count"]),
                    reverse=True)
    return ranked[0][0]


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
    # 2. Tap the avatar (top-left of lobby). Coords are computed from
    # the actual screen resolution so this works on any device.
    lobby_img = _screencap(serial)
    w, h = lobby_img.size
    tap_x = int(w * AVATAR_TAP_RATIO[0])
    tap_y = int(h * AVATAR_TAP_RATIO[1])
    log.debug("tapping avatar at (%d, %d) on %dx%d screen", tap_x, tap_y, w, h)
    _adb("shell", "input", "tap", str(tap_x), str(tap_y), serial=serial)
    # 3. Wait for profile screen, then OCR candidates + validate via brawlace.
    time.sleep(2.0)
    img = _screencap(serial)
    candidates = _ocr_player_tag_candidates(img)
    log.info("OCR candidates (%d): %s", len(candidates), candidates[:10])
    # 4. Always try to return to lobby (BACK key) BEFORE network calls.
    _adb("shell", "input", "keyevent", "4", serial=serial)
    time.sleep(1.0)
    # 5. Validate against brawlace.com — first candidate that returns
    #    >0 brawlers is the real tag. Delay between requests to avoid
    #    rate-limit (brawlace returns 403 if hit too fast).
    # Limit candidates: flaresolverr ~5-12s per call, cap to first 12.
    for i, c in enumerate(candidates[:12]):
        if i > 0:
            time.sleep(0.2)
        try:
            profile = fetch_account_profile(c, timeout=60)
            if profile.get("brawlers"):
                log.info("VALIDATED tag via cloud/flaresolverr: #%s (%s, %d brawlers)",
                         c, profile.get("name"), len(profile["brawlers"]))
                return c
        except Exception:
            continue
    log.warning("no OCR candidate validated via cloud (tried %d)", min(12, len(candidates)))
    return None


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


# In-process cache for the (rate-limited) brawlace profile. Brawlace via
# flaresolverr returns 403/504 if hit too often, and the profile barely
# changes minute-to-minute, so we serve a cached copy for a while and only
# re-fetch when stale. On a fetch failure we serve the last good copy
# rather than an empty list (so push_max can still start, etc.).
_PROFILE_CACHE: dict = {}            # tag -> (ts, profile)
_PROFILE_TTL_S = 300.0               # 5 min


def fetch_account_profile(tag: str, timeout: float = 30.0, *, force: bool = False) -> dict:
    """Return {"name": "zeffut2.0", "brawlers": [...]} for the account.

    Cached for `_PROFILE_TTL_S`; pass force=True to bypass (periodic refresh).
    Goes through the cloud panel `/api/util/brawler_profile/{tag}` which
    proxies brawlace through flaresolverr (bypasses Cloudflare).
    Falls back to direct brawlace, then to the last cached copy.
    """
    import time as _t
    tag = tag.lstrip("#").upper()
    now = _t.time()
    cached = _PROFILE_CACHE.get(tag)
    # Fresh cache → skip the API entirely (this is the rate-limit guard).
    if cached and not force and (now - cached[0]) < _PROFILE_TTL_S:
        log.debug("profile cache hit for %s (%.0fs old)", tag, now - cached[0])
        return cached[1]

    cloud_url = _cloud_url()
    if cloud_url:
        url = f"{cloud_url.rstrip('/')}/api/util/brawler_profile/{tag}"
        log.info("fetching brawler profile via cloud: %s", url)
        try:
            resp = requests.get(url, timeout=timeout)
            if resp.status_code == 200:
                j = resp.json()
                prof = {"name": j.get("name"), "brawlers": j.get("brawlers", [])}
                if prof["brawlers"]:
                    _PROFILE_CACHE[tag] = (now, prof)
                    return prof
                # Empty list (brawlace hiccup) — prefer stale cache below.
                log.warning("cloud profile returned 0 brawlers for %s", tag)
            else:
                log.warning("cloud profile endpoint HTTP %d: %s", resp.status_code, resp.text[:200])
        except Exception as exc:
            log.warning("cloud profile request failed: %s", exc)
    else:
        # Fallback: direct (will probably 403 due to Cloudflare).
        url = f"https://brawlace.com/players/{tag}"
        log.info("fallback: direct brawlace %s", url)
        try:
            resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
            if resp.status_code == 200:
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
                if brawlers:
                    prof = {"name": name, "brawlers": brawlers}
                    _PROFILE_CACHE[tag] = (now, prof)
                    return prof
            else:
                log.warning("brawlace returned HTTP %d", resp.status_code)
        except requests.RequestException as exc:
            log.warning("brawlace request failed: %s", exc)

    # Everything failed → serve the last good profile instead of empty, so
    # callers (push_max start, seeding) keep working through a rate-limit.
    if cached:
        log.warning("profile fetch failed for %s — serving cached copy (%.0fs old)",
                    tag, now - cached[0])
        return cached[1]
    return {"name": None, "brawlers": []}


def _cloud_url() -> str | None:
    try:
        import tomllib
        from pathlib import Path
        p = Path(__file__).resolve().parent / "cfg" / "cloud.toml"
        with p.open("rb") as f:
            return tomllib.load(f).get("url")
    except Exception:
        return None


def fetch_owned_brawlers(tag: str, timeout: float = 8.0) -> List[dict]:
    """Legacy helper — returns just the brawlers list."""
    return fetch_account_profile(tag, timeout)["brawlers"]
