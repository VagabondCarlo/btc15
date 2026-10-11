"""Pull completed 15-minute windows and their per-second BRTI into SQLite.

Uses only the public gateway, so this runs with no credentials and no money
at risk. The resulting database is what the backtest replays.
"""

from __future__ import annotations

import sqlite3
import sys
import time
from datetime import datetime, timezone

from .gateway import (Gateway, event_slug, parse_terms, twap_at,
                      window_starts, WINDOW_SECS)

HARVEST_SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS windows (
    window_start   INTEGER PRIMARY KEY,
    window_end     INTEGER NOT NULL,
    event_slug     TEXT,
    market_slug    TEXT,
    market_id      TEXT,
    price_to_beat  REAL,
    settlement     REAL,
    outcome        TEXT,
    fee_coefficient REAL,
    min_qty        REAL,
    tick           REAL,
    recon_open     REAL,
    recon_close    REAL,
    n_open         INTEGER,
    n_close        INTEGER,
    n_prints       INTEGER,
    harvested_at   INTEGER
);
CREATE TABLE IF NOT EXISTS prints (
    ts    INTEGER PRIMARY KEY,
    value REAL NOT NULL
);
"""


def open_db(path: str = "data/harvest.db") -> sqlite3.Connection:
    from pathlib import Path
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=60.0)
    con.row_factory = sqlite3.Row
    con.executescript(HARVEST_SCHEMA)
    return con


def harvest(n_windows: int, db_path: str = "data/harvest.db",
            progress_every: int = 25) -> dict:
    con = open_db(db_path)
    gw = Gateway()
    have = {r[0] for r in con.execute("SELECT window_start FROM windows")}

    targets = [ws for ws in window_starts(n_windows)
               if int(ws.timestamp()) not in have]
    if not targets:
        return {"new": 0, "skipped": n_windows}

    targets.sort()
    lo = int(targets[0].timestamp()) - 70
    hi = int(targets[-1].timestamp()) + WINDOW_SECS + 5

    t0 = time.time()
    print(f"fetching per-second BRTI for {(hi - lo) / 86400:.2f} days "
          f"({hi - lo:,}s)...", flush=True)
    prints = gw.index_range(lo, hi)
    print(f"  {len(prints):,} prints in {time.time() - t0:.0f}s", flush=True)
    con.executemany("INSERT OR IGNORE INTO prints(ts,value) VALUES (?,?)",
                    sorted(prints.items()))
    con.commit()

    added = failed = 0
    for i, ws in enumerate(targets, 1):
        slug = event_slug(ws)
        try:
            ev = gw.event(slug)
        except Exception as e:
            print(f"  {slug}: {e}", flush=True)
            failed += 1
            continue
        if not ev:
            failed += 1
            continue
        t = parse_terms(ev)
        if not t or t["window_start"] is None:
            failed += 1
            continue

        ro, no = twap_at(prints, t["window_start"])
        rc, nc = twap_at(prints, t["window_end"])
        ptb = t["price_to_beat"] if t["price_to_beat"] is not None else ro
        stl = t["settlement"] if t["settlement"] is not None else rc
        outcome = None
        if ptb is not None and stl is not None:
            outcome = "UP" if stl >= ptb else "DOWN"   # ties resolve UP

        n_in = sum(1 for ts in range(t["window_start"], t["window_end"] + 1)
                   if ts in prints)
        con.execute(
            """INSERT OR REPLACE INTO windows VALUES
               (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (t["window_start"], t["window_end"], t["event_slug"],
             t["market_slug"], t["market_id"], ptb, stl, outcome,
             t["fee_coefficient"], t["min_qty"], t["tick"],
             ro, rc, no, nc, n_in, int(time.time())))
        added += 1
        if i % progress_every == 0:
            con.commit()
            print(f"  {i}/{len(targets)} windows, {added} stored", flush=True)
    con.commit()

    chk = con.execute(
        """SELECT COUNT(*) n,
                  SUM(ABS(COALESCE(recon_open,-1) - price_to_beat) < 0.005) ok_o,
                  SUM(ABS(COALESCE(recon_close,-1) - settlement) < 0.005) ok_c
           FROM windows WHERE price_to_beat IS NOT NULL
             AND settlement IS NOT NULL AND recon_open IS NOT NULL""").fetchone()
    return {"new": added, "failed": failed, "total": len(have) + added,
            "checked": chk["n"], "open_match": chk["ok_o"],
            "close_match": chk["ok_c"], "secs": round(time.time() - t0)}


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 400
    print(f"harvesting {n} windows ({n * 15 / 60 / 24:.1f} days)", flush=True)
    r = harvest(n)
    print("RESULT:", r, flush=True)
