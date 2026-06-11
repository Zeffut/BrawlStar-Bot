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


def _ts_on(weekday: int, hour: int) -> float:
    """Timestamp at given LOCAL hour on the NEXT date matching `weekday` (0=Mon)."""
    import datetime as _dt
    d = _dt.date.today()
    while d.weekday() != weekday:
        d += _dt.timedelta(days=1)
    lt = time.struct_time((d.year, d.month, d.day, hour, 30, 0,
                           weekday, 0, -1))
    return time.mktime(lt)


def test_pause_window_blocks_play():
    cfg = {**CFG, "pause_windows": [{"start": "12:00", "end": "13:00",
                                     "jitter_minutes": 0, "label": "déjeuner"}]}
    s = PlaySchedule(cfg)
    ok, why = s.should_play_now(_ts(12))     # 12:30 — inside the lunch window
    assert not ok and "déjeuner" in why
    assert s.state(_ts(12)) == "pause"
    assert s.should_play_now(_ts(15))[0]     # 15:30 — active


def test_max_blocks_per_day_caps():
    s = PlaySchedule({**CFG, "max_blocks_per_day": 2, "blocks_jitter": 0})
    now = _ts(14)
    s.block_minutes(); s.block_minutes()     # consume 2 blocks
    ok, why = s.should_play_now(now)
    assert not ok and "quota" in why
    assert s.state(now) == "cap"


def test_dayoff_explicit_weekday():
    s = PlaySchedule({**CFG, "dayoff_weekdays": ["sunday"]})
    sun = _ts_on(6, 15)                       # Sunday 15:30
    ok, why = s.should_play_now(sun)
    assert not ok and "repos" in why
    assert s.state(sun) == "dayoff"
    assert s.should_play_now(_ts_on(2, 15))[0]   # Wednesday active


def test_dayoff_chance_deterministic():
    # chance=1.0 → every day is off; chance=0.0 → never.
    assert PlaySchedule({**CFG, "dayoff_chance": 1.0}).state(_ts(15)) == "dayoff"
    assert PlaySchedule({**CFG, "dayoff_chance": 0.0}).state(_ts(15)) != "dayoff"


def test_weekend_override_changes_cap():
    cfg = {**CFG, "daily_match_cap": 5, "daily_cap_jitter": 0,
           "weekend": {"daily_match_cap": 2}}
    s = PlaySchedule(cfg)
    sat = _ts_on(5, 14)
    s.record_match(sat); s.record_match(sat)
    assert not s.should_play_now(sat)[0]      # weekend cap=2 hit


def test_hot_reload_picks_up_new_mtime(tmp_path, monkeypatch):
    """Hot-reload without the heavy `utils`/`toml` dep: inject a fake utils
    module whose load_toml_as_dict reads our tmp TOML via a tiny parser, and
    drive the config purely through file mtimes."""
    import sys, types, play_schedule as ps

    def _tiny_load(path):
        out = {"schedule": {}}
        try:
            with open(path) as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or line.startswith("["):
                        continue
                    k, _, v = line.partition("=")
                    k = k.strip(); v = v.strip()
                    if v in ("true", "false"):
                        out["schedule"][k] = (v == "true")
                    else:
                        try: out["schedule"][k] = int(v)
                        except ValueError: out["schedule"][k] = v.strip('"')
        except OSError:
            pass
        return out

    fake_utils = types.ModuleType("utils")
    fake_utils.load_toml_as_dict = _tiny_load
    monkeypatch.setitem(sys.modules, "utils", fake_utils)

    base = tmp_path / "general_config.toml"
    local = tmp_path / "schedule.local.toml"
    base.write_text(
        "[schedule]\nenabled = true\nsleep_start_hour = 1\nsleep_end_hour = 9\n"
        "block_min_minutes = 40\nblock_max_minutes = 85\n"
        "break_min_minutes = 20\nbreak_max_minutes = 70\n"
        "daily_match_cap = 200\ndaily_cap_jitter = 0\n")
    monkeypatch.setattr(ps, "_config_paths", lambda: [str(base), str(local)])

    s = ps.PlaySchedule()                 # loads from files via fake utils
    now = _ts(14)
    assert s.daily_cap == 200
    local.write_text("[schedule]\ndaily_match_cap = 100\ndaily_cap_jitter = 0\n")
    s._last_reload_check = 0.0             # bypass the 10 s throttle
    s.state(now + 1)                       # triggers _maybe_reload
    assert s.daily_cap == 100             # tunables updated in place


