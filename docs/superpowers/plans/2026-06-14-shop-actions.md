# Shop Actions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Donner au bot la capacité d'effectuer des actions d'amélioration sur les fiches brawler — débloquer des hypercharges sur les brawlers maxés sans HC, et monter le power level — avec un mode dry-run sûr par défaut et une exécution live gardée par `confirm`.

**Architecture:** Un module standalone `revente/shop_actions.py` (serial-based, adb-only, calqué sur `revente/read_hypercharges.py`) avec trois couches : **détection** (cv2, testée sur fixtures réelles), **planner** (pur), **exécuteur** `ShopActionEngine(serial, dry_run)`. La navigation des fiches (entrer fiche, carrousel) est réutilisée depuis `read_hypercharges`. Surface de commande via `worker_link.COMMANDS` + endpoints `cloud_panel/app.py`, calquée sur `session_start`.

**Tech Stack:** Python, OpenCV (cv2), PIL, easyocr (best-effort, importorskip dans les tests), adb shell input, pytest, FastAPI (cloud panel), websockets (worker link).

**Raffinement vs spec :** « maxé » est détecté par **absence du bouton vert AMÉLIORER** (OCR-free) plutôt que par OCR « NIVEAU MAX » ; les boutons sont localisés par **centroïde du plus gros blob vert** (cv2). Cela rend `hc_buy_eligible` et la détection du bouton d'upgrade testables sans easyocr.

---

## File Structure

- **Create** `revente/shop_actions.py` — détection + planner + `ShopActionEngine` + CLI. Responsabilité unique : exécuter des actions shop/upgrade sur le device.
- **Create** `tests/test_shop_actions.py` — tests détection (fixtures réelles), planner (purs), moteur (mockés).
- **Modify** `worker_link.py` — 3 handlers `_cmd_shop_*` + enregistrement dans `COMMANDS` (~ligne 796+).
- **Modify** `cloud_panel/app.py` — 3 endpoints `POST /api/accounts/{id}/shop/*` + un `BaseModel` (~ligne 1219+, après `api_account_start`).
- **Reuse (no edit)** `revente/read_hypercharges.py` : on importe les helpers privés `_enter_detail, _swipe_carousel_next, _screencap, _geom_for, _portrait_hash, _detail_has_hypercharge, _crop, _img_mean_diff` (couplage assumé et documenté — même projet, ces fonctions SONT les primitives de navigation fiche).

### Conventions de coordonnées
- Toutes les coordonnées de tap/région sont en **ratios [0..1]** du frame landscape (comme `read_hypercharges`).
- Une région = tuple `(x0, x1, y0, y1)` en ratios (même ordre que `_crop`).

### Fixtures disponibles (réelles, Mi9T 2340×1080)
- `tests/fixtures/hc/bull_detail_p1.png` — P1, **bouton vert AMÉLIORER** présent (coûts 20/20), magenta présent (faux positif HC, exclu par la garde maxé).
- `tests/fixtures/hc/shelly_detail.png` — P11 maxé, **pas** de bouton vert, **pas** de HC → **éligible HC**.
- `tests/fixtures/hc/maisie_detail.png` — P11 maxé, **pas** de bouton vert, HC possédée (magenta) → non éligible.

---

## Task 1: Détection — dataclasses + blob vert + bouton d'upgrade

