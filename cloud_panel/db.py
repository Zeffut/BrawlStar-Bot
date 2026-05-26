"""Cloud-side SQLite schema — multi-instance aggregator.

Same shape as the local panel DB, plus an `instances` table that scopes
everything else.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

DB_PATH = Path(os.environ.get("CLOUD_DB_PATH", "/data/cloud.db"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
    return _conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS instances (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id     TEXT NOT NULL UNIQUE,
    name            TEXT,
    last_seen_at    REAL NOT NULL,
    metadata        TEXT
);

CREATE TABLE IF NOT EXISTS accounts (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id           INTEGER NOT NULL REFERENCES instances(id),
    tag                   TEXT NOT NULL,
    name                  TEXT,
    created_at            REAL NOT NULL,
    last_seen_at          REAL NOT NULL,
    brawlers_json         TEXT,
    brawlers_refreshed_at REAL,
    UNIQUE(instance_id, tag)
);

CREATE TABLE IF NOT EXISTS sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      INTEGER NOT NULL REFERENCES accounts(id),
    brawler         TEXT NOT NULL,
    target_trophies INTEGER NOT NULL,
    start_trophies  INTEGER,
    end_trophies    INTEGER,
    started_at      REAL NOT NULL,
    ended_at        REAL,
    status          TEXT NOT NULL DEFAULT 'running'
);
CREATE INDEX IF NOT EXISTS idx_sessions_account ON sessions(account_id);

CREATE TABLE IF NOT EXISTS matches (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id               INTEGER REFERENCES sessions(id),
    account_id               INTEGER NOT NULL REFERENCES accounts(id),
    brawler                  TEXT NOT NULL,
    result                   TEXT NOT NULL,
    trophies_before          INTEGER,
    trophies_after           INTEGER,
    account_trophies_after   INTEGER,
    timestamp                REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_matches_account ON matches(account_id);
CREATE INDEX IF NOT EXISTS idx_matches_ts      ON matches(timestamp);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id INTEGER NOT NULL REFERENCES instances(id),
    account_id  INTEGER REFERENCES accounts(id),
    type        TEXT NOT NULL,
    payload     TEXT NOT NULL,
    timestamp   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(timestamp);
"""


def init() -> None:
    with _lock:
        conn().executescript(SCHEMA)
        # Lightweight migration: add brawlers columns if missing.
        c = conn().cursor()
        existing = {r["name"] for r in c.execute("PRAGMA table_info(accounts)").fetchall()}
        if "brawlers_json" not in existing:
            c.execute("ALTER TABLE accounts ADD COLUMN brawlers_json TEXT")
        if "brawlers_refreshed_at" not in existing:
            c.execute("ALTER TABLE accounts ADD COLUMN brawlers_refreshed_at REAL")


def set_account_brawlers(account_id: int, brawlers: list[dict]) -> None:
    with _lock:
        conn().execute(
            "UPDATE accounts SET brawlers_json = ?, brawlers_refreshed_at = ? WHERE id = ?",
            (json.dumps(brawlers), time.time(), account_id),
        )


def get_account_brawlers(account_id: int) -> tuple[list[dict], float | None]:
    with _lock:
        r = conn().execute(
            "SELECT brawlers_json, brawlers_refreshed_at FROM accounts WHERE id = ?",
            (account_id,),
        ).fetchone()
    if not r or not r["brawlers_json"]:
        return [], None
    try:
        return json.loads(r["brawlers_json"]), r["brawlers_refreshed_at"]
    except Exception:
        return [], None


