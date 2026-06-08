"""Tests for the humane play schedule (no 24/7 grind)."""
import time

from play_schedule import PlaySchedule

CFG = {
    "enabled": True,
    "sleep_start_hour": 1, "sleep_end_hour": 9,
    "sleep_jitter_minutes": 0,   # deterministic boundaries for the base tests
    "block_min_minutes": 40, "block_max_minutes": 85,
    "break_min_minutes": 20, "break_max_minutes": 70,
    "daily_match_cap": 180, "daily_cap_jitter": 50,
}


def _ts(hour: int) -> float:
    """A timestamp at a given LOCAL hour today."""
    lt = time.localtime()
    base = time.struct_time((lt.tm_year, lt.tm_mon, lt.tm_mday, hour, 30, 0,
                             lt.tm_wday, lt.tm_yday, lt.tm_isdst))
    return time.mktime(base)


def test_sleep_window_blocks_play():
    s = PlaySchedule(CFG)
    ok, why = s.should_play_now(_ts(3))      # 03:30 — inside 1h–9h sleep
    assert not ok and "sommeil" in why
    ok, _ = s.should_play_now(_ts(14))       # 14:30 — active
    assert ok


def test_disabled_always_plays():
    s = PlaySchedule({**CFG, "enabled": False})
    assert s.should_play_now(_ts(3))[0] is True


def test_break_blocks_then_clears():
    # Breaks are tracked on the MONOTONIC clock (clock-jump safe), so we don't
    # drive them via the wall-clock `now` — we assert the gate directly.
    s = PlaySchedule(CFG)
    now = _ts(14)
    assert s.should_play_now(now)[0]
    s.break_min = s.break_max = 30          # force a 30-min break
    s.start_break()
    ok, why = s.should_play_now(now)
    assert not ok and "pause" in why        # in a break → blocked
    s._break_until = 0.0                     # break elapsed
    assert s.should_play_now(now)[0]         # → clear


def test_daily_cap_blocks_play():
    s = PlaySchedule({**CFG, "daily_match_cap": 5, "daily_cap_jitter": 0})
    now = _ts(14)
    for _ in range(5):
        s.record_match(now)
    ok, why = s.should_play_now(now)
    assert not ok and "quota" in why


def test_daily_reset_is_forward_only():
    # Regression: a backward clock jump crossing midnight must NOT re-roll the
    # daily cap (that would let the bot exceed it — defeats the anti-detection).
    s = PlaySchedule({**CFG, "daily_match_cap": 5, "daily_cap_jitter": 0})
    today = _ts(14)
    for _ in range(5):
        s.record_match(today)
    assert not s.should_play_now(today)[0]                 # quota hit
    # Clock rewinds to "yesterday" → date string goes backward → NO reset.
    assert not s.should_play_now(today - 86400)[0]
    # A genuine new day (forward) → reset → playable again.
    assert s.should_play_now(today + 86400)[0]


def test_state_categories():
    # state() drives whether the worker closes the game: 'sleep'/'cap' = long
    # pause (close BS), 'break' = short (leave open), 'play' = active.
    s = PlaySchedule({**CFG, "daily_match_cap": 3, "daily_cap_jitter": 0})
    assert s.state(_ts(14)) == "play"
    assert s.state(_ts(3)) == "sleep"          # 03:30 inside 1h–9h
    for _ in range(3):
        s.record_match(_ts(14))
    assert s.state(_ts(14)) == "cap"           # quota hit → long pause
    s2 = PlaySchedule(CFG)
    s2.break_min = s2.break_max = 30
    s2.start_break()
    assert s2.state(_ts(14)) == "break"        # short pause → keep game open
    assert PlaySchedule({**CFG, "enabled": False}).state(_ts(3)) == "play"


def test_block_minutes_in_range():
    s = PlaySchedule(CFG)
    for _ in range(50):
        assert 40 <= s.block_minutes() <= 85


def test_wrapping_sleep_window():
    # Sleep 23h → 7h (wraps midnight).
    s = PlaySchedule({**CFG, "sleep_start_hour": 23, "sleep_end_hour": 7})
    assert not s.should_play_now(_ts(2))[0]    # 02:30 — asleep
    assert not s.should_play_now(_ts(23))[0]   # 23:30 — asleep
    assert s.should_play_now(_ts(15))[0]       # 15:30 — active


# ---- variable (jittered) bed/wake times ----

def test_sleep_jitter_within_bounds():
    s = PlaySchedule({**CFG, "sleep_jitter_minutes": 40})
    s.state(_ts(3))                              # rolls today's window
    # base 01:00 = 60 min, 09:00 = 540 min; each wobbles ±40.
    assert 60 - 40 <= s._today_sleep_start_min <= 60 + 40
    assert 540 - 40 <= s._today_sleep_end_min <= 540 + 40


