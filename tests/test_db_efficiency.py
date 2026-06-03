"""db.brawler_efficiency — the realized per-brawler stats that feed push_max's
data-driven pick order. Runs against a throwaway SQLite file so it doesn't
touch the real bot.db."""
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


def _log(account_id, brawler, result, before, after, ts):
    # session_id is NOT NULL in the local schema; FKs aren't enforced (no
    # PRAGMA foreign_keys), so a dummy id is fine for these read-path tests.
    db.conn().execute(
        "INSERT INTO matches(session_id, account_id, brawler, result, "
        "trophies_before, trophies_after, account_trophies_after, timestamp) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (1, account_id, brawler, result, before, after, after, ts),
    )


def test_brawler_efficiency_basic(fresh_db):
    now = time.time()
    aid = db.upsert_account("TAG1", "Acc")
    # carl: 3 wins, net +30 → npm 10, wr 100
    _log(aid, "carl", "victory", 100, 110, now - 300)
    _log(aid, "carl", "victory", 110, 120, now - 200)
    _log(aid, "carl", "victory", 120, 130, now - 100)
    # brock: 1 win 1 loss, net +2 → npm 1, wr 50
    _log(aid, "brock", "victory", 500, 508, now - 250)
    _log(aid, "brock", "defeat", 508, 502, now - 150)

    eff = db.brawler_efficiency(aid, days=30, now=now)
    assert set(eff) == {"carl", "brock"}
    assert eff["carl"] == {"matches": 3, "wins": 3, "net": 30,
                           "net_per_match": 10.0, "win_rate": 100}
    assert eff["brock"]["matches"] == 2
    assert eff["brock"]["net"] == 2
    assert eff["brock"]["net_per_match"] == 1.0
    assert eff["brock"]["win_rate"] == 50


def test_brawler_efficiency_window_excludes_old_matches(fresh_db):
    now = time.time()
    aid = db.upsert_account("TAG2", "Acc")
    _log(aid, "colt", "victory", 100, 110, now - 2 * 86400)   # inside 30d
    _log(aid, "colt", "victory", 110, 120, now - 40 * 86400)  # outside 30d
    eff = db.brawler_efficiency(aid, days=30, now=now)
    assert eff["colt"]["matches"] == 1
    assert eff["colt"]["net"] == 10


def test_brawler_efficiency_skips_null_endpoints(fresh_db):
    now = time.time()
    aid = db.upsert_account("TAG3", "Acc")
    _log(aid, "gene", "victory", None, None, now - 100)       # ignored (no deltas)
    _log(aid, "gene", "victory", 200, 209, now - 50)
    eff = db.brawler_efficiency(aid, days=30, now=now)
    assert eff["gene"]["matches"] == 1
    assert eff["gene"]["net"] == 9


def test_brawler_efficiency_keys_lowercased(fresh_db):
    now = time.time()
    aid = db.upsert_account("TAG4", "Acc")
    _log(aid, "8-Bit", "victory", 100, 110, now - 100)
    eff = db.brawler_efficiency(aid, days=30, now=now)
    assert "8-bit" in eff
