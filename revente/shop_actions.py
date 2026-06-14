"""Perform brawler-detail upgrade actions on a live Brawl Stars device.

Two capabilities: unlock hypercharges on maxed (P11) brawlers that don't
have one yet, and raise a brawler's power level. Standalone & serial-based
(adb only).

Navigation (LIVE-VERIFIED 2026-06-14):
- The BRAWLERS button opens the TROPHÉES road, NOT the stats card →
  the old carousel strategy (reused from read_hypercharges) is WRONG.
- The correct path: lobby → tap the centre brawler (~0.52, 0.44) → opens
  the COLLECTION GRID ("BRAWLERS (n/103)").  Then tap each tile → opens
  that brawler's POUVOIR/stats card.  BACK returns to the grid.
- The POUVOIR card has NO horizontal carousel: swiping does not advance.

Design notes (calibrated on real Mi9T detail fixtures):
- "Maxed" (power 11) = ABSENCE of the green AMÉLIORER button (OCR-free).
- Buttons are located by the centroid of the largest green blob in a region.
- HC flame (magenta) reused from read_hypercharges; only meaningful on maxed.

SAFETY: dry_run defaults to True. Live execution (real gold spend) requires
confirm=True and goes through the dedicated _spend_tap seam only.
"""
from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass, field

from revente.read_hypercharges import (
    _screencap, _geom_for,
    _portrait_hash, _detail_has_hypercharge, _img_mean_diff,
)

log = logging.getLogger("shop_actions")

# ---------------------------------------------------------------------------
# Geometry (ratios, landscape frame)
# ---------------------------------------------------------------------------
# Bottom-right green AMÉLIORER button area (power upgrade). Calibrated on Mi9T.
UPGRADE_REGION = (0.74, 1.00, 0.80, 1.00)
# Hypercharge slot tap (top-right ability cluster). CALIBRATE LIVE.
HC_SLOT_TAP = (0.945, 0.29)
# Purchase-confirm dialog green button region. CALIBRATE LIVE.
CONFIRM_REGION = (0.38, 0.82, 0.50, 0.92)

# OpenCV HSV for the bright in-game green of action buttons.
GREEN_HSV_LO = (35, 80, 80)
GREEN_HSV_HI = (90, 255, 255)
GREEN_MIN_PX = 1200
HC_COST_DEFAULT = 5000  # coins per hypercharge

# ---------------------------------------------------------------------------
# Grid navigation geometry — Mi9T 2340×1080 landscape, current BS UI.
# LIVE-CALIBRATED 2026-06-14 (tune if UI or resolution differs).
# ---------------------------------------------------------------------------
BS_PKG = "com.supercell.brawlstars"
GRID_OPEN_TAP  = (0.52, 0.44)           # lobby centre brawler → collection grid
GRID_COLS      = (0.19, 0.50, 0.80)     # 3 column centres (tap the portrait)
GRID_ROWS      = (0.27, 0.61)           # two fully-visible tile rows
GRID_SCROLL    = (0.52, 0.80, 0.52, 0.34, 500)  # x0,y0,x1,y1,ms — swipe up one page
GRID_BACK_ARROW = (0.025, 0.05)         # (unused; card→grid uses the BACK key)
MAX_GRID_PAGES = 12


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class UpgradeButton:
    xr: float
    yr: float
    powerpoint_cost: "int | None" = None
    coin_cost: "int | None" = None


@dataclass
class Action:
    kind: str   # "buy_hypercharge" | "upgrade_power"
    coin_cost: int
    powerpoint_cost: int = 0
    note: str = ""


@dataclass
class ActionResult:
    action: Action
    executed: bool
    verified: bool
    error: "str | None" = None


@dataclass
class Report:
    dry_run: bool
    coins_before: "int | None" = None
    coins_after: "int | None" = None
    planned: list[Action] = field(default_factory=list)
    results: list[ActionResult] = field(default_factory=list)
    summary: str = ""

    def as_dict(self) -> dict:
        return {
            "dry_run": self.dry_run,
            "coins_before": self.coins_before,
            "coins_after": self.coins_after,
            "planned": [a.__dict__ for a in self.planned],
            "results": [{"action": r.action.__dict__, "executed": r.executed,
                         "verified": r.verified, "error": r.error}
                        for r in self.results],
            "summary": self.summary,
        }


