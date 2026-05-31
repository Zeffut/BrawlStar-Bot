"""Cloud-side Postgres schema — multi-instance aggregator.

Same shape as the local panel DB, plus an `instances` table that scopes
everything else. Migrated from SQLite to a Dokploy-managed Postgres so the
data is a first-class, backed-up database (visible in Dokploy) rather than a
loose SQLite file on a bind mount.

Connection comes from `DATABASE_URL` (libpq URI), e.g.
    postgresql://user:pass@host:5432/dbname
falling back to the standard PG* env vars psycopg2 reads on its own.
"""
from __future__ import annotations

import json
import os
import threading
import time

import psycopg2
import psycopg2.extras

DATABASE_URL = os.environ.get("DATABASE_URL") or os.environ.get("CLOUD_DATABASE_URL")

_lock = threading.Lock()
_conn = None


def _connect():
    if DATABASE_URL:
        c = psycopg2.connect(DATABASE_URL)
    else:
        # psycopg2 reads PGHOST/PGPORT/PGDATABASE/PGUSER/PGPASSWORD from env.
        c = psycopg2.connect()
    c.autocommit = True
    return c


def conn():
    """Return a live connection, reconnecting if the socket dropped.

    Single shared connection guarded by `_lock` (every public function takes
    the lock), so all DB access is serialized — fine for the panel's load and
    matches the previous SQLite design.
    """
    global _conn
    if _conn is None or _conn.closed:
        _conn = _connect()
        return _conn
    # Cheap liveness check; reconnect on a dropped connection.
    try:
        with _conn.cursor() as c:
            c.execute("SELECT 1")
        return _conn
    except psycopg2.Error:
        try:
            _conn.close()
        except Exception:
            pass
        _conn = _connect()
        return _conn


def _cur():
    """Dict-returning cursor: rows behave like dicts (r["col"]) and dict(r)."""
    return conn().cursor(cursor_factory=psycopg2.extras.RealDictCursor)


SCHEMA = """
CREATE TABLE IF NOT EXISTS instances (
    id                 SERIAL PRIMARY KEY,
    instance_id        TEXT NOT NULL UNIQUE,
    name               TEXT,
    last_seen_at       DOUBLE PRECISION NOT NULL,
    metadata           TEXT,
    last_snapshot_json TEXT,
    last_snapshot_at   DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS accounts (
    id                    SERIAL PRIMARY KEY,
    instance_id           INTEGER NOT NULL REFERENCES instances(id),
    tag                   TEXT NOT NULL,
    name                  TEXT,
    created_at            DOUBLE PRECISION NOT NULL,
    last_seen_at          DOUBLE PRECISION NOT NULL,
    brawlers_json         TEXT,
    brawlers_refreshed_at DOUBLE PRECISION,
    UNIQUE(instance_id, tag)
);

CREATE TABLE IF NOT EXISTS sessions (
    id              SERIAL PRIMARY KEY,
    account_id      INTEGER NOT NULL REFERENCES accounts(id),
    brawler         TEXT NOT NULL,
    target_trophies INTEGER NOT NULL,
    start_trophies  INTEGER,
    end_trophies    INTEGER,
    started_at      DOUBLE PRECISION NOT NULL,
    ended_at        DOUBLE PRECISION,
    status          TEXT NOT NULL DEFAULT 'running'
);
CREATE INDEX IF NOT EXISTS idx_sessions_account ON sessions(account_id);

CREATE TABLE IF NOT EXISTS matches (
    id                       SERIAL PRIMARY KEY,
    session_id               INTEGER REFERENCES sessions(id),
    account_id               INTEGER NOT NULL REFERENCES accounts(id),
    brawler                  TEXT NOT NULL,
    result                   TEXT NOT NULL,
    trophies_before          INTEGER,
    trophies_after           INTEGER,
    account_trophies_after   INTEGER,
    timestamp                DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_matches_account ON matches(account_id);
CREATE INDEX IF NOT EXISTS idx_matches_ts      ON matches(timestamp);

CREATE TABLE IF NOT EXISTS events (
    id          SERIAL PRIMARY KEY,
    instance_id INTEGER NOT NULL REFERENCES instances(id),
    account_id  INTEGER REFERENCES accounts(id),
    type        TEXT NOT NULL,
    payload     TEXT NOT NULL,
    timestamp   DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(timestamp);

CREATE TABLE IF NOT EXISTS config (
    name       TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS instance_state (
    instance_id INTEGER PRIMARY KEY REFERENCES instances(id) ON DELETE CASCADE,
    payload     TEXT NOT NULL,
    updated_at  DOUBLE PRECISION NOT NULL
);
"""


