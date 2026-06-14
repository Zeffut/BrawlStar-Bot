"""Perform brawler-detail upgrade actions on a live Brawl Stars device.

Two capabilities (v1): unlock hypercharges on maxed (P11) brawlers that don't
have one yet, and raise a brawler's power level. Standalone & serial-based
(adb only), mirroring revente/read_hypercharges.py — it REUSES that module's
detail-navigation primitives.

Design notes (calibrated on real Mi9T detail fixtures):
- "Maxed" (power 11) is detected by the ABSENCE of the green AMÉLIORER button
  in the bottom-right (OCR-free). A non-maxed detail shows a big green upgrade
  button there; a maxed one shows the yellow "NIVEAU MAX !" label (no green).
- Buttons are located by the centroid of the largest green blob in a region.
- The hypercharge flame (magenta) is reused from read_hypercharges; HC is only
  meaningful on a maxed detail (a non-maxed detail shows purple power circles
  that also fire magenta — excluded by the maxed gate).

SAFETY: dry_run defaults to True everywhere. Live execution (real taps that
SPEND in-game gold, irreversible) requires confirm=True and goes through the
dedicated _spend_tap seam, so dry-run is provably tap-free.
"""
from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass, field

from revente.read_hypercharges import (
    _enter_detail, _swipe_carousel_next, _screencap, _geom_for,
    _portrait_hash, _detail_has_hypercharge, _img_mean_diff,
)

log = logging.getLogger("shop_actions")

# --- geometry (ratios x0,x1,y0,y1 of the landscape frame) ---
# Bottom-right green AMÉLIORER button area (power upgrade). Calibrated on Mi9T.
UPGRADE_REGION = (0.74, 1.00, 0.80, 1.00)
# Hypercharge slot to tap to buy (top-right ability cluster); CALIBRATE LIVE.
HC_SLOT_TAP = (0.945, 0.29)
# Where a purchase-confirm dialog's green button appears; CALIBRATE LIVE.
CONFIRM_REGION = (0.38, 0.82, 0.50, 0.92)

# OpenCV HSV (H 0-180) for the bright in-game green of action buttons.
GREEN_HSV_LO = (35, 80, 80)
GREEN_HSV_HI = (90, 255, 255)
GREEN_MIN_PX = 1200          # calibrated between maxed (~0) and a real button
HC_COST_DEFAULT = 5000       # coins per hypercharge (mirrors sale_report.HC_COST)
MAX_CAROUSEL = 130


@dataclass
class UpgradeButton:
    xr: float
    yr: float
    powerpoint_cost: "int | None" = None
    coin_cost: "int | None" = None


@dataclass
class Action:
    kind: str                # "buy_hypercharge" | "upgrade_power"
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
    planned: list = field(default_factory=list)
    results: list = field(default_factory=list)
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
    """Centroid (xr, yr) of the largest green blob in `region`, or None if the
    green area is below GREEN_MIN_PX. Coordinates are full-frame ratios."""
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
    """Return an UpgradeButton if the green AMÉLIORER button is present (power<11),
    else None. The button centre is the green-blob centroid; costs are best-effort."""
    center = _find_green_button_center(pil_image, w, h, UPGRADE_REGION)
    if center is None:
        return None
    pp, coin = _ocr_costs(pil_image, w, h)
    return UpgradeButton(xr=center[0], yr=center[1], powerpoint_cost=pp, coin_cost=coin)


def _ocr_costs(pil_image, w: int, h: int):
    """Best-effort OCR of the two cost numbers next to AMÉLIORER. Returns
    (powerpoint_cost, coin_cost) or (None, None). Never raises (easyocr optional)."""
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
    """Power 11 ⟺ no green pixels in the upgrade-button zone (OCR-free).
    Uses _green_count directly so it is not affected by a monkeypatch of
    _find_green_button_center (which is reserved for the confirm-dialog seam)."""
    crop = _crop_region(pil_image, w, h, UPGRADE_REGION)
    return _green_count(crop) < GREEN_MIN_PX


def hc_buy_eligible(pil_image, w: int, h: int) -> bool:
    """True if this detail is a maxed brawler WITHOUT a hypercharge yet.
    maxed = no green upgrade button; HC owned = magenta flame in the slot."""
    return is_maxed(pil_image, w, h) and not _detail_has_hypercharge(pil_image, w, h)


def plan_hypercharges(eligible: int, coins: int, *, hc_cost: int = HC_COST_DEFAULT,
                      coin_floor: int = 0, max_count: "int | None" = None) -> list:
    """How many hypercharges to buy: bounded by eligible brawlers, by the coins
    spendable above `coin_floor`, and by `max_count`. Pure / deterministic."""
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
    """Number of +1 upgrades from power_now to reach target (target clamped 1..11)."""
    target = max(1, min(int(target), 11))
    return max(0, target - max(0, int(power_now)))


def _spend_tap(serial: str, w: int, h: int, xr: float, yr: float) -> None:
    """DEDICATED seam for purchase/confirm taps (irreversible spend). Kept separate
    from navigation taps so dry-run is provably tap-free (tests spy on this)."""
    subprocess.run(["adb", "-s", serial, "shell", "input", "tap",
                    str(int(w * xr)), str(int(h * yr))], capture_output=True, timeout=10)


def _read_coins(serial: str) -> "int | None":
    """Read current coins/gold from the lobby top bar (best-effort)."""
    try:
        from revente.read_currencies import read_lobby_numbers
        nums = read_lobby_numbers(serial)
        return nums.get("gold")
    except Exception:
        log.debug("coin read failed", exc_info=True)
        return None


