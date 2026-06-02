import difflib
import logging
import time
import unicodedata


def _normalize_name(s: str) -> str:
    """Lowercase, strip accents, and drop all separators/punctuation so OCR'd
    French names match the aliases regardless of dots/apostrophes/@/accents.
    e.g. 'A.R.K.A.D'→'arkad', "D'jinn"→'djinn', 'Béa'→'bea', 'Éliz@'→'eliz'."""
    s = unicodedata.normalize("NFKD", s.lower().strip())
    s = "".join(c for c in s if not unicodedata.combining(c))
    for symbol in (" ", "-", ".", "&", "_", "'", "’", "@", ":", ",", "!"):
        s = s.replace(symbol, "")
    return s

import numpy as np

from stage_manager import load_image
from typization import BrawlerName
from utils import extract_text_and_positions, count_hsv_pixels, load_toml_as_dict, find_template_center

debug = load_toml_as_dict("cfg/general_config.toml")['super_debug'] == "yes"
log = logging.getLogger(__name__)


class BrawlerNotFoundError(ValueError):
    """The brawler's name never appeared in the menu OCR after a full scan.
    Retrying the same scan is pointless (the OCR can't read that name, e.g.
    '8-BIT' → garbage), so select_brawler fails fast instead of repeating the
    ~36-swipe scan 3× — which left the bot 'galère à choisir' for ~10 min,
    churning through several unreadable brawlers."""

# Brawl Stars localizes some brawler names. brawlace (and push_max) use the
# ENGLISH names, but the in-game menu (French here) shows the FRENCH names, so
# the menu OCR never matches e.g. "barley" — the card reads "BARTABA". When
# searching the menu we try the localized alias FIRST, then fall back to the
# English name (most brawlers are identical, and accent-only differences like
# bea→Béa / jacky→Jackie are caught by the fuzzy match, so they don't need an
# entry). Only names that differ SUBSTANTIALLY need one.
#
# Confirmed via the FR wiki + observed OCR (the "garbage" the OCR read —
# 'airkad', 'costo', 'ricochet' — were in fact the correct French names):
#   8-bit→A.R.K.A.D, el primo→El Costo, rico→Ricochet, piper→Polly,
#   barley→Bartaba, crow→Corbac, gene→D'jinn.
# Names are matched accent/dot/apostrophe-insensitively (see _normalize), so
# the exact punctuation here is just for readability.
# Values may be a single name OR a list of candidate names (the menu tries each
# — useful when the exact in-game spelling is uncertain or version-dependent).
BRAWLER_FR_ALIASES: dict[str, "str | list[str]"] = {
    "barley": "bartaba",
    "crow": "corbac",
    "gene": "d'jinn",
    # 8-bit: Zeffut confirms the in-game FR name is "ARCADE"; keep "A.R.K.A.D"
    # too (an old guide + the OCR literally read 'airkad') so either spelling
    # matches across versions.
    "8-bit": ["arcade", "a.r.k.a.d"],
    "8bit": ["arcade", "a.r.k.a.d"],
    "el primo": "el costo",
    "piper": "polly",
    "rico": "ricochet",
    # mr. p — FR name uncertain (sources disagree: "M. P" vs "Monsieur M.");
    # left as a best guess, pending confirmation.
    "mr. p": "m. p",
    "mrp": "m. p",
    "mr p": "m. p",
}


