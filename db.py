"""SQLite-backed event log for the bot — designed for multi-account.

One DB shared by:
  - the bot worker(s) that write match/session/event rows
  - the web panel that reads them for live dashboards

Schema is intentionally simple — every observation goes into `events` for
forensics, and the typed tables (`accounts`, `sessions`, `matches`) are
projections of the most-queried slices.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

DB_PATH = Path(__file__).resolve().parent / "data" / "bot.db"
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
CREATE TABLE IF NOT EXISTS accounts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tag             TEXT NOT NULL UNIQUE,
    name            TEXT,
    device_serial   TEXT,
    telegram_chat_id INTEGER,
    created_at      REAL NOT NULL,
    last_seen_at    REAL NOT NULL
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
    status          TEXT NOT NULL DEFAULT 'running'  -- running|stopped|completed
);
CREATE INDEX IF NOT EXISTS idx_sessions_account ON sessions(account_id);

CREATE TABLE IF NOT EXISTS matches (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id               INTEGER NOT NULL REFERENCES sessions(id),
    account_id               INTEGER NOT NULL REFERENCES accounts(id),
    brawler                  TEXT NOT NULL,
    result                   TEXT NOT NULL,  -- victory|defeat|draw
    trophies_before          INTEGER,
    trophies_after           INTEGER,
    account_trophies_after   INTEGER,        -- account-wide total at this point
    timestamp                REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_matches_session ON matches(session_id);
CREATE INDEX IF NOT EXISTS idx_matches_account ON matches(account_id);
CREATE INDEX IF NOT EXISTS idx_matches_ts ON matches(timestamp);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id  INTEGER REFERENCES accounts(id),
    type        TEXT NOT NULL,
    payload     TEXT NOT NULL,
    timestamp   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(timestamp);
"""


def init() -> None:
    log.info("DB init at %s", DB_PATH)
    with _lock:
        cur = conn().cursor()
        cur.executescript(SCHEMA)
        # Light migration: ALTER tables to add columns we introduced
        # after the first deployment. SQLite is forgiving — no rebuild
        # needed.
        for ddl in [
            "ALTER TABLE matches ADD COLUMN account_trophies_after INTEGER",
        ]:
            try:
                cur.execute(ddl)
                log.info("DB migrated: %s", ddl)
            except Exception:
                pass  # column already exists


# --------------------------------------------------------------- accounts


def upsert_account(tag: str, name: str | None = None,
                   device_serial: str | None = None,
                   telegram_chat_id: int | None = None) -> int:
    """Insert-or-touch the account row; returns its id."""
    now = time.time()
    with _lock:
        c = conn().cursor()
        c.execute("SELECT id FROM accounts WHERE tag = ?", (tag,))
        row = c.fetchone()
        if row:
            c.execute(
                "UPDATE accounts SET last_seen_at = ?, "
                "name = COALESCE(?, name), "
                "device_serial = COALESCE(?, device_serial), "
                "telegram_chat_id = COALESCE(?, telegram_chat_id) "
                "WHERE id = ?",
                (now, name, device_serial, telegram_chat_id, row["id"]),
            )
            return row["id"]
        c.execute(
            "INSERT INTO accounts(tag, name, device_serial, telegram_chat_id, "
            "                     created_at, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (tag, name, device_serial, telegram_chat_id, now, now),
        )
        log.info("DB account inserted: id=%d tag=%s name=%r", c.lastrowid, tag, name)
        return c.lastrowid


