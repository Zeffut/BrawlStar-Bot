# bs-account-estimator Skill — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Claude Code skill that drives BlueStacks Air via ADB to log into Brawl Stars accounts, extract their readable data, and output a resale price estimate.

**Architecture:** Reuse the repo's existing device-control stack (ADB, screencap, OCR, state detection, brawler list). Add 5 focused modules under `revente/`: pure valuation logic, currency OCR, IMAP code retrieval, Supercell ID auto-login (device nav), and best-effort collection capture. Package as `.claude/skills/bs-account-estimator/`.

**Tech Stack:** Python 3.11, adbutils/adb CLI, easyocr (existing `extract_text_and_positions`), Pillow/numpy, imaplib (stdlib), pytest.

**Buildable now (device-independent, TDD):** Tasks 1, 2, 4a.
**Blocked on Phase 0 (live emulator + loaded account):** Tasks 3, 4b, 5, 6, 7-live.

---

## Task 1: Valuation logic (`revente/estimate.py`) — buildable now

**Files:**
- Create: `revente/__init__.py`
- Create: `revente/estimate.py`
- Test: `tests/test_estimate.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_estimate.py
from revente.estimate import AccountData, estimate

def test_low_trophy_account_is_bottom_tier():
    e = estimate(AccountData(tag="#X", name="z5", trophies=2500, brawlers=25, power11=0))
    assert e.tier == "basique"
    assert e.price_max_usd <= 13
    assert e.confidence == "low"
    assert any("skins/hypercharges" in n for n in e.notes)

def test_20k_loaded_account_is_charged_tier():
    e = estimate(AccountData(tag="#Y", name="z", trophies=21000, brawlers=60,
                             power11=10, hypercharges=7, rare_skins=1))
    assert e.tier == "chargé"
    assert e.price_min_usd >= 35
    assert e.price_max_usd <= 60

def test_20k_empty_account_is_basique():
    e = estimate(AccountData(tag="#Z", name="z", trophies=21000, brawlers=40, power11=1))
    assert e.tier == "basique"
    assert e.price_min_usd >= 15 and e.price_max_usd <= 19

def test_30k_charged_top_of_grid():
    e = estimate(AccountData(tag="#W", name="z", trophies=31000, brawlers=70,
                             power11=12, hypercharges=5, rare_skins=2))
    assert e.tier == "chargé"
    assert e.price_max_usd >= 60
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_estimate.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'revente.estimate'`

- [ ] **Step 3: Write minimal implementation**

```python
# revente/estimate.py
"""Resale price estimation from extracted account data.

Pricing grid sourced from revente/grille_prix.md (benchmark 2026-05-31).
Amorçage prices (new-seller, -10/-15% under market) are used as the
default output range.
"""
from dataclasses import dataclass, field


@dataclass
class AccountData:
    tag: str
    name: str
    trophies: int
    brawlers: int
    power11: int
    gems: int | None = None
    gold: int | None = None
    level: int | None = None
    rare_skins: int = 0     # confirmed rare/old skins (visual pass)
    hypercharges: int = 0   # unlocked hypercharges (visual pass)


@dataclass
class Estimate:
    tier: str               # "basique" | "chargé"
    price_min_usd: float
    price_max_usd: float
    confidence: str         # "low" | "medium" | "high"
    notes: list[str] = field(default_factory=list)


# (palier_min_trophies, basique (min,max), chargé (min,max)) — amorçage USD
_GRID = [
    (30000, (35, 45), (60, 90)),
    (25000, (20, 26), (40, 70)),
    (20000, (15, 19), (35, 60)),
    (15000, (9, 12),  (15, 25)),
    (0,     (3, 8),   (10, 25)),   # below the value grid
]


def _bucket(trophies: int):
    for floor, basique, charge in _GRID:
        if trophies >= floor:
            return basique, charge
    return (3, 8), (10, 25)


def _is_charged(data: AccountData) -> bool:
    return data.power11 >= 8 or data.hypercharges >= 3 or data.rare_skins >= 1


def estimate(data: AccountData) -> Estimate:
    basique, charge = _bucket(data.trophies)
    charged = _is_charged(data)
    tier = "chargé" if charged else "basique"
    lo, hi = charge if charged else basique

    notes: list[str] = []
    # Skins + hypercharges are the biggest levers and are NOT auto-measured
    # reliably. If they weren't provided, the estimate is indicative only.
    if data.hypercharges == 0 and data.rare_skins == 0:
        notes.append("skins/hypercharges non mesurés — fourchette indicative")
        confidence = "low"
    else:
        confidence = "medium"
    if data.trophies < 15000:
        notes.append("sous le seuil de valeur (15k) — marge quasi nulle après frais")

    return Estimate(tier=tier, price_min_usd=float(lo), price_max_usd=float(hi),
                    confidence=confidence, notes=notes)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_estimate.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add revente/__init__.py revente/estimate.py tests/test_estimate.py
git commit -m "feat(revente): account resale valuation logic + tests"
```

