"""Harvest per-window contract price statistics from the public book endpoint.

GET /v1/markets/{market_slug}/book returns, for an expired window, the UP
contract's open / high / low / close price with the timestamp of each. Four
timestamped points per window is not a full price path, but each one can be
paired with the BRTI gap at that exact second, which is enough to measure
what the market was charging for a given gap with a given amount of time left.

That is the number the backtest cannot otherwise see, and it decides whether
the gap rule makes or loses money.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime

from .gateway import Gateway
from .harvest import open_db

BOOK_SCHEMA = """
CREATE TABLE IF NOT EXISTS book_stats (
    window_start INTEGER PRIMARY KEY,
    market_slug  TEXT,
    open_px REAL, open_ts INTEGER,
    high_px REAL, high_ts INTEGER,
    low_px  REAL, low_ts  INTEGER,
    close_px REAL, close_ts INTEGER,
    last_px REAL, last_ts INTEGER,
    settlement_px REAL,
    shares_traded REAL,
    notional_traded REAL,
    state TEXT,
    fetched_at INTEGER
);
"""


def _px(d, k):
    v = (d.get(k) or {}).get("value")
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _ts(d, k):
    s = d.get(k)
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    if "." in s:                      # trim nanoseconds to microseconds
        head, _, tail = s.partition(".")
        frac, _, off = tail.partition("+")
        s = f"{head}.{frac[:6]}+{off}" if off else f"{head}.{frac[:6]}"
    try:
        return int(datetime.fromisoformat(s).timestamp())
    except ValueError:
        return None


def harvest_books(db_path: str = "data/harvest.db",
                  progress_every: int = 100) -> dict:
    con = open_db(db_path)
    con.executescript(BOOK_SCHEMA)
    gw = Gateway()

    todo = con.execute("""
        SELECT w.window_start, w.market_slug FROM windows w
        LEFT JOIN book_stats b USING(window_start)
        WHERE b.window_start IS NULL AND w.market_slug IS NOT NULL
        ORDER BY w.window_start""").fetchall()

    added = failed = 0
    t0 = time.time()
    for i, row in enumerate(todo, 1):
        try:
            md = gw._get(f"/v1/markets/{row['market_slug']}/book")
            md = (md or {}).get("marketData") or {}
            st = md.get("stats") or {}
            if not st:
                failed += 1
                continue
            con.execute("""INSERT OR REPLACE INTO book_stats VALUES
                           (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                row["window_start"], row["market_slug"],
                _px(st, "openPx"), _ts(st, "openSetTime"),
                _px(st, "highPx"), _ts(st, "highSetTime"),
                _px(st, "lowPx"), _ts(st, "lowSetTime"),
                _px(st, "closePx"), _ts(st, "closeSetTime"),
                _px(st, "lastTradePx"), _ts(st, "lastTradeSetTime"),
                _px(st, "settlementPx"),
                float(st.get("sharesTraded") or 0),
                _px(st, "notionalTraded"),
                md.get("state"), int(time.time())))
            added += 1
        except Exception:
            failed += 1
        if i % progress_every == 0:
            con.commit()
            print(f"  {i}/{len(todo)}  stored {added}", flush=True)
    con.commit()
    return {"added": added, "failed": failed, "secs": round(time.time() - t0)}


if __name__ == "__main__":
    print("harvesting contract price stats...", flush=True)
    print("RESULT:", harvest_books(), flush=True)
