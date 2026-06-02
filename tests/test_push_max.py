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


def test_prefers_higher_tier_over_lower_trophy_lower_tier():
    # Don't grind an A at 100 while an S below the ceiling is available —
    # Pyla wins more with S-tier so it climbs further/more reliably.
    owned = [
        {"name": "shelly", "trophies": 100},   # A, low trophies
        {"name": "brock", "trophies": 400},    # S, higher trophies but < ceiling
    ]
    s = PushMaxStrategy.from_owned(owned)
    assert s.pick_next().name == "brock"


def test_tier_priority_within_ceiling_then_lowest_trophies():
    owned = [
        {"name": "brock", "trophies": 600},   # S
        {"name": "bea", "trophies": 120},     # S  ← lowest-trophy S, expected
        {"name": "shelly", "trophies": 30},   # A, lower still but wrong tier
    ]
    s = PushMaxStrategy.from_owned(owned)
    assert s.pick_next().name == "bea"


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
    # Cap below the efficiency ceiling so the capped brawler is the one pushed.
    owned = [{"name": "shelly", "trophies": 250}]   # below ceiling + below cap
    s = PushMaxStrategy.from_owned(owned, brawler_max_trophies=300)
    s.pick_next()
    s.record_match("shelly", "victory", 290)        # under the cap → keep going
    assert not s.brawlers["shelly"].exhausted
    s.record_match("shelly", "victory", 305)        # reached the cap → done
    assert s.brawlers["shelly"].exhausted


def test_pushes_easiest_skips_above_ceiling():
    owned = [
        {"name": "brock", "trophies": 940},    # S-tier but above the ceiling
        {"name": "shelly", "trophies": 80},    # A-tier, lots of headroom
    ]
    s = PushMaxStrategy.from_owned(owned)
    # Despite brock's higher tier, a 940 brawler loses trophies on average →
    # push the easy shelly instead.
    assert s.pick_next().name == "shelly"


def test_above_ceiling_never_picked():
    # New contract: an above-ceiling brawler is NEVER auto-picked. When nothing
    # below the efficiency ceiling is left to grind, pick_next returns None so
    # the caller STOPS (grinding a 1000+ brawler nets ~0 = wasted battery).
    owned = [{"name": "brock", "trophies": 940}]   # only brawler, above ceiling
    s = PushMaxStrategy.from_owned(owned)
    assert s.pick_next() is None

    # A below-ceiling brawler IS picked, above-ceiling ones ignored.
    owned = [
        {"name": "brock", "trophies": 940},    # S, above ceiling → ignored
        {"name": "buzz", "trophies": 300},     # B, below ceiling → picked
    ]
    s = PushMaxStrategy.from_owned(owned)
    assert s.pick_next().name == "buzz"
    # Once the easy one exhausts, no fallback to the 940 brock → None (stop).
    s.brawlers["buzz"].exhausted = True
    s.current = None
    assert s.pick_next() is None


def test_per_brawler_cap_skips_already_capped():
    owned = [
        {"name": "brock", "trophies": 1000},   # S, already at/above cap
        {"name": "shelly", "trophies": 100},   # A
    ]
    s = PushMaxStrategy.from_owned(owned, brawler_max_trophies=900)
    assert s.brawlers["brock"].exhausted          # never grind a capped brawler
    assert s.pick_next().name == "shelly"


def test_zero_trophy_brawlers_are_grindable():
    # brawlace lists ONLY owned brawlers (mirrors the official API), so a
    # 0-trophy entry is an OWNED brawler never pushed in trophy mode — the best
    # grind target (max headroom). It must NOT be pre-exhausted.
    owned = [
        {"name": "frank", "trophies": 0},     # owned, power 7 IRL, 0 trophies
        {"name": "shelly", "trophies": 80},   # owned, already pushed a bit
    ]
    s = PushMaxStrategy.from_owned(owned)
    assert not s.brawlers["frank"].exhausted
    # frank (B) vs shelly (A): tier A sorts first, but both eligible. The point
    # is the 0-trophy brawler is a valid candidate.
    picked = s.pick_next()
    assert picked is not None
    assert not s.brawlers["frank"].exhausted

    # A 0-trophy S-tier brawler is picked first (tier S beats A, 0 = max headroom).
    owned = [
        {"name": "8-bit", "trophies": 0},     # S tier, 0 trophies
        {"name": "shelly", "trophies": 80},   # A tier
    ]
    s = PushMaxStrategy.from_owned(owned)
    assert s.pick_next().name == "8-bit"


def test_no_swap_stays_on_equipped_and_ignores_stagnation():
    owned = [{"name": "bea", "trophies": 484}, {"name": "shelly", "trophies": 50}]
    s = PushMaxStrategy.from_owned(owned, no_swap=True)
    # The equipped brawler is set explicitly (telegram_main does this from OCR).
    s.current = "bea"
    assert s.pick_next().name == "bea"
    # Losses / stagnation must NOT exhaust it (we can't reliably swap away).
    t = 484
    for _ in range(10):
        t -= 5
        s.record_match("bea", "defeat", t)
    assert not s.brawlers["bea"].exhausted
    assert s.pick_next().name == "bea"


def test_no_swap_stops_only_on_cap():
    owned = [{"name": "bea", "trophies": 480}]
    s = PushMaxStrategy.from_owned(owned, brawler_max_trophies=500, no_swap=True)
    s.current = "bea"
    s.record_match("bea", "victory", 495)        # under cap → keep grinding
    assert not s.brawlers["bea"].exhausted
    assert s.pick_next().name == "bea"
    s.record_match("bea", "victory", 505)        # cap reached → done, no swap
    assert s.brawlers["bea"].exhausted
    assert s.pick_next() is None


def test_revive_grindable_keeps_session_alive():
    owned = [
        {"name": "bea", "trophies": 484},      # S, grindable
        {"name": "shelly", "trophies": 200},   # A, grindable
        {"name": "8-bit", "trophies": 0},      # owned, 0 trophies → grindable
    ]
    s = PushMaxStrategy.from_owned(owned)
    s.brawlers["bea"].exhausted = True
    s.brawlers["shelly"].exhausted = True
    s.brawlers["8-bit"].exhausted = True
    assert s.all_done()
    # All three are grindable (>0 or 0-trophy owned, none capped) → revived.
    assert s.revive_grindable() == 3
    assert not s.brawlers["bea"].exhausted
    assert not s.brawlers["8-bit"].exhausted
    assert s.pick_next() is not None           # session can continue


def test_revive_skips_capped():
    owned = [{"name": "bea", "trophies": 900}]
    s = PushMaxStrategy.from_owned(owned, brawler_max_trophies=800)
    assert s.brawlers["bea"].exhausted         # already at/above cap
    assert s.revive_grindable() == 0           # capped → stays out


def test_no_cap_keeps_pushing():
    owned = [{"name": "brock", "trophies": 1500}]
    s = PushMaxStrategy.from_owned(owned)          # no cap
    s.pick_next()
    s.record_match("brock", "victory", 1600)
    assert not s.brawlers["brock"].exhausted       # only defeats/stagnation exhaust