**Files:**
- Create: `revente/shop_actions.py`
- Test: `tests/test_shop_actions.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_shop_actions.py
import pytest
from pathlib import Path

FIX = Path(__file__).parent / "fixtures" / "hc"


def _img(name):
    Image = pytest.importorskip("PIL.Image", reason="Pillow not installed")
    return Image.open(FIX / name).convert("RGB")


def test_detect_power_upgrade_present_on_nonmaxed():
    pytest.importorskip("cv2"); pytest.importorskip("numpy")
    from revente.shop_actions import detect_power_upgrade, UpgradeButton
    img = _img("bull_detail_p1.png")
    w, h = img.size
    btn = detect_power_upgrade(img, w, h)
    assert isinstance(btn, UpgradeButton)
    # centroïde du bouton vert dans le coin bas-droite
    assert 0.74 <= btn.xr <= 1.0, btn.xr
    assert 0.80 <= btn.yr <= 1.0, btn.yr


def test_detect_power_upgrade_absent_on_maxed():
    pytest.importorskip("cv2"); pytest.importorskip("numpy")
    from revente.shop_actions import detect_power_upgrade
    for name in ("shelly_detail.png", "maisie_detail.png"):
        img = _img(name)
        w, h = img.size
        assert detect_power_upgrade(img, w, h) is None, name


def test_green_separation_nonmaxed_vs_maxed():
    # Le compteur de vert dans la zone bouton sépare nettement P1 (gros bouton)
    # des maxés (≈0). C'est ce qui calibre GREEN_MIN_PX.
    pytest.importorskip("cv2"); pytest.importorskip("numpy")
    from revente.shop_actions import _green_count, _crop_region, UPGRADE_REGION
    bull = _img("bull_detail_p1.png"); bw, bh = bull.size
    shelly = _img("shelly_detail.png"); sw, sh = shelly.size
    g_bull = _green_count(_crop_region(bull, bw, bh, UPGRADE_REGION))
    g_shelly = _green_count(_crop_region(shelly, sw, sh, UPGRADE_REGION))
    assert g_bull > g_shelly * 3, (g_bull, g_shelly)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_shop_actions.py -v`
Expected: FAIL — `ModuleNotFoundError: revente.shop_actions`.

- [ ] **Step 3: Write minimal implementation**

```python
# revente/shop_actions.py
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
```

- [ ] **Step 4: Run test to verify it passes (calibrate GREEN_MIN_PX)**

Run: `pytest tests/test_shop_actions.py -v`
Expected: PASS. If `test_detect_power_upgrade_absent_on_maxed` fails, print the green counts to calibrate:
`python -c "from PIL import Image; from revente.shop_actions import _green_count,_crop_region,UPGRADE_REGION as R; [print(n, _green_count(_crop_region((i:=Image.open(f'tests/fixtures/hc/{n}').convert('RGB')), *i.size, R))) for n in ('bull_detail_p1.png','shelly_detail.png','maisie_detail.png')]"`
Set `GREEN_MIN_PX` strictly between the maxed values (low) and bull's value (high).

- [ ] **Step 5: Commit**

```bash
git add revente/shop_actions.py tests/test_shop_actions.py
git commit -m "feat(shop-actions): green-blob detection of the power-upgrade button + dataclasses"
```

---

## Task 2: Détection — éligibilité hypercharge

**Files:**
- Modify: `revente/shop_actions.py`
- Test: `tests/test_shop_actions.py`

- [ ] **Step 1: Write the failing test**

```python
def test_hc_buy_eligible():
    pytest.importorskip("cv2"); pytest.importorskip("numpy")
    from revente.shop_actions import hc_buy_eligible
    cases = {
        "shelly_detail.png": True,    # maxé (pas de bouton vert), pas de HC
        "maisie_detail.png": False,   # maxé mais HC déjà possédée
        "bull_detail_p1.png": False,  # pas maxé (bouton vert présent)
    }
    for name, expected in cases.items():
        img = _img(name); w, h = img.size
        assert hc_buy_eligible(img, w, h) is expected, name


def test_is_maxed_by_green_absence():
    pytest.importorskip("cv2"); pytest.importorskip("numpy")
    from revente.shop_actions import is_maxed
    assert is_maxed(*_wh("shelly_detail.png")) is True
    assert is_maxed(*_wh("maisie_detail.png")) is True
    assert is_maxed(*_wh("bull_detail_p1.png")) is False


def _wh(name):
    img = _img(name); w, h = img.size
    return img, w, h
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_shop_actions.py::test_hc_buy_eligible -v`
Expected: FAIL — `ImportError: cannot import name 'hc_buy_eligible'`.

- [ ] **Step 3: Write minimal implementation**

```python
# append to revente/shop_actions.py
def is_maxed(pil_image, w: int, h: int) -> bool:
    """Power 11 ⟺ no green AMÉLIORER button on the detail (OCR-free)."""
    return detect_power_upgrade(pil_image, w, h) is None


def hc_buy_eligible(pil_image, w: int, h: int) -> bool:
    """True if this detail is a maxed brawler WITHOUT a hypercharge yet.
    maxed = no green upgrade button; HC owned = magenta flame in the slot."""
    return is_maxed(pil_image, w, h) and not _detail_has_hypercharge(pil_image, w, h)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_shop_actions.py -v -k "eligible or maxed"`
