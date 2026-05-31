"""PushMax strategy invariants — stickiness, tier ordering, stagnation."""
from __future__ import annotations

from push_max import PushMaxStrategy, get_tier, BrawlerState


def test_tier_lookup():
    assert get_tier("brock") == "S"
    assert get_tier("shelly") == "A"
    assert get_tier("bull") == "B"
    assert get_tier("dynamike") == "C"
    assert get_tier("UNKNOWN_NEW_BRAWLER") == "B"  # default


def test_picks_highest_tier_first():
    owned = [
        {"name": "bull", "trophies": 100},     # B
        {"name": "brock", "trophies": 50},     # S
        {"name": "shelly", "trophies": 200},   # A
    ]
    s = PushMaxStrategy.from_owned(owned)
    pick = s.pick_next()
    assert pick.name == "brock", f"expected brock (S-tier), got {pick.name}"


def test_pick_is_sticky_until_exhausted():
    owned = [
        {"name": "brock", "trophies": 100},
        {"name": "shelly", "trophies": 100},
    ]
    s = PushMaxStrategy.from_owned(owned, defeat_limit=3)
    first = s.pick_next()
    assert first.name == "brock"
    # Win → still brock.
    s.record_match("brock", "victory", 110)
    assert s.pick_next().name == "brock"
    # 2 losses → still brock (limit=3).
    s.record_match("brock", "defeat", 108)
    s.record_match("brock", "defeat", 106)
    assert s.pick_next().name == "brock"
    # 3rd loss → brock exhausted; next pick is shelly.
    s.record_match("brock", "defeat", 104)
    assert s.brawlers["brock"].exhausted
    nxt = s.pick_next()
    assert nxt.name == "shelly"


def test_stagnation_window_triggers_exhausted():
    owned = [{"name": "brock", "trophies": 500}]
    s = PushMaxStrategy.from_owned(owned)
    s.pick_next()
    # Net 0 over the window: +1, -1, +1, -1, … should NOT exhaust (>0 net? no, exactly 0).
    # Make it slightly negative to force stagnation.
    deltas = [+2, -3, +1, -2, +2, -3, +1, -2]   # sum = -4
    t = 500
    for d in deltas:
        t += d
        s.record_match("brock", "victory" if d > 0 else "defeat", t)
    assert s.brawlers["brock"].exhausted, "should be exhausted via stagnation"


def test_stagnation_not_triggered_when_progressing():
    owned = [{"name": "brock", "trophies": 500}]
    s = PushMaxStrategy.from_owned(owned)
    s.pick_next()
    t = 500
    for d in [+5, -1, +4, -2, +3, -1, +2, -1]:  # sum = +9
        t += d
        s.record_match("brock", "victory" if d > 0 else "defeat", t)
    assert not s.brawlers["brock"].exhausted, "should keep grinding while net > 0"


def test_all_done():
    owned = [{"name": "brock", "trophies": 50}]
    s = PushMaxStrategy.from_owned(owned, defeat_limit=2)
    s.pick_next()
    s.record_match("brock", "defeat", 48)
    s.record_match("brock", "defeat", 46)
    assert s.all_done()
    assert s.pick_next() is None


def test_per_brawler_cap_exhausts_on_reach():
    owned = [
        {"name": "brock", "trophies": 900},    # S
        {"name": "shelly", "trophies": 100},   # A
    ]
    s = PushMaxStrategy.from_owned(owned, brawler_max_trophies=950)
    assert s.pick_next().name == "brock"
    # Win toward the cap but not reaching it → keep brock.
    s.record_match("brock", "victory", 945)
    assert not s.brawlers["brock"].exhausted
    assert s.pick_next().name == "brock"
    # Reach the cap → exhausted, rotate to the next brawler.
    s.record_match("brock", "victory", 955)
    assert s.brawlers["brock"].exhausted
    assert s.pick_next().name == "shelly"


def test_per_brawler_cap_skips_already_capped():
    owned = [
        {"name": "brock", "trophies": 1000},   # S, already at/above cap
        {"name": "shelly", "trophies": 100},   # A
    ]
    s = PushMaxStrategy.from_owned(owned, brawler_max_trophies=900)
    assert s.brawlers["brock"].exhausted          # never grind a capped brawler
    assert s.pick_next().name == "shelly"


def test_no_cap_keeps_pushing():
    owned = [{"name": "brock", "trophies": 1500}]
    s = PushMaxStrategy.from_owned(owned)          # no cap
    s.pick_next()
    s.record_match("brock", "victory", 1600)
    assert not s.brawlers["brock"].exhausted       # only defeats/stagnation exhaust