def accounts_needing_refresh(stale_after_s: float) -> list[dict]:
    """Return accounts whose brawlers cache is older than stale_after_s
    (or never fetched). Used by the background refresher."""
    cutoff = time.time() - stale_after_s
    with _lock:
        rows = conn().execute(
            "SELECT a.id, a.tag, a.brawlers_refreshed_at "
            "FROM accounts a "
            "WHERE a.brawlers_refreshed_at IS NULL OR a.brawlers_refreshed_at < ? "
            "ORDER BY a.brawlers_refreshed_at IS NOT NULL, a.brawlers_refreshed_at",
            (cutoff,),
        ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------- instances


def upsert_instance(instance_id: str, name: str | None = None,
                    metadata: dict | None = None) -> int:
    now = time.time()
    with _lock:
        c = conn().cursor()
        c.execute("SELECT id FROM instances WHERE instance_id = ?", (instance_id,))
        row = c.fetchone()
        if row:
            c.execute(
                "UPDATE instances SET last_seen_at = ?, "
                "name = COALESCE(?, name), metadata = COALESCE(?, metadata) "
                "WHERE id = ?",
                (now, name, json.dumps(metadata) if metadata else None, row["id"]),
            )
            return row["id"]
        c.execute(
            "INSERT INTO instances(instance_id, name, last_seen_at, metadata) "
            "VALUES (?, ?, ?, ?)",
            (instance_id, name, now, json.dumps(metadata or {})),
        )
        return c.lastrowid


def list_instances() -> list[dict]:
    with _lock:
        rows = conn().execute(
            "SELECT * FROM instances ORDER BY last_seen_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------- accounts


def upsert_account(instance_db_id: int, tag: str, name: str | None) -> int:
    now = time.time()
    with _lock:
        c = conn().cursor()
        c.execute(
            "SELECT id FROM accounts WHERE instance_id = ? AND tag = ?",
            (instance_db_id, tag),
        )
        row = c.fetchone()
        if row:
            c.execute(
                "UPDATE accounts SET last_seen_at = ?, name = COALESCE(?, name) "
                "WHERE id = ?",
                (now, name, row["id"]),
            )
            return row["id"]
        c.execute(
            "INSERT INTO accounts(instance_id, tag, name, created_at, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (instance_db_id, tag, name, now, now),
        )
        return c.lastrowid


def list_accounts(instance_id: int | None = None) -> list[dict]:
    with _lock:
        if instance_id is not None:
            rows = conn().execute(
                "SELECT a.*, i.instance_id AS instance_uid, i.name AS instance_name "
                "FROM accounts a JOIN instances i ON a.instance_id = i.id "
                "WHERE a.instance_id = ? ORDER BY a.last_seen_at DESC",
                (instance_id,),
            ).fetchall()
        else:
            rows = conn().execute(
                "SELECT a.*, i.instance_id AS instance_uid, i.name AS instance_name "
                "FROM accounts a JOIN instances i ON a.instance_id = i.id "
                "ORDER BY a.last_seen_at DESC"
            ).fetchall()
    return [dict(r) for r in rows]


def get_account(account_id: int) -> dict | None:
    with _lock:
        r = conn().execute(
            "SELECT a.*, i.instance_id AS instance_uid, i.name AS instance_name "
            "FROM accounts a JOIN instances i ON a.instance_id = i.id "
            "WHERE a.id = ?",
            (account_id,),
        ).fetchone()
    return dict(r) if r else None


# ---------------------------------------------------------- sessions


def start_session(account_id: int, brawler: str, target_trophies: int,
                  start_trophies: int | None, started_at: float | None = None) -> int:
    with _lock:
        c = conn().cursor()
        c.execute(
            "INSERT INTO sessions(account_id, brawler, target_trophies, "
            "                     start_trophies, started_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (account_id, brawler, target_trophies, start_trophies,
             started_at or time.time()),
        )
        return c.lastrowid


def end_session(session_id: int, status: str = "stopped",
                end_trophies: int | None = None,
                ended_at: float | None = None) -> None:
    with _lock:
        conn().execute(
            "UPDATE sessions SET ended_at = ?, status = ?, end_trophies = ? "
            "WHERE id = ?",
            (ended_at or time.time(), status, end_trophies, session_id),
        )


def list_sessions(account_id: int, limit: int = 50) -> list[dict]:
    with _lock:
        rows = conn().execute(
            "SELECT * FROM sessions WHERE account_id = ? "
            "ORDER BY started_at DESC LIMIT ?",
            (account_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def current_session(account_id: int) -> dict | None:
    with _lock:
        r = conn().execute(
            "SELECT * FROM sessions WHERE account_id = ? AND ended_at IS NULL "
            "ORDER BY started_at DESC LIMIT 1",
            (account_id,),
        ).fetchone()
    return dict(r) if r else None


# ----------------------------------------------------------- matches


def log_match(account_id: int, session_id: int | None, brawler: str,
              result: str, trophies_before: int | None, trophies_after: int | None,
              account_trophies_after: int | None, timestamp: float | None = None) -> int:
    with _lock:
        c = conn().cursor()
        c.execute(
            "INSERT INTO matches(session_id, account_id, brawler, result, "
            "                    trophies_before, trophies_after, "
            "                    account_trophies_after, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, account_id, brawler, result, trophies_before,
             trophies_after, account_trophies_after, timestamp or time.time()),
        )
        return c.lastrowid


def recent_matches(account_id: int, limit: int = 200) -> list[dict]:
    with _lock:
        rows = conn().execute(
            "SELECT * FROM matches WHERE account_id = ? "
            "ORDER BY timestamp DESC LIMIT ?",
            (account_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def win_rate_by_brawler(account_id: int) -> list[dict]:
    with _lock:
        rows = conn().execute(
            "SELECT brawler, "
            "       SUM(result='victory') AS wins, "
            "       SUM(result='defeat')  AS losses, "
            "       SUM(result='draw')    AS draws, "
            "       COUNT(*) AS total "
            "FROM matches WHERE account_id = ? "
            "GROUP BY brawler ORDER BY total DESC",
            (account_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------------ events


def log_event(instance_db_id: int, event_type: str, payload: dict | None = None,
              account_id: int | None = None,
              timestamp: float | None = None) -> None:
    with _lock:
        conn().execute(
            "INSERT INTO events(instance_id, account_id, type, payload, timestamp) "
            "VALUES (?, ?, ?, ?, ?)",
            (instance_db_id, account_id, event_type,
             json.dumps(payload or {}), timestamp or time.time()),
        )
