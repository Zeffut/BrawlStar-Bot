"""Tests for the humane play schedule (no 24/7 grind)."""
import time

from play_schedule import PlaySchedule

CFG = {
    "enabled": True,
    "sleep_start_hour": 1, "sleep_end_hour": 9,
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
    s = PlaySchedule(CFG)
    now = _ts(14)
    assert s.should_play_now(now)[0]
    s.break_min = s.break_max = 30          # force a 30-min break
    s.start_break(now)
    assert not s.should_play_now(now + 5 * 60)[0]      # 5 min in → still break
    assert "pause" in s.should_play_now(now + 5 * 60)[1]
    assert s.should_play_now(now + 31 * 60)[0]         # after 30 min → clear


def test_daily_cap_blocks_play():
    s = PlaySchedule({**CFG, "daily_match_cap": 5, "daily_cap_jitter": 0})
    now = _ts(14)
    for _ in range(5):
        s.record_match(now)
    ok, why = s.should_play_now(now)
    assert not ok and "quota" in why


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
