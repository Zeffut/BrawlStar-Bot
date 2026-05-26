"""Pure-stdlib parser for brawlace.com player pages.

Extracted from app.py so tests can import it without dragging in
FastAPI (which requires py 3.10+).
"""
from __future__ import annotations

import re

BRAWLACE_ROW_RE = re.compile(
    r'/brawlers/([A-Za-z0-9_\-\.]+)\.png[^>]*>\s*([A-Z0-9 \.\-&!]+?)</td>'
    r'<td[^>]*>(\d+)</td>'
    r'<td[^>]*>.*?/tiers/\d+\.png.*?</td>'
    r'<td[^>]*>(\d+)</td>',
    re.DOTALL,
)
BRAWLACE_NAME_RE = re.compile(
    r'<meta name="description" content="([^"]+?) Brawl Stars Stats',
    re.IGNORECASE,
)


def parse_profile(html: str) -> dict:
    """Return {name, brawlers:[{name, power, trophies}, ...]} from brawlace HTML."""
    nm = BRAWLACE_NAME_RE.search(html)
    name = nm.group(1).strip() if nm else None
    brawlers = []
    for _img, display, power, trophies in BRAWLACE_ROW_RE.findall(html):
        brawlers.append({
            "name": display.strip().lower(),
            "power": int(power),
            "trophies": int(trophies),
        })
    return {"name": name, "brawlers": brawlers}