def init() -> None:
    with _lock:
        with conn().cursor() as c:
            c.execute(SCHEMA)


def query(sql: str, params: tuple = (), one: bool = False):
    """Generic read helper for the handful of ad-hoc queries in app.py.

    Returns dict rows: a list, or a single dict / None when one=True.
    Use %s placeholders (psycopg2 style)."""
    with _lock, _cur() as c:
        c.execute(sql, params)
        if one:
            r = c.fetchone()
            return dict(r) if r else None
        return [dict(r) for r in c.fetchall()]


# --------------------------------------------------- instance_state / config


def get_instance_state(instance_db_id: int) -> dict:
    with _lock, _cur() as c:
        c.execute(
            "SELECT payload FROM instance_state WHERE instance_id = %s",
            (instance_db_id,),
        )
        row = c.fetchone()
    if row is None:
        return {}
    try:
        return json.loads(row["payload"])
    except Exception:
        return {}


def set_instance_state(instance_db_id: int, payload: dict) -> None:
    with _lock, _cur() as c:
        c.execute(
            "INSERT INTO instance_state (instance_id, payload, updated_at) "
            "VALUES (%s, %s, %s) "
            "ON CONFLICT(instance_id) DO UPDATE SET payload = excluded.payload, "
            "updated_at = excluded.updated_at",
            (instance_db_id, json.dumps(payload), time.time()),
        )


def clear_instance_state(instance_db_id: int) -> None:
    with _lock, _cur() as c:
        c.execute(
            "DELETE FROM instance_state WHERE instance_id = %s",
            (instance_db_id,),
        )


def get_config(name: str) -> dict:
    with _lock, _cur() as c:
        c.execute("SELECT payload FROM config WHERE name = %s", (name,))
        row = c.fetchone()
    if row is None:
        return {}
    try:
        return json.loads(row["payload"])
    except Exception:
        return {}


def set_config(name: str, payload: dict) -> None:
    with _lock, _cur() as c:
        c.execute(
            "INSERT INTO config (name, payload, updated_at) VALUES (%s, %s, %s) "
            "ON CONFLICT(name) DO UPDATE SET payload = excluded.payload, "
            "updated_at = excluded.updated_at",
            (name, json.dumps(payload), time.time()),
        )


# ------------------------------------------------------------ snapshots


def save_instance_snapshot(instance_id: str, snapshot: dict) -> None:
    """Persist the latest snapshot for an instance so it survives DC."""
    with _lock, _cur() as c:
        c.execute(
            "UPDATE instances SET last_snapshot_json = %s, last_snapshot_at = %s "
            "WHERE instance_id = %s",
            (json.dumps(snapshot), time.time(), instance_id),
        )


def load_instance_snapshot(instance_id: str) -> tuple[dict | None, float | None]:
    with _lock, _cur() as c:
        c.execute(
            "SELECT last_snapshot_json, last_snapshot_at FROM instances "
            "WHERE instance_id = %s",
            (instance_id,),
        )
        r = c.fetchone()
    if not r or not r["last_snapshot_json"]:
        return None, None
    try:
        return json.loads(r["last_snapshot_json"]), r["last_snapshot_at"]
    except Exception:
        return None, None


# ------------------------------------------------------------ brawlers


def set_account_brawlers(account_id: int, brawlers: list[dict]) -> None:
    # Never wipe a populated cache with an empty list: a transient brawlace
    # failure must not clear the brawlers (that breaks push_max start and
    # makes /util/brawler_profile hang on a live re-fetch).
    if not brawlers:
        return
    with _lock, _cur() as c:
        c.execute(
            "UPDATE accounts SET brawlers_json = %s, brawlers_refreshed_at = %s "
            "WHERE id = %s",
            (json.dumps(brawlers), time.time(), account_id),
        )


def get_account_brawlers(account_id: int) -> tuple[list[dict], float | None]:
    with _lock, _cur() as c:
        c.execute(
            "SELECT brawlers_json, brawlers_refreshed_at FROM accounts WHERE id = %s",
            (account_id,),
        )
        r = c.fetchone()
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
    with _lock, _cur() as c:
        c.execute(
            "SELECT a.id, a.tag, a.brawlers_refreshed_at "
            "FROM accounts a "
            "WHERE a.brawlers_refreshed_at IS NULL OR a.brawlers_refreshed_at < %s "
            "ORDER BY a.brawlers_refreshed_at ASC NULLS FIRST",
            (cutoff,),
        )
        rows = c.fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------- instances