Expected: PASS (3 cases each).

- [ ] **Step 5: Commit**

```bash
git add revente/shop_actions.py tests/test_shop_actions.py
git commit -m "feat(shop-actions): hc_buy_eligible (maxed-by-green-absence AND no HC flame)"
```

---

## Task 3: Planner — plan_hypercharges (pur)

**Files:**
- Modify: `revente/shop_actions.py`
- Test: `tests/test_shop_actions.py`

- [ ] **Step 1: Write the failing test**

```python
def test_plan_hypercharges_affordability():
    from revente.shop_actions import plan_hypercharges
    # 3 brawlers éligibles, 12000 coins, 5000/HC → 2 achats
    acts = plan_hypercharges(eligible=3, coins=12000, hc_cost=5000)
    assert len(acts) == 2
    assert all(a.kind == "buy_hypercharge" and a.coin_cost == 5000 for a in acts)


def test_plan_hypercharges_coin_floor():
    from revente.shop_actions import plan_hypercharges
    # garder >=3000 coins → on ne dépense que 9000 → 1 achat
    acts = plan_hypercharges(eligible=3, coins=12000, hc_cost=5000, coin_floor=3000)
    assert len(acts) == 1


def test_plan_hypercharges_max_count_cap():
    from revente.shop_actions import plan_hypercharges
    acts = plan_hypercharges(eligible=3, coins=100000, hc_cost=5000, max_count=1)
    assert len(acts) == 1


def test_plan_hypercharges_none_eligible_or_broke():
    from revente.shop_actions import plan_hypercharges
    assert plan_hypercharges(eligible=0, coins=99999) == []
    assert plan_hypercharges(eligible=5, coins=4999, hc_cost=5000) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_shop_actions.py -v -k plan_hypercharges`
Expected: FAIL — `ImportError: cannot import name 'plan_hypercharges'`.

- [ ] **Step 3: Write minimal implementation**

```python
# append to revente/shop_actions.py
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_shop_actions.py -v -k plan_hypercharges`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add revente/shop_actions.py tests/test_shop_actions.py
git commit -m "feat(shop-actions): pure plan_hypercharges (affordability + caps)"
```

---

## Task 4: Planner — levels_to_target (pur)

**Files:**
- Modify: `revente/shop_actions.py`
- Test: `tests/test_shop_actions.py`

- [ ] **Step 1: Write the failing test**

```python
def test_levels_to_target():
    from revente.shop_actions import levels_to_target
    assert levels_to_target(9, 11) == 2
    assert levels_to_target(11, 11) == 0
    assert levels_to_target(1, 99) == 10   # clamp cible à 11
    assert levels_to_target(5, 3) == 0     # cible déjà atteinte
    assert levels_to_target(0, 11) == 11   # garde-fou bas
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_shop_actions.py -v -k levels_to_target`
Expected: FAIL — `ImportError`.

- [ ] **Step 3: Write minimal implementation**

```python
# append to revente/shop_actions.py
def levels_to_target(power_now: int, target: int) -> int:
    """Number of +1 upgrades from power_now to reach target (target clamped 1..11)."""
    target = max(1, min(int(target), 11))
    return max(0, target - max(0, int(power_now)))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_shop_actions.py -v -k levels_to_target`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add revente/shop_actions.py tests/test_shop_actions.py
git commit -m "feat(shop-actions): pure levels_to_target helper"
```

---

## Task 5: Exécuteur — buy_hypercharges (dry-run + live, seam _spend_tap)

**Files:**
- Modify: `revente/shop_actions.py`
- Test: `tests/test_shop_actions.py`

- [ ] **Step 1: Write the failing test**

