#!/usr/bin/env python3
"""What the contract costs as certainty arrives, and whether that leaves room.

This uses data already collected rather than waiting for more. Two sources:

  harvest.db  every finished window carries `notionalTraded` and
              `sharesTraded`, so their ratio is the volume weighted average
              price of every trade in that window. That is a real, unbiased
              price, and there are 726 of them: 182 hours of market.

  live.db     recorded quotes, which give the asking price against how much
              time is left.

Together they answer the question that matters. The market prices direction
correctly: buying at the opening price returns exactly zero before fees. The
price then climbs as the outcome becomes obvious, from about 59 cents with
13 minutes left to 91 cents with 2 to 4 minutes left. An entry rule that
waits for a decisive move is, by construction, buying at the expensive end
of its own window.
"""

from __future__ import annotations

import sqlite3
import statistics as st

from btc15.fees import THETA_TAKER


def pnl_at(rows, price_of, label, side="YES"):
    tot = n = wins = 0.0
    for r in rows:
        p = price_of(r)
        if p is None or not (0.01 <= p <= 0.99):
            continue
        won = (r["outcome"] == "UP") if side == "YES" else (r["outcome"] == "DOWN")
        px = p if side == "YES" else 1.0 - p
        if not (0.01 <= px <= 0.99):
            continue
        tot += (1.0 if won else 0.0) - (px + THETA_TAKER * px * (1 - px))
        n += 1
        wins += won
    if n:
        print(f"  {label:<36}{n:>6.0f}{wins / n:>8.1%}{tot / n * 100:>+10.1f}c")


def main() -> None:
    h = sqlite3.connect("data/harvest.db")
    h.row_factory = sqlite3.Row
    rows = h.execute(
        """SELECT w.outcome, b.notional_traded/b.shares_traded vwap,
                  b.low_px, b.high_px, b.open_px
           FROM book_stats b JOIN windows w USING(window_start)
           WHERE b.shares_traded>0 AND b.notional_traded>0
             AND w.outcome IS NOT NULL AND w.price_to_beat IS NOT NULL
             AND ABS(w.recon_open-w.price_to_beat)<0.005
             AND b.low_px IS NOT NULL AND b.high_px IS NOT NULL""").fetchall()
    h.close()

    print(f"BUYING AT DIFFERENT PRICES, {len(rows)} finished windows "
          f"({len(rows) * 15 / 60:.0f} hours)\n")
    print(f"  {'what you buy':<36}{'n':>6}{'win':>8}{'per trade':>11}")
    print("-" * 62)
    pnl_at(rows, lambda r: r["open_px"], "Yes at the opening price")
    pnl_at(rows, lambda r: r["vwap"], "Yes at the window's average price")
    pnl_at(rows, lambda r: r["vwap"], "No  at the window's average price", "NO")
    pnl_at(rows, lambda r: r["low_px"], "Yes at the window's lowest price")
    pnl_at(rows, lambda r: r["high_px"], "Yes at the window's highest price")

    try:
        l = sqlite3.connect("data/live.db")
        l.row_factory = sqlite3.Row
        q = l.execute(
            """SELECT secs_left sl,
                      CASE WHEN gap>0 THEN best_ask ELSE 1.0-best_bid END px
               FROM live_book
               WHERE best_ask IS NOT NULL AND best_bid IS NOT NULL
                 AND gap IS NOT NULL""").fetchall()
        l.close()
    except sqlite3.Error:
        return
    if len(q) < 500:
        return

    print(f"\nWHAT IT COSTS AS THE ANSWER ARRIVES  ({len(q):,} quotes)\n")
    print(f"  {'time left':>12}{'n':>8}{'median ask':>13}{'cheapest 25%':>15}")
    print("-" * 50)
    for lo, hi, lbl in ((780, 900, "13-15 min"), (600, 780, "10-13 min"),
                        (420, 600, "7-10 min"), (240, 420, "4-7 min"),
                        (120, 240, "2-4 min"), (60, 120, "1-2 min"),
                        (0, 60, "under 1 min")):
        v = sorted(r["px"] for r in q if lo <= r["sl"] < hi)
        if len(v) < 50:
            continue
        print(f"  {lbl:>12}{len(v):>8}{st.median(v):>13.2f}"
              f"{v[len(v) // 4]:>15.2f}")

    print("\n  The opening price returns zero before fees, so the market is")
    print("  not mispricing direction. The price then climbs with certainty.")
    print("  Waiting for a decisive move means buying at the top of that")
    print("  climb, and the money in the window went to whoever bought the")
    print("  dip before anyone knew which way it would go.")


if __name__ == "__main__":
    main()
