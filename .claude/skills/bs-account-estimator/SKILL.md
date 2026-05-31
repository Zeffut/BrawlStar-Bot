---
name: bs-account-estimator
description: Use when estimating the resale value of a Brawl Stars account running on the local BlueStacks emulator. Drives the emulator via ADB, reads trophies/brawlers/gems/gold (easyocr), and outputs a price estimate from the benchmark grid in revente/grille_prix.md.
---

# Brawl Stars Account Estimator

Estimates a Brawl Stars account's resale value by driving the local
**BlueStacks Air** emulator over ADB. Self-contained: uses adb + easyocr +
the brawlace proxy, NOT the bot's heavy game_api/utils chain.

## Prerequisites (one-time, by the user)
- BlueStacks Air running, Brawl Stars installed and **at the lobby**, an account logged in.
- ADB reachable: `adb connect 127.0.0.1:<port>` (BlueStacks → Settings → Advanced → Android Debug Bridge).
- `cfg/device.toml` → `serial = "127.0.0.1:<port>"` (already set to `127.0.0.1:5555`).
- Python deps in the 3.11 interpreter: `easyocr`, `requests`, `Pillow`, `numpy` (installed 2026-05-31).

## Usage

Always run with the project's 3.11 interpreter (bare `python3` is Xcode 3.9 and breaks):

```bash
# Reliable path — pass the known tag (recommended for your own accounts):
/opt/homebrew/bin/python3.11 -m revente.estimate_account --tag PYLV98LG9

# Autonomous path — auto-detect the loaded account's tag (best-effort OCR):
/opt/homebrew/bin/python3.11 -m revente.estimate_account

# Capture the brawler collection (to read skins / Power 11 / hypercharges by eye):
/opt/homebrew/bin/python3.11 -m revente.capture_collection <TAG> 127.0.0.1:5555
# → saves revente/captures/<TAG>_collection_*.png ; open them and set
#   rare_skins / hypercharges in AccountData to refine the estimate.
```

Output: JSON `{ok, account{tag,name,trophies,brawlers,power11,gems,gold}, estimate{tier,price_min_usd,price_max_usd,confidence,notes}}`.

## What works
- **Currency OCR** (trophies/gems/gold) — easyocr, verified live. ✅
- **Profile data** (trophies/brawlers/Power 11) — via brawlace proxy from the tag. ✅
- **Valuation** — `revente/estimate.py` against `revente/grille_prix.md`. ✅
- **Emulator control** — capture, tap, navigate to profile. ✅

## Known limitations
- **Tag auto-detection is best-effort.** Brawl Stars' stylised thin tag font makes
  easyocr drop a character (e.g. reads `PYV98LG9` for `PYLV98LG9`), and brawlace
  validation is ~14 s/call so we cannot brute-force. **Pass `--tag` for reliability**;
  store each account's tag in `revente/inventaire_template.csv`.
- **Skins / hypercharges are not auto-counted** (no public source; ambiguous in-grid).
  Use `revente.capture_collection` to grab the collection grid (per-brawler power &
  trophies are readable, e.g. Shelly P11), then read skins/hypercharges by eye and set
  `rare_skins`/`hypercharges` to refine the estimate upward — they are the biggest levers.
- **Account login/switching (Phase 2)** is not built yet — needs `cfg/imap.toml` creds.

## Files
- `revente/estimate.py` — valuation logic
- `revente/read_currencies.py` — live trophies/gems/gold OCR
- `revente/read_tag.py` — best-effort tag auto-detection
- `revente/estimate_account.py` — end-to-end orchestrator
- `revente/grille_prix.md` — pricing reference
