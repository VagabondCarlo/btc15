"""SQLite store for recorded cycles, index prints, and book snapshots.

Everything the bot ever sees goes in here. The edge question cannot be
answered from live observation; it is answered by replaying this database
offline against the model. So the recorder is the real product and the
trader is downstream of it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS cycles (
    market_id       TEXT PRIMARY KEY,
    slug            TEXT,
    interval_start  INTEGER NOT NULL,
    interval_end    INTEGER NOT NULL,
    price_to_beat   REAL,
    settlement      REAL,
    outcome         TEXT,          -- 'UP' | 'DOWN'
    index_symbol    TEXT,
    first_seen      INTEGER NOT NULL,
    resolved_at     INTEGER
);
CREATE INDEX IF NOT EXISTS ix_cycles_end ON cycles(interval_end);

-- Per-second reference index prints. ts is the candle's unix second.
CREATE TABLE IF NOT EXISTS index_prints (
    ts     INTEGER PRIMARY KEY,
    value  REAL NOT NULL,
    src    TEXT DEFAULT 'asset-prices'
);

-- Order book top of book, sampled. One row per poll per market.
CREATE TABLE IF NOT EXISTS book_snaps (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms       INTEGER NOT NULL,
    market_id   TEXT NOT NULL,
    secs_left   REAL NOT NULL,
    up_bid      REAL, up_bid_sz  REAL,
    up_ask      REAL, up_ask_sz  REAL,
    index_last  REAL,
    raw         TEXT
);
CREATE INDEX IF NOT EXISTS ix_book_mkt ON book_snaps(market_id, ts_ms);

-- What the model thought at the time. Written even when not trading.
CREATE TABLE IF NOT EXISTS decisions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms       INTEGER NOT NULL,
    market_id   TEXT NOT NULL,
    secs_left   REAL NOT NULL,
    model_p     REAL,
    model_sd    REAL,
    locked_frac REAL,
    mkt_bid     REAL,
    mkt_ask     REAL,
    edge_up     REAL,
    edge_down   REAL,
    action      TEXT,
    reason      TEXT,
    size        INTEGER,
    mode        TEXT           -- 'record' | 'paper' | 'live'
);
CREATE INDEX IF NOT EXISTS ix_dec_mkt ON decisions(market_id, ts_ms);

CREATE TABLE IF NOT EXISTS fills (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms       INTEGER NOT NULL,
    market_id   TEXT NOT NULL,
    side        TEXT, price REAL, size INTEGER,
    fee         REAL, mode TEXT, order_id TEXT,
    pnl         REAL
);
"""


def connect(path: str | Path = "data/btc15.db") -> sqlite3.Connection:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p, timeout=30.0)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def upsert_index(con: sqlite3.Connection, rows: list[tuple[int, float]]) -> int:
    """Insert index prints, ignoring ones already stored."""
    cur = con.executemany(
        "INSERT OR IGNORE INTO index_prints(ts, value) VALUES (?, ?)", rows
    )
    con.commit()
    return cur.rowcount


def index_series(con: sqlite3.Connection, start: int, end: int):
    rows = con.execute(
        "SELECT ts, value FROM index_prints WHERE ts BETWEEN ? AND ? ORDER BY ts",
        (start, end),
    ).fetchall()
    return [r["ts"] for r in rows], [r["value"] for r in rows]