def list_accounts() -> list[dict]:
    with _lock:
        rows = conn().execute(
            "SELECT * FROM accounts ORDER BY last_seen_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_account(account_id: int) -> dict | None:
    with _lock:
        r = conn().execute("SELECT * FROM accounts WHERE id = ?",
                           (account_id,)).fetchone()
    return dict(r) if r else None


# --------------------------------------------------------------- sessions


def start_session(account_id: int, brawler: str, target_trophies: int,
                  start_trophies: int | None) -> int:
    with _lock:
        c = conn().cursor()
        c.execute(
            "INSERT INTO sessions(account_id, brawler, target_trophies, "
            "                     start_trophies, started_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (account_id, brawler, target_trophies, start_trophies, time.time()),
        )
        return c.lastrowid


def end_session(session_id: int, status: str = "stopped",
                end_trophies: int | None = None) -> None:
    log.info("DB session end: id=%d status=%s end_trophies=%s",
             session_id, status, end_trophies)
    with _lock:
        conn().execute(
            "UPDATE sessions SET ended_at = ?, status = ?, end_trophies = ? "
            "WHERE id = ? AND ended_at IS NULL",
            (time.time(), status, end_trophies, session_id),
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


# ---------------------------------------------------------------- matches


def log_match(session_id: int, account_id: int, brawler: str, result: str,
              trophies_before: int | None, trophies_after: int | None,
              account_trophies_after: int | None = None) -> int:
    with _lock:
        c = conn().cursor()
        c.execute(
            "INSERT INTO matches(session_id, account_id, brawler, result, "
            "                    trophies_before, trophies_after, "
            "                    account_trophies_after, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, account_id, brawler, result, trophies_before,
             trophies_after, account_trophies_after, time.time()),
        )
        return c.lastrowid


def recent_matches(account_id: int, limit: int = 100) -> list[dict]:
    with _lock:
        rows = conn().execute(
            "SELECT * FROM matches WHERE account_id = ? "
            "ORDER BY timestamp DESC LIMIT ?",
            (account_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def count_matches_today(account_id: int, now: float | None = None) -> int:
    """Number of matches logged for this account since LOCAL midnight. Used to
    seed the humane schedule's daily cap so it survives worker restarts (an
    in-memory counter resets to 0 on every restart → the cap is defeated)."""
    if account_id is None:
        return 0
    now = now or time.time()
    lt = time.localtime(now)
    midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0,
                            lt.tm_wday, lt.tm_yday, -1))
    with _lock:
        row = conn().execute(
            "SELECT COUNT(*) FROM matches WHERE account_id = ? AND timestamp >= ?",
            (account_id, midnight),
        ).fetchone()
    return int(row[0]) if row and row[0] else 0


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


def brawler_efficiency(account_id: int, days: int = 30,
                       now: float | None = None) -> dict[str, dict]:
    """Per-brawler realized performance over the last `days`, keyed by brawler.

    Returns ``{brawler_lower: {matches, wins, net, net_per_match, win_rate}}``
    where `net` sums the per-match trophy delta (after-before) — the true
    realized gain, accurate even when absolute counts are noisy.

    This feeds push_max's data-driven pick order: instead of trusting the
    hand-picked Pyla skill tiers, we rank brawlers by what they ACTUALLY net
    per match on this account (shrunk toward the tier prior for small samples,
    done in push_max). Only matches with both trophy endpoints are counted.
    """
    now = now or time.time()
    since = now - days * 86400
    with _lock:
        rows = conn().execute(
            "SELECT brawler, "
            "       COUNT(*) AS matches, "
            "       SUM(result='victory') AS wins, "
            "       SUM(COALESCE(trophies_after,0) - COALESCE(trophies_before,0)) AS net "
            "FROM matches "
            "WHERE account_id = ? AND timestamp >= ? "
            "  AND trophies_before IS NOT NULL AND trophies_after IS NOT NULL "
            "GROUP BY brawler",
            (account_id, since),
        ).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        m = r["matches"] or 0
        if m <= 0:
            continue
        net = int(r["net"] or 0)
        wins = int(r["wins"] or 0)
        out[r["brawler"].lower().strip()] = {
            "matches": m,
            "wins": wins,
            "net": net,
            "net_per_match": round(net / m, 2),
            "win_rate": round(wins / m * 100),
        }
    return out


# ----------------------------------------------------------------- events


def log_event(event_type: str, payload: dict | None = None,
              account_id: int | None = None) -> None:
    with _lock:
        conn().execute(
            "INSERT INTO events(account_id, type, payload, timestamp) "
            "VALUES (?, ?, ?, ?)",
            (account_id, event_type, json.dumps(payload or {}), time.time()),
        )


def recent_events(account_id: int | None = None, limit: int = 100) -> list[dict]:
    with _lock:
        if account_id is None:
            rows = conn().execute(
                "SELECT * FROM events ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = conn().execute(
                "SELECT * FROM events WHERE account_id = ? "
                "ORDER BY timestamp DESC LIMIT ?",
                (account_id, limit),
            ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["payload"] = json.loads(d["payload"])
        except Exception:
            pass
        out.append(d)
    return out