---

## Task 2: Currency number parser (`revente/read_currencies.py` — parse helper) — buildable now

**Files:**
- Create: `revente/read_currencies.py`
- Test: `tests/test_read_currencies.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_read_currencies.py
from revente.read_currencies import parse_currency_number

def test_plain_number():
    assert parse_currency_number("26157") == 26157

def test_with_thousands_separators():
    assert parse_currency_number("1 647") == 1647
    assert parse_currency_number("1,647") == 1647

def test_picks_longest_run_ignoring_noise():
    assert parse_currency_number("x 43  26157") == 26157

def test_no_digits_returns_none():
    assert parse_currency_number("abc") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_read_currencies.py -v`
Expected: FAIL with `ModuleNotFoundError` / `ImportError: cannot import name 'parse_currency_number'`

- [ ] **Step 3: Write minimal implementation**

```python
# revente/read_currencies.py
"""Read gems / gold / level from a Brawl Stars lobby screenshot.

The parse helper is pure (unit-tested). The device-facing reader
(`read_currencies`) reuses the same OCR + crop pattern as
game_api._ocr_trophies and is exercised live (Phase 1).
"""
import re


def parse_currency_number(ocr_text: str) -> int | None:
    """Extract a currency integer from a noisy OCR string.

    Removes thousands separators (space/comma/dot between digits), then
    returns the longest digit run (tie -> largest), mirroring the
    trophy-OCR heuristic.
    """
    joined = re.sub(r"(?<=\d)[ ,.](?=\d)", "", ocr_text)
    runs = re.findall(r"\d+", joined)
    if not runs:
        return None
    return int(max(runs, key=lambda s: (len(s), int(s))))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_read_currencies.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add revente/read_currencies.py tests/test_read_currencies.py
git commit -m "feat(revente): currency OCR number parser + tests"
```

---

## Task 3: Device-facing currency reader — BLOCKED on Phase 0 (live calibration)

**Files:**
- Modify: `revente/read_currencies.py` (add `read_currencies(serial=None)`)
- Manual calibration against a real BlueStacks 1920×1080 lobby screenshot.

- [ ] **Step 1: Add the reader following the `_ocr_trophies` pattern**

```python
# appended to revente/read_currencies.py
import numpy as np
import game_api
from detect import extract_text_and_positions  # same OCR helper game_api uses

# Crop ratios (landscape lobby). CALIBRATE on a real BlueStacks lobby:
# gems + gold sit in the top bar; level/XP bottom-left.
_CROPS = {
    "gems":  (0.00, 0.09, 0.60, 0.72),   # (y0, y1, x0, x1) as ratios
    "gold":  (0.00, 0.09, 0.72, 0.86),
    "level": (0.92, 1.00, 0.00, 0.10),
}

def _read_field(arr, box) -> int | None:
    h, w = arr.shape[:2]
    y0, y1, x0, x1 = box
    crop = arr[int(h*y0):int(h*y1), int(w*x0):int(w*x1)]
    text = extract_text_and_positions(crop)
    joined = " ".join(text.keys()) if isinstance(text, dict) else str(text)
    return parse_currency_number(joined)

def read_currencies(serial: str | None = None) -> dict:
    """Return {'gems': int|None, 'gold': int|None, 'level': int|None}
    from the current lobby screen. Assumes BS is at the lobby."""
    img = game_api._adb_screencap()
    arr = np.array(img)
    return {k: _read_field(arr, box) for k, box in _CROPS.items()}
```

- [ ] **Step 2: Calibrate live** — capture a real lobby screenshot via the skill, verify each crop lands on the right number; adjust `_CROPS` ratios until `read_currencies()` returns the on-screen values (cross-check by eye).

- [ ] **Step 3: Commit**

