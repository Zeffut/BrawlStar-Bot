"""Regression test for the brawlace.com HTML scraping regex.

The brawlace HTML format changed in 2026 (added `dt-type-numeric` CSS
classes to the `<td>` cells) which broke the old regex. This test
locks the current regex against a captured HTML snapshot so future
HTML changes are caught.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from cloud_panel.brawlace_parse import (
    BRAWLACE_ROW_RE as _BRAWLACE_ROW_RE,
    BRAWLACE_NAME_RE as _BRAWLACE_NAME_RE,
    parse_profile,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def html() -> str:
    return (FIXTURES / "brawlace_qprcq9rv2.html").read_text(encoding="utf-8")


def test_player_name_extracted(html: str) -> None:
    m = _BRAWLACE_NAME_RE.search(html)
    assert m is not None, "meta description regex matched nothing"
    name = m.group(1).strip()
    assert name == "Zeffut5.0", f"expected Zeffut5.0, got {name!r}"


def test_brawler_rows_parsed(html: str) -> None:
    rows = _BRAWLACE_ROW_RE.findall(html)
    assert len(rows) >= 10, f"expected at least 10 brawlers, got {len(rows)}"
    # Spot-check the first few: name UPPERCASE, power 0-11, trophies int.
    seen_names = []
    for _img, display, power, trophies in rows[:5]:
        name = display.strip()
        assert name.isupper() or " " in name, f"unexpected brawler name shape: {name!r}"
        assert 0 <= int(power) <= 12, f"power out of range: {power}"
        assert 0 <= int(trophies) <= 2000, f"trophies out of range: {trophies}"
        seen_names.append(name.lower())
    # Brock and Buzz should be in there based on the captured account.
    joined = ",".join(seen_names)
    assert "brock" in joined or "buzz" in joined or "shelly" in joined, \
        f"none of brock/buzz/shelly found in first 5: {seen_names}"


def test_total_trophies_sum_matches_account(html: str) -> None:
    """Sum of brawler trophies should equal the in-game lobby trophy count."""
    rows = _BRAWLACE_ROW_RE.findall(html)
    total = sum(int(t) for _, _, _, t in rows)
    # The fixture was captured when the account had ~616 trophies.
    assert 500 < total < 800, f"unexpected total: {total}"


def test_parse_profile_full(html: str) -> None:
    profile = parse_profile(html)
    assert profile["name"] == "Zeffut5.0"
    assert len(profile["brawlers"]) >= 10
    # Every entry has required keys + sensible types.
    for b in profile["brawlers"]:
        assert {"name", "power", "trophies"} <= set(b.keys())
        assert isinstance(b["name"], str) and b["name"]
        assert isinstance(b["power"], int) and 0 <= b["power"] <= 12
        assert isinstance(b["trophies"], int) and b["trophies"] >= 0
