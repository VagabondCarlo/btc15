"""Tests for persisted state. Without this a crash clears every safety limit."""
import sqlite3

import pytest

from btc15 import state as bs
from btc15.strategy import Config, Risk


def _con():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    bs.init(c)
    return c


def _risk_with_history():
    r = Risk(Config())
    r.note_balance(100.0)
    ws = 1790000000
    for p, won in [(0.8, True)] * 5 + [(0.8, False)] * 2:
        r.record(p, won, ws)
        ws += 900
    return r


def test_round_trip_preserves_limits():
    con = _con()
    r = _risk_with_history()
    before = (r.s.trades, r.s.wins, r.s.loss_streak, r.s.edge_penalty,
              r.s.cooldown_until_window, r.s.peak_balance)
    bs.save(con, 118.42, r, None, "paper")

    r2 = Risk(Config())
    bal, oo = bs.load(con, r2, "paper")
    assert bal == pytest.approx(118.42)
    assert (r2.s.trades, r2.s.wins, r2.s.loss_streak, r2.s.edge_penalty,
            r2.s.cooldown_until_window, r2.s.peak_balance) == before
    assert len(r2.history) == len(r.history)


def test_cooldown_survives_restart():
    """A crash loop must not clear the cooldown and let it trade straight on."""
    con = _con()
    r = Risk(Config())
    r.note_balance(100.0)
    r.record(0.8, False, 1790000000)
    assert r.blocked(100.0, 1790000900) is not None
    bs.save(con, 100.0, r, None, "paper")

    r2 = Risk(Config())
    bs.load(con, r2, "paper")
    assert r2.blocked(100.0, 1790000900) is not None, "cooldown was lost"


def test_halt_survives_restart():
    con = _con()
    r = Risk(Config())
    r.note_balance(100.0)
    r.blocked(10.0, 1790000000)          # trips the capital floor
    assert r.s.halted
    bs.save(con, 10.0, r, None, "paper")
    r2 = Risk(Config())
    bs.load(con, r2, "paper")
    assert r2.s.halted, "a halted bot came back unhalted"


def test_paper_state_never_loads_into_live():
    con = _con()
    r = _risk_with_history()
    bs.save(con, 500.0, r, {"side": "UP"}, "paper")
    r2 = Risk(Config())
    bal, oo = bs.load(con, r2, "live")
    assert bal is None and oo is None and r2.s.trades == 0


def test_open_order_round_trip():
    con = _con()
    r = Risk(Config())
    oo = {"ws": 1790000000, "we": 1790000900, "side": "UP", "limit": 0.78,
          "shares": 10.0, "cost": 7.78, "filled": True}
    bs.save(con, 100.0, r, oo, "paper")
    _, back = bs.load(con, Risk(Config()), "paper")
    assert back["side"] == "UP" and back["filled"] is True
    assert back["shares"] == pytest.approx(10.0)


def test_corrupt_state_does_not_crash():
    con = _con()
    con.execute("""INSERT INTO bot_state(id,balance,risk_json,history_json,
                   open_order_json,mode,updated_at)
                   VALUES (1,100,'{bad json',?,NULL,'paper',0)""", ("[]",))
    con.commit()
    bal, oo = bs.load(con, Risk(Config()), "paper")
    assert bal is None and oo is None


def test_empty_state_is_fresh():
    assert bs.load(_con(), Risk(Config()), "paper") == (None, None)
