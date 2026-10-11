#!/usr/bin/env python3
"""Replace the fixed win-rate table with something regime-independent.

The table maps a dollar gap to a win rate. That is wrong, and measurably so:
a $30 gap three minutes out is worth 95% when the market is moving $45 per
window and 62% when it is moving $240. The backtest ran through a stretch
averaging $30 per window. Today it is $102. Every number derived from that
table is therefore specific to a calm market, which is precisely how a
strategy passes its backtest and fails live.

The fix is not a bigger table. It is to stop using dollars. The TWAP model
already prices the position in the only unit that travels across regimes:
how far the index sits from the strike relative to how far it can still
move. This measures whether that model is honest, bucket by bucket, and
writes a correction curve the bot applies to its raw output.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from btc15.harvest import open_db
from btc15.twap import forecast_settlement, realized_sigma_per_sec

OUT = Path("data/calibration.json")
BUCKETS = [(0.50, 0.60), (0.60, 0.70), (0.70, 0.80), (0.80, 0.875),
           (0.875, 0.925), (0.925, 0.96), (0.96, 0.985), (0.985, 1.01)]


def collect(db="data/harvest.db", secs_lo=120, secs_hi=240, step=5):
    con = open_db(db)
    P = {r[0]: r[1] for r in con.execute("SELECT ts,value FROM prints")}
    W = con.execute("""SELECT * FROM windows WHERE outcome IS NOT NULL
        AND ABS(recon_open-price_to_beat)<0.005
        AND ABS(recon_close-settlement)<0.005
        AND n_open=60 AND n_close=60 ORDER BY window_start""").fetchall()

    rows = []
    for w in W:
        ws, we, ptb, res = (w["window_start"], w["window_end"],
                            w["price_to_beat"], w["outcome"])
        hist = np.array([P[t] for t in range(ws - 600, ws) if t in P])
        sig = realized_sigma_per_sec(hist)
        if not (sig == sig) or sig <= 0:
            continue
        for t in range(we - secs_hi, we - secs_lo, step):
            px = P.get(t)
            if px is None:
                continue
            win = [P[s] for s in range(we - 60, t + 1) if s in P]
            f = forecast_settlement(np.asarray(win), we - t, px, sig)
            p_up = f.prob_up(ptb)
            side_p = max(p_up, 1 - p_up)
            side = "UP" if p_up >= 0.5 else "DOWN"
            rows.append((side_p, int(side == res), sig, abs(px - ptb)))
    return np.array(rows), len(W)


def main() -> None:
    rows, nwin = collect()
    pred, won, sig, gap = rows[:, 0], rows[:, 1], rows[:, 2], rows[:, 3]
    print(f"{len(rows):,} samples from {nwin} windows\n")

    print("IS THE MODEL HONEST? predicted vs what happened\n")
    print(f"{'model says':>14}{'actually won':>14}{'n':>9}{'correction':>12}")
    curve = []
    for lo, hi in BUCKETS:
        m = (pred >= lo) & (pred < hi)
        if m.sum() < 150:
            continue
        p, a = float(pred[m].mean()), float(won[m].mean())
        curve.append({"lo": lo, "hi": hi, "pred": p, "actual": a,
                      "n": int(m.sum())})
        print(f"{p:>13.1%}{a:>14.1%}{int(m.sum()):>9,}{a - p:>+11.1%}")

    print("\nDOES IT HOLD IN BOTH A CALM AND A FAST MARKET?\n")
    med = float(np.median(sig))
    print(f"split at {med:.2f}/sec (${med * 30:.0f} per window)\n")
    print(f"{'model says':>14}{'calm':>10}{'fast':>10}{'difference':>13}")
    stable = True
    for lo, hi in BUCKETS:
        m = (pred >= lo) & (pred < hi)
        calm, fast = m & (sig <= med), m & (sig > med)
        if calm.sum() < 80 or fast.sum() < 80:
            continue
        c, f_ = float(won[calm].mean()), float(won[fast].mean())
        flag = "" if abs(c - f_) < 0.07 else "   <-- drifts"
        if flag:
            stable = False
        print(f"{(lo + hi) / 2:>13.0%}{c:>10.1%}{f_:>10.1%}{f_ - c:>+12.1%}{flag}")

    print(f"\n  {'stable across regimes' if stable else 'NOT stable'}")

    print("\nFOR CONTRAST, the dollar-gap table the bot uses now:\n")
    print(f"{'gap':>12}{'calm':>10}{'fast':>10}{'difference':>13}")
    for g0, g1 in ((25, 50), (50, 75), (75, 100)):
        m = (gap >= g0) & (gap < g1)
        calm, fast = m & (sig <= med), m & (sig > med)
        if calm.sum() < 80 or fast.sum() < 80:
            continue
        c, f_ = float(won[calm].mean()), float(won[fast].mean())
        print(f"{f'${g0}-{g1}':>12}{c:>10.1%}{f_:>10.1%}{f_ - c:>+12.1%}"
              f"{'   <-- drifts' if abs(c - f_) >= 0.07 else ''}")

    OUT.write_text(json.dumps({"buckets": curve}, indent=2))
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