```python
def test_buy_hypercharges_dry_run_spends_nothing(monkeypatch):
    pytest.importorskip("cv2"); pytest.importorskip("numpy")
    import revente.shop_actions as S
    seq = ["shelly_detail.png", "maisie_detail.png", "bull_detail_p1.png",
           "shelly_detail.png"]  # le 4e répète le hash de Shelly → wrap
    it = iter(seq)
    last = {"name": seq[-1]}

    def fake_screencap(serial):
        try:
            last["name"] = next(it)
        except StopIteration:
            pass
        return _img(last["name"])

    monkeypatch.setattr(S, "_screencap", fake_screencap)
    monkeypatch.setattr(S, "_enter_detail", lambda *a, **k: True)
    monkeypatch.setattr(S, "_swipe_carousel_next", lambda *a, **k: None)
    monkeypatch.setattr(S, "_read_coins", lambda serial: 10000)
    spends = []
    monkeypatch.setattr(S, "_spend_tap", lambda *a, **k: spends.append(a))

    eng = S.ShopActionEngine("fake", dry_run=True, hc_cost=5000)
    report = eng.buy_hypercharges()
    assert report.dry_run is True
    assert spends == []                                  # AUCUNE dépense en dry-run
    assert len([a for a in report.planned
                if a.kind == "buy_hypercharge"]) == 1    # 1 seul éligible (Shelly)


def test_buy_hypercharges_live_taps_when_confirmed(monkeypatch):
    pytest.importorskip("cv2"); pytest.importorskip("numpy")
    import revente.shop_actions as S
    monkeypatch.setattr(S, "_enter_detail", lambda *a, **k: True)
    monkeypatch.setattr(S, "_swipe_carousel_next", lambda *a, **k: None)
    monkeypatch.setattr(S, "_read_coins", lambda serial: 10000)
    # une seule fiche éligible puis wrap immédiat
    frames = iter(["shelly_detail.png", "shelly_detail.png", "shelly_detail.png"])
    cur = {"n": "shelly_detail.png"}

    def fake_screencap(serial):
        try:
            cur["n"] = next(frames)
        except StopIteration:
            pass
        return _img(cur["n"])

    monkeypatch.setattr(S, "_screencap", fake_screencap)
    # confirm trouvé (centroïde vert factice) + vérif HC OK
    monkeypatch.setattr(S, "_find_green_button_center", lambda *a, **k: (0.6, 0.8))
    monkeypatch.setattr(S, "_confirm_hc_applied", lambda self, serial, w, h: True)
    spends = []
    monkeypatch.setattr(S, "_spend_tap", lambda *a, **k: spends.append(a))

    eng = S.ShopActionEngine("fake", dry_run=False, hc_cost=5000)
    report = eng.buy_hypercharges(confirm=True, max_count=1)
    assert report.dry_run is False
    assert len(spends) >= 1                               # au moins le tap d'achat
    assert any(r.executed for r in report.results)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_shop_actions.py -v -k buy_hypercharges`
Expected: FAIL — `AttributeError: ShopActionEngine` / `_spend_tap` / `_read_coins` undefined.

- [ ] **Step 3: Write minimal implementation**

```python
# append to revente/shop_actions.py

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


class ShopActionEngine:
    def __init__(self, serial: str, *, dry_run: bool = True,
                 hc_cost: int = HC_COST_DEFAULT):
        self.serial = serial
        self.dry_run = dry_run
        self.hc_cost = hc_cost

    # -- helpers --------------------------------------------------------
    def _cap(self):
        return _screencap(self.serial)

    def _confirm_hc_applied(self, serial: str, w: int, h: int) -> bool:
        """After a buy, re-read the detail: HC owned ⟺ magenta flame present."""
        time.sleep(1.2)
        img = _screencap(serial)
        return _detail_has_hypercharge(img, w, h)

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
                        verified = confirmed and self._confirm_hc_applied(
                            self.serial, w, h)
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_shop_actions.py -v -k buy_hypercharges`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add revente/shop_actions.py tests/test_shop_actions.py
git commit -m "feat(shop-actions): ShopActionEngine.buy_hypercharges (dry-run safe, live gated)"
```

---

## Task 6: Exécuteur — upgrade_power (scope current + walk)

**Files:**
- Modify: `revente/shop_actions.py`
- Test: `tests/test_shop_actions.py`

- [ ] **Step 1: Write the failing test**

```python
def test_upgrade_power_current_dry_run(monkeypatch):
    pytest.importorskip("cv2"); pytest.importorskip("numpy")
    import revente.shop_actions as S
    # Bull P1 : bouton vert présent → 1 action planifiée par tick jusqu'à cible.
    monkeypatch.setattr(S, "_screencap", lambda s: _img("bull_detail_p1.png"))
    monkeypatch.setattr(S, "_read_coins", lambda s: 100000)
    spends = []
    monkeypatch.setattr(S, "_spend_tap", lambda *a, **k: spends.append(a))
    eng = S.ShopActionEngine("fake", dry_run=True)
    rep = eng.upgrade_power(target_level=3, scope="current", max_steps=5)
    assert rep.dry_run is True
    assert spends == []
    assert len(rep.planned) >= 1
    assert all(a.kind == "upgrade_power" for a in rep.planned)


