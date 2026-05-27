"""Regression tests for the battery gate logic.

Goal: the bot must never end up grinding while the phone is at risk of
dying. Covers all combinations of (level, charging, paused-flag).
"""
from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture
def api_factory():
    """Build a GameAPI-like object with controllable battery_status."""
    try:
        import game_api
    except ImportError as exc:
        pytest.skip(f"game_api deps missing: {exc}", allow_module_level=True)

    def make(level, charging, paused=False):
        api = game_api.GameAPI.__new__(game_api.GameAPI)
        api._battery_paused = paused
        with patch.object(api, "battery_status",
                          return_value={"level": level, "charging": charging}):
            ok, reason = api.can_play()
        return ok, reason, api._battery_paused

    return make


# ---- below LOW threshold ------------------------------------------


def test_low_not_charging_refuses(api_factory):
    ok, reason, paused = api_factory(level=20, charging=False)
    assert ok is False
    assert paused is True


def test_low_charging_still_refuses(api_factory):
    """Plugged in but still low: don't keep grinding. The whole point of
    the gate is to let it recharge to RESUME, charging alone isn't enough."""
    ok, reason, paused = api_factory(level=13, charging=True)
    assert ok is False
    assert paused is True


def test_critical_charging_refuses(api_factory):
    ok, reason, paused = api_factory(level=5, charging=True)
    assert ok is False


# ---- hysteresis ----------------------------------------------------


def test_paused_below_resume_stays_paused(api_factory):
    # Once paused, only RESUME_PCT clears it — even at 60% we wait.
    ok, reason, paused = api_factory(level=60, charging=True, paused=True)
    assert ok is False
    assert "recharging" in reason or "resume" in reason


def test_paused_at_resume_clears(api_factory):
    import game_api
    ok, reason, paused = api_factory(
        level=game_api.BATTERY_RESUME_PCT, charging=True, paused=True)
    assert ok is True
    assert paused is False


# ---- unknown (ADB failure) ----------------------------------------


def test_unknown_level_refuses(api_factory):
    """ADB returning None must be treated as danger, never as OK."""
    ok, reason, paused = api_factory(level=None, charging=None)
    assert ok is False
    assert "unknown" in reason.lower() or "adb" in reason.lower()
    assert paused is True


# ---- happy path ---------------------------------------------------


def test_high_level_allows_play(api_factory):
    ok, reason, paused = api_factory(level=85, charging=False)
    assert ok is True
    assert paused is False


def test_full_battery_charging_ok(api_factory):
    ok, reason, paused = api_factory(level=100, charging=True)
    assert ok is True
