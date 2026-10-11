#!/usr/bin/env python3
"""Run the pre-registered test in DECISION.md and print the verdict.

The parameters below are fixed by that file and must not be edited. They
were written down on 2026-10-11, before the deciding data existed, for the
specific reason that a threshold you can move after seeing the result is
not a threshold.

    PYTHONPATH=. ./.venv/bin/python decide.py
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import sys

import numpy as np

from btc15.fees import THETA_TAKER

# --- pre-registered, do not edit -------------------------------------
BAND = (0.20, 0.35)      # market mid for UP
THRESHOLD = 2.73         # Bonferroni for the 8 bands that were searched
MIN_WINDOWS = 341        # sample size fixed in advance
DECISION_DATE = dt.date(2026, 10, 18)
REGISTERED = dt.date(2026, 10, 11)
# ---------------------------------------------------------------------


def observations(db: str = "data/live.db"):
    """One row per window: the first moment the mid entered the band.

    Quotes inside a single window are the same observation seen again and
    again. Counting them separately inflates the sample roughly 500-fold and
    turns noise into certainty.
    """
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """SELECT b.window_start ws, b.ts_ms, b.best_bid bb, b.best_ask ba,
                  w.outcome
           FROM live_book b JOIN live_windows w USING(window_start)
           WHERE w.outcome IS NOT NULL
             AND b.best_ask IS NOT NULL AND b.best_bid IS NOT NULL
             AND b.best_ask > b.best_bid
             AND b.best_ask <= 0.99 AND b.best_bid >= 0.01
           ORDER BY b.ts_ms""").fetchall()
    con.close()

    first: dict[int, sqlite3.Row] = {}
    for r in rows:
        mid = (r["bb"] + r["ba"]) / 2
        if BAND[0] <= mid < BAND[1] and r["ws"] not in first:
            first[r["ws"]] = r
    return [first[k] for k in sorted(first)]


def main() -> int:
    today = dt.date.today()
    try:
        obs = observations()
    except sqlite3.Error as e:
        print(f"cannot read the recording database: {e}")
        return 2

    # Buy the side opposite the cheap one, at its ask, fee included.
    costs = np.clip(np.array([1.0 - r["ba"] for r in obs]), 0.01, 0.99)
    won = np.array([0.0 if r["outcome"] == "UP" else 1.0 for r in obs])
    edge = won - (costs + THETA_TAKER * costs * (1 - costs))
    n = len(obs)

    print(f"PRE-REGISTERED TEST, fixed {REGISTERED:%d %B %Y}\n")
    print(f"  claim      : UP priced {BAND[0]:.2f}-{BAND[1]:.2f} happens less "
          f"often than that; buy the other side")
    print(f"  threshold  : t > {THRESHOLD}")
    print(f"  sample     : {MIN_WINDOWS} windows")
    print(f"  decide on  : {DECISION_DATE:%d %B %Y}\n")

    if n < 2:
        print(f"  only {n} observations; nothing to test yet")
        return 1

    se = edge.std(ddof=1) / np.sqrt(n)
    t = edge.mean() / se if se else 0.0
    print(f"  windows collected : {n} of {MIN_WINDOWS}")
    print(f"  other side wins   : {won.mean():.1%}")
    print(f"  average cost      : {costs.mean():.3f}")
    print(f"  edge per contract : {edge.mean() * 100:+.1f}c "
          f"(95% {edge.mean() * 100 - 1.96 * se * 100:+.1f} to "
          f"{edge.mean() * 100 + 1.96 * se * 100:+.1f})")
    print(f"  t statistic       : {t:.2f}\n")

    if today < DECISION_DATE or n < MIN_WINDOWS:
        short = max(0, MIN_WINDOWS - n)
        print(f"  NOT YET. {short} more windows, "
              f"{max(0, (DECISION_DATE - today).days)} days to go.")
        print("  Reading the result early and acting on it is the same")
        print("  mistake as moving the threshold.")
        return 1

    print("  " + "=" * 56)
    if t > THRESHOLD:
        print("  CONFIRMED. Trade it.")
        print("  $5 ring-fenced, 3% a stake, every existing guard on, and")
        print("  one week of paper first to check live fills match the")
        print("  recorded quotes. No more money until 100 live trades.")
        return 0
    print("  NOT CONFIRMED. Stop.")
    print(f"  t = {t:.2f} against a threshold of {THRESHOLD}.")
    print("  Not a nearby band, not a longer sample, not a tweak to the")
    print("  entry. This was the last claim standing and it did not clear.")
    print("  DECISION.md says stop, and it was written before the data.")
    return 3


if __name__ == "__main__":
    sys.exit(main())