def test_upgrade_power_live_stops_when_no_button(monkeypatch):
    pytest.importorskip("cv2"); pytest.importorskip("numpy")
    import revente.shop_actions as S
    # 1er screen: bouton présent (Bull) ; après le tap+confirm: maxé (Shelly) → stop.
    frames = iter(["bull_detail_p1.png", "shelly_detail.png", "shelly_detail.png"])
    cur = {"n": "bull_detail_p1.png"}

    def fake_cap(s):
        try:
            cur["n"] = next(frames)
        except StopIteration:
            pass
        return _img(cur["n"])

    monkeypatch.setattr(S, "_screencap", fake_cap)
    monkeypatch.setattr(S, "_read_coins", lambda s: 100000)
    monkeypatch.setattr(S, "_find_green_button_center", lambda *a, **k: (0.6, 0.8))
    spends = []
    monkeypatch.setattr(S, "_spend_tap", lambda *a, **k: spends.append(a))
    eng = S.ShopActionEngine("fake", dry_run=False)
    rep = eng.upgrade_power(target_level=11, scope="current", confirm=True, max_steps=5)
    assert len(spends) >= 1
    assert any(r.executed for r in rep.results)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_shop_actions.py -v -k upgrade_power`
Expected: FAIL — `AttributeError: 'ShopActionEngine' object has no attribute 'upgrade_power'`.

- [ ] **Step 3: Write minimal implementation**

```python
# append inside class ShopActionEngine in revente/shop_actions.py
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_shop_actions.py -v -k upgrade_power`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add revente/shop_actions.py tests/test_shop_actions.py
git commit -m "feat(shop-actions): ShopActionEngine.upgrade_power (current + walk scopes)"
```

---

## Task 7: CLI (`python -m revente.shop_actions`)

**Files:**
- Modify: `revente/shop_actions.py`
- Test: `tests/test_shop_actions.py`

- [ ] **Step 1: Write the failing test**

```python
def test_cli_parser_defaults_to_plan():
    from revente.shop_actions import _build_parser
    p = _build_parser()
    ns = p.parse_args(["--serial", "x"])
    assert ns.action == "plan"
    assert ns.confirm is False
    ns2 = p.parse_args(["--serial", "x", "--buy-hc", "--confirm", "--max-count", "2"])
    assert ns2.action == "buy-hc" and ns2.confirm is True and ns2.max_count == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_shop_actions.py -v -k cli`
Expected: FAIL — `ImportError: cannot import name '_build_parser'`.

- [ ] **Step 3: Write minimal implementation**

