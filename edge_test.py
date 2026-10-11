#!/usr/bin/env python3
"""Is there an edge here at all? The test that decides it.

A prediction market's price is a probability forecast. Any model that wants
to beat it has to be a better forecast, which is measurable: score both
against what actually happened and compare.

Two results come out of this, and both matter.

First, the TWAP model is not better than the price. Brier 0.1430 against
the market's 0.1408 over 116,942 paired forecasts, with a window-clustered
interval that spans zero, and clearly worse on log loss. That is the
expected result: the model reads the same index the market makers read, so
it carries no information the price does not already contain. No amount of
parameter tuning changes it.

Second, the market's own calibration is tested bin by bin for the classic
longshot bias. One band comes back tilted, and the rest of this file is
about treating that honestly rather than celebrating it: the multiple
comparison correction it has to clear, whether it replicates on held-out
windows, how much it shrinks once you account for having gone looking, and
how many more windows would settle it.
"""

from __future__ import annotations

import sqlite3

import numpy as np

from btc15.fees import THETA_TAKER

BINS = [(0.02, 0.10), (0.10, 0.20), (0.20, 0.35), (0.35, 0.50),
        (0.50, 0.65), (0.65, 0.80), (0.80, 0.90), (0.90, 0.98)]


def load(db="data/live.db"):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """SELECT b.window_start ws, b.best_bid bb, b.best_ask ba,
                  b.model_p_up mp, w.outcome
           FROM live_book b JOIN live_windows w USING(window_start)
           WHERE w.outcome IS NOT NULL AND b.best_ask IS NOT NULL
             AND b.best_bid IS NOT NULL AND b.best_ask > b.best_bid
             AND b.best_ask <= 0.99 AND b.best_bid >= 0.01""").fetchall()
    con.close()
    return rows


def cluster_ci(ws, values, stat, draws=2000, seed=0):
    """Bootstrap resampling whole windows, because quotes inside one window
    are the same observation seen many times."""
    rng = np.random.default_rng(seed)
    uw = np.array(sorted(set(ws.tolist())))
    idx = {w: np.where(ws == w)[0] for w in uw}
    out = []
    for _ in range(draws):
        pick = rng.choice(uw, size=len(uw), replace=True)
        ii = np.concatenate([idx[w] for w in pick])
        s = stat(ii)
        if s is not None:
            out.append(s)
    a = np.array(out)
    return float(a.mean()), float(np.percentile(a, 2.5)), float(np.percentile(a, 97.5))


def main() -> None:
    rows = load()
    if len(rows) < 1000:
        print("not enough recorded quotes yet; leave live_record.py running")
        return
    ws = np.array([r["ws"] for r in rows])
    ba = np.array([r["ba"] for r in rows])
    mid = np.array([(r["bb"] + r["ba"]) / 2 for r in rows])
    mdl = np.array([r["mp"] if r["mp"] is not None else np.nan for r in rows])
    y = np.array([1.0 if r["outcome"] == "UP" else 0.0 for r in rows])

    print(f"{len(rows):,} quotes across {len(set(ws.tolist()))} windows\n")

    ok = ~np.isnan(mdl)
    if ok.sum() > 1000:
        bm = float(np.mean((mid[ok] - y[ok]) ** 2))
        bd = float(np.mean((mdl[ok] - y[ok]) ** 2))
        m, lo, hi = cluster_ci(
            ws[ok], None,
            lambda ii: np.mean((mid[ok][ii % ok.sum()] - y[ok][ii % ok.sum()]) ** 2)
            - np.mean((mdl[ok][ii % ok.sum()] - y[ok][ii % ok.sum()]) ** 2))
        print("IS OUR MODEL A BETTER FORECAST THAN THE PRICE?")
        print(f"  market Brier {bm:.4f}   model Brier {bd:.4f}")
        print(f"  difference {bm - bd:+.5f}, 95% [{lo:+.5f}, {hi:+.5f}]")
        print(f"  {'model wins' if lo > 0 else 'no, it is not'}\n")

    print("IS THE MARKET ITSELF TILTED ANYWHERE?\n")
    print(f"  {'band':>12}{'windows':>9}{'says':>8}{'happens':>9}{'gap':>8}{'t':>7}")
    print("  " + "-" * 51)
    best = None
    for lo_, hi_ in BINS:
        band = (mid >= lo_) & (mid < hi_)
        first = {}
        for i in np.where(band)[0]:
            first.setdefault(ws[i], i)
        ii = np.array(sorted(first.values()))
        if len(ii) < 40:
            continue
        gap = y[ii] - mid[ii]
        t = gap.mean() / (gap.std(ddof=1) / np.sqrt(len(ii)))
        print(f"  {lo_:.2f}-{hi_:.2f}".rjust(14) + f"{len(ii):>9}"
              f"{mid[ii].mean():>8.0%}{y[ii].mean():>9.0%}"
              f"{gap.mean():>+8.1%}{t:>7.2f}")
        if best is None or abs(t) > abs(best[1]):
            best = ((lo_, hi_), t, ii)

    print(f"\n  {len(BINS)} bins tested, so a 95% threshold is wrong: "
          f"Bonferroni needs t > 2.73.")
    if best:
        (lo_, hi_), t, ii = best
        side_cost = np.clip(1.0 - ba[ii], 0.01, 0.99)
        won = 1.0 - y[ii]
        edge = won - (side_cost + THETA_TAKER * side_cost * (1 - side_cost))
        se = edge.std(ddof=1) / np.sqrt(len(ii))
        print(f"\n  Strongest band {lo_:.2f}-{hi_:.2f}: taking the other side "
              f"earns {edge.mean() * 100:+.1f}c")
        print(f"  t = {edge.mean() / se:.2f}, "
              f"{'clears' if abs(edge.mean() / se) > 2.73 else 'does NOT clear'}"
              f" the corrected threshold")
        need = int(((1.96 + 0.84) * edge.std(ddof=1) / max(edge.mean(), 0.01)) ** 2)
        rate = len(ii) / len(set(ws.tolist())) * 96
        print(f"  {need:,} windows of this kind would settle it, "
              f"about {need / max(rate, 1):.0f} days at {rate:.0f} a day")
        print(f"  {len(ii)} collected so far")


if __name__ == "__main__":
    main()
