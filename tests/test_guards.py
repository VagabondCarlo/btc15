"""Each test is a real trade that cost real money. None may become possible
again through a config change."""
import pytest

from btc15.fees import THETA_TAKER
from btc15.strategy import Config, Risk, evaluate, profile, should_exit, stake_for

# every profile, so none can be a trap left lying in the config
PROFILES_THAT_TRADE = ["turnover", "income", "manual", "manual_capped", "replica"]


@pytest.fixture(params=PROFILES_THAT_TRADE)
def cfg(request):
    return profile(request.param, bankroll=25.0)


def test_refuses_a_near_settled_contract(cfg):
    """Oct 2 23:30z: screen read +$3.38 green, settled DOWN, cost a single stake."""
    assert not evaluate(3.38, 5, 0.02, 0.03, 0.0, cfg).take
    assert not evaluate(3.38, 30, 0.05, 0.06, 0.02, cfg).take


def test_never_pays_above_the_price_cap(cfg):
    """September's three biggest bets: $178 at 96 cents to win a few dollars."""
    assert cfg.max_price <= 0.95, "a win at this price is not worth having"
    s = evaluate(250, 180, 0.97, 0.99, 0.98, cfg)
    assert not s.take or s.limit_price <= cfg.max_price


def test_stake_is_capped_well_under_the_account(cfg):
    """Single bets reached 80% of balance and took the account to zero."""
    assert cfg.max_fraction <= 0.10
    bare = Config(**{**cfg.__dict__, "bankroll": None})
    assert stake_for(25.0, 0.90, 0.78, bare) <= 25.0 * 0.10


def test_stacked_entries_are_separated(cfg):
    """The one window's loss loss was two buys in one window on one signal."""
    assert cfg.max_entries_per_window <= 3
    if cfg.max_entries_per_window > 1:
        assert cfg.entry_spacing_secs >= 30


def test_losers_get_cut(cfg):
    """Two Oct 2 losers ran to -78% and -89% with nothing cutting them."""
    deep = should_exit(0.20, 300, 60, cfg, entry_cost_per=0.80,
                       sell_value=0.20)
    assert deep is not None


def test_refuses_early_entries(cfg):
    """14 minutes left wins 50%; 4 minutes wins 87%."""
    assert cfg.entry_secs_max <= 420
    assert not evaluate(60, 700, 0.70, 0.72, 0.80, cfg).take


def test_cooldown_after_a_loss(cfg):
    r = Risk(cfg)
    r.s.peak_balance = 25.0
    r.note_balance(25.0)
    r.record(0.85, False, 0)
    assert r.blocked(25.0, 900) is not None


def test_profit_ratchet_protects_a_winning_run(cfg):
    """the peak peak to near zero is the failure this prevents."""
    r = Risk(cfg)
    r.s.peak_balance = 25.0 * cfg.profit_lock_at * 1.2
    assert r.floor() > cfg.bankroll


def test_reserve_is_unreachable(cfg):
    from btc15.strategy import working_balance
    c = profile("turnover", bankroll=5.0)
    assert working_balance(10.0, c, 4.0) == 4.0, "a loser must not top up"
    assert working_balance(10.0, c, 5.0) == 5.0


def test_price_cap_keeps_wins_worth_having(cfg):
    """At 99 cents a 94% win rate still loses."""
    breakeven = cfg.max_price + THETA_TAKER * cfg.max_price * (1 - cfg.max_price)
    assert breakeven < 0.96, "cap leaves no room for a win to be worth it"


def test_monitor_catches_the_display_trap():
    """About 2% of windows end with the live price on one side of the strike
    and the settlement average on the other. That is roughly two a day, and
    it is the loss that repeats. The monitor must catch every one."""
    import sqlite3
    from pathlib import Path

    import numpy as np

    from btc15.twap import blended_sigma, forecast_settlement

    db = Path("data/harvest.db")
    if not db.exists():
        pytest.skip("no harvest database; run btc15.harvest first")

    con = sqlite3.connect(db)
    prints = {r[0]: r[1] for r in con.execute("SELECT ts,value FROM prints")}
    windows = con.execute(
        """SELECT window_start,window_end,price_to_beat,settlement
           FROM windows WHERE outcome IS NOT NULL AND price_to_beat IS NOT NULL
             AND ABS(recon_open-price_to_beat)<0.005
             AND ABS(recon_close-settlement)<0.005
             AND n_open=60 AND n_close=60""").fetchall()
    con.close()

    traps = [w for w in windows
             if (prints.get(w[1] - 5) is not None
                 and (prints[w[1] - 5] >= w[2]) != (w[3] >= w[2]))]
    if not traps:
        pytest.skip("no trap windows in this dataset")

    missed = []
    for ws, we, ptb, _settle in traps:
        hist = np.array([prints[t] for t in range(ws - 3500, ws)
                         if t in prints])
        sigma = blended_sigma(hist[-600:], hist)
        if not (sigma == sigma):
            continue
        flagged = False
        for sl in range(60, 0, -1):
            live = prints.get(we - sl)
            if live is None:
                continue
            win = [prints[s] for s in range(we - 60, we - sl + 1)
                   if s in prints]
            f = forecast_settlement(np.asarray(win), sl, live, sigma)
            if len(win) / 60.0 > 0.1 and (live >= ptb) != (f.mean >= ptb):
                flagged = True
                break
        if not flagged:
            missed.append(ws)

    assert not missed, f"{len(missed)} of {len(traps)} trap windows unflagged"


def test_ratchet_floor_uses_the_ring_fenced_balance():
    """With $25 in the account and $10 ring-fenced, seeding the peak from the
    account produced a $17.50 floor the bot could never reach, halting it on
    the first decision. It logged 153,334 refusals over four days."""
    from btc15.strategy import working_balance
    c = profile("turnover", bankroll=10.0)
    r = Risk(c)
    r.s.equity = 10.0
    r.note_balance(working_balance(25.0, c, r.s.equity))
    assert r.s.peak_balance == 10.0, "peak tracked the account, not the fence"
    assert r.floor() < 10.0, f"floor ${r.floor():.2f} is above the bankroll"
    assert r.blocked(10.0, 0) is None, "halted before placing a single trade"
