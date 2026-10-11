#!/usr/bin/env python3
"""Does cutting a losing position beat holding it to settlement?

The honest starting point: in a fairly priced market the answer is no. The
contract price is an estimate of the probability, so the expected value of
holding equals the current price. Selling gets you that same number minus
the spread and the fee. Cutting a loss would then cost exactly the
transaction cost, every time, with no upside.

Two reasons to test it anyway.

  1. This market is not perfectly priced. The measured win rate for a given
     gap differs from what the market charges, so the price a position falls
     to may be below or above what it is really worth.
  2. There is a sound non-panic reason to exit. You bought because the gap
     was $50 and worth 93.6%. If the gap collapses to $10 the position is
     worth about 70% and it is something you would never have bought. That
     is not cutting a loss, it is closing a trade whose reason has gone.

Selling is modelled as crossing to the bid: fair value less half the one
tick spread, less the taker fee. That is what it costs to get out in a hurry.
"""

from __future__ import annotations

import argparse

import numpy as np

from btc15.fees import THETA_TAKER
from btc15.harvest import open_db
from btc15.strategy import WIN_RATE_TABLE
from btc15.twap import forecast_settlement, realized_sigma_per_sec

HALF_SPREAD = 0.005
VOL_LOOKBACK = 600


def table_wr(gap):
    for lo, hi, wr in WIN_RATE_TABLE:
        if lo <= abs(gap) < hi:
            return wr
    return None


def sell_proceeds(fair: float) -> float:
    px = max(0.01, fair - HALF_SPREAD)
    return px - THETA_TAKER * px * (1 - px)


def simulate(db, entry_lo=25.0, entry_hi=75.0, secs_lo=120, secs_hi=240):
    """Collect every position the bot would open, with its full price path."""
    con = open_db(db)
    P = {r[0]: r[1] for r in con.execute("SELECT ts,value FROM prints")}
    W = con.execute("""SELECT * FROM windows WHERE outcome IS NOT NULL
        AND ABS(recon_open-price_to_beat)<0.005
        AND ABS(recon_close-settlement)<0.005
        AND n_open=60 AND n_close=60 ORDER BY window_start""").fetchall()

    out = []
    for w in W:
        ws, we, ptb, res = (w["window_start"], w["window_end"],
                            w["price_to_beat"], w["outcome"])
        hist = np.array([P[t] for t in range(ws - VOL_LOOKBACK, ws) if t in P])
        sig = realized_sigma_per_sec(hist)
        if not (sig == sig) or sig <= 0:
            continue
        sig *= 1.25

        entry_t = None
        for t in range(we - secs_hi, we - secs_lo):
            px = P.get(t)
            if px is None:
                continue
            if entry_lo <= abs(px - ptb) < entry_hi:
                entry_t = t
                break
        if entry_t is None:
            continue

        g0 = P[entry_t] - ptb
        side = "UP" if g0 > 0 else "DOWN"
        wr = table_wr(g0)
        if wr is None:
            continue
        entry_price = round(wr - 0.02, 2)      # the bot's resting price

        path = []
        for t in range(entry_t, we + 1):
            px = P.get(t)
            if px is None:
                continue
            win = [P[s] for s in range(we - 60, t + 1) if s in P]
            p_up = forecast_settlement(np.asarray(win), we - t, px,
                                       sig).prob_up(ptb)
            fair = p_up if side == "UP" else 1.0 - p_up
            path.append((t, fair, abs(px - ptb)))
        if path:
            out.append({"entry": entry_price, "side": side, "won": side == res,
                        "path": path})
    return out


def evaluate(positions, rule, **kw):
    """Return mean P&L per contract under an exit rule."""
    pnl = []
    exits = 0
    for pos in positions:
        e = pos["entry"]
        got = None
        for t, fair, gap in pos["path"]:
            if rule == "hold":
                break
            if rule == "stop" and fair <= e - kw["drop"]:
                got = sell_proceeds(fair)
                break
            if rule == "floor" and fair <= kw["level"]:
                got = sell_proceeds(fair)
                break
            if rule == "thesis" and gap < kw["gap_min"]:
                got = sell_proceeds(fair)
                break
        if got is None:
            got = 1.0 if pos["won"] else 0.0
        else:
            exits += 1
        pnl.append(got - e)
    a = np.array(pnl)
    return a.mean(), a.std(), exits, len(pnl)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/harvest.db")
    a = ap.parse_args()

    pos = simulate(a.db)
    print(f"{len(pos)} positions the bot would have opened "
          f"(gap $25-75, 2-4 min left)\n")
    base, bsd, _, n = evaluate(pos, "hold")
    print(f"{'exit rule':<34}{'exits':>8}{'EV/contract':>14}{'swing':>9}"
          f"{'vs holding':>13}")
    print("-" * 78)
    print(f"{'hold to settlement':<34}{0:>8}{base * 100:>+13.2f}c"
          f"{bsd * 100:>8.0f}c{'baseline':>13}")
    rows = [("sell if it drops 10c", "stop", {"drop": 0.10}),
            ("sell if it drops 20c", "stop", {"drop": 0.20}),
            ("sell if it drops 30c", "stop", {"drop": 0.30}),
            ("sell if fair falls under 0.60", "floor", {"level": 0.60}),
            ("sell if fair falls under 0.50", "floor", {"level": 0.50}),
            ("sell if fair falls under 0.40", "floor", {"level": 0.40}),
            ("sell if gap shrinks under $25", "thesis", {"gap_min": 25.0}),
            ("sell if gap shrinks under $15", "thesis", {"gap_min": 15.0})]
    for label, rule, kw in rows:
        ev, sd, ex, _ = evaluate(pos, rule, **kw)
        print(f"{label:<34}{ex:>8}{ev * 100:>+13.2f}c{sd * 100:>8.0f}c"
              f"{(ev - base) * 100:>+12.2f}c")