```python
# append to revente/shop_actions.py
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_shop_actions.py -v -k cli`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add revente/shop_actions.py tests/test_shop_actions.py
git commit -m "feat(shop-actions): CLI (--plan default, --buy-hc/--upgrade need --confirm)"
```

---

## Task 8: Commandes worker (`worker_link.py`)

**Files:**
- Modify: `worker_link.py` (handlers près des autres `_cmd_*`, ~ligne 760 ; registre `COMMANDS` ~ligne 796)
- Test: `tests/test_shop_commands.py`

Contexte vérifié : `_adb_serial()` (worker_link.py:51) → `device.adb_serial()`. `_local_get`/`_resolve_local_account_id` existent. `_cmd_session_state` lit `/api/accounts/{aid}/push_max_state`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_shop_commands.py
import pytest


def _setup(monkeypatch, *, session_running=False):
    import worker_link as W
    monkeypatch.setattr(W, "_adb_serial", lambda: "fakeserial")
    monkeypatch.setattr(W, "_resolve_local_account_id", lambda tag: 1)

    state = {"running": session_running}
    monkeypatch.setattr(W, "_cmd_session_state",
                        lambda args: {"ok": True, "state": {"running": state["running"]}})

    calls = {"buy": 0, "upgrade": 0, "dry": None}

    class FakeReport:
        def as_dict(self):
            return {"ok": True}

    class FakeEngine:
        def __init__(self, serial, dry_run=True, **kw):
            calls["dry"] = dry_run

        def buy_hypercharges(self, **kw):
            calls["buy"] += 1
            return FakeReport()

        def upgrade_power(self, **kw):
            calls["upgrade"] += 1
            return FakeReport()

    import revente.shop_actions as S
    monkeypatch.setattr(S, "ShopActionEngine", FakeEngine)
    return W, calls


def test_shop_plan_is_dry_run_and_runs(monkeypatch):
    W, calls = _setup(monkeypatch)
    out = W._cmd_shop_plan({"tag": "#T"})
    assert out["ok"] is True
    assert calls["dry"] is True and calls["buy"] == 1


def test_buy_hc_without_confirm_is_dry_run(monkeypatch):
    W, calls = _setup(monkeypatch)
    out = W._cmd_shop_buy_hypercharges({"tag": "#T"})  # pas de confirm
    assert out["ok"] is True
    assert calls["dry"] is True and calls["buy"] == 1


def test_buy_hc_confirm_refused_when_session_running(monkeypatch):
    W, calls = _setup(monkeypatch, session_running=True)
    out = W._cmd_shop_buy_hypercharges({"tag": "#T", "confirm": True})
    assert out["ok"] is False
    assert "session" in out["error"].lower()
    assert calls["buy"] == 0          # n'a pas exécuté


def test_buy_hc_confirm_runs_live_when_idle(monkeypatch):
    W, calls = _setup(monkeypatch, session_running=False)
    out = W._cmd_shop_buy_hypercharges({"tag": "#T", "confirm": True})
    assert out["ok"] is True
    assert calls["dry"] is False and calls["buy"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_shop_commands.py -v`
Expected: FAIL — `AttributeError: module 'worker_link' has no attribute '_cmd_shop_plan'`.

- [ ] **Step 3: Write minimal implementation**

```python
# add to worker_link.py near the other game/session _cmd_* handlers
def _session_is_running(tag: str) -> bool:
    """True if a grind session is active for this tag (don't drive the device twice)."""
    try:
        st = _cmd_session_state({"tag": tag})
        if not st.get("ok"):
            return False
        s = st.get("state") or {}
        return bool(s.get("running") or s.get("active") or s.get("session_id"))
    except Exception:
        return False


def _run_shop(args: dict, kind: str) -> dict:
    """Shared driver for shop commands. kind ∈ {'plan','buy','upgrade'}."""
    tag = args.get("tag")
    if not tag:
        return {"ok": False, "error": "missing tag"}
    confirm = bool(args.get("confirm")) and kind != "plan"
    if confirm and _session_is_running(tag):
        return {"ok": False,
                "error": "a grind session is running — stop it first (session_stop)"}
    try:
        serial = _adb_serial()
    except Exception as exc:
        return {"ok": False, "error": f"no device: {exc}"}
    import revente.shop_actions as S
    eng = S.ShopActionEngine(serial, dry_run=not confirm)
    try:
        if kind == "upgrade":
            rep = eng.upgrade_power(
                target_level=int(args.get("target_level", 11)),
                scope=args.get("scope", "current"), confirm=confirm,
                max_brawlers=int(args.get("max_brawlers", 1)))
        else:  # plan | buy
            rep = eng.buy_hypercharges(
                max_count=args.get("max_count"),
                coin_floor=int(args.get("coin_floor", 0)), confirm=confirm)
        return {"ok": True, "report": rep.as_dict()}
    except Exception as exc:
        log.exception("shop command %s failed", kind)
        return {"ok": False, "error": str(exc)}


def _cmd_shop_plan(args: dict) -> dict:
    """Dry-run: walk the collection and report which hypercharges would be bought."""
    return _run_shop({**args, "confirm": False}, "plan")


def _cmd_shop_buy_hypercharges(args: dict) -> dict:
    """Buy hypercharges on maxed brawlers. Live only if args['confirm'] is True."""
    return _run_shop(args, "buy")


def _cmd_shop_upgrade_power(args: dict) -> dict:
    """Raise power level(s). Live only if args['confirm'] is True."""
    return _run_shop(args, "upgrade")
```

