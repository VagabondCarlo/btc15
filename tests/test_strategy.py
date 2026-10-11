"""Tests for entry, exit and sizing."""
import pytest

from btc15.strategy import (Config, evaluate, limit_for_edge, should_exit,
                            stake_for, table_win_rate)


def test_refuses_outside_entry_window():
    c = Config()
    assert not evaluate(30, 600, 0.70, 0.72, None, c).take
    assert not evaluate(30, 60, 0.70, 0.72, None, c).take


def test_refuses_overpriced_gap_band():
    c = Config()
    s = evaluate(300, 180, 0.97, 0.99, None, c)
    assert not s.take and "overpriced" in s.reason


def test_down_side_mirrors_the_book():
    """The book quotes the Up side. A Down bet costs 1 minus the Up price."""
    c = Config()
    up = evaluate(30, 180, 0.70, 0.72, None, c)
    down = evaluate(-30, 180, 0.28, 0.30, None, c)
    assert up.take and down.take
    assert down.side == "DOWN"
    assert down.limit_price == pytest.approx(up.limit_price)


def test_limit_is_priced_off_fair_value_not_the_quote():
    """The resting price comes from what the bet is worth, not from the
    quote. The spread is one tick, so anchoring to the quote would just buy
    the overpricing a hair cheaper."""
    c = Config()
    fair_limit = limit_for_edge(table_win_rate(30), c.min_edge, True, c.tick)

    # Market well above our price: rest at ours and wait, whatever it quotes.
    for bid, ask in ((0.90, 0.92), (0.85, 0.86), (0.95, 0.96)):
        s = evaluate(30, 180, bid, ask, None, c)
        assert s.take and s.limit_price == pytest.approx(fair_limit)

    # Market already cheaper than our price: take the cheaper one, never pay
    # more than the offer.
    s = evaluate(30, 180, 0.70, 0.72, None, c)
    assert s.take and s.limit_price == pytest.approx(0.72)
    assert s.edge > c.min_edge, "a cheaper fill must mean more edge, not less"


def test_never_pays_more_than_the_offer():
    c = Config()
    s = evaluate(30, 180, 0.50, 0.52, None, c)
    assert s.limit_price <= 0.52


def test_limit_for_edge_leaves_the_edge():
    from btc15.strategy import entry_cost
    for wr, need in ((0.815, 0.03), (0.936, 0.07), (0.981, 0.05)):
        L = limit_for_edge(wr, need, True)
        assert wr - entry_cost(L, True) >= need - 1e-9


def test_model_disagreement_blocks_the_trade():
    c = Config()
    s = evaluate(30, 180, 0.70, 0.72, 0.40, c)   # model says Up is unlikely
    assert not s.take and "apart" in s.reason


def test_exit_only_when_badly_broken():
    c = Config()
    assert should_exit(0.35, 180, 60, c)
    assert not should_exit(0.55, 180, 60, c)


def test_no_exit_in_the_blackout_or_too_early():
    c = Config()
    assert not should_exit(0.20, 10, 60, c), "sold into a thin book at the end"
    assert not should_exit(0.20, 180, 2, c), "sold before the trade had a chance"


def test_stake_is_capped_and_compounds():
    c = Config()
    small = stake_for(100.0, 0.815, 0.78, c)
    big = stake_for(1000.0, 0.815, 0.78, c)
    assert small <= 100.0 * c.max_fraction + 1e-9
    assert big == pytest.approx(small * 10)


def test_no_stake_without_edge():
    c = Config()
    assert stake_for(100.0, 0.50, 0.78, c) == 0.0


def test_win_rate_table_covers_the_entry_band():
    c = Config()
    for gap in (c.gap_min, (c.gap_min + c.gap_max) / 2, c.gap_max - 1):
        assert table_win_rate(gap) is not None


def test_bankroll_ring_fences_the_account():
    """With $10 loaded and a $5 bankroll the bot must never size against $10."""
    from btc15.strategy import working_balance
    c = Config()
    c.bankroll = 5.0
    assert working_balance(10.0, c) == 5.0
    assert working_balance(3.0, c) == 3.0, "a smaller account is the real cap"
    assert stake_for(10.0, 0.815, 0.78, c) == pytest.approx(
        stake_for(5.0, 0.815, 0.78, c))


def test_no_bankroll_means_the_whole_account():
    from btc15.strategy import working_balance
    c = Config()
    assert c.bankroll is None
    assert working_balance(42.0, c) == 42.0


def test_bankroll_is_the_bots_own_money_only():
    """The reserve exists for the operator's own manual trades. A losing bot must
    never be topped back up out of it."""
    from btc15.strategy import working_balance
    c = Config()
    c.bankroll = 5.0
    assert working_balance(10.0, c, 5.0) == 5.0
    assert working_balance(11.2, c, 6.2) == 6.2, "winnings must compound"
    assert working_balance(9.1, c, 4.1) == 4.1, "a losing bot is a smaller bot"
    assert working_balance(6.75, c, 1.75) == 1.75
    assert working_balance(10.0, c, 20.0) == 10.0, "cannot exceed the account"


def test_reserve_is_untouchable_across_a_losing_run():
    """Whatever the bot does, the reserved half stays whole."""
    from btc15.strategy import working_balance
    c = Config()
    c.bankroll = 5.0
    account, equity = 10.0, 5.0
    for _ in range(40):
        stake = stake_for(working_balance(account, c, equity), 0.815, 0.78,
                          Config(**{**c.__dict__, "bankroll": None}))
        equity -= stake
        account -= stake
        assert account - working_balance(account, c, equity) >= 5.0 - 1e-9
    assert equity > 0


def test_profit_ratchet_stops_a_win_becoming_a_loss():
    from btc15.strategy import Risk
    c = Config()
    c.bankroll = 5.0
    r = Risk(c)
    r.s.peak_balance = 5.0
    assert r.floor() == pytest.approx(1.75), "no lock before any progress"
    r.s.peak_balance = 8.0
    assert r.floor() == pytest.approx(6.5), "half the gain above $5 is kept"
    assert r.floor() > 5.0, "a winning run must not end below the stake"
    r.s.peak_balance = 25.0
    assert r.floor() == pytest.approx(15.0)


def test_ratchet_needs_no_bankroll_to_fall_back_on_trailing():
    from btc15.strategy import Risk
    c = Config()
    r = Risk(c)
    r.s.peak_balance = 100.0
    assert r.floor() == pytest.approx(35.0)


def test_halt_fires_at_the_floor():
    from btc15.strategy import Risk
    c = Config()
    c.bankroll = 5.0
    r = Risk(c)
    r.s.peak_balance = 8.0
    assert r.blocked(7.0, 0) is None
    assert r.blocked(6.0, 0) is not None
