"""One simulator, used by every backtest.

There were three before this, written at different times in different
scripts, and they disagreed. One took a single entry per window and reported
$65 growing to $338; another stacked three and reported $5 shrinking to
$1.17 on the same data. Both were "the same strategy". That is not a result,
it is a bug surface, and any number produced by either was unusable.

This is the only one now. It is deliberately boring: explicit about every
assumption, and it reports a distribution rather than a single path, because
a single path through 278 windows is an anecdote.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import numpy as np

from .fees import THETA_MAKER_REBATE, THETA_TAKER
from .prices import load as load_prices
from .strategy import Config, Risk, WIN_RATE_TABLE, stake_for, working_balance


@dataclass
class Result:
    final: float
    peak: float
    max_drawdown: float
    trades: int
    wins: int
    halted: bool
    windows_sat_out: int

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades else 0.0


def bucket(gap: float):
    for lo, hi, wr in WIN_RATE_TABLE:
        if lo <= abs(gap) < hi:
            return (lo, hi), wr
    return None, None


def load_windows(db: str = "data/harvest.db", date_from: str | None = None,
                 date_to: str | None = None):
    """Window paths with their start times, cleanest windows only."""
    from tune import build_paths
    paths = build_paths(db)
    con = sqlite3.connect(db)
    starts = [r[0] for r in con.execute(
        """SELECT window_start FROM windows
           WHERE outcome IS NOT NULL AND ABS(recon_open-price_to_beat)<0.005
             AND ABS(recon_close-settlement)<0.005
             AND n_open=60 AND n_close=60 ORDER BY window_start""")]
    pairs = list(zip(paths, starts))
    if date_from or date_to:
        q = "SELECT window_start FROM windows WHERE 1=1"
        args = []
        if date_from:
            q += " AND date(window_start,'unixepoch') >= ?"; args.append(date_from)
        if date_to:
            q += " AND date(window_start,'unixepoch') <= ?"; args.append(date_to)
        keep = {r[0] for r in con.execute(q, args)}
        pairs = [(p, s) for p, s in pairs if s in keep]
    con.close()
    return pairs


# Median notional traded in a 15-minute window, from the harvested book
# stats. Taking more than a small share of that moves the price against you,
# and a backtest that ignores it will happily compound a $5 account into
# six figures inside a market that trades $85k a window.
WINDOW_NOTIONAL = 85_000.0
MAX_SHARE_OF_WINDOW = 0.002      # 0.2%, about $170 a trade


def run(windows, cfg: Config, start: float, ask: dict | None = None,
        fill_rate: float = 1.0, rng=None, flat_stake: float | None = None,
        liquidity_cap: float = WINDOW_NOTIONAL * MAX_SHARE_OF_WINDOW) -> Result:
    """Walk the windows once under `cfg`.

    `ask` maps a gap bucket to the price a taker pays. A maker config instead
    posts at the price that leaves its required edge and fills with
    probability `fill_rate`.
    """
    ask = ask if ask is not None else load_prices()
    rng = rng or np.random.default_rng(0)
    risk = Risk(cfg)
    risk.s.equity = start
    risk.note_balance(start)
    bal = peak = start
    dd = 0.0
    n = wins = sat = 0

    for idx, (w, _ws) in enumerate(windows):
        if risk.blocked(bal, idx * 900):
            sat += 1
            continue
        pts = {sl: (g, pu) for sl, g, pu in w["pts"]}
        taken = 0
        last_sl = None

        for sl in range(cfg.entry_secs_max, cfg.entry_secs_min - 1, -1):
            if taken >= max(cfg.max_entries_per_window, 1):
                break
            # Stacked entries must be separated in time, or "three entries"
            # is one trade at triple size off a single unchanged signal.
            if last_sl is not None and last_sl - sl < cfg.entry_spacing_secs:
                continue
            if sl not in pts:
                continue
            gap, p_up = pts[sl]
            key, wr = bucket(gap)
            if key is None or key not in ask:
                continue
            if not (cfg.gap_min <= abs(gap) < cfg.gap_max):
                continue

            if cfg.maker:
                limit = round(wr - max(cfg.min_edge, 0.0), 2)
                if limit <= 0 or limit > ask[key]:
                    continue
                cost = limit - THETA_MAKER_REBATE * limit * (1 - limit)
                if limit < ask[key] and rng.random() > fill_rate:
                    continue
            else:
                limit = ask[key]
                if limit > cfg.max_price:
                    continue
                cost = limit + THETA_TAKER * limit * (1 - limit)

            if wr - cost < cfg.min_edge:
                continue

            work = working_balance(bal, cfg, risk.s.equity)
            if flat_stake is not None:
                # Fixed dollars per trade. Strips compounding out so the
                # edge itself is visible, rather than the exponent.
                stake = min(flat_stake, work)
            else:
                stake = stake_for(work, wr, limit,
                                  Config(**{**cfg.__dict__, "bankroll": None}))
            stake = min(stake, liquidity_cap)
            if stake < cfg.min_stake:
                break
            shares = stake / cost
            side = "UP" if gap > 0 else "DOWN"

            settled = None
            if cfg.exit_enabled:
                for s2 in range(sl - cfg.exit_min_hold, cfg.exit_blackout, -1):
                    if s2 not in pts:
                        continue
                    fair = pts[s2][1] if side == "UP" else 1 - pts[s2][1]
                    # What selling right now would actually net, after
                    # crossing the spread and paying the taker fee again.
                    q = max(0.01, fair - 0.005)
                    out = q - THETA_TAKER * q * (1 - q)
                    ret = out / cost - 1.0
                    hit = (fair <= cfg.exit_fair_floor
                           or (cfg.exit_profit_target is not None
                               and ret >= cfg.exit_profit_target)
                           or (cfg.exit_stop_loss is not None
                               and ret <= -abs(cfg.exit_stop_loss)))
                    if hit:
                        settled = shares * out
                        break
            if settled is None:
                settled = shares if side == w["outcome"] else 0.0

            pnl = settled - stake
            bal += pnl
            risk.s.equity = bal
            peak = max(peak, bal)
            dd = max(dd, (peak - bal) / peak if peak else 0.0)
            risk.note_balance(bal)
            risk.record(wr, pnl > 0, idx * 900)
            n += 1
            wins += pnl > 0
            taken += 1
            last_sl = sl
            if bal < 0.05:
                break
        if bal < 0.05:
            break

    return Result(bal, peak, dd, n, wins, bool(risk.s.halted), sat)


def bootstrap(windows, cfg: Config, start: float, paths_n: int = 200,
              block: int = 24, ask: dict | None = None,
              fill_rate: float = 1.0, seed: int = 0) -> list[Result]:
    """Many runs over resampled window order.

    One pass through a fixed window list is a single path, and a single path
    is an anecdote: it says what happened to one account that started on one
    particular afternoon. Resampling in contiguous blocks keeps the local
    sequence of a real market, which matters because volatility clusters,
    while varying which stretches are met and in what order.
    """
    rng = np.random.default_rng(seed)
    out = []
    nb = max(1, len(windows) // block)
    for i in range(paths_n):
        idx = rng.integers(0, max(1, len(windows) - block), size=nb)
        order = [w for s in idx for w in windows[s:s + block]]
        out.append(run(order, cfg, start, ask, fill_rate,
                       np.random.default_rng(seed + i)))
    return out