```bash
git add revente/read_currencies.py
git commit -m "feat(revente): live currency reader (gems/gold/level) calibrated on BlueStacks"
```

---

## Task 4a: Supercell verification-code parser (`revente/imap_codes.py`) — buildable now

**Files:**
- Create: `revente/imap_codes.py`
- Test: `tests/test_imap_codes.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_imap_codes.py
from revente.imap_codes import extract_supercell_code

def test_extracts_6_digit_code_near_keyword():
    body = "Your Supercell ID verification code is 482913. Do not share it."
    assert extract_supercell_code(body) == "482913"

def test_french_body():
    body = "Votre code de vérification Supercell ID est 100200."
    assert extract_supercell_code(body) == "100200"

def test_ignores_unrelated_long_numbers():
    body = "Order 1234567890. Your code: 654321"
    assert extract_supercell_code(body) == "654321"

def test_no_code_returns_none():
    assert extract_supercell_code("welcome to supercell") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_imap_codes.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# revente/imap_codes.py
"""Retrieve Supercell ID email verification codes via IMAP.

`extract_supercell_code` is pure (unit-tested). `wait_for_code` polls
an IMAP inbox and is exercised live (Phase 2). Credentials live in
cfg/imap.toml (gitignored) — never commit them.
"""
import re

# A standalone 6-digit number, preferring one that follows a "code" keyword.
_CODE_NEAR = re.compile(r"code[^0-9]{0,20}(\d{6})", re.IGNORECASE)
_ANY_6 = re.compile(r"(?<!\d)(\d{6})(?!\d)")


def extract_supercell_code(body: str) -> str | None:
    m = _CODE_NEAR.search(body)
    if m:
        return m.group(1)
    m = _ANY_6.search(body)
    return m.group(1) if m else None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_imap_codes.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add revente/imap_codes.py tests/test_imap_codes.py
git commit -m "feat(revente): Supercell verification-code parser + tests"
```

---

## Task 4b: IMAP poller — BLOCKED on Phase 2 (needs cfg/imap.toml creds)

**Files:**
- Modify: `revente/imap_codes.py` (add `wait_for_code`)
- Create: `cfg/imap.toml.example`
- Modify: `.gitignore` (add `cfg/imap.toml`)

- [ ] **Step 1: Add the poller**

```python
# appended to revente/imap_codes.py
import imaplib, email, time, tomllib
from pathlib import Path

def _imap_cfg() -> dict:
    p = Path(__file__).resolve().parent.parent / "cfg" / "imap.toml"
    with p.open("rb") as f:
        return tomllib.load(f)

def wait_for_code(since_epoch: float, timeout_s: float = 120, poll_s: float = 5) -> str | None:
    """Poll the IMAP inbox for a Supercell code arriving after since_epoch."""
    cfg = _imap_cfg()
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        M = imaplib.IMAP4_SSL(cfg["host"], cfg.get("port", 993))
        try:
            M.login(cfg["user"], cfg["password"])
            M.select("INBOX")
            typ, data = M.search(None, '(FROM "supercell")')
            for num in reversed(data[0].split()):
                typ, msg_data = M.fetch(num, "(RFC822)")
                msg = email.message_from_bytes(msg_data[0][1])
                body = _msg_text(msg)
                code = extract_supercell_code(body)
                if code:
                    return code
        finally:
            M.logout()
        time.sleep(poll_s)
    return None

def _msg_text(msg) -> str:
    if msg.is_multipart():
        return "".join(_msg_text(p) for p in msg.get_payload())
    try:
        return msg.get_payload(decode=True).decode(errors="ignore")
    except Exception:
        return ""
```

- [ ] **Step 2: Create the example config + gitignore the real one**

```toml
# cfg/imap.toml.example
host = "imap.gmail.com"
port = 993
user = "compte-supercell@example.com"
password = "mot-de-passe-application-dedie"
```

Add to `.gitignore`: `cfg/imap.toml`

- [ ] **Step 3: Live test** with a real dedicated app-password, on a throwaway account.

- [ ] **Step 4: Commit** (config example + poller only — never the real toml)

```bash
git add revente/imap_codes.py cfg/imap.toml.example .gitignore
git commit -m "feat(revente): IMAP poller for Supercell codes (creds gitignored)"
```

---

## Task 5: Supercell ID auto-login (`revente/login_supercell.py`) — BLOCKED on Phase 2

**Files:**
- Create: `revente/login_supercell.py`