def upsert_instance(instance_id: str, name: str | None = None,
                    metadata: dict | None = None) -> int:
    now = time.time()
    with _lock, _cur() as c:
        c.execute("SELECT id FROM instances WHERE instance_id = %s", (instance_id,))
        row = c.fetchone()
        if row:
            c.execute(
                "UPDATE instances SET last_seen_at = %s, "
                "name = COALESCE(%s, name), metadata = COALESCE(%s, metadata) "
                "WHERE id = %s",
                (now, name, json.dumps(metadata) if metadata else None, row["id"]),
            )
            return row["id"]
        c.execute(
            "INSERT INTO instances(instance_id, name, last_seen_at, metadata) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (instance_id, name, now, json.dumps(metadata or {})),
        )
        return c.fetchone()["id"]


def list_instances() -> list[dict]:
    with _lock, _cur() as c:
        c.execute("SELECT * FROM instances ORDER BY last_seen_at DESC")
        rows = c.fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------- accounts


def upsert_account(instance_db_id: int, tag: str, name: str | None) -> int:
    now = time.time()
    with _lock, _cur() as c:
        c.execute(
            "SELECT id FROM accounts WHERE instance_id = %s AND tag = %s",
            (instance_db_id, tag),
        )
        row = c.fetchone()
        if row:
            c.execute(
                "UPDATE accounts SET last_seen_at = %s, name = COALESCE(%s, name) "
                "WHERE id = %s",
                (now, name, row["id"]),
            )
            return row["id"]
        c.execute(
            "INSERT INTO accounts(instance_id, tag, name, created_at, last_seen_at) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (instance_db_id, tag, name, now, now),
        )
        return c.fetchone()["id"]


def list_accounts(instance_id: int | None = None) -> list[dict]:
    with _lock, _cur() as c:
        if instance_id is not None:
            c.execute(
                "SELECT a.*, i.instance_id AS instance_uid, i.name AS instance_name "
                "FROM accounts a JOIN instances i ON a.instance_id = i.id "
                "WHERE a.instance_id = %s ORDER BY a.last_seen_at DESC",
                (instance_id,),
            )
        else:
            c.execute(
                "SELECT a.*, i.instance_id AS instance_uid, i.name AS instance_name "
                "FROM accounts a JOIN instances i ON a.instance_id = i.id "
                "ORDER BY a.last_seen_at DESC"
            )
        rows = c.fetchall()
    return [dict(r) for r in rows]


def get_account(account_id: int) -> dict | None:
    with _lock, _cur() as c:
        c.execute(
            "SELECT a.*, i.instance_id AS instance_uid, i.name AS instance_name "
            "FROM accounts a JOIN instances i ON a.instance_id = i.id "
            "WHERE a.id = %s",
            (account_id,),
        )
        r = c.fetchone()
    return dict(r) if r else None


# ---------------------------------------------------------- sessions


def start_session(account_id: int, brawler: str, target_trophies: int,
                  start_trophies: int | None, started_at: float | None = None) -> int:
    """Insert a session — idempotent on (account_id, started_at, brawler).

    Worker may re-push the same session during gap-sync; dedup prevents
    duplicates after a cloud DB wipe.
    """
    ts = started_at or time.time()
    with _lock, _cur() as c:
        c.execute(
            "SELECT id FROM sessions WHERE account_id = %s AND started_at = %s "
            "AND brawler = %s",
            (account_id, ts, brawler),
        )
        existing = c.fetchone()
        if existing:
            return existing["id"]
        c.execute(
            "INSERT INTO sessions(account_id, brawler, target_trophies, "
            "                     start_trophies, started_at) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (account_id, brawler, target_trophies, start_trophies, ts),
        )
        return c.fetchone()["id"]


def end_session(session_id: int, status: str = "stopped",
                end_trophies: int | None = None,
                ended_at: float | None = None) -> None:
    with _lock, _cur() as c:
        c.execute(
            "UPDATE sessions SET ended_at = %s, status = %s, end_trophies = %s "
            "WHERE id = %s",
            (ended_at or time.time(), status, end_trophies, session_id),
        )


def end_running_sessions(account_id: int, status: str = "stopped",
                         ended_at: float | None = None,
                         except_id: int | None = None) -> int:
    """Mark every still-'running' session of an account as ended.

    Workers run one session at a time, and the local→cloud session-id map
    is in-memory (lost on worker restart), so a crashed/restarted grind can
    leave the cloud session stuck 'running'. Closing stale running rows when
    a new session starts (or on stop) keeps the panel honest.
    """
    with _lock, _cur() as c:
        c.execute(
            "UPDATE sessions SET ended_at = %s, status = %s "
            "WHERE account_id = %s AND status = 'running' "
            "AND (%s IS NULL OR id != %s)",
            (ended_at or time.time(), status, account_id, except_id, except_id),
        )
        return c.rowcount


