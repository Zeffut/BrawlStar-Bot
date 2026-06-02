"""One-shot migration: cloud panel SQLite -> Dokploy Postgres.

Run inside a container that can reach the Postgres service and has the
SQLite file mounted. Idempotent-ish: it creates the schema if missing and
skips rows that already exist (ON CONFLICT DO NOTHING on primary keys).

Env:
    SQLITE_PATH    path to the old cloud.db   (default /src/cloud.db)
    DATABASE_URL   target Postgres URI
"""
import os
import sqlite3

import psycopg2

SQLITE_PATH = os.environ.get("SQLITE_PATH", "/src/cloud.db")
DATABASE_URL = os.environ["DATABASE_URL"]

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

# FK-safe insertion order.
TABLES = ["instances", "accounts", "sessions", "matches", "events",
          "config", "instance_state"]
SERIAL_TABLES = ["instances", "accounts", "sessions", "matches", "events"]


def main():
    src = sqlite3.connect(SQLITE_PATH)
    src.row_factory = sqlite3.Row
    dst = psycopg2.connect(DATABASE_URL)
    dst.autocommit = False
    cur = dst.cursor()
    cur.execute(SCHEMA)

    for table in TABLES:
        # Only migrate columns that exist in BOTH the source and target.
        sqlite_cols = [r["name"] for r in
                       src.execute(f"PRAGMA table_info({table})").fetchall()]
        if not sqlite_cols:
            print(f"{table}: absent in SQLite, skip")
            continue
        rows = src.execute(f"SELECT * FROM {table}").fetchall()
        if not rows:
            print(f"{table}: 0 rows")
            continue
        cols = sqlite_cols
        placeholders = ", ".join(["%s"] * len(cols))
        collist = ", ".join(f'"{c}"' for c in cols)
        sql = (f"INSERT INTO {table} ({collist}) VALUES ({placeholders}) "
               f"ON CONFLICT DO NOTHING")
        n = 0
        for r in rows:
            cur.execute(sql, tuple(r[c] for c in cols))
            n += cur.rowcount
        print(f"{table}: {len(rows)} read, {n} inserted")

    # Reset SERIAL sequences so new inserts don't collide with migrated ids.
    for table in SERIAL_TABLES:
        cur.execute(
            f"SELECT setval(pg_get_serial_sequence('{table}','id'), "
            f"COALESCE((SELECT MAX(id) FROM {table}), 1), "
            f"(SELECT COUNT(*) FROM {table}) > 0)"
        )
    dst.commit()

    # Sanity counts.
    for table in TABLES:
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        print(f"  PG {table}: {cur.fetchone()[0]}")
    print("MIGRATION DONE")


if __name__ == "__main__":
    main()