Device-nav module (no unit test — integration only). Implements:

- [ ] **Step 1: Implement the login flow**

```python
# revente/login_supercell.py
"""Automate Supercell ID logout/login inside Brawl Stars on the emulator.

Fragile UI navigation — relies on OCR of settings labels + ADB text input.
Test on a throwaway account first. Reuses game_api for tap/screencap/state.
"""
import time
import game_api
from revente.imap_codes import wait_for_code

def _type_text(serial, text):
    import subprocess
    subprocess.run(["adb", "-s", serial, "shell", "input", "text", text.replace("@", "\\@")], check=True)

def login(email: str, serial: str | None = None, timeout_s: float = 180) -> bool:
    """Log into `email`'s Supercell ID. Returns True on success.

    Steps (to calibrate live against BlueStacks layout):
      1. ensure_brawlstars_at_lobby
      2. open Settings (gear) -> Supercell ID -> Log Out -> confirm
      3. Log In -> enter email -> request code
      4. wait_for_code(since) via IMAP -> type code -> confirm
      5. ensure lobby, verify account_detect.detect_player_tag changed
    """
    api = game_api.get()
    ok, _ = api.ensure_brawlstars_at_lobby()
    if not ok:
        return False
    # NOTE: exact tap coords + OCR labels calibrated live in Phase 2.
    # ... navigate to Supercell ID screen, logout, enter email ...
    since = time.time()
    # ... tap "send code" ...
    code = wait_for_code(since, timeout_s=timeout_s)
    if not code:
        return False
    # ... type code, confirm ...
    api.ensure_brawlstars_at_lobby()
    return True
```

- [ ] **Step 2: Calibrate + integration-test live** on a throwaway account; fill in the exact taps/OCR. Iterate until login succeeds end-to-end.

- [ ] **Step 3: Commit**

```bash
git add revente/login_supercell.py
git commit -m "feat(revente): Supercell ID auto-login flow (calibrated live)"
```

---

## Task 6: Best-effort collection capture (`revente/capture_collection.py`) — BLOCKED on Phase 0

**Files:**
- Create: `revente/capture_collection.py`

- [ ] **Step 1: Implement capture-and-save**

```python
# revente/capture_collection.py
"""Navigate to the brawler collection and save screenshots for visual
reading of skins/hypercharges (no brittle enumeration)."""
import time, game_api
from pathlib import Path

OUT = Path(__file__).resolve().parent / "captures"

def capture(tag: str, serial: str | None = None) -> list[str]:
    OUT.mkdir(exist_ok=True)
    api = game_api.get()
    api.ensure_brawlstars_at_lobby()
    api.tap(0.045, 0.50)          # BRAWLERS button (calibrate live)
    time.sleep(1.5)
    paths = []
    for i in range(3):            # capture a few scroll positions
        img = game_api._adb_screencap()
        p = OUT / f"{tag.lstrip('#')}_collection_{i}.png"
        img.save(p); paths.append(str(p))
        api.swipe(0.5, 0.7, 0.5, 0.3) if hasattr(api, "swipe") else None
        time.sleep(1.0)
    api.goto_lobby()
    return paths
```

- [ ] **Step 2: Calibrate live** (BRAWLERS button coords, scroll). Verify Claude can read the saved PNGs and identify skins/hypercharges by eye.

- [ ] **Step 3: Commit**

```bash
git add revente/capture_collection.py
git commit -m "feat(revente): best-effort collection screenshot capture"
```

---

## Task 7: Skill packaging + orchestrator (`.claude/skills/bs-account-estimator/`)

**Files:**
- Create: `.claude/skills/bs-account-estimator/SKILL.md`
- Create: `revente/estimate_account.py` (end-to-end orchestrator)

- [ ] **Step 1: Write the orchestrator**

