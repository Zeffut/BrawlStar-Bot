# Auto Hypercharge Count Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Auto-count owned hypercharges from the live game so the resale estimator's `AccountData.hypercharges` is read from the device instead of hardcoded 0.

**Architecture:** A new serial-agnostic module `revente/read_hypercharges.py` opens the brawler collection over ADB, scrolls it, OCRs each card's power badge to find P11 brawlers, taps each P11 card to its detail screen, and detects the magenta hypercharge flame on the detail screen's right ability column (the only reliably-isolable signal — grid color detection is too noisy, brawlace's P11 list is stale). Pure helpers (parsing, magenta pixel count, resolution-aware geometry) are unit-tested on real Mi9T fixtures; navigation I/O is validated live.

**Tech Stack:** Python 3.12 (HP worker) / 3.11 (Mac), adb (`exec-out screencap`, `input tap/swipe`), easyocr (lazy), OpenCV+numpy (HSV magenta), Pillow. Device: Mi9T 2340×1080 (19.5:9) via HP worker ADB `192.168.60.18:5555`.

---

## Execution model

- **Device-coupled tasks (1, 8)** are run by the ORCHESTRATOR (has SSH to HP + must pause/resume the `brawlbot` service). Do NOT delegate these to a sandboxed subagent.
- **Device-independent tasks (2–7)** are subagent-driven TDD against committed fixtures.
- Test interpreter: the repo suite runs under system `python3` (3.9) with minimal deps. OCR/CV tests that need easyocr/cv2 MUST guard with `pytest.importorskip` so the suite stays green where those aren't installed. Pure-logic tests need no heavy deps.

## File structure

- Create `revente/read_hypercharges.py` — the whole feature (constants, pure helpers, navigation I/O, `count_hypercharges`). One focused module, mirrors `revente/read_currencies.py` / `revente/capture_collection.py` style.
- Create `tests/test_read_hypercharges.py` — pure-function + fixture tests.
- Create `tests/fixtures/hc/` — committed reference crops from the Mi9T.
- Modify `revente/estimate_account.py` — call `count_hypercharges`, fill `AccountData.hypercharges`.
- Modify `revente/estimate.py` — extend the "non mesurés" note logic so a measured 0 is not labelled "non mesuré" (small wording fix).

## Calibrated constants (locked from live Mi9T measurements 2026-06-13/14)

```python
# OpenCV HSV (H 0-180). Hot-magenta hypercharge flame. Measured: HC ability
# column ≈ 1800 magenta px; solid blue detail background = 0.
HC_HSV_LO = (145, 120, 120)
HC_HSV_HI = (172, 255, 255)
HC_MIN_PX = 300                      # flame ≈1800, noise ≈0 → 300 is a safe gate

# Resolution-aware geometry, ratios of the landscape frame (w,h).
# "wide" = ~19.5:9 phone (Mi9T 2340×1080), calibrated live. 16:9 left for later.
_GEOM_WIDE = {
    "brawlers_btn": (0.060, 0.435),          # lobby → open collection
    "back_arrow":   (0.025, 0.052),          # detail/collection → previous screen
    "scroll":       (0.60, 0.78, 0.60, 0.30, 500),  # x, y_from, _, y_to, ms (swipe up)
    # 3-col grid, 2 rows fully visible. (x0,x1,y0,y1) per cell, ratios.
    "cells": [
        (0.10, 0.36, 0.13, 0.47), (0.38, 0.63, 0.13, 0.47), (0.655, 0.905, 0.13, 0.47),
        (0.10, 0.36, 0.50, 0.84), (0.38, 0.63, 0.50, 0.84), (0.655, 0.905, 0.50, 0.84),
    ],
    # power badge: bottom-left circle of a cell, as fractions OF THE CELL rect.
    "badge_in_cell": (0.02, 0.20, 0.80, 0.99),   # x0,x1,y0,y1 within cell
    # name strip: bottom band of the cell (right of the badge).
    "name_in_cell": (0.20, 0.98, 0.80, 0.99),
    # detail screen: right ability column (blue bg) to scan for the flame.
    "detail_hc": (0.82, 0.97, 0.10, 0.55),
    # detail screen header region to OCR for the brawler name (large text top-left).
    "detail_name": (0.06, 0.40, 0.12, 0.24),
}
MAX_SCROLLS = 12
MAX_TAPS = 10
```

---

### Task 1: Calibrate geometry & commit fixtures (ORCHESTRATOR, device)

**Files:**
- Create: `tests/fixtures/hc/maisie_detail.png` (HC present)
- Create: `tests/fixtures/hc/shelly_detail.png` (P11, no HC — negative)
- Create: `tests/fixtures/hc/grid_maisie.png` (collection view containing Maisie + P11 badges)

- [ ] **Step 1:** Pause the bot: `sshpass -p 9464 ssh -p 2222 zeffut@72.60.94.131 "echo 9464 | sudo -S systemctl stop brawlbot"`. Verify `inactive`.
- [ ] **Step 2:** Drive the Mi9T (`192.168.60.18:5555`) to: lobby → collection → capture a grid view with Maisie (`grid_maisie.png`); open Maisie detail → `maisie_detail.png`; open Shelly detail (P11, no HC) → `shelly_detail.png`. Pull all three to `tests/fixtures/hc/`. (Reuse the already-captured `/tmp/maisie_detail.png` and `/tmp/m6.png` if still valid; only Shelly's detail must be freshly captured.)
- [ ] **Step 3:** Verify the locked geometry against the fixtures with a throwaway script: each cell rect lands on a card; `badge_in_cell` over Maisie's cell OCRs `11`; `detail_hc` crop on `maisie_detail.png` has ≥`HC_MIN_PX` magenta px and on `shelly_detail.png` has `<HC_MIN_PX`; `detail_name` OCRs `maisie`/`shelly`. Adjust ratios in the constants block above if any check fails, re-verify.
- [ ] **Step 4:** Resume the bot: `... "echo 9464 | sudo -S systemctl start brawlbot"`. Verify `active`.
- [ ] **Step 5: Commit**

```bash
git add tests/fixtures/hc/
git commit -m "test(fixtures): real Mi9T HC reference screenshots (maisie+/shelly-/grid)"
```

---

### Task 2: `_parse_power` pure parser

**Files:**
- Create: `revente/read_hypercharges.py`
- Test: `tests/test_read_hypercharges.py`

- [ ] **Step 1: Write the failing test**

```python
from revente.read_hypercharges import _parse_power

def test_parse_power_eleven():
    assert _parse_power("11") == 11
def test_parse_power_single_digit():
    assert _parse_power("1") == 1
    assert _parse_power("4") == 4
def test_parse_power_noise_returns_none():
    assert _parse_power("") is None
    assert _parse_power("x") is None
def test_parse_power_clamped_range():
    # OCR junk that isn't a plausible power level → None
    assert _parse_power("9999") is None
```

- [ ] **Step 2: Run test to verify it fails** — `python3 -m pytest tests/test_read_hypercharges.py -q` → ImportError/fail.
- [ ] **Step 3: Write minimal implementation** (top of `revente/read_hypercharges.py`)

```python
"""Auto-count owned hypercharges from the live Brawl Stars collection.

Strategy: the hypercharge flame is only reliably isolable on a brawler's
DETAIL screen (magenta on a solid blue background); grid color detection is
too noisy and brawlace's P11 list is stale. So we scan the collection grid to
find P11 brawlers (power badge OCR), open each P11 card, and detect the flame.
Pure helpers are unit-tested; navigation I/O is validated live. adb only.
"""
from __future__ import annotations
import re
import subprocess
import time

def _parse_power(ocr_text: str) -> "int | None":
    """Parse a brawler power level (1..11) from a power-badge OCR string."""
    m = re.findall(r"\d+", ocr_text or "")
    if not m:
        return None
    val = int(max(m, key=len))
    return val if 1 <= val <= 11 else None
```

- [ ] **Step 4: Run test to verify it passes** — `python3 -m pytest tests/test_read_hypercharges.py -q` → PASS.
- [ ] **Step 5: Commit**

```bash
git add revente/read_hypercharges.py tests/test_read_hypercharges.py
git commit -m "feat(revente): _parse_power helper for hypercharge scan"
```

---

### Task 3: Magenta detection (`_magenta_count`, `_detail_has_hypercharge`)

**Files:**
- Modify: `revente/read_hypercharges.py`
- Test: `tests/test_read_hypercharges.py`

- [ ] **Step 1: Write the failing test** (fixture-guarded)

```python
import pytest
from pathlib import Path

FIX = Path(__file__).parent / "fixtures" / "hc"

def _img(name):
    Image = pytest.importorskip("PIL.Image", reason="Pillow not installed")
    return Image.open(FIX / name).convert("RGB")

def test_detail_has_hypercharge_positive():
    pytest.importorskip("cv2"); pytest.importorskip("numpy")
    from revente.read_hypercharges import _detail_has_hypercharge
    img = _img("maisie_detail.png"); w, h = img.size
    assert _detail_has_hypercharge(img, w, h) is True

def test_detail_has_hypercharge_negative():
    pytest.importorskip("cv2"); pytest.importorskip("numpy")
    from revente.read_hypercharges import _detail_has_hypercharge
    img = _img("shelly_detail.png"); w, h = img.size
    assert _detail_has_hypercharge(img, w, h) is False
```

- [ ] **Step 2: Run test to verify it fails** — `python3.11 -m pytest tests/test_read_hypercharges.py -k hypercharge -q` (use 3.11 where cv2/PIL exist) → fail (function missing).
- [ ] **Step 3: Write minimal implementation** (append to `revente/read_hypercharges.py`)

```python
HC_HSV_LO = (145, 120, 120)
HC_HSV_HI = (172, 255, 255)
HC_MIN_PX = 300

def _magenta_count(pil_crop) -> int:
    """Count hot-magenta (hypercharge-flame) pixels in a PIL crop."""
    import numpy as np, cv2
    hsv = cv2.cvtColor(np.array(pil_crop), cv2.COLOR_RGB2HSV)
    import numpy as _np
    mask = cv2.inRange(hsv, _np.array(HC_HSV_LO), _np.array(HC_HSV_HI))
    return int(mask.sum() // 255)

def _detail_has_hypercharge(pil_image, w: int, h: int) -> bool:
    """True if the detail screen's right ability column shows the HC flame."""
    x0, x1, y0, y1 = _geom_for(w, h)["detail_hc"]
    crop = pil_image.crop((int(w * x0), int(h * y0), int(w * x1), int(h * y1)))
    return _magenta_count(crop) >= HC_MIN_PX
```

(Note: `_geom_for` is defined in Task 4; if implementing Task 3 first, temporarily inline the `detail_hc` ratios `(0.82,0.97,0.10,0.55)` and replace with `_geom_for` in Task 4.)

- [ ] **Step 4: Run test to verify it passes** — same command → PASS (both positive and negative).
- [ ] **Step 5: Commit**

```bash
git add revente/read_hypercharges.py tests/test_read_hypercharges.py
git commit -m "feat(revente): magenta hypercharge-flame detector (detail screen)"
```

---

### Task 4: Resolution-aware geometry (`_geom_for`)

**Files:**
- Modify: `revente/read_hypercharges.py`
- Test: `tests/test_read_hypercharges.py`

- [ ] **Step 1: Write the failing test**

```python
def test_geom_for_phone():
    from revente.read_hypercharges import _geom_for
    g = _geom_for(2340, 1080)
    assert len(g["cells"]) == 6
    assert "brawlers_btn" in g and "detail_hc" in g and "scroll" in g

def test_geom_for_unknown_aspect_defaults_phone():
    # Until 16:9 is calibrated, any aspect returns the phone set (documented).
    from revente.read_hypercharges import _geom_for
    assert _geom_for(1920, 1080)["cells"] == _geom_for(2340, 1080)["cells"]
```

- [ ] **Step 2: Run test to verify it fails** — `python3 -m pytest tests/test_read_hypercharges.py -k geom -q` → fail.
- [ ] **Step 3: Write minimal implementation** (insert the `_GEOM_WIDE` dict from the constants block above, plus:)

```python
def _geom_for(w: int, h: int) -> dict:
    """Geometry for the frame aspect. Only the ~19.5:9 phone set is calibrated;
    it is returned for every aspect until a 16:9 emulator set is added."""
    return _GEOM_WIDE
```

- [ ] **Step 4: Run test to verify it passes** — PASS. Also re-run Task 3's tests to confirm `_detail_has_hypercharge` now uses `_geom_for`.
- [ ] **Step 5: Commit**

```bash
git add revente/read_hypercharges.py tests/test_read_hypercharges.py
git commit -m "feat(revente): resolution-aware collection geometry"
```

---

### Task 5: Navigation I/O primitives

**Files:**
- Modify: `revente/read_hypercharges.py`
- (No unit test — pure device I/O, validated live in Task 8. Keep functions small/auditable.)

- [ ] **Step 1: Implement the adb I/O helpers** (append)

```python
def _screencap(serial: str):
    from PIL import Image
    import io
    out = subprocess.run(["adb", "-s", serial, "exec-out", "screencap", "-p"],
                         capture_output=True, timeout=15, check=True)
    return Image.open(io.BytesIO(out.stdout)).convert("RGB")

def _tap(serial: str, w: int, h: int, xr: float, yr: float) -> None:
    subprocess.run(["adb", "-s", serial, "shell", "input", "tap",
                    str(int(w * xr)), str(int(h * yr))], capture_output=True, timeout=10)

def _swipe(serial: str, w: int, h: int, g) -> None:
    x, y0, _, y1, ms = g["scroll"]
    subprocess.run(["adb", "-s", serial, "shell", "input", "swipe",
                    str(int(w * x)), str(int(h * y0)), str(int(w * x)), str(int(h * y1)),
                    str(ms)], capture_output=True, timeout=10)

def _ocr_text(pil_crop, allow=None) -> str:
    """easyocr a crop to a lowercase string (lazy shared reader)."""
    import numpy as np
    from revente.read_currencies import _ocr_digits_reader  # reuse shared reader
    rd = _ocr_digits_reader()
    kw = {"detail": 0}
    if allow:
        kw["allowlist"] = allow
    res = rd.readtext(np.array(pil_crop), **kw)
    return " ".join(res).lower().strip()
```

- [ ] **Step 2: Implement screen-state checks** (append)

```python
def _on_collection(serial: str, w: int, h: int) -> bool:
    """Collection header 'BRAWLERS (n/m)' present near the top."""
    img = _screencap(serial)
    crop = img.crop((int(w * 0.30), 0, int(w * 0.70), int(h * 0.10)))
    return "brawler" in _ocr_text(crop)

def _detail_brawler_name(img, w: int, h: int) -> "str | None":
    """Brawler name from the detail header; None if not on a detail screen."""
    x0, x1, y0, y1 = _geom_for(w, h)["detail_name"]
    txt = _ocr_text(img.crop((int(w * x0), int(h * y0), int(w * x1), int(h * y1))))
    return txt or None
```

- [ ] **Step 3: Commit**

```bash
git add revente/read_hypercharges.py
git commit -m "feat(revente): adb navigation + screen-state primitives"
```

---

### Task 6: `count_hypercharges` orchestration

**Files:**
- Modify: `revente/read_hypercharges.py`

- [ ] **Step 1: Implement** (append)

```python
def count_hypercharges(serial: str) -> dict:
    """Open the collection, find P11 brawlers, confirm each one's hypercharge
    on its detail screen. Returns {"count": int|None, "brawlers": [str]}.
    Returns count=None on a navigation failure (estimator keeps hypercharges=0)."""
    import logging
    log = logging.getLogger("read_hypercharges")
    try:
        probe = _screencap(serial); w, h = probe.size
        g = _geom_for(w, h)
        # open collection (idempotent retry)
        for _ in range(3):
            if _on_collection(serial, w, h):
                break
            _tap(serial, w, h, *g["brawlers_btn"]); time.sleep(2.5)
        else:
            return {"count": None, "brawlers": []}

        seen: set[str] = set()
        hc: list[str] = []
        scrolls = 0
        taps = 0
        while scrolls <= MAX_SCROLLS:
            view = _screencap(serial)
            new_on_view = False
            for (cx0, cx1, cy0, cy1) in g["cells"]:
                # dedup by name
                nx0, nx1, ny0, ny1 = g["name_in_cell"]
                cw, ch = (cx1 - cx0), (cy1 - cy0)
                name = _ocr_text(view.crop((
                    int(w * (cx0 + cw * nx0)), int(h * (cy0 + ch * ny0)),
                    int(w * (cx0 + cw * nx1)), int(h * (cy0 + ch * ny1)))))
                if not name or name in seen:
                    continue
                seen.add(name); new_on_view = True
                # power badge
                bx0, bx1, by0, by1 = g["badge_in_cell"]
                power = _parse_power(_ocr_text(view.crop((
                    int(w * (cx0 + cw * bx0)), int(h * (cy0 + ch * by0)),
                    int(w * (cx0 + cw * bx1)), int(h * (cy0 + ch * by1)))), allow="0123456789"))
                if power is not None and power < 9:
                    continue  # only P11 can have HC; tap ambiguous(None)/>=9 to be safe
                if taps >= MAX_TAPS:
                    log.warning("hc scan: MAX_TAPS hit, stopping early"); break
                # open detail, detect, return
                _tap(serial, w, h, (cx0 + cx1) / 2, (cy0 + cy1) / 2); time.sleep(2.0); taps += 1
                detail = _screencap(serial)
                dname = _detail_brawler_name(detail, w, h)
                if dname and _detail_has_hypercharge(detail, w, h):
                    hc.append(dname)
                _tap(serial, w, h, *g["back_arrow"]); time.sleep(1.5)
            if not new_on_view:
                break  # bottom reached
            _swipe(serial, w, h, g); time.sleep(1.3); scrolls += 1
        return {"count": len(hc), "brawlers": sorted(set(hc))}
    except Exception:
        log.exception("count_hypercharges failed")
        return {"count": None, "brawlers": []}
```

- [ ] **Step 2:** Syntax check — `python3 -m py_compile revente/read_hypercharges.py`.
- [ ] **Step 3:** Full suite stays green — `python3 -m pytest tests/ -q`.
- [ ] **Step 4: Commit**

```bash
git add revente/read_hypercharges.py
git commit -m "feat(revente): count_hypercharges grid scan + detail confirmation"
```

---

### Task 7: Wire into the estimator

**Files:**
- Modify: `revente/estimate_account.py`
- Modify: `revente/estimate.py:70-72` (note wording)

- [ ] **Step 1:** In `revente/estimate_account.py`, after currencies are read, before building `AccountData`:

```python
    from revente.read_hypercharges import count_hypercharges
    hc = count_hypercharges(serial)
```

and pass `hypercharges=(hc.get("count") or 0)` into `AccountData(...)`, and append a note when measured:

```python
    if hc.get("count") is not None:
        # recorded in notes by estimate() consumer; store brawler list for the caller
        data_hc_brawlers = hc.get("brawlers", [])
```

Return `{"ok": True, "account": data.__dict__, "estimate": est.__dict__, "hypercharge_brawlers": hc.get("brawlers", [])}`.

- [ ] **Step 2:** In `revente/estimate.py`, refine the note so a *measured* 0 is not mislabelled:

```python
    if data.hypercharges == 0 and data.rare_skins == 0:
        notes.append("skins/hypercharges non mesurés ou nuls — fourchette indicative")
        confidence = "low"
    else:
        confidence = "medium"
```

- [ ] **Step 3:** `python3 -m py_compile revente/estimate_account.py revente/estimate.py` and `python3 -m pytest tests/ -q` → green.
- [ ] **Step 4: Commit**

```bash
git add revente/estimate_account.py revente/estimate.py
git commit -m "feat(revente): estimator auto-fills hypercharges from count_hypercharges"
```

---

### Task 8: Live validation on the Mi9T (ORCHESTRATOR, device)

**Files:** none (validation only)

- [ ] **Step 1:** Pause the bot (`systemctl stop brawlbot`), confirm `inactive`.
- [ ] **Step 2:** From the HP, drive the Mi9T to the lobby (wake/unlock/launch BS if needed), then run `count_hypercharges("192.168.60.18:5555")` via the HP venv python.
- [ ] **Step 3:** Expected: `{"count": 1, "brawlers": ["maisie"]}`. If wrong, inspect intermediate screenshots, adjust geometry constants (Task 1 block), re-run. Capture `shelly_detail.png` negative fixture here if not already done.
- [ ] **Step 4:** Resume the bot (`systemctl start brawlbot`), confirm `active`.
- [ ] **Step 5:** Run the full end-to-end estimate against the Mi9T and confirm the output now reports `hypercharges: 1` and lists maisie.
- [ ] **Step 6: Commit** any constant adjustments made during validation.

```bash
git add -A && git commit -m "fix(revente): calibrate hypercharge scan geometry against live Mi9T"
```

---

## Self-review

- **Spec coverage:** detail-screen detection (T3), bounded P11 navigation (T5/T6), resolution-aware geometry (T4), estimator integration (T7), gems already shipped, live validation expecting count=1/maisie (T8), fixtures (T1). Robustness (caps, state checks, clean failure → count=None) in T6. YAGNI items (skins, worker auto-pause, 16:9) explicitly excluded. All covered.
- **Placeholder scan:** constants are concrete (HSV, thresholds, ratios); all code steps show code. Geometry ratios are provisional-but-concrete and are explicitly re-verified/adjusted against fixtures in T1 and live in T8.
- **Type consistency:** `_geom_for` keys (`brawlers_btn`, `back_arrow`, `scroll`, `cells`, `badge_in_cell`, `name_in_cell`, `detail_hc`, `detail_name`) used consistently in T5/T6. `count_hypercharges` returns `{"count","brawlers"}` consumed identically in T7/T8. `_parse_power`/`_magenta_count`/`_detail_has_hypercharge` signatures stable across tasks.
