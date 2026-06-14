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
import tomllib
from pathlib import Path

from revente.estimate import AccountData, estimate
from revente.read_currencies import read_lobby_numbers
from revente.read_hypercharges import count_hypercharges
from revente.read_tag import _validate, detect_tag


def _default_serial() -> str:
    p = Path(__file__).resolve().parent.parent / "cfg" / "device.toml"
    try:
        with p.open("rb") as f:
            return tomllib.load(f).get("serial", "127.0.0.1:5555")
    except Exception:
        return "127.0.0.1:5555"


def run(tag: str | None = None, serial: str | None = None) -> dict:
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

    hc = count_hypercharges(serial)

    data = AccountData(
        tag=tag or "?", name=prof.get("name") or "?", trophies=trophies,
        brawlers=len(brawlers), power11=power11,
        gems=cur.get("gems"), gold=cur.get("gold"), bling=cur.get("bling"),
        hypercharges=(hc.get("count") or 0),
    )
    est = estimate(data)
    return {"ok": True, "account": data.__dict__, "estimate": est.__dict__,
            "hypercharge_brawlers": hc.get("brawlers", [])}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default=None)
    ap.add_argument("--serial", default=None)
    args = ap.parse_args()
    print(json.dumps(run(args.tag, args.serial), ensure_ascii=False, indent=2))
