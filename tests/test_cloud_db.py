"""Cloud panel SQLite layer: brawler persistence + freshness queries."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest


@pytest.fixture
def cdb(tmp_path, monkeypatch):
    """Spin up a clean cloud_panel.db module pointing at a temp DB."""
    os.environ["CLOUD_DB_PATH"] = str(tmp_path / "test.db")
    # Force re-import so the module re-reads CLOUD_DB_PATH.
    for mod in list(sys.modules):
        if mod == "db" or mod.endswith(".db"):
            sys.modules.pop(mod, None)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cloud_panel"))
    import db as cdb  # noqa: E402
    # Force a fresh connection.
    cdb._conn = None
    cdb.init()
    return cdb


def test_set_and_get_brawlers(cdb):
    inst = cdb.upsert_instance("hp-mi9t", "HP + Mi9T")
    acc = cdb.upsert_account(inst, "QPRCQ9RV2", "Zeffut5.0")
    brawlers = [
        {"name": "brock", "power": 4, "trophies": 355},
        {"name": "buzz", "power": 7, "trophies": 212},
        {"name": "shelly", "power": 5, "trophies": 40},
    ]
    cdb.set_account_brawlers(acc, brawlers)
    stored, ts = cdb.get_account_brawlers(acc)
    assert stored == brawlers
    assert ts is not None and (time.time() - ts) < 5


def test_get_empty_when_unset(cdb):
    inst = cdb.upsert_instance("x", "X")
    acc = cdb.upsert_account(inst, "TAG", "n")
    stored, ts = cdb.get_account_brawlers(acc)
    assert stored == []
    assert ts is None


def test_accounts_needing_refresh(cdb):
    inst = cdb.upsert_instance("x", "X")
    fresh = cdb.upsert_account(inst, "AAA", None)
    stale = cdb.upsert_account(inst, "BBB", None)
    never = cdb.upsert_account(inst, "CCC", None)
    cdb.set_account_brawlers(fresh, [{"name": "a", "power": 1, "trophies": 1}])
    cdb.set_account_brawlers(stale, [{"name": "b", "power": 1, "trophies": 1}])
    # Manually backdate "stale" so its refreshed_at is > 1h old.
    cdb.conn().execute(
        "UPDATE accounts SET brawlers_refreshed_at = ? WHERE id = ?",
        (time.time() - 7200, stale),
    )
    needs = cdb.accounts_needing_refresh(stale_after_s=3600)
    ids = {a["id"] for a in needs}
    assert never in ids, "never-fetched account should always need refresh"
    assert stale in ids, "stale account should need refresh"
    assert fresh not in ids, "fresh account should NOT need refresh"


def test_latest_account_trophies(cdb):
    inst = cdb.upsert_instance("x", "X")
    acc = cdb.upsert_account(inst, "T", None)
    # No match yet → None (caller falls back to brawler sum).
    assert cdb.latest_account_trophies(acc) is None
    s = cdb.start_session(acc, "bea", 5000, 100, time.time())
    base = time.time()
    cdb.log_match(acc, s, "bea", "victory", 100, 110, 1010, timestamp=base)
    cdb.log_match(acc, s, "bea", "victory", 110, 118, 1018, timestamp=base + 1)
    # Most recent match wins, even though an older one exists.
    assert cdb.latest_account_trophies(acc) == 1018
    # A later match that didn't record a total must not shadow the last known.
    cdb.log_match(acc, s, "bea", "draw", 118, 118, None, timestamp=base + 2)
    assert cdb.latest_account_trophies(acc) == 1018


def test_match_stats_counts_whole_history(cdb):
    inst = cdb.upsert_instance("x", "X")
    acc = cdb.upsert_account(inst, "T", None)
    s = cdb.start_session(acc, "bea", 5000, 100, time.time())
    base = time.time()
    results = ["victory"] * 5 + ["defeat"] * 3 + ["draw"] * 2  # 10 total
    for i, r in enumerate(results):
        cdb.log_match(acc, s, "bea", r, 100, 110, 110, timestamp=base + i)
    st = cdb.match_stats(acc)
    assert st == {"total": 10, "wins": 5, "losses": 3, "draws": 2}
    # Empty account → all zeros (no division-by-zero downstream).
    other = cdb.upsert_account(inst, "U", None)
    assert cdb.match_stats(other) == {"total": 0, "wins": 0, "losses": 0, "draws": 0}


def test_end_running_sessions(cdb):
    inst = cdb.upsert_instance("hp-mi9t", "HP")
    a1 = cdb.upsert_account(inst, "AAA", None)
    a2 = cdb.upsert_account(inst, "BBB", None)
    s1 = cdb.start_session(a1, "brock", 99999, 100, time.time())
    s2 = cdb.start_session(a1, "shelly", 99999, 50, time.time())
    cdb.start_session(a2, "colt", 99999, 10, time.time())
    n = cdb.end_running_sessions(a1)
    assert n == 2
    rows = {r["id"]: r["status"] for r in cdb.list_sessions(a1)}
    assert rows[s1] == "stopped" and rows[s2] == "stopped"
    # Other account's running session must NOT be touched.
    assert {r["status"] for r in cdb.list_sessions(a2)} == {"running"}


def test_end_running_sessions_except(cdb):
    inst = cdb.upsert_instance("x", "X")
    a = cdb.upsert_account(inst, "T", None)
    s1 = cdb.start_session(a, "brock", 99999, 1, time.time())
    s2 = cdb.start_session(a, "shelly", 99999, 1, time.time())
    cdb.end_running_sessions(a, except_id=s2)
    rows = {r["id"]: r["status"] for r in cdb.list_sessions(a)}
    assert rows[s1] == "stopped"
    assert rows[s2] == "running"   # the excepted session stays running
