import tomllib
from schedule_config import DEFAULTS, merge_defaults, to_toml


def test_defaults_roundtrip():
    txt = to_toml({})
    parsed = tomllib.loads(txt)["schedule"]
    assert parsed["enabled"] is True
    assert parsed["daily_match_cap"] == 180
    assert parsed["sleep_start_hour"] == 1


def test_full_config_roundtrip():
    cfg = {
        "enabled": True, "daily_match_cap": 150, "dayoff_weekdays": ["sunday"],
        "dayoff_chance": 0.1,
        "pause_windows": [{"start": "12:30", "end": "13:30", "jitter_minutes": 20, "label": "dej"}],
        "weekend": {"daily_match_cap": 260, "sleep_start_hour": 2},
    }
    parsed = tomllib.loads(to_toml(cfg))["schedule"]
    assert parsed["daily_match_cap"] == 150
    assert parsed["dayoff_weekdays"] == ["sunday"]
    assert abs(parsed["dayoff_chance"] - 0.1) < 1e-9
    assert parsed["pause_windows"][0]["label"] == "dej"
    assert parsed["pause_windows"][0]["start"] == "12:30"
    assert parsed["weekend"]["daily_match_cap"] == 260


def test_merge_validates_and_clamps():
    m = merge_defaults({"dayoff_chance": 5.0, "dayoff_weekdays": ["sunday", "bogus"],
                        "daily_match_cap": "abc"})
    assert m["dayoff_chance"] == 1.0
    assert m["dayoff_weekdays"] == ["sunday"]
    assert m["daily_match_cap"] == 180  # invalid → default


def test_label_with_quotes_is_escaped():
    txt = to_toml({"pause_windows": [{"start": "1:00", "end": "2:00", "label": 'a"b'}]})
    parsed = tomllib.loads(txt)["schedule"]
    assert parsed["pause_windows"][0]["label"] == 'a"b'