Then register in the `COMMANDS` dict (after the `game_*` block):

```python
    # shop / upgrade actions
    "shop_plan":              _cmd_shop_plan,
    "shop_buy_hypercharges":  _cmd_shop_buy_hypercharges,
    "shop_upgrade_power":     _cmd_shop_upgrade_power,
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_shop_commands.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add worker_link.py tests/test_shop_commands.py
git commit -m "feat(shop-actions): worker commands shop_plan/buy_hypercharges/upgrade_power (session-exclusive, confirm-gated)"
```

---

## Task 9: Endpoints cloud (`cloud_panel/app.py`)

**Files:**
- Modify: `cloud_panel/app.py` (après `api_account_start`, ~ligne 1219)
- Test: `tests/test_shop_endpoints.py`

Contexte vérifié : `_cmd_for_account(account_id, name, args, timeout_s)` (app.py:1155) ; pattern endpoint = `@app.post(...)` → `_cmd_for_account(...)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_shop_endpoints.py
import pytest


def test_shop_routes_registered():
    app_mod = pytest.importorskip("cloud_panel.app")
    paths = {r.path for r in app_mod.app.routes}
    assert "/api/accounts/{account_id}/shop/plan" in paths
    assert "/api/accounts/{account_id}/shop/buy_hypercharges" in paths
    assert "/api/accounts/{account_id}/shop/upgrade_power" in paths


def test_shop_body_model_defaults():
    app_mod = pytest.importorskip("cloud_panel.app")
    b = app_mod.ShopBody()
    assert b.confirm is False
    assert b.target_level == 11
    assert b.scope == "current"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_shop_endpoints.py -v`
Expected: FAIL — route absente / `ShopBody` indéfini.

- [ ] **Step 3: Write minimal implementation**

```python
# add to cloud_panel/app.py after api_account_start
class ShopBody(BaseModel):
    confirm: bool = False
    max_count: int | None = None
    coin_floor: int = 0
    target_level: int = 11
    scope: str = "current"
    max_brawlers: int = 1


@app.post("/api/accounts/{account_id}/shop/plan")
async def api_account_shop_plan(account_id: int) -> dict:
    # Dry-run read/enumeration : marche le carrousel, peut prendre du temps.
    return await _cmd_for_account(account_id, "shop_plan", {}, timeout_s=600)


@app.post("/api/accounts/{account_id}/shop/buy_hypercharges")
async def api_account_shop_buy_hc(account_id: int, payload: ShopBody | None = None) -> dict:
    p = payload or ShopBody()
    return await _cmd_for_account(account_id, "shop_buy_hypercharges", {
        "confirm": bool(p.confirm), "max_count": p.max_count,
        "coin_floor": int(p.coin_floor),
    }, timeout_s=900)


@app.post("/api/accounts/{account_id}/shop/upgrade_power")
async def api_account_shop_upgrade(account_id: int, payload: ShopBody | None = None) -> dict:
    p = payload or ShopBody()
    return await _cmd_for_account(account_id, "shop_upgrade_power", {
        "confirm": bool(p.confirm), "target_level": int(p.target_level),
        "scope": p.scope, "max_brawlers": int(p.max_brawlers),
    }, timeout_s=900)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_shop_endpoints.py -v`
Expected: PASS. (Si `cloud_panel.app` ne s'importe pas hors environnement cloud, le `importorskip` saute proprement — relancer sur l'env cloud/HP.)

- [ ] **Step 5: Commit**

```bash
git add cloud_panel/app.py tests/test_shop_endpoints.py
git commit -m "feat(shop-actions): cloud endpoints /api/accounts/{id}/shop/{plan,buy_hypercharges,upgrade_power}"
```

---

## Task 10: Vérification complète + validation live read-only (HP)

