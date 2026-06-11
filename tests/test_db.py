"""db.latest_account_trophies — most recent account-wide trophy total.

Runs against a throwaway SQLite file so it doesn't touch the real bot.db.
"""
from __future__ import annotations

import time

import pytest

import db


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    """Point db at an isolated temp file and reset its cached connection."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(db, "_conn", None)
    db.init()
    yield db
    if db._conn is not None:
        db._conn.close()
        db._conn = None


def _log_match(account_id, account_trophies_after, ts):
    """Insert a minimal match row directly (FKs not enforced, dummy session_id)."""
    db.conn().execute(
        "INSERT INTO matches(session_id, account_id, brawler, result, "
        "trophies_before, trophies_after, account_trophies_after, timestamp) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (1, account_id, "colt", "victory", 100, 108, account_trophies_after, ts),
    )


# ------------------------------------------------------------------ tests


def test_latest_account_trophies_returns_most_recent(fresh_db):
    """Returns the account_trophies_after of the MOST RECENT match."""
    now = time.time()
    aid = db.upsert_account("#TEST1", "TestAcc")
    _log_match(aid, 21000, now - 200)  # older
    _log_match(aid, 21008, now - 100)  # most recent
    assert db.latest_account_trophies(aid) == 21008


def test_latest_account_trophies_none_for_unknown_account(fresh_db):
    """Returns None when the account has no matches at all."""
    assert db.latest_account_trophies(99999) is None


def test_latest_account_trophies_none_for_none_input(fresh_db):
    """Returns None when account_id is None (convenience guard)."""
    assert db.latest_account_trophies(None) is None


def test_latest_account_trophies_skips_null_values(fresh_db):
    """Returns None when all matches have NULL account_trophies_after."""
    now = time.time()
    aid = db.upsert_account("#TEST2", "TestAcc2")
    _log_match(aid, None, now - 100)  # NULL — should be ignored
    assert db.latest_account_trophies(aid) is None
