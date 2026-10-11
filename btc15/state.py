"""Persisted bot state. Without this, a crash erases every safety limit.

The risk limits lived only in memory. The systemd units restart on failure,
so a bot that crashed repeatedly would come back each time with its loss
streak at zero, its cooldown cleared, its edge penalty forgotten and its
peak balance reset to whatever the balance happened to be. Every brake would
release exactly when things were going worst.

Open positions had the same problem. A resting order or a filled position
lives on the venue, not in this process. A restart forgot it, so the order
stayed live unattended, the settlement was never booked, and the next window
could open a second position on top of it.

Everything here is keyed to a single row so reads and writes stay trivial.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict, fields

STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS bot_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    balance REAL,
    risk_json TEXT,
    history_json TEXT,
    open_order_json TEXT,
    mode TEXT,
    updated_at INTEGER
);
"""


def init(con: sqlite3.Connection) -> None:
    con.executescript(STATE_SCHEMA)
    con.commit()


def save(con: sqlite3.Connection, balance: float, risk, open_order,
         mode: str) -> None:
    con.execute(
        """INSERT INTO bot_state(id,balance,risk_json,history_json,
               open_order_json,mode,updated_at)
           VALUES (1,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
               balance=excluded.balance, risk_json=excluded.risk_json,
               history_json=excluded.history_json,
               open_order_json=excluded.open_order_json,
               mode=excluded.mode, updated_at=excluded.updated_at""",
        (balance, json.dumps(asdict(risk.s)), json.dumps(risk.history[-200:]),
         json.dumps(open_order) if open_order else None, mode,
         int(time.time())))
    con.commit()


def load(con: sqlite3.Connection, risk, mode: str):
    """Restore state. Returns (balance, open_order) or (None, None) if fresh.

    Refuses to restore state saved in a different mode, so a paper run's
    balance can never be mistaken for a live one.
    """
    row = con.execute("SELECT * FROM bot_state WHERE id = 1").fetchone()
    if row is None:
        return None, None
    if row["mode"] != mode:
        print(f"  ignoring saved state from {row['mode']} mode")
        return None, None

    try:
        saved = json.loads(row["risk_json"] or "{}")
        valid = {f.name for f in fields(risk.s)}
        for k, v in saved.items():
            if k in valid:
                setattr(risk.s, k, v)
        risk.history = [tuple(x) for x in json.loads(row["history_json"] or "[]")]
    except (ValueError, TypeError) as e:
        print(f"  saved risk state unreadable ({e}), starting fresh")
        return None, None

    oo = None
    if row["open_order_json"]:
        try:
            oo = json.loads(row["open_order_json"])
        except ValueError:
            oo = None

    age = int(time.time()) - (row["updated_at"] or 0)
    print(f"  restored state from {age}s ago: balance ${row['balance']:.2f}, "
          f"{risk.s.trades} trades, streak {risk.s.loss_streak}, "
          f"edge penalty {risk.s.edge_penalty * 100:.0f}c")
    if risk.s.halted:
        print(f"  NOTE: bot is halted: {risk.s.halted}")
    return row["balance"], oo


def clock_skew() -> float:
    """Seconds this machine's clock differs from the venue's.

    Signed requests are rejected beyond 30 seconds of skew, and the
    settlement average is taken over exact one-second slots, so a drifting
    clock corrupts every calculation silently. A Raspberry Pi has no
    battery-backed clock and is the usual offender.

    Uses the venue's own Date header, so it measures the difference that
    actually matters rather than trusting an arbitrary time server.
    """
    import email.utils
    import urllib.request
    req = urllib.request.Request("https://gateway.polymarket.us/v1/events?limit=1",
                                 headers={"User-Agent": "btc15/0.1"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=15) as r:
        hdr = r.headers.get("Date")
    rtt = time.time() - t0
    if not hdr:
        return 0.0
    server = email.utils.parsedate_to_datetime(hdr).timestamp()
    return (t0 + rtt / 2) - server