**Files:** aucun (vérification).

- [ ] **Step 1: Lancer toute la suite shop localement**

Run: `pytest tests/test_shop_actions.py tests/test_shop_commands.py tests/test_shop_endpoints.py -v`
Expected: tout PASS ou SKIP (skips = cv2/easyocr/cloud_panel absents en local). Aucun FAIL.

- [ ] **Step 2: Vérifier la non-régression du module réutilisé**

Run: `pytest tests/test_read_hypercharges.py -v`
Expected: PASS/SKIP comme avant (on n'a pas modifié `read_hypercharges.py`).

- [ ] **Step 3: Exécuter les tests easyocr-dépendants sur le HP (env complet)**

Si des tests ont été SKIPPÉS localement (easyocr/cv2 absents), les exécuter sur le HP :
Run: `sshpass -p 9464 ssh -p 2222 zeffut@72.60.94.131 'cd ~/BrawlStar-Bot && git fetch && git checkout feature/shop-actions && git pull && python -m pytest tests/test_shop_actions.py -v'`
Expected: les tests détection/éligibilité/moteur s'exécutent réellement et PASSENT.
(⚠️ nécessite que la branche soit poussée — voir note d'handoff ; demander confirmation avant `git push`.)

- [ ] **Step 4: Validation live READ-ONLY (dry-run) contre le device réel — AUCUNE dépense**

Seulement si le Mi9T est joignable via le HP. C'est read-only (screencaps + swipes de carrousel, zéro achat) :
Run: `sshpass -p 9464 ssh -p 2222 zeffut@72.60.94.131 'cd ~/BrawlStar-Bot && adb connect 192.168.60.18:5555 >/dev/null 2>&1; python -m revente.shop_actions --plan'`
Expected: JSON `Report` avec `dry_run:true`, `coins_before` lu, et `planned` listant les hypercharges qui SERAIENT achetées. Vérifier que `coins_after == coins_before` (rien dépensé).
Si le device est offline : noter que la validation live est différée ; les garanties logique/détection/dry-run restent acquises.

- [ ] **Step 5: Commit (doc d'état si besoin) — pas de dépense live sans confirmation utilisateur**

La bascule live (`--confirm` / `confirm:true`) n'est PAS déclenchée par l'agent (dépense d'or irréversible + compte en vente). Documenter dans le récap final comment l'utilisateur l'active (commande CLI ou endpoint), et s'arrêter là.

---

## Self-Review (rempli par l'auteur du plan)

**Spec coverage :**
- buy_hypercharges (action phare) → Tasks 2,3,5,8,9 ✓
- upgrade_power → Tasks 1,4,6,8,9 ✓
- Détection bouton/HC sur fiches → Tasks 1,2 (fixtures réelles) ✓
- Planner pur testable → Tasks 3,4 ✓
- Dry-run par défaut + garantie « aucun tap » → Task 5 (seam `_spend_tap`) ✓
- Live gardé par confirm + exclusivité de session → Task 8 ✓
- Surface commande worker + endpoints cloud → Tasks 8,9 ✓
- Vérification + validation live read-only → Task 10 ✓
- Sécurité dépense irréversible : dry-run défaut, confirm requis, pas de déclenchement live par l'agent → Tasks 5,8,10 ✓

**Placeholders :** aucun ; les points « CALIBRATE LIVE » (HC_SLOT_TAP, CONFIRM_REGION) sont des constantes réelles avec valeurs de départ + chemin de calibration (Task 10 step 4), pas des TODO.

**Type consistency :** `Action(kind,coin_cost,powerpoint_cost,note)`, `ActionResult(action,executed,verified,error)`, `Report(... .as_dict())`, `UpgradeButton(xr,yr,powerpoint_cost,coin_cost)`, `ShopActionEngine(serial,dry_run,hc_cost).buy_hypercharges(max_count,coin_floor,confirm)` / `.upgrade_power(target_level,scope,confirm,max_steps,max_brawlers)` — noms cohérents entre tasks 1/5/6/7/8/9. `_spend_tap`, `_read_coins`, `_screencap`, `_find_green_button_center`, `_crop_region`, `UPGRADE_REGION` référencés de façon cohérente. ✓
```
