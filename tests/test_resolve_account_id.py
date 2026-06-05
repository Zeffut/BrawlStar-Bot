"""Regression tests for _try_resume_session's account-binding fix.

A cold HP reboot can let the auto-resume thread win the race against the
_bootstrap_account thread, leaving runner._account_id None. Because
db.start_session / brawler_efficiency / log_match are ALL gated on
_account_id, the bot then grinds for hours recording NOTHING and on
tier-prior order instead of the real data-driven one. The fix resolves the
account id from the local DB before starting; this tests that resolution.

telegram_main is too heavy to import in CI (easyocr, discord, …), so — as
with test_target_reached_edge — we mirror the pure helper logic here.
"""
from __future__ import annotations


def _resolve_account_id(accounts, serial):
    """Mirror of telegram_main._resolve_account_id."""
    match = next((a for a in accounts if a.get("device_serial") == serial), None)
    if match is None and len(accounts) == 1:
        match = accounts[0]
    return match["id"] if match else None


def test_exact_serial_match():
    accs = [
        {"id": 2, "tag": "QPRCQ9RV2", "device_serial": "192.168.60.18:5555"},
        {"id": 5, "tag": "OTHER", "device_serial": "192.168.60.20:5555"},
    ]
    assert _resolve_account_id(accs, "192.168.60.18:5555") == 2
    assert _resolve_account_id(accs, "192.168.60.20:5555") == 5


def test_single_account_fallback_when_serial_unknown():
    # The real cold-boot case: device down → serial None, but the sole
    # account is unambiguous → bind it rather than grind un-persisted.
    accs = [{"id": 2, "tag": "QPRCQ9RV2", "device_serial": "192.168.60.18:5555"}]
    assert _resolve_account_id(accs, None) == 2


def test_single_account_fallback_when_serial_mismatch():
    # Serial changed (e.g. new WiFi IP) but it's still the only account.
    accs = [{"id": 2, "tag": "QPRCQ9RV2", "device_serial": "192.168.60.18:5555"}]
    assert _resolve_account_id(accs, "192.168.60.99:5555") == 2


def test_ambiguous_multi_account_no_serial_match_defers():
    # >1 account and no serial match → None → caller DEFERS (never guesses).
    accs = [
        {"id": 2, "device_serial": "192.168.60.18:5555"},
        {"id": 5, "device_serial": "192.168.60.20:5555"},
    ]
    assert _resolve_account_id(accs, None) is None
    assert _resolve_account_id(accs, "192.168.60.77:5555") is None


def test_empty_db_defers():
    assert _resolve_account_id([], "192.168.60.18:5555") is None
    assert _resolve_account_id([], None) is None