def list_sessions(account_id: int, limit: int = 50) -> list[dict]:
    with _lock, _cur() as c:
        c.execute(
            "SELECT * FROM sessions WHERE account_id = %s "
            "ORDER BY started_at DESC LIMIT %s",
            (account_id, limit),
        )
        rows = c.fetchall()
    return [dict(r) for r in rows]


def current_session(account_id: int) -> dict | None:
    with _lock, _cur() as c:
        c.execute(
            "SELECT * FROM sessions WHERE account_id = %s AND ended_at IS NULL "
            "ORDER BY started_at DESC LIMIT 1",
            (account_id,),
        )
        r = c.fetchone()
    return dict(r) if r else None


# ----------------------------------------------------------- matches


def log_match(account_id: int, session_id: int | None, brawler: str,
              result: str, trophies_before: int | None, trophies_after: int | None,
              account_trophies_after: int | None, timestamp: float | None = None) -> int:
    """Insert a match — idempotent on (account_id, timestamp, brawler).

    Worker may re-push the same match during gap-sync after a cloud DB
    wipe; dedup ensures we don't double-count.
    """
    ts = timestamp or time.time()
    with _lock, _cur() as c:
        c.execute(
            "SELECT id FROM matches WHERE account_id = %s AND timestamp = %s "
            "AND brawler = %s",
            (account_id, ts, brawler),
        )
        existing = c.fetchone()
        if existing:
            return existing["id"]
        c.execute(
            "INSERT INTO matches(session_id, account_id, brawler, result, "
            "                    trophies_before, trophies_after, "
            "                    account_trophies_after, timestamp) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (session_id, account_id, brawler, result, trophies_before,
             trophies_after, account_trophies_after, ts),
        )
        return c.fetchone()["id"]


def recent_matches(account_id: int, limit: int = 200) -> list[dict]:
    with _lock, _cur() as c:
        c.execute(
            "SELECT * FROM matches WHERE account_id = %s "
            "ORDER BY timestamp DESC LIMIT %s",
            (account_id, limit),
        )
        rows = c.fetchall()
    return [dict(r) for r in rows]


def match_stats(account_id: int) -> dict:
    """Account-wide match totals over the WHOLE history (not just the page
    of recent matches loaded for the table). Lets the KPI show the real
    count + win-rate without loading every match into the client."""
    with _lock, _cur() as c:
        c.execute(
            "SELECT COUNT(*) AS total, "
            "       COUNT(*) FILTER (WHERE result = 'victory') AS wins, "
            "       COUNT(*) FILTER (WHERE result = 'defeat')  AS losses, "
            "       COUNT(*) FILTER (WHERE result = 'draw')    AS draws "
            "FROM matches WHERE account_id = %s",
            (account_id,),
        )
        row = c.fetchone()
    return {
        "total": row["total"] or 0,
        "wins": row["wins"] or 0,
        "losses": row["losses"] or 0,
        "draws": row["draws"] or 0,
    }


def latest_account_trophies(account_id: int) -> int | None:
    """Account-wide trophy total kept live by per-match deltas.

    The worker adds each match's trophy delta to this running total and
    pushes it on every match end (`account_trophies_after`) — no external
    API call. It's therefore the freshest source for the displayed total,
    ahead of the periodic brawlace brawler-list refresh. Returns None when
    no match has recorded a total yet (fall back to the brawler sum).
    """
    with _lock, _cur() as c:
        c.execute(
            "SELECT account_trophies_after FROM matches "
            "WHERE account_id = %s AND account_trophies_after IS NOT NULL "
            "ORDER BY timestamp DESC LIMIT 1",
            (account_id,),
        )
        row = c.fetchone()
    return row["account_trophies_after"] if row else None


def win_rate_by_brawler(account_id: int) -> list[dict]:
    with _lock, _cur() as c:
        c.execute(
            "SELECT brawler, "
            "       COUNT(*) FILTER (WHERE result = 'victory') AS wins, "
            "       COUNT(*) FILTER (WHERE result = 'defeat')  AS losses, "
            "       COUNT(*) FILTER (WHERE result = 'draw')    AS draws, "
            "       COUNT(*) AS total "
            "FROM matches WHERE account_id = %s "
            "GROUP BY brawler ORDER BY total DESC",
            (account_id,),
        )
        rows = c.fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------------ events


def log_event(instance_db_id: int, event_type: str, payload: dict | None = None,
              account_id: int | None = None,
              timestamp: float | None = None) -> None:
    with _lock, _cur() as c:
        c.execute(
            "INSERT INTO events(instance_id, account_id, type, payload, timestamp) "
            "VALUES (%s, %s, %s, %s, %s)",
            (instance_db_id, account_id, event_type,
             json.dumps(payload or {}), timestamp or time.time()),
        )
