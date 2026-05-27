"""Regression tests for the target_reached edge-trigger logic.

The notif must fire exactly once on the match that crosses the
account-wide trophy target, and the bot must still stop even if the
target was already reached at session start (no edge to detect).
"""
from __future__ import annotations


def _simulate_match(account_before: int, delta: int, target: int,
                    already_notified: bool) -> tuple[bool, bool]:
    """Mirror the logic in telegram_main.wrapped().

    Returns (should_stop, should_notify).
    """
    account_after = account_before + delta
    should_stop = account_after >= target
    should_notify = (
        should_stop
        and (account_after - delta) < target
        and not already_notified
    )
    return should_stop, should_notify


def test_crosses_target_on_victory():
    # account 995 → +10 = 1005, target=1000 → edge, fire notif.
    stop, notify = _simulate_match(995, 10, 1000, False)
    assert stop is True
    assert notify is True


def test_already_above_target_at_start_still_stops():
    # account 1050 → +5 = 1055, target=1000 → already above, no edge,
    # but the bot must STILL stop.
    stop, notify = _simulate_match(1050, 5, 1000, False)
    assert stop is True
    assert notify is False  # no edge → no notif


def test_subsequent_match_above_target_does_not_renotify():
    # First match crossed → notified flag set. Next match still above
    # the target must not re-fire.
    stop, notify = _simulate_match(1005, 8, 1000, True)
    assert stop is True
    assert notify is False  # already notified


def test_bounce_below_then_back_above_no_duplicate_notif():
    # If the bot ever drops below the target and comes back, the
    # edge-trigger would re-fire — but the notified flag suppresses it.
    stop, notify = _simulate_match(995, 8, 1000, True)
    assert stop is True
    assert notify is False


def test_below_target_no_stop_no_notif():
    stop, notify = _simulate_match(500, 10, 1000, False)
    assert stop is False
    assert notify is False
