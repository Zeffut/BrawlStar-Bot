"""End-to-end account estimation on the BlueStacks emulator.

Self-contained — does NOT import the bot's heavy game_api/utils chain.
Pipeline: resolve tag (explicit, else OCR auto-detect) -> brawlace profile
-> live currency OCR -> valuation.

Usage:
    python3 -m revente.estimate_account [--tag PYLV98LG9] [--serial 127.0.0.1:5555]

If --tag is omitted, the loaded account's tag is auto-detected from the
profile screen (best-effort OCR). For your own fleet accounts, passing
--tag is the reliable path (store it in revente/inventaire_template.csv).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from revente.estimate import AccountData, estimate
from revente.read_currencies import read_lobby_numbers
from revente.read_hypercharges import count_hypercharges
from revente.read_tag import _validate, detect_tag


def _default_serial() -> str:
    p = Path(__file__).resolve().parent.parent / "cfg" / "device.toml"
    try:
        import tomllib  # lazy: stdlib only on 3.11+; keep module importable on 3.9
        with p.open("rb") as f:
            return tomllib.load(f).get("serial", "127.0.0.1:5555")
    except Exception:
        return "127.0.0.1:5555"


def _load_override(tag: str | None) -> dict:
    """Read detection-assisted manual overrides (hypercharges, rare_skins) for a tag
    from revente/account_overrides.csv. These are the RELIABLE source for fields the
    game/API don't expose — see the file header. Returns {} if absent/unreadable."""
    if not tag:
        return {}
    key = tag.lstrip("#").upper()
    path = Path(__file__).resolve().parent / "account_overrides.csv"
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split(",")]
            if parts and parts[0].lstrip("#").upper() == key:
                out = {}
                if len(parts) > 1 and parts[1].isdigit():
                    out["hypercharges"] = int(parts[1])
                if len(parts) > 2 and parts[2].isdigit():
                    out["rare_skins"] = int(parts[2])
                return out
    except Exception:
        pass
    return {}


def run(tag: str | None = None, serial: str | None = None,
        scan_hypercharges: bool = False) -> dict:
    serial = serial or _default_serial()

    if tag:
        tag = "#" + tag.lstrip("#").upper()
        prof = _validate(tag.lstrip("#"))
    else:
        tag, prof = detect_tag(serial)

    if not prof:
        return {"ok": False, "error": f"could not resolve account profile (tag={tag})"}

    brawlers = prof.get("brawlers", [])
    power11 = sum(1 for b in brawlers if b.get("power") == 11)
    trophies = sum(b.get("trophies", 0) for b in brawlers)

    cur = read_lobby_numbers(serial)
    # cross-check: prefer brawlace sum, fall back to lobby OCR
    if not trophies and cur.get("trophies"):
        trophies = cur["trophies"]

    # Hypercharges/rare skins: prefer the reliable detection-assisted override file;
    # else the OPT-IN auto-scan (slow + under-counts live, see read_hypercharges);
    # else 0. The override is authoritative because the full screen-scrape walk is
    # defeated by Brawl Stars' dynamic UI and no API exposes hypercharge ownership.
    override = _load_override(tag)
    hc = count_hypercharges(serial) if scan_hypercharges else {"count": None, "brawlers": []}
    hypercharges = override.get("hypercharges")
    if hypercharges is None:
        hypercharges = hc.get("count") or 0

    data = AccountData(
        tag=tag or "?", name=prof.get("name") or "?", trophies=trophies,
        brawlers=len(brawlers), power11=power11,
        gems=cur.get("gems"), gold=cur.get("gold"), bling=cur.get("bling"),
        hypercharges=hypercharges, rare_skins=override.get("rare_skins", 0),
    )
    est = estimate(data)
    return {"ok": True, "account": data.__dict__, "estimate": est.__dict__,
            "hypercharge_brawlers": hc.get("brawlers", [])}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default=None)
    ap.add_argument("--serial", default=None)
    ap.add_argument("--scan-hc", action="store_true",
                    help="opt-in: auto-scan the collection for hypercharges (slow, experimental)")
    args = ap.parse_args()
    print(json.dumps(run(args.tag, args.serial, scan_hypercharges=args.scan_hc),
                     ensure_ascii=False, indent=2))
