"""Auto-detect the player tag of the account currently loaded on the
emulator, then validate it against brawlace (via the cloud-panel proxy).

Light + self-contained: easyocr + adb + the panel proxy. Borrows the
avatar-tap coords and tag scan-band from account_detect.py but does not
import its heavy `utils` chain (discord/aiohttp).
"""
from __future__ import annotations

import io
import re
import subprocess
import time
import tomllib
from pathlib import Path

from revente.read_currencies import _ocr_digits_reader, _screencap

AVATAR_TAP_RATIO = (0.08, 0.085)
# Supercell player tags use a reduced alphabet — constraining the OCR
# allowlist to it kills impossible misreads (B, I, 1, O…).
SUPERCELL_ALPHABET = "0289PYLQGRJCUV"
TAG_CHARS = set(SUPERCELL_ALPHABET)
_TAG_RE = re.compile(r"#?([0289PYLQGRJCUV]{5,12})")
_MAX_VALIDATIONS = 5  # brawlace validation is ~14s/call — keep this small


def _adb(serial: str, *args) -> None:
    subprocess.run(["adb", "-s", serial, *args], capture_output=True, timeout=15)


def _cloud_url() -> str | None:
    p = Path(__file__).resolve().parent.parent / "cfg" / "cloud.toml"
    try:
        with p.open("rb") as f:
            return tomllib.load(f).get("url")
    except Exception:
        return None


def _validate(tag: str) -> dict | None:
    """Return the brawlace profile for `tag` if it has brawlers, else None."""
    import requests
    base = _cloud_url()
    if not base:
        return None
    try:
        r = requests.get(f"{base.rstrip('/')}/api/util/brawler_profile/{tag}", timeout=40)
        if r.status_code == 200:
            j = r.json()
            if j.get("brawlers"):
                return j
    except Exception:
        pass
    return None


def _ocr_tag_candidates(profile_img) -> list[str]:
    """OCR the upper-left profile band for tag candidates (ranked, deduped).

    Best-effort: the stylised thin tag font is hard — easyocr sometimes
    drops a character. We crop the tag line tightly and OCR at several
    upscales with the Supercell-alphabet allowlist, then return the
    distinct reads (longest first) for the caller to validate.
    """
    import numpy as np
    from PIL import Image
    reader = _ocr_digits_reader()
    w, h = profile_img.size
    crop = profile_img.crop((int(w * 0.0), int(h * 0.275), int(w * 0.20), int(h * 0.345)))
    seen: set[str] = set()
    ranked: list[str] = []
    for scale in (6, 5, 8, 4):
        c = crop.resize((crop.width * scale, crop.height * scale), Image.LANCZOS)
        for key in reader.readtext(np.array(c), allowlist="#" + SUPERCELL_ALPHABET, detail=0):
            m = _TAG_RE.search(key.upper())
            if not m:
                continue
            cand = m.group(1)
            if cand not in seen:
                seen.add(cand)
                ranked.append(cand)
    ranked.sort(key=len, reverse=True)  # a 9-char read beats an 8-char drop
    return ranked


def detect_tag(serial: str) -> tuple[str | None, dict | None]:
    """Open the profile, OCR the tag, return (tag, profile) — both None on
    failure. Returns to the lobby afterwards. The profile is the validated
    brawlace payload (name + brawlers) so the caller need not re-fetch."""
    from PIL import Image
    lobby = Image.open(io.BytesIO(_screencap(serial))).convert("RGB")
    w, h = lobby.size
    _adb(serial, "shell", "input", "tap",
         str(int(w * AVATAR_TAP_RATIO[0])), str(int(h * AVATAR_TAP_RATIO[1])))
    time.sleep(2.0)
    profile_img = Image.open(io.BytesIO(_screencap(serial))).convert("RGB")
    _adb(serial, "shell", "input", "keyevent", "4")  # back to lobby
    time.sleep(1.0)
    for cand in _ocr_tag_candidates(profile_img)[:_MAX_VALIDATIONS]:
        prof = _validate(cand)
        if prof:
            return "#" + cand, prof
    return None, None