```python
# revente/estimate_account.py
"""End-to-end: (optionally login) -> read -> estimate -> report.

Usage: python3 -m revente.estimate_account [--email EMAIL]
Reads the currently-loaded account if --email is omitted.
"""
import argparse, json
import game_api, account_detect
from revente.estimate import AccountData, estimate
from revente.read_currencies import read_currencies

def run(email: str | None = None) -> dict:
    api = game_api.get()
    if email:
        from revente.login_supercell import login
        if not login(email):
            return {"ok": False, "error": f"login failed for {email}"}
    api.ensure_brawlstars_at_lobby()
    tag = account_detect.detect_player_tag()
    prof = account_detect.fetch_account_profile(tag) if tag else {"name": None, "brawlers": []}
    brawlers = prof.get("brawlers", [])
    power11 = sum(1 for b in brawlers if b.get("power") == 11)
    trophies = api.read_trophies() or sum(b.get("trophies", 0) for b in brawlers)
    cur = read_currencies()
    data = AccountData(tag=tag or "?", name=prof.get("name") or "?",
                       trophies=trophies, brawlers=len(brawlers), power11=power11,
                       gems=cur.get("gems"), gold=cur.get("gold"), level=cur.get("level"))
    est = estimate(data)
    return {"ok": True, "account": data.__dict__, "estimate": est.__dict__}

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", default=None)
    args = ap.parse_args()
    print(json.dumps(run(args.email), ensure_ascii=False, indent=2))
```

- [ ] **Step 2: Write SKILL.md**

```markdown
---
name: bs-account-estimator
description: Use when estimating the resale value of a Brawl Stars account on the BlueStacks emulator. Drives the emulator via ADB, reads trophies/brawlers/currencies, and outputs a price estimate from the benchmark grid.
---

# Brawl Stars Account Estimator

Drives BlueStacks Air (ADB at `127.0.0.1:<port>`, set in `cfg/device.toml`)
to estimate a Brawl Stars account's resale value.

## Prerequisites (Phase 0, one-time, by the user)
- BlueStacks Air installed, BS installed, one account logged in
- `cfg/device.toml` → `serial = "127.0.0.1:<port>"`
- For auto-login (Phase 2): `cfg/imap.toml` (gitignored) + account email list

## Usage
- Estimate the currently-loaded account:
  `python3 -m revente.estimate_account`
- Estimate a specific account (auto-login):
  `python3 -m revente.estimate_account --email <supercell-id-email>`

Output: JSON with extracted data + price estimate (min/max USD, tier, confidence).
Skins/hypercharges are NOT auto-measured — run `revente/capture_collection.py`
and read the screenshots to refine the estimate upward when present.

## Pricing reference
See `revente/grille_prix.md`.
```

- [ ] **Step 3: Live smoke test**

Run: `python3 -m revente.estimate_account`
Expected: JSON with the loaded account's tag, trophies, brawlers, power11, currencies, and an estimate block.

- [ ] **Step 4: Commit**

```bash
git add .claude/skills/bs-account-estimator/SKILL.md revente/estimate_account.py
git commit -m "feat(revente): bs-account-estimator skill + end-to-end orchestrator"
```

---

## Phase gating summary
- **Now (no emulator needed):** Task 1, 2, 4a → ✅ **DONE 2026-05-31 (12/12 tests green)**.
- **After Phase 0 (BlueStacks + 1 account + ADB port):** ✅ **DONE 2026-05-31**
  - Env: adb↔BlueStacks `127.0.0.1:5555`, BS at lobby (Zeffut2.0), easyocr+requests installed.
  - Task 3 (currency reader) ✅ live-verified (trophies 4093 / gems 3674 / gold 10484).
  - Task 7 (orchestrator + SKILL.md) ✅ end-to-end live with `--tag`.
  - Tag auto-detect (`read_tag.py`) = ⚠️ **best-effort only**: easyocr drops a char on the
    stylised tag font (`PYV98LG9` vs `PYLV98LG9`); brawlace validation ~14s/call so no
    brute-force. **Pass `--tag` for reliability.** Task 6 (collection capture) not built.
- **After Phase 2 (IMAP creds + throwaway account):** Task 4b, Task 5 — NOT started.

## Dev environment note
- Run tests with **`/opt/homebrew/bin/python3.11 -m pytest`** — bare `python3` resolves to Xcode's **3.9.6** which chokes on `int | None` at import. pytest was installed into 3.11 on 2026-05-31.
- Pure-logic modules use `from __future__ import annotations` for portability.

## Self-review notes
- Spec §2 reuse map → Tasks reuse game_api/account_detect/device as documented. ✅
- Spec §3 flow → Task 7 orchestrator mirrors it. ✅
- Spec §4 data+valuation → Tasks 1,2,3,7. ✅
- Spec §5 setup → SKILL.md prerequisites + device.toml. ✅
- Spec §6 risks → throwaway-account-first noted in Tasks 4b/5; IMAP gitignored in Task 4b. ✅