# ---------------------------------------------------------------------------
# Detection helpers (KEEP UNCHANGED — proven on real fixtures)
# ---------------------------------------------------------------------------

def _crop_region(pil_image, w: int, h: int, region):
    x0, x1, y0, y1 = region
    return pil_image.crop((int(w * x0), int(h * y0), int(w * x1), int(h * y1)))


def _green_count(pil_crop) -> int:
    import numpy as np
    import cv2
    hsv = cv2.cvtColor(np.array(pil_crop), cv2.COLOR_RGB2HSV)
    mask = cv2.inRange(hsv, np.array(GREEN_HSV_LO), np.array(GREEN_HSV_HI))
    return int(mask.sum() // 255)


def _find_green_button_center(pil_image, w: int, h: int, region):
    """Centroid (xr, yr) of the largest green blob in `region`, or None."""
    import numpy as np
    import cv2
    x0, x1, y0, y1 = region
    crop = _crop_region(pil_image, w, h, region)
    hsv = cv2.cvtColor(np.array(crop), cv2.COLOR_RGB2HSV)
    mask = cv2.inRange(hsv, np.array(GREEN_HSV_LO), np.array(GREEN_HSV_HI))
    if int(mask.sum() // 255) < GREEN_MIN_PX:
        return None
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return None
    c = max(cnts, key=cv2.contourArea)
    m = cv2.moments(c)
    if m["m00"] == 0:
        return None
    cx = m["m10"] / m["m00"]
    cy = m["m01"] / m["m00"]
    cw = crop.size[0]
    ch = crop.size[1]
    xr = x0 + (cx / cw) * (x1 - x0)
    yr = y0 + (cy / ch) * (y1 - y0)
    return (xr, yr)


def detect_power_upgrade(pil_image, w: int, h: int):
    """Return an UpgradeButton if the green AMÉLIORER button is present, else None."""
    center = _find_green_button_center(pil_image, w, h, UPGRADE_REGION)
    if center is None:
        return None
    pp, coin = _ocr_costs(pil_image, w, h)
    return UpgradeButton(xr=center[0], yr=center[1], powerpoint_cost=pp, coin_cost=coin)


def _ocr_costs(pil_image, w: int, h: int):
    """Best-effort OCR of the two cost numbers. Returns (pp_cost, coin_cost)."""
    try:
        from revente.read_currencies import _ocr_digits
        crop = _crop_region(pil_image, w, h, (0.78, 1.00, 0.93, 1.00))
        import re
        nums = re.findall(r"\d+", _ocr_digits(crop))
        ints = [int(n) for n in nums if n]
        if len(ints) >= 2:
            return ints[0], ints[1]
        if len(ints) == 1:
            return None, ints[0]
    except Exception:
        log.debug("cost OCR failed", exc_info=True)
    return None, None


def is_maxed(pil_image, w: int, h: int) -> bool:
    """Power 11 ⟺ no green pixels in the upgrade-button zone."""
    crop = _crop_region(pil_image, w, h, UPGRADE_REGION)
    return _green_count(crop) < GREEN_MIN_PX


def hc_buy_eligible(pil_image, w: int, h: int) -> bool:
    """True if this detail is a maxed brawler WITHOUT a hypercharge yet."""
    return is_maxed(pil_image, w, h) and not _detail_has_hypercharge(pil_image, w, h)


def plan_hypercharges(eligible: int, coins: int, *, hc_cost: int = HC_COST_DEFAULT,
                      coin_floor: int = 0, max_count: "int | None" = None) -> list:
    """Pure planner: how many HCs to buy given resources."""
    if hc_cost <= 0:
        return []
    spendable = max(0, coins - coin_floor)
    by_coins = spendable // hc_cost
    n = min(eligible, by_coins)
    if max_count is not None:
        n = min(n, max(0, max_count))
    return [Action(kind="buy_hypercharge", coin_cost=hc_cost,
                   note=f"hypercharge #{i + 1}") for i in range(int(n))]


def levels_to_target(power_now: int, target: int) -> int:
    """Number of +1 upgrades from power_now to reach target."""
    target = max(1, min(int(target), 11))
    return max(0, target - max(0, int(power_now)))


# ---------------------------------------------------------------------------
# SPEND seam (purchase/confirm taps only — dry-run must never call this)
# ---------------------------------------------------------------------------

def _spend_tap(serial: str, w: int, h: int, xr: float, yr: float) -> None:
    """DEDICATED seam for irreversible purchase/confirm taps.
    Kept separate from _nav_tap so dry-run is provably tap-free."""
    subprocess.run(["adb", "-s", serial, "shell", "input", "tap",
                    str(int(w * xr)), str(int(h * yr))], capture_output=True, timeout=10)


# ---------------------------------------------------------------------------
# Standalone navigation helpers (serial-based, no game_api import needed)
# ---------------------------------------------------------------------------

def _nav_tap(serial, w, h, xr, yr):
    """Navigation tap (distinct from _spend_tap — purchase-tap-free in dry-run)."""
    subprocess.run(["adb", "-s", serial, "shell", "input", "tap",
                    str(int(w * xr)), str(int(h * yr))],
                   capture_output=True, timeout=10)


def _nav_swipe(serial, w, h, x0, y0, x1, y1, ms=400):
    subprocess.run(["adb", "-s", serial, "shell", "input", "swipe",
                    str(int(w * x0)), str(int(h * y0)),
                    str(int(w * x1)), str(int(h * y1)),
                    str(ms)], capture_output=True, timeout=12)


def _nav_back(serial):
    """Android BACK keyevent — the reliable way back from a brawler card to the
    collection grid (the card's top-left counter is NOT a back button)."""
    subprocess.run(["adb", "-s", serial, "shell", "input", "keyevent", "4"],
                   capture_output=True, timeout=5)


def _adb_out(serial, *args, timeout=8):
    return subprocess.run(["adb", "-s", serial, "shell", *args],
                          capture_output=True, text=True, timeout=timeout).stdout or ""


def _screen_state(pil_image):
    """Classify the current screen via the project's state finder."""
    try:
        from state_finder.main import get_state
        return get_state(pil_image)
    except Exception:
        return "unknown"


def _ocr_map(pil_image):
    """{text: {center,...}} via shared OCR. Empty dict on failure (easyocr absent)."""
    try:
        import numpy as np
        from utils import extract_text_and_positions
        return extract_text_and_positions(np.array(pil_image))
    except Exception:
        return {}


def _is_locked(serial) -> bool:
    """True if screen off or keyguard up."""
    out = _adb_out(serial, "dumpsys", "window")
    disp = _adb_out(serial, "dumpsys", "display")
    return ("mScreenState=OFF" in disp) or ("mDreamingLockscreen=true" in out) or \
           ("KeyguardController" in out and "showing=true" in out)


def _unlock(serial):
    """Wake + swipe up + enter PIN. Mirrors game_api.exit_power_save."""
    if "mScreenState=OFF" in _adb_out(serial, "dumpsys", "display"):
        subprocess.run(["adb", "-s", serial, "shell", "input", "keyevent", "26"],
                       capture_output=True, timeout=5)
        time.sleep(0.6)
    subprocess.run(["adb", "-s", serial, "shell", "input", "swipe",
                    "540", "1500", "540", "500", "200"],
                   capture_output=True, timeout=5)
    time.sleep(0.6)
    try:
        from game_api import _load_lockscreen_pin
        pin = _load_lockscreen_pin()
    except Exception:
        pin = None
    if pin:
        subprocess.run(["adb", "-s", serial, "shell", "input", "text", pin],
                       capture_output=True, timeout=5)
        time.sleep(0.3)
        subprocess.run(["adb", "-s", serial, "shell", "input", "keyevent", "66"],
                       capture_output=True, timeout=5)
        time.sleep(0.6)


def _foreground_is_bs(serial) -> bool:
    return BS_PKG in _adb_out(serial, "dumpsys", "window")


def _launch_bs(serial):
    subprocess.run(["adb", "-s", serial, "shell", "monkey", "-p", BS_PKG,
                    "-c", "android.intent.category.LAUNCHER", "1"],
                   capture_output=True, timeout=10)


def _dismiss_team_invite(serial, w, h) -> bool:
    """Detect INVITATION D'ÉQUIPE popup and tap REFUSER."""
    m = _ocr_map(_screencap(serial))
    joined = " ".join(k.lower() for k in m.keys())
    has_invite = (
        ("invitation" in joined and ("equipe" in joined or "team" in joined or "equip" in joined))
        or ("refuser" in joined and "accepter" in joined)
        or ("decline" in joined and "accept" in joined)
    )
    if not has_invite:
        return False
    for k, v in m.items():
        if "refus" in k.lower() or "decline" in k.lower():
            cx, cy = v.get("center", [0, 0])
            if cx > 0 and cy > 0:
                _nav_tap(serial, w, h, cx / w, cy / h)
                return True
    _nav_tap(serial, w, h, 0.38, 0.76)
    return True


def _ensure_lobby(serial, max_attempts=15) -> bool:
    """Reach the lobby robustly (standalone, mirrors game_api.goto_lobby shotgun).
    Unlock if locked, relaunch BS if not foreground, dismiss invites/popups."""
    for i in range(max_attempts):
        if _is_locked(serial):
            _unlock(serial)
            time.sleep(2.0)
            continue
        if not _foreground_is_bs(serial):
            _launch_bs(serial)
            time.sleep(8)
            continue
        img = _screencap(serial)
        w, h = img.size
        st = _screen_state(img)
        if st == "lobby":
            if _dismiss_team_invite(serial, w, h):
                time.sleep(1.0)
                continue
            return True
        if _dismiss_team_invite(serial, w, h):
            time.sleep(1.0)
            continue
        # Shotgun dismiss (mirrors goto_lobby): centre tap, continue spots, X, BACK
        for (xr, yr) in [(0.5, 0.5), (0.92, 0.94), (0.5, 0.93), (0.96, 0.05)]:
            _nav_tap(serial, w, h, xr, yr)
            time.sleep(0.3)
        subprocess.run(["adb", "-s", serial, "shell", "input", "keyevent", "4"],
                       capture_output=True, timeout=5)
        time.sleep(1.2)
    return False


# ---------------------------------------------------------------------------
# Grid navigation helpers
# ---------------------------------------------------------------------------

def _on_grid(pil_image, w, h) -> bool:
    """True if the collection grid is showing (header contains BRAWLERS/103)."""
    try:
        from revente.read_hypercharges import _grid_header_text, _looks_like_grid
        return _looks_like_grid(_grid_header_text(pil_image, w, h))
    except Exception:
        return False


def _open_grid(serial, w, h) -> bool:
    """From the lobby, open the collection grid (tap the centre brawler)."""
    for _ in range(3):
        img = _screencap(serial)
        if _on_grid(img, w, h):
            return True
        _nav_tap(serial, w, h, *GRID_OPEN_TAP)
        time.sleep(2.2)
    return _on_grid(_screencap(serial), w, h)


def _ensure_grid(serial, w, h, max_attempts=20) -> bool:
    """Reach the collection grid from ANY state. Anchored on the reliable _on_grid
    OCR (the project's state_finder mis-classifies this account's lobby/grid as
    'match'). Unlocks, relaunches BS if backgrounded, dismisses team invites, and
    hard-relaunches to a clean lobby as a fallback when stuck."""
    stuck = 0
    for _ in range(max_attempts):
        if _is_locked(serial):
            _unlock(serial)
            time.sleep(2.5)
            continue
        if not _foreground_is_bs(serial):
            _launch_bs(serial)
            time.sleep(10)
            continue
        if _on_grid(_screencap(serial), w, h):
            return True
        if _dismiss_team_invite(serial, w, h):
            time.sleep(1.0)
            continue
        stuck += 1
        if stuck % 6 == 0:
            # Hard reset to a clean lobby (clears unknown popups/menus).
            subprocess.run(["adb", "-s", serial, "shell", "am", "force-stop", BS_PKG],
                           capture_output=True, timeout=6)
            time.sleep(2)
            _launch_bs(serial)
            time.sleep(40)
            continue
        # From the lobby the centre-brawler tap opens the grid; from a card/menu
        # the BACK key steps toward it. Alternate so we converge from either side.
        if stuck % 2 == 1:
            _nav_tap(serial, w, h, *GRID_OPEN_TAP)
            time.sleep(1.8)
        else:
            _nav_back(serial)
            time.sleep(1.4)
    return _on_grid(_screencap(serial), w, h)


def _back_to_grid(serial, w, h) -> bool:
    """From a brawler card, return to the grid via the BACK key (dismiss popups)."""
    for _ in range(4):
        img = _screencap(serial)
        if _on_grid(img, w, h):
            return True
        if _dismiss_team_invite(serial, w, h):
            time.sleep(1.0)
            continue
        _nav_back(serial)
        time.sleep(1.4)
    return _on_grid(_screencap(serial), w, h)


def _scroll_grid(serial, w, h):
    x0, y0, x1, y1, ms = GRID_SCROLL
    _nav_swipe(serial, w, h, x0, y0, x1, y1, ms)


def _scroll_to_top(serial, w, h, max_swipes=8):
    """Swipe the grid DOWN until it stops moving (top), so the walk starts from a
    consistent position (the grid re-opens at its last scroll otherwise)."""
    prev = _screencap(serial)
    for _ in range(max_swipes):
        _nav_swipe(serial, w, h, 0.52, 0.34, 0.52, 0.80, 500)  # reverse of scroll
        time.sleep(0.8)
        cur = _screencap(serial)
        if _img_mean_diff(prev, cur) < 4.0:
            break
        prev = cur


def _card_is_locked(pil_image, w, h) -> bool:
    """True if the open card is a LOCKED (unowned) brawler — it shows a DÉBLOQUER /
    UNLOCK button instead of stats. Locked brawlers can't have a hypercharge, so the
    walk skips them (they also read as a green 'button' = false non-maxed)."""
    joined = " ".join(k.lower() for k in _ocr_map(pil_image).keys())
    return any(t in joined for t in ("debloquer", "débloquer", "deblo", "unlock"))


# ---------------------------------------------------------------------------
# Grid walk — visits each brawler's POUVOIR card once
# ---------------------------------------------------------------------------

def _walk_cards(serial, w, h, visit):
    """Open the collection grid and visit each brawler's POUVOIR card exactly once.

    `visit(card_img)` is called for each unique brawler (deduped by portrait hash).
    Returns the number of unique cards visited. Re-opens the grid if lost.

    Grid geometry constants (LIVE-CALIBRATED 2026-06-14) are module-level;
    tune GRID_COLS / GRID_ROWS / GRID_SCROLL if the UI or resolution changes.
    """
    # Bound the walk to OWNED brawlers (the grid header reads "n/103"); locked
    # brawlers come after and would open unlock screens, not POUVOIR cards.
    owned = _grid_owned_count(serial, w, h)
    cap = (owned + 2) if owned else 48
    if not _ensure_grid(serial, w, h):
        return 0
    _scroll_to_top(serial, w, h)  # start from a consistent grid position
    seen: set = set()
    empty_pages = 0
    for _page in range(MAX_GRID_PAGES):
        new_here = 0
        for yr in GRID_ROWS:
            for xr in GRID_COLS:
                # PRE-TAP guard: must be on the grid before tapping a tile, else a
                # tap lands on a card/menu and the walk drifts into garbage screens.
                # (This guard is essential — removing it broke the walk live.)
                if not _on_grid(_screencap(serial), w, h):
                    if not _ensure_grid(serial, w, h):
                        return len(seen)
                _nav_tap(serial, w, h, xr, yr)
                time.sleep(1.8)
                card = _screencap(serial)
                # The reliable "did a real POUVOIR card open?" signal is OCR: a card
                # has no grid header. (cv2 image-diff proved unreliable here — it let
                # grid-scroll transitions through as false cards.)
                if _on_grid(card, w, h):
                    continue  # empty cell / nothing opened — still on the grid
                if _card_is_locked(card, w, h):
                    # LOCKED (unowned) brawler — can't have a hypercharge. Skip it
                    # (don't count, don't classify; it would read as a false non-maxed).
                    _back_to_grid(serial, w, h)
                    continue
                ph = _portrait_hash(card, w, h)
                if ph not in seen:
                    seen.add(ph)
                    new_here += 1
                    visit(card)
                if not _back_to_grid(serial, w, h):
                    if not _ensure_grid(serial, w, h):
                        return len(seen)
                if len(seen) >= cap:
                    return len(seen)
        if new_here == 0:
            empty_pages += 1
            if empty_pages >= 2:
                break
        else:
            empty_pages = 0
        _scroll_grid(serial, w, h)
        time.sleep(1.4)
    return len(seen)


def _grid_owned_count(serial, w, h):
    """Best-effort count of OWNED brawlers from the grid header 'BRAWLERS (n/103)'.
    Returns the int n, or None if unreadable. Used to bound the walk."""
    try:
        import re
        from revente.read_hypercharges import _grid_header_text
        txt = _grid_header_text(_screencap(serial), w, h)
        m = re.search(r"(\d+)\s*/\s*\d{2,3}", txt.replace(" ", ""))
        if m:
            n = int(m.group(1))
            return n if 0 < n <= 110 else None
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Hypercharge-tab guard
# ---------------------------------------------------------------------------

def _on_hypercharge_tab(pil_image, w, h) -> bool:
    """True only if the open ability panel is the HYPERCHARGE tab.
    Prevents buying a star power by mistake. Best-effort (easyocr optional)."""
    joined = " ".join(k.lower() for k in _ocr_map(pil_image).keys())
    return "hypercharge" in joined


# ---------------------------------------------------------------------------
# Coin reader + misc
# ---------------------------------------------------------------------------

def _read_coins(serial: str) -> "int | None":
    """Read current coins/gold from the lobby top bar (best-effort)."""
    try:
        from revente.read_currencies import read_lobby_numbers
        nums = read_lobby_numbers(serial)
        return nums.get("gold")
    except Exception:
        log.debug("coin read failed", exc_info=True)
        return None


def _read_power_level(pil_image, w: int, h: int):
    """Best-effort OCR of the brawler power-level badge. CALIBRATE LIVE."""
    try:
        from revente.read_hypercharges import _ocr_text, _parse_power
        crop = _crop_region(pil_image, w, h, (0.74, 0.86, 0.30, 0.42))
        return _parse_power(_ocr_text(crop))
    except Exception:
        return None


def _confirm_hc_applied(_engine, serial: str, w: int, h: int) -> bool:
    """After a buy, re-read the detail: HC owned ⟺ magenta flame present."""
    time.sleep(1.2)
    img = _screencap(serial)
    return _detail_has_hypercharge(img, w, h)


# ---------------------------------------------------------------------------
# ShopActionEngine
# ---------------------------------------------------------------------------

class ShopActionEngine:
    def __init__(self, serial: str, *, dry_run: bool = True,
                 hc_cost: int = HC_COST_DEFAULT):
        self.serial = serial
        self.dry_run = dry_run
        self.hc_cost = hc_cost

    def _cap(self):
        return _screencap(self.serial)

    def _tap_confirm(self, w: int, h: int) -> bool:
        """Locate and tap the green confirm button in a purchase dialog."""
        img = _screencap(self.serial)
        center = _find_green_button_center(img, w, h, CONFIRM_REGION)
        if center is None:
            return False
        _spend_tap(self.serial, w, h, *center)
        time.sleep(1.0)
        return True

    # -- public actions ----------------------------------------------------

    def buy_hypercharges(self, *, max_count: "int | None" = None,
                         coin_floor: int = 0, confirm: bool = False) -> Report:
        """Walk every brawler via the collection GRID, buy HC on eligible ones.

        Navigation (LIVE-VERIFIED 2026-06-14):
        - Lobby → tap centre brawler → collection grid → tap each tile →
          brawler POUVOIR card → BACK to grid for the next tile.
        - The carousel strategy (old) is wrong for the current BS UI.

        Hypercharge-tab GUARD: before each live buy, the ability panel is
        inspected via OCR to confirm it shows the HYPERCHARGE tab (not a star
        power or gadget tab). If the check fails the buy is skipped
        (executed=False) with an explicit error. Note that HC_SLOT_TAP /
        the ability-tab geometry still needs a supervised live calibration pass.
        """
        live = (not self.dry_run) and confirm
        rep = Report(dry_run=not live)
        try:
            rep.coins_before = _read_coins(self.serial)
            coins = rep.coins_before if rep.coins_before is not None else 0

            probe = self._cap()
            w, h = probe.size

            if not _ensure_lobby(self.serial):
                rep.summary = "could not reach the lobby"
                return rep

            if live:
                log.warning(
                    "HC_SLOT_TAP=%s and ability-tab geometry are uncalibrated — "
                    "verify before live use. The hypercharge-tab guard will "
                    "abort if the wrong tab is open.", HC_SLOT_TAP)

            bought = 0

            def visit(card_img):
                nonlocal coins, bought
                if not hc_buy_eligible(card_img, w, h):
                    return
                # affordability + caps
                if (max_count is not None and bought >= max_count) or \
                        (coins - self.hc_cost) < coin_floor:
                    return  # eligible but unaffordable / cap reached
                act = Action(kind="buy_hypercharge", coin_cost=self.hc_cost,
                             note=f"maxed brawler (grid walk)")
                rep.planned.append(act)
                if not live:
                    rep.results.append(ActionResult(act, executed=False, verified=False))
                else:
                    # Tap the HC slot to open the purchase panel
                    _spend_tap(self.serial, w, h, *HC_SLOT_TAP)
                    time.sleep(1.0)
                    # Hypercharge-tab guard: confirm correct tab before spending
                    panel_img = _screencap(self.serial)
                    if not _on_hypercharge_tab(panel_img, w, h):
                        rep.results.append(ActionResult(
                            act, executed=False, verified=False,
                            error="not on hypercharge tab — aborted to avoid wrong purchase"))
                        return
                    confirmed = self._tap_confirm(w, h)
                    verified = confirmed and _confirm_hc_applied(self, self.serial, w, h)
                    rep.results.append(ActionResult(
                        act, executed=True, verified=verified,
                        error=None if confirmed else "no confirm button found"))
                coins -= self.hc_cost
                bought += 1

            _walk_cards(self.serial, w, h, visit)

            rep.coins_after = _read_coins(self.serial) if live else rep.coins_before
            n_plan = len([a for a in rep.planned if a.kind == "buy_hypercharge"])
            rep.summary = (f"{'planned' if not live else 'bought'} {n_plan} hypercharge(s); "
                           f"coins {rep.coins_before}→{rep.coins_after}")
        except Exception as exc:
            log.exception("buy_hypercharges aborted")
            rep.summary = f"aborted: {exc}"
        return rep

    def upgrade_power(self, *, target_level: int = 11, scope: str = "current",
                      confirm: bool = False, max_steps: int = 11,
                      max_brawlers: int = 1,
                      current_power: "int | None" = None) -> Report:
        """Raise power level(s). scope='current' acts on the open detail.
        scope='walk' walks the collection grid (LIVE-VERIFIED nav 2026-06-14)."""
        live = (not self.dry_run) and confirm
        rep = Report(dry_run=not live)
        try:
            rep.coins_before = _read_coins(self.serial)

            if live and target_level < 11 and current_power is None:
                rep.summary = ("refused: live upgrade to target_level<11 needs current_power "
                               "(power-level OCR region is uncalibrated)")
                return rep

            probe = self._cap()
            w, h = probe.size

            # C1: compute step budget
            if current_power is not None:
                step_budget = min(max_steps, levels_to_target(current_power, target_level))
            else:
                step_budget = max_steps
                if target_level < 11:
                    log.warning(
                        "upgrade_power: target_level=%s<11 with unknown current power; "
                        "relying on per-step OCR + max_steps backstop", target_level)

            if scope == "walk":
                if not _ensure_lobby(self.serial):
                    rep.summary = "could not reach the lobby"
                    return rep
                visited = 0

                def visit_upgrade(card_img):
                    nonlocal visited
                    if max_brawlers > 0 and visited >= max_brawlers:
                        return
                    self._upgrade_current(rep, w, h, target_level, live, step_budget)
                    visited += 1

                _walk_cards(self.serial, w, h, visit_upgrade)
            else:
                # scope='current': act on the already-open card
                self._upgrade_current(rep, w, h, target_level, live, step_budget)

            rep.coins_after = _read_coins(self.serial) if live else rep.coins_before
            rep.summary = (f"{'planned' if not live else 'did'} "
                           f"{len(rep.planned)} power upgrade(s)")
        except Exception as exc:
            log.exception("upgrade_power aborted")
            rep.summary = f"aborted: {exc}"
        return rep

    def _upgrade_current(self, rep: Report, w: int, h: int, target_level: int,
                         live: bool, step_budget: int) -> None:
        for _ in range(step_budget):
            img = self._cap()
            pwr = _read_power_level(img, w, h)
            if pwr is not None and levels_to_target(pwr, target_level) == 0:
                break
            btn = detect_power_upgrade(img, w, h)
            if btn is None:
                break
            act = Action(kind="upgrade_power",
                         coin_cost=btn.coin_cost or 0,
                         powerpoint_cost=btn.powerpoint_cost or 0,
                         note="one power level")
            rep.planned.append(act)
            if not live:
                rep.results.append(ActionResult(act, executed=False, verified=False))
                break  # dry-run: one tick suffices (screen doesn't change)
            before = img
            _spend_tap(self.serial, w, h, btn.xr, btn.yr)
            time.sleep(0.8)
            confirmed = self._tap_confirm(w, h)
            after = self._cap()
            verified = confirmed and _img_mean_diff(before, after) >= 4.0
            rep.results.append(ActionResult(
                act, executed=True, verified=verified,
                error=None if confirmed else "no confirm button found"))
            if not verified:
                break


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser():
    import argparse
    p = argparse.ArgumentParser(description="Brawl Stars shop/upgrade actions")
    p.add_argument("--serial", help="adb serial (default: device.adb_serial())")
    grp = p.add_mutually_exclusive_group()
    grp.add_argument("--plan", dest="action", action="store_const", const="plan",
                     help="dry-run: enumerate eligible hypercharges (default)")
    grp.add_argument("--buy-hc", dest="action", action="store_const", const="buy-hc",
                     help="buy hypercharges on maxed brawlers (needs --confirm)")
    grp.add_argument("--upgrade", dest="action", action="store_const", const="upgrade",
                     help="upgrade power (needs --confirm)")
    p.set_defaults(action="plan")
    p.add_argument("--confirm", action="store_true", help="actually spend (live)")
    p.add_argument("--max-count", type=int, default=None)
    p.add_argument("--coin-floor", type=int, default=0)
    p.add_argument("--target-level", type=int, default=11)
    p.add_argument("--scope", choices=("current", "walk"), default="current")
    return p


def main(argv=None) -> int:
    import json
    ns = _build_parser().parse_args(argv)
    serial = ns.serial
    if not serial:
        import device
        serial = device.adb_serial()
    dry = (ns.action == "plan") or (not ns.confirm)
    eng = ShopActionEngine(serial, dry_run=dry)
    if ns.action == "upgrade":
        rep = eng.upgrade_power(target_level=ns.target_level, scope=ns.scope,
                                confirm=ns.confirm)
    else:
        rep = eng.buy_hypercharges(max_count=ns.max_count, coin_floor=ns.coin_floor,
                                   confirm=ns.confirm)
    print(json.dumps(rep.as_dict(), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