def _fr_aliases(brawler: str) -> list[str]:
    """Return the list of French-name candidates for `brawler` (possibly empty).
    Normalizes the str-or-list value of BRAWLER_FR_ALIASES."""
    v = BRAWLER_FR_ALIASES.get((brawler or "").lower().strip())
    if not v:
        return []
    return [v] if isinstance(v, str) else list(v)

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
        # Short-circuit: if the target is ALREADY the equipped brawler, don't
        # open the menu at all. The brawler-grid navigation is the fragile part
        # (OCR + scroll), and the common case — grinding / resuming the brawler
        # that's already equipped — doesn't need it. This is what made the bot
        # "galère à chercher tick" for minutes when tick was already selected.
        if self._is_already_equipped(brawler):
            log.info("select_brawler: %r already equipped — skipping menu", brawler)
            return
        last_exc: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            log.info("select_brawler attempt %d/%d", attempt, max_attempts)
            try:
                self._select_brawler_once(brawler)
                log.info("select_brawler: SUCCESS on attempt %d", attempt)
                return
            except BrawlerNotFoundError as exc:
                # The name is unreadable in the menu OCR — retrying the same
                # full scan is wasted time (≈2 min each). Fail immediately so
                # push_max marks it unselectable and moves to the next brawler.
                log.warning("select_brawler: %s — not retrying (unreadable name)", exc)
                self._reset_to_lobby()
                raise
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

    def _is_already_equipped(self, brawler: str) -> bool:
        """True if `brawler` is the brawler currently equipped in the lobby.

        Reads the equipped-brawler name (OCR above the PLAY button via
        game_api) and fuzzy-matches it against the target AND its French alias.
        Best-effort: any failure → False (fall through to normal selection)."""
        try:
            import game_api
            api = game_api.get()
            if api is None:
                return False
            equipped = api.read_current_brawler()
            if not equipped:
                return False
            cands = [equipped]
            if self._fuzzy_match(brawler, cands):
                return True
            for alias in _fr_aliases(brawler):
                if self._fuzzy_match(alias, cands):
                    return True
        except Exception:
            log.debug("_is_already_equipped check failed", exc_info=True)
        return False

    @staticmethod
    def _fuzzy_match(target: str, candidates, min_ratio: float = 0.7) -> str | None:
        """Return the candidate whose name is the closest fuzzy match to
        `target` (Ratcliff/Obershelp ratio ≥ min_ratio), or None.

        Also accepts an exact substring match — useful when OCR adds
        leading/trailing chars (e.g. `?colt`).
        """
        # Normalize accents/dots/apostrophes/@ on BOTH sides so French names
        # match regardless of punctuation (A.R.K.A.D→arkad, D'jinn→djinn,
        # Béa→bea, El Costo→elcosto).
        target = _normalize_name(target)
        if not target:
            return None
        best: tuple[float, str] | None = None
        for c in candidates:
            c_norm = _normalize_name(c)
            # Skip empty + pure-number tokens (trophy counts).
            if not c_norm or c_norm.isdigit():
                continue
            # Exact match wins (also covers short names like "bo").
            if c_norm == target:
                return c
            # Skip stray short fragments — a lone 'a'/'i' must NOT match a
            # long name. (This is what made 'a' "match" barley and tap junk.)
            if len(c_norm) < 3:
                continue
            # OCR appends junk (trophy count, badges); accept when the FULL
            # target name appears inside the token. We do NOT accept the
            # reverse (token as a substring of target) — that's the unsafe
            # direction that let 'a' match 'barley'.
            if len(target) >= 3 and target in c_norm:
                return c
            ratio = difflib.SequenceMatcher(None, target, c_norm).ratio()
            if ratio >= min_ratio and (best is None or ratio > best[0]):
                best = (ratio, c)
        return best[1] if best else None

    def _reset_to_lobby(self) -> None:
        """Return to the lobby by tapping the top-right home button.

        We never use the Android BACK key: BS/Unity ignores it on
        sub-screens (so it can't escape the Trophy Road / shop / brawler
        menu), AND pressing BACK at the lobby pops a "Quit Brawl Stars?"
        dialog that then confuses the state detector. The top-right home
        button reliably returns to the lobby from every sub-screen.
        """
        wc = self.window_controller
        try:
            fw, fh = wc.width or 0, wc.height or 0
            if fw and fh:
                # Home button sits in the top-right corner (~95% width, ~6.5% height).
                wc.click(int(fw * 0.952), int(fh * 0.065))
                time.sleep(1.5)
            log.debug("reset_to_lobby: home-button tapped")
        except Exception as exc:
            log.warning("reset_to_lobby failed: %s", exc)

    def _open_brawler_menu(self) -> bool:
        """Open the brawler-selection menu by OCR-tapping the "BRAWLERS"
        button in the lobby. Returns True if a candidate was tapped.

        Replaces the old brawler_menu_btn.png template match, which failed
        to match above 0.5 on the current lobby and then false-matched the
        trophy counter — opening the Trophy Road instead of the menu.
        """
        try:
            shot = self.window_controller.screenshot()
            small = shot.resize((int(shot.width * 0.65), int(shot.height * 0.65)))
            results = extract_text_and_positions(np.array(small))
            norm = {}
            for k in results.keys():
                kk = k
                for symbol in [' ', '-', '.', '&', '_', ',']:
                    kk = kk.replace(symbol, "")
                norm[kk] = results[k]
            # Match the BRAWLERS label by difflib ratio only — NOT the
            # substring shortcut in _fuzzy_match (a single 'a' is a
            # substring of "brawlers" and would match garbage). Require a
            # reasonably long token so we don't tap a stray letter.
            best = None
            for k, v in norm.items():
                if len(k) < 6:
                    continue
                ratio = difflib.SequenceMatcher(None, "brawlers", k).ratio()
                if ratio >= 0.6 and (best is None or ratio > best[0]):
                    best = (ratio, k, v)
            if best is None:
                log.debug("_open_brawler_menu: 'brawlers' not OCR'd; keys=%s",
                          list(norm.keys())[:20])
                return False
            match = best[1]
            bx, by = best[2]['center']
            real_x, real_y = int(bx * 1.5385), int(by * 1.5385)
            log.info("opening brawler menu via OCR %r at (%d,%d)", match, real_x, real_y)
            self.window_controller.click(real_x, real_y)
            time.sleep(1.5)
            return True
        except Exception:
            log.exception("_open_brawler_menu failed")
            return False

    def _select_brawler_once(self, brawler):
        # Open the brawler-selection menu. Prefer OCR (robust to layout
        # and capture scale); fall back to the template only down to 0.6
        # — below that it false-matches the trophy counter and opens the
        # Trophy Road instead.
        if not self._open_brawler_menu():
            brawler_menu_treshold = 0.8
            brawler_menu_btn_coords = None
            while brawler_menu_treshold >= 0.6:
                brawler_menu_btn_coords = find_template_center(
                    self.window_controller.screenshot(),
                    load_image(r'state_finder/images_to_detect/brawler_menu_btn.png',
                               self.window_controller.scale_factor),
                    brawler_menu_treshold)
                if brawler_menu_btn_coords:
                    break
                log.debug("brawler menu button not found at threshold=%.2f", brawler_menu_treshold)
                brawler_menu_treshold -= 0.1
                time.sleep(1)
            if not brawler_menu_btn_coords:
                raise ValueError("Brawler menu button not found (OCR + template ≥0.6).")
            x, y = brawler_menu_btn_coords
            log.debug("clicking brawler menu button (template) at (%d,%d)", x, y)
            self.window_controller.click(x, y)
            time.sleep(1.2)  # menu open animation
        wr = self.window_controller.width_ratio
        hr = self.window_controller.height_ratio

        # Two scroll passes: forward (down through the list) then reverse
        # (back up) in case we scrolled past the target. Big swipes cover
        # the whole grid in few steps.
        for phase, swipes in enumerate([20, 16]):
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
                # Game is in French → try the localized name FIRST (the card
                # shows e.g. "Bartaba" for barley), then fall back to the
                # English name from brawlace (most brawlers are identical).
                # Try the French-name candidate(s) FIRST (the card shows the
                # localized name), then fall back to the English name.
                match_key = None
                for alias in _fr_aliases(brawler):
                    match_key = self._fuzzy_match(alias, reworked_results.keys())
                    if match_key is not None:
                        break
                if match_key is None:
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
                # Scroll the grid with a LARGE swipe in the centre column.
                # The old swipe was tiny (~50px) and over the right-hand
                # cards, so it registered as a TAP and opened a brawler's
                # detail page instead of scrolling.
                cx = int(960 * wr)
                if phase == 0:                      # scroll down the list
                    sy, ey = int(800 * hr), int(300 * hr)
                else:                               # scroll back up
                    sy, ey = int(300 * hr), int(800 * hr)
                self.window_controller.swipe(cx, sy, cx, ey, duration=0.5)
                time.sleep(0.8)

        # Not found anywhere after a full scan — the OCR can't read this name.
        # Fail FAST (no retry): retrying the same deterministic scan won't help.
        raise BrawlerNotFoundError(f"Brawler '{brawler}' not found in menu OCR.")

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