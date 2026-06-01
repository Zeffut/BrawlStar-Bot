"""Cloud DB dedup invariants — replays must be safe."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest


@pytest.fixture
def cdb(monkeypatch):
    """Clean cloud_panel.db pointing at a TEST Postgres (skips without it).

    Requires TEST_DATABASE_URL (a throwaway DB whose URL contains 'test' —
    the fixture DROPs all tables). Mirrors tests/test_cloud_db.py."""
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set — Postgres needed for db tests")
    if "test" not in url.lower():
        pytest.skip("refusing to run destructive db tests on a non-'test' DB URL")
    os.environ["DATABASE_URL"] = url
    for mod in list(sys.modules):
        if mod == "db" or mod.endswith(".db"):
            sys.modules.pop(mod, None)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cloud_panel"))
    import db as cdb
    cdb._conn = None
    with cdb.conn().cursor() as c:
        c.execute(
            "DROP TABLE IF EXISTS matches, sessions, events, instance_state, "
            "accounts, instances, config CASCADE"
        )
    cdb.init()
    return cdb


def test_log_match_dedups_same_key(cdb):
    inst = cdb.upsert_instance("x", "X")
    acc = cdb.upsert_account(inst, "T", None)
    ts = 1700000000.5
    id1 = cdb.log_match(acc, None, "brock", "victory", 100, 110, 600, ts)
    id2 = cdb.log_match(acc, None, "brock", "victory", 100, 110, 600, ts)
    assert id1 == id2, "same (account, ts, brawler) must be deduplicated"
    rows = cdb.recent_matches(acc, limit=10)
    assert len(rows) == 1


def test_log_match_different_brawler_not_dedup(cdb):
    inst = cdb.upsert_instance("x", "X")
    acc = cdb.upsert_account(inst, "T", None)
    ts = 1700000000.5
    a = cdb.log_match(acc, None, "brock", "victory", 0, 0, 0, ts)
    b = cdb.log_match(acc, None, "buzz",  "victory", 0, 0, 0, ts)
    assert a != b


def test_start_session_dedups_same_key(cdb):
    inst = cdb.upsert_instance("x", "X")
    acc = cdb.upsert_account(inst, "T", None)
    ts = 1700000000.0
    s1 = cdb.start_session(acc, "brock", 1000, 500, ts)
    s2 = cdb.start_session(acc, "brock", 1000, 500, ts)
    assert s1 == s2


def test_account_brawlers_total_trophies(cdb):
    inst = cdb.upsert_instance("x", "X")
    acc = cdb.upsert_account(inst, "T", None)
    cdb.set_account_brawlers(acc, [
        {"name": "brock", "power": 4, "trophies": 369},
        {"name": "buzz",  "power": 7, "trophies": 212},
        {"name": "shelly","power": 5, "trophies": 61},
    ])
    brawlers, _ = cdb.get_account_brawlers(acc)
    total = sum(b["trophies"] for b in brawlers)
    assert total == 642
