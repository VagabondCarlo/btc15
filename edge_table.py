#!/usr/bin/env python3
"""The decisive table: break-even price versus what the market charges.

Left number in each cell is the highest price you could pay and still break
even, derived from the realised win rate of betting the gap's side in that
bucket. Right number is what the market actually charged, taken from real
contract trades at moments when the gap and time matched that bucket.

Market price below break-even means the bet was worth taking.
Market price above break-even means you were being overcharged.

Win rates come from independent window outcomes. Market prices come from the
window high and low, which are extremes, so the median of those is a fair
read of the price level but the quartile column is optimistic by
construction. Treat the cheap quartile as "the best you might catch", not as
what you should expect to pay.
"""

from __future__ import annotations

import math
import statistics as st

import numpy as np

from btc15.fees import THETA_TAKER
from btc15.harvest import open_db
from btc15.twap import realized_sigma_per_sec

GAPS = [(10, 20), (20, 40), (40, 60), (60, 100), (100, 200), (200, 10**9)]
SECS = [(5, 60), (60, 120), (120, 240), (240, 420)]
VOL_LOOKBACK = 600


def breakeven(w, theta=THETA_TAKER):
    if w <= 0: return 0.0
    if w >= 1: return 1.0
    a, b, c = theta, -(1.0 + theta), w
    d = b * b - 4 * a * c
    return 1.0 if d < 0 else (-b - math.sqrt(d)) / (2 * a)


def main(db="data/harvest.db"):
    con = open_db(db)
    prints = {r[0]: r[1] for r in con.execute("SELECT ts,value FROM prints")}
    wins = con.execute("""
        SELECT w.*, b.high_px,b.high_ts,b.low_px,b.low_ts
        FROM windows w LEFT JOIN book_stats b USING(window_start)
        WHERE w.outcome IS NOT NULL AND w.price_to_beat IS NOT NULL
          AND ABS(w.recon_open-w.price_to_beat)<0.005
          AND ABS(w.recon_close-w.settlement)<0.005
          AND w.n_open=60 AND w.n_close=60 ORDER BY w.window_start""").fetchall()

    outcomes: dict[tuple, list] = {}
    prices: dict[tuple, list] = {}

    for w in wins:
        ws, we, ptb, out = (w["window_start"], w["window_end"],
                            w["price_to_beat"], w["outcome"])
        for t in range(ws, we + 1, 5):
            sl = we - t
            px = prints.get(t)
            if px is None:
                continue
            gap = abs(px - ptb)
            side = "UP" if px - ptb > 0 else "DOWN"
            for gi, (g0, g1) in enumerate(GAPS):
                if not (g0 <= gap < g1):
                    continue
                for si, (s0, s1) in enumerate(SECS):
                    if s0 <= sl < s1:
                        outcomes.setdefault((gi, si), []).append(int(side == out))
                break
        for k in ("high", "low"):
            ts, p = w[f"{k}_ts"], w[f"{k}_px"]
            if ts is None or p is None:
                continue
            sl, b = we - ts, prints.get(ts)
            if b is None:
                continue
            gap = b - ptb
            cost = p if gap > 0 else 1.0 - p
            for gi, (g0, g1) in enumerate(GAPS):
                if not (g0 <= abs(gap) < g1):
                    continue
                for si, (s0, s1) in enumerate(SECS):
                    if s0 <= sl < s1:
                        prices.setdefault((gi, si), []).append(cost)
                break

    print("BREAK-EVEN PRICE  vs  MARKET PRICE (median / cheap quartile)")
    print("green = market cheaper than break-even, i.e. worth taking\n")
    hdr = f"{'gap':>10} | " + " | ".join(f"{f'{a}-{b}s':>22}" for a, b in SECS)
    print(hdr); print("-" * len(hdr))
    verdicts = []
    for gi, (g0, g1) in enumerate(GAPS):
        cells = []
        for si, (s0, s1) in enumerate(SECS):
            o, p = outcomes.get((gi, si), []), prices.get((gi, si), [])
            if len(o) < 40 or len(p) < 12:
                cells.append(f"{'-':>22}")
                continue
            be = breakeven(sum(o) / len(o))
            med = st.median(p)
            q1 = sorted(p)[len(p) // 4]
            flag = "<<" if med < be else ("~" if q1 < be else "  ")
            cells.append(f"{be:>5.3f} vs {med:.3f}/{q1:.3f}{flag:>3}")
            verdicts.append((f"${g0}-{g1 if g1 < 10**8 else '+'}",
                             f"{s0}-{s1}s", be, med, q1, len(o)))
        lbl = f"${g0}+" if g1 > 10**8 else f"${g0}-{g1}"
        print(f"{lbl:>10} | " + " | ".join(cells))

    print("\n<< market cheaper than break-even at the median (a real edge)")
    print(" ~ only the cheapest quartile clears break-even (thin, rare)")
    print("   blank means the market charges more than the bet is worth\n")

    good = [v for v in verdicts if v[3] < v[2]]
    thin = [v for v in verdicts if v[3] >= v[2] > v[4]]
    if good:
        print("Buckets with a median edge:")
        for g, s, be, med, q1, n in good:
            print(f"   {g:>9} {s:>9}  break-even {be:.3f}  market {med:.3f}"
                  f"  edge {be - med:+.3f}  (n={n})")
    else:
        print("No bucket is priced below break-even at the median.")
    if thin:
        print("\nBuckets where only the cheapest quartile clears:")
        for g, s, be, med, q1, n in thin:
            print(f"   {g:>9} {s:>9}  break-even {be:.3f}  median {med:.3f}"
                  f"  cheap quartile {q1:.3f}")
    con.close()


if __name__ == "__main__":
    main()