def test_sleep_jitter_deterministic_per_day_restart_stable():
    # Two fresh instances (= a worker restart) must roll the SAME window and
    # cap for a given day, so a restart never shifts the wake time nor re-rolls
    # the daily cap (which could otherwise let the bot exceed it).
    now = _ts(3)
    a = PlaySchedule({**CFG, "sleep_jitter_minutes": 40})
    b = PlaySchedule({**CFG, "sleep_jitter_minutes": 40})
    a.state(now)
    b.state(now)
    assert a._today_sleep_start_min == b._today_sleep_start_min
    assert a._today_sleep_end_min == b._today_sleep_end_min
    assert a._today_cap == b._today_cap


def test_sleep_jitter_varies_across_days():
    s = PlaySchedule({**CFG, "sleep_jitter_minutes": 40})
    windows = set()
    for d in range(20):                          # 20 consecutive (forward) days
        s.state(_ts(3) + d * 86400)
        windows.add((s._today_sleep_start_min, s._today_sleep_end_min))
    assert len(windows) > 1, "bed/wake times should differ day to day"


def test_jitter_keeps_deep_window_and_far_outside_correct():
    s = PlaySchedule({**CFG, "sleep_jitter_minutes": 40})
    assert s.state(_ts(4)) == "sleep"            # 04:30 — deep inside even ±40
    assert s.state(_ts(14)) == "play"            # 14:30 — far outside


from play_schedule import _parse_hhmm, _Window


def test_parse_hhmm():
    assert _parse_hhmm("01:00") == 60
    assert _parse_hhmm("12:30") == 750
    assert _parse_hhmm("9") == 540          # bare hour
    assert _parse_hhmm("23:59") == 1439
    assert _parse_hhmm("bad") is None
    assert _parse_hhmm("") is None


def test_window_contains_simple():
    w = _Window(start_min=60, end_min=540, jitter=0, label="sleep")  # 01:00–09:00
    assert w.contains(180)        # 03:00 inside
    assert not w.contains(600)    # 10:00 outside
    assert w.contains(60)         # inclusive start
    assert not w.contains(540)    # exclusive end


def test_window_contains_wraps_midnight():
    w = _Window(start_min=1380, end_min=420, jitter=0, label="sleep")  # 23:00–07:00
    assert w.contains(1410)       # 23:30 inside
    assert w.contains(60)         # 01:00 inside (after midnight)
    assert not w.contains(600)    # 10:00 outside


def test_window_empty_when_start_eq_end():
    w = _Window(start_min=300, end_min=300, jitter=0, label="x")
    assert not w.contains(300)
    assert not w.contains(0)


def test_window_roll_jittered_deterministic_and_bounded():
    import random
    w = _Window(start_min=60, end_min=540, jitter=40, label="sleep")
    r1 = w.rolled(random.Random("sched:2026-06-08"))
    r2 = w.rolled(random.Random("sched:2026-06-08"))
    assert (r1.start_min, r1.end_min) == (r2.start_min, r2.end_min)   # same seed
    assert abs(((r1.start_min - 60 + 720) % 1440) - 720) <= 40        # within ±40


from play_schedule import _resolve_day_params, _DEFAULTS


def test_resolve_day_params_base_only():
    base = {"daily_match_cap": 180, "sleep_start_hour": 1}
    out = _resolve_day_params(base, {}, weekday=2)   # mercredi
    assert out["daily_match_cap"] == 180
    assert out["sleep_start_hour"] == 1


def test_resolve_day_params_weekend_override():
    base = {"daily_match_cap": 180, "sleep_start_hour": 1}
    overrides = {"weekend": {"daily_match_cap": 260, "sleep_start_hour": 2}}
    sat = _resolve_day_params(base, overrides, weekday=5)   # samedi
    assert sat["daily_match_cap"] == 260 and sat["sleep_start_hour"] == 2
    wed = _resolve_day_params(base, overrides, weekday=2)   # mercredi
    assert wed["daily_match_cap"] == 180                    # base inchangée


def test_resolve_day_params_per_day_beats_weekend():
    base = {"daily_match_cap": 180}
    overrides = {"weekend": {"daily_match_cap": 260},
                 "days": {"sunday": {"daily_match_cap": 90}}}
    sun = _resolve_day_params(base, overrides, weekday=6)   # dimanche
    assert sun["daily_match_cap"] == 90                     # days.sunday gagne
    sat = _resolve_day_params(base, overrides, weekday=5)   # samedi
    assert sat["daily_match_cap"] == 260                    # weekend