def _confirm_hc_applied(engine, serial: str, w: int, h: int) -> bool:
    """After a buy, re-read the detail: HC owned ⟺ magenta flame present.
    Module-level so tests can monkeypatch via S._confirm_hc_applied.
    `engine` is passed but unused (reserved for future subclass override)."""
    time.sleep(1.2)
    img = _screencap(serial)
    return _detail_has_hypercharge(img, w, h)


class ShopActionEngine:
    def __init__(self, serial: str, *, dry_run: bool = True,
                 hc_cost: int = HC_COST_DEFAULT):
        self.serial = serial
        self.dry_run = dry_run
        self.hc_cost = hc_cost

    # -- helpers --------------------------------------------------------
    def _cap(self):
        return _screencap(self.serial)

    def _tap_confirm(self, w: int, h: int) -> bool:
        """Locate and tap the green confirm button in a purchase dialog.
        Returns False if no confirm button is found."""
        img = _screencap(self.serial)
        center = _find_green_button_center(img, w, h, CONFIRM_REGION)
        if center is None:
            return False
        _spend_tap(self.serial, w, h, *center)
        time.sleep(1.0)
        return True

    # -- public actions -------------------------------------------------
    def buy_hypercharges(self, *, max_count: "int | None" = None,
                         coin_floor: int = 0, confirm: bool = False) -> Report:
        live = (not self.dry_run) and confirm
        rep = Report(dry_run=not live)
        rep.coins_before = _read_coins(self.serial)
        coins = rep.coins_before if rep.coins_before is not None else 0

        probe = self._cap()
        w, h = probe.size
        g = _geom_for(w, h)
        if not _enter_detail(self.serial, w, h, g):
            rep.summary = "could not reach a brawler detail screen"
            return rep

        seen: set = set()
        dup_streak = 0
        bought = 0
        for _ in range(MAX_CAROUSEL):
            img = self._cap()
            ph = _portrait_hash(img, w, h)
            if ph in seen:
                dup_streak += 1
                if dup_streak >= 3:
                    break
                _swipe_carousel_next(self.serial, w, h, g)
                time.sleep(1.5)
                continue
            dup_streak = 0
            seen.add(ph)

            if hc_buy_eligible(img, w, h):
                # affordability + caps
                if (max_count is not None and bought >= max_count) or \
                        (coins - self.hc_cost) < coin_floor:
                    pass  # éligible mais non finançable / plafond atteint
                else:
                    act = Action(kind="buy_hypercharge", coin_cost=self.hc_cost,
                                 note=f"maxed brawler #{len(seen)}")
                    rep.planned.append(act)
                    if not live:
                        rep.results.append(ActionResult(act, executed=False,
                                                        verified=False))
                    else:
                        _spend_tap(self.serial, w, h, *HC_SLOT_TAP)
                        time.sleep(1.0)
                        confirmed = self._tap_confirm(w, h)
                        verified = confirmed and _confirm_hc_applied(
                            self, self.serial, w, h)
                        rep.results.append(ActionResult(
                            act, executed=True, verified=verified,
                            error=None if confirmed else "no confirm button found"))
                        coins -= self.hc_cost
                    bought += 1

            _swipe_carousel_next(self.serial, w, h, g)
            time.sleep(1.4)

        rep.coins_after = _read_coins(self.serial) if live else rep.coins_before
        n_plan = len([a for a in rep.planned if a.kind == "buy_hypercharge"])
        rep.summary = (f"{'planned' if not live else 'bought'} {n_plan} hypercharge(s); "
                       f"coins {rep.coins_before}→{rep.coins_after}")
        return rep

    def upgrade_power(self, *, target_level: int = 11, scope: str = "current",
                      confirm: bool = False, max_steps: int = 11,
                      max_brawlers: int = 1) -> Report:
        """Raise power level(s). scope='current' acts on the open detail; scope='walk'
        walks the carousel applying the target to each brawler (up to max_brawlers)."""
        live = (not self.dry_run) and confirm
        rep = Report(dry_run=not live)
        rep.coins_before = _read_coins(self.serial)
        probe = self._cap()
        w, h = probe.size
        g = _geom_for(w, h)
        if scope == "walk" and not _enter_detail(self.serial, w, h, g):
            rep.summary = "could not reach a brawler detail screen"
            return rep

        brawlers = max_brawlers if scope == "walk" else 1
        seen: set = set()
        for _bi in range(brawlers):
            if scope == "walk":
                ph = _portrait_hash(self._cap(), w, h)
                if ph in seen:
                    break
                seen.add(ph)
            self._upgrade_current(rep, w, h, target_level, live, max_steps)
            if scope == "walk":
                _swipe_carousel_next(self.serial, w, h, g)
                time.sleep(1.4)

        rep.coins_after = _read_coins(self.serial) if live else rep.coins_before
        rep.summary = (f"{'planned' if not live else 'did'} "
                       f"{len(rep.planned)} power upgrade(s)")
        return rep

    def _upgrade_current(self, rep: Report, w: int, h: int, target_level: int,
                         live: bool, max_steps: int) -> None:
        for _ in range(max_steps):
            img = self._cap()
            btn = detect_power_upgrade(img, w, h)
            if btn is None:
                break  # maxé / plus de bouton
            act = Action(kind="upgrade_power",
                         coin_cost=btn.coin_cost or 0,
                         powerpoint_cost=btn.powerpoint_cost or 0,
                         note="one power level")
            rep.planned.append(act)
            if not live:
                rep.results.append(ActionResult(act, executed=False, verified=False))
                break  # en dry-run, un seul tick suffit (l'écran ne change pas)
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
    else:  # plan | buy-hc
        rep = eng.buy_hypercharges(max_count=ns.max_count, coin_floor=ns.coin_floor,
                                   confirm=ns.confirm)
    print(json.dumps(rep.as_dict(), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