def test_hot_reload_preserves_day_counters(tmp_path, monkeypatch):
    """A mid-day config reload must NOT zero the match counter (cap-safety):
    the bot must not be able to blow past the daily cap by saving config."""
    import sys, types, play_schedule as ps

    def _tiny_load(path):
        out = {"schedule": {}}
        try:
            with open(path) as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or line.startswith("["):
                        continue
                    k, _, v = line.partition("=")
                    k = k.strip(); v = v.strip()
                    if v in ("true", "false"):
                        out["schedule"][k] = (v == "true")
                    else:
                        try: out["schedule"][k] = int(v)
                        except ValueError: out["schedule"][k] = v.strip('"')
        except OSError:
            pass
        return out

    fake_utils = types.ModuleType("utils")
    fake_utils.load_toml_as_dict = _tiny_load
    monkeypatch.setitem(sys.modules, "utils", fake_utils)

    base = tmp_path / "general_config.toml"
    local = tmp_path / "schedule.local.toml"
    base.write_text(
        "[schedule]\nenabled = true\nsleep_start_hour = 1\nsleep_end_hour = 9\n"
        "block_min_minutes = 40\nblock_max_minutes = 85\n"
        "break_min_minutes = 20\nbreak_max_minutes = 70\n"
        "daily_match_cap = 200\ndaily_cap_jitter = 0\n")
    monkeypatch.setattr(ps, "_config_paths", lambda: [str(base), str(local)])

    s = ps.PlaySchedule()
    now = _ts(14)
    for _ in range(50):
        s.record_match(now)
    assert s._matches_today == 50
    # Save a config change mid-day → reload must keep the counter.
    local.write_text("[schedule]\ndaily_match_cap = 100\ndaily_cap_jitter = 0\n")
    s._last_reload_check = 0.0
    s.state(now + 1)                      # triggers _maybe_reload
    assert s.daily_cap == 100             # tunables updated
    assert s._matches_today == 50         # counter PRESERVED (cap-safety)


def test_daily_cap_seeds_from_provider_restart_safe():
    """The daily cap must survive worker restarts: a fresh PlaySchedule seeds
    _matches_today from the DB-backed provider (matches already played today),
    not 0. Otherwise every restart resets the counter and the cap is defeated."""
    import play_schedule as ps
    ps.set_match_count_provider(lambda: 200)   # 200 already played today (DB)
    try:
        s = ps.PlaySchedule({**CFG, "daily_match_cap": 180, "daily_cap_jitter": 0})
        now = _ts(14)
        ok, why = s.should_play_now(now)
        assert s._matches_today == 200          # seeded from DB, not 0
        assert not ok and "quota" in why        # already over cap=180 → blocked
    finally:
        ps.set_match_count_provider(None)


def test_no_provider_seeds_zero_backward_compatible():
    import play_schedule as ps
    ps.set_match_count_provider(None)
    s = ps.PlaySchedule({**CFG, "daily_match_cap": 5, "daily_cap_jitter": 0})
    now = _ts(14)
    assert s._matches_today == 0                 # no provider → 0 (unchanged)
    assert s.should_play_now(now)[0]


# ---- sale_ready gate (auto-stop at trophy sale target) ----

def test_sale_ready_when_total_reaches_target():
    s = PlaySchedule({"enabled": True, "sleep_start_hour": 3,
                      "sleep_end_hour": 4, "sleep_jitter_minutes": 0,
                      "sale_target_trophies": 25000})
    s.set_trophy_total_provider(lambda: 24999)
    noon = _ts(12)
    assert s.state(noon) == "play"
    s.set_trophy_total_provider(lambda: 25000)
    assert s.state(noon) == "sale_ready"


def test_sale_target_zero_never_triggers():
    s = PlaySchedule({"enabled": True, "sleep_start_hour": 3,
                      "sleep_end_hour": 4, "sleep_jitter_minutes": 0,
                      "sale_target_trophies": 0})
    s.set_trophy_total_provider(lambda: 999999)
    assert s.state(_ts(12)) == "play"


def test_sleep_outranks_sale_ready():
    s = PlaySchedule({"enabled": True, "sleep_start_hour": 1,
                      "sleep_end_hour": 9, "sleep_jitter_minutes": 0,
                      "sale_target_trophies": 25000})
    s.set_trophy_total_provider(lambda: 30000)
    assert s.state(_ts(3)) == "sleep"
