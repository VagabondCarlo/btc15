"""Entry logic, sizing and risk limits.

The shape of the strategy comes from what the data actually supports, which
is close to the opposite of the manual rule it replaces.

  * Moderate gaps, not large ones. A $25-75 move with 2-4 minutes left is
    worth 81-94%, and the market will sometimes sell it below that. A $100+
    move is worth 97.8-98.8% and the market charges 99, which is a loss at
    every price observed.
  * Rest a limit order, do not take the offer. Taking at the median price
    loses 1 to 11 cents a contract. The same bets bought in the cheapest
    quarter of prices make 2 to 4 cents, and resting also collects the maker
    rebate instead of paying the taker fee, worth about another cent.
  * Size as a fraction of the balance so wins compound, but derive the
    fraction from Kelly on the measured edge and then cut it hard. Full
    Kelly on an estimated probability is how an account dies with a winning
    strategy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .fees import THETA_MAKER_REBATE, THETA_TAKER, kelly_fraction

# Measured win rate of betting the gap's side, by gap size, 2-4 minutes left.
# From backtest_gap_rule.py over 720 windows. Keep this table and the backtest
# in step: if you re-run on more data, update these numbers.
WIN_RATE_TABLE: list[tuple[float, float, float]] = [
    # gap_low, gap_high, measured win rate
    (10.0, 25.0, 0.707),
    (25.0, 50.0, 0.815),
    (50.0, 75.0, 0.936),
    (75.0, 100.0, 0.981),
    (100.0, 150.0, 0.978),
    (150.0, 250.0, 0.988),
    (250.0, float("inf"), 0.995),   # measured 100%, held back to 99.5%
]

# Buckets where no observed price produced a positive edge. Taking or making,
# the market charges more than the bet is worth.
DEAD_ABOVE_GAP = 75.0


@dataclass
class Config:
    entry_secs_max: int = 240       # no earlier than 4 minutes left
    entry_secs_min: int = 120       # no later than 2 minutes left
    # Chosen by robust.py, not by tune.py. tune.py picks the single best of
    # 240 combinations on the whole history, and walkforward.py showed what
    # that is worth: settings chosen on the first half of the data were
    # still profitable on the second half only 42% of the time, with a
    # median that lost money. That is how a backtest lies.
    #
    # robust.py instead splits the history into four blocks and keeps only
    # settings that made money in every one, then ranks by the WORST block.
    # 62 of 240 survived, and they cluster around 7 cents of required edge
    # with a deep exit, which is a plateau rather than a lucky peak. This
    # setting won 82% across all four blocks.
    # Block-by-block at a 10% fill rate, the wider/pickier variants either
    # barely grew or failed outright. The 84% win-rate version everyone
    # reaches for first went +13.6%, +13.7%, -13.7%, -13.0%: it made money
    # for four days and gave it back over the next four, which is what a
    # fitted result looks like. This one went +38.0%, +41.6%, +2.6%, +9.8%,
    # positive in every block, for 8 points less win rate.
    gap_min: float = 25.0
    gap_max: float = 50.0
    min_edge: float = 0.03          # need 3 cents of edge after costs
    max_price: float = 0.95
    maker: bool = True              # rest a limit order rather than cross
    tick: float = 0.01

    # The handoff spec said IMMEDIATE_OR_CANCEL. That is a taker-only
    # instruction: it fills against what is already there or dies on the
    # spot, so an order posted below the market would cancel instantly and
    # the bot would log "unfilled" forever without ever placing a resting
    # bid. A maker strategy needs an order that sits in the book.
    # preflight.py probes which values this venue accepts.
    time_in_force: str = "TIME_IN_FORCE_GOOD_TILL_CANCEL"
    cancel_before_expiry: int = 20   # pull an unfilled order this close to the end

    kelly_scale: float = 0.25       # quarter Kelly
    max_fraction: float = 0.08      # never more than 8% of balance
    min_stake: float = 0.05         # below this the order is pointless

    # Ring-fence. The bot reads the real account balance from the venue, so
    # without this it would size against every dollar sitting there. Set it
    # to the amount you are putting at risk; the rest of the account is
    # invisible to the bot.
    #
    # The bot starts with exactly this much and then lives on its own
    # results. Winnings raise what it may stake, losses lower it, and it
    # never touches a cent beyond the allocation. Money set aside for your
    # own manual trades stays yours whatever the bot does.
    bankroll: float | None = None

    # Below this many contracts the taker fee rounds badly: the true fee on
    # half a contract is under a cent and gets rounded up to a full one,
    # roughly doubling it and eating a third of the edge. Under this size
    # the bot will only rest an order, never cross the spread.
    maker_only_below_contracts: float = 3.0

    # No cap on how many trades a day. After a loss the bot sits out a few
    # windows, checks whether it is still performing to spec, and carries on.
    one_trade_per_window: bool = True
    max_entries_per_window: int = 1   # he stacked up to 5 buys in one window
    # Seconds between stacked entries. Without this, "up to 3 entries" means
    # three fills on the same second at the same price off the same signal,
    # which is one trade at triple size, not three decisions. He re-entered
    # minutes apart as the move developed.
    entry_spacing_secs: int = 45
    cooldown_windows: int = 2        # windows to sit out after a loss
    cooldown_escalate: bool = True   # double it on consecutive losses
    cooldown_max: int = 8
    review_trades: int = 20          # how far back the review looks
    review_tolerance: float = 0.08   # underperform by this and tighten up
    edge_bump: float = 0.01          # extra edge demanded while underperforming
    edge_bump_max: float = 0.05
    hard_floor_frac: float = 0.35    # stop entirely below this share of peak

    # Profit ratchet. Without it the stop is simply 35% of the high water
    # mark, so a bot that turned $5 into $8 could give back down to $2.80
    # before halting: ahead, then behind on the original stake, which is the
    # worst way to end. Once it has made real progress the allocation itself
    # becomes the floor, so a run that worked cannot turn into a loss.
    profit_lock_at: float = 1.4      # multiple of the allocation that arms it
    profit_lock_keep: float = 0.5    # and then keep this share of the gains

    # Exiting a position that has gone wrong. Measured over 446 positions:
    # cutting early is actively harmful, because most dips recover. A 10c
    # stop cost 3.1c a contract against simply holding. Only a deep exit
    # helps, and its main benefit is a smaller swing, which matters a lot to
    # an account that compounds.
    exit_enabled: bool = True
    exit_fair_floor: float = 0.40   # bail when the position is worth this little
    exit_min_hold: int = 20         # seconds before an exit is allowed
    exit_blackout: int = 25         # no exits this close to settlement

    # Sell into strength instead of holding for the last few cents.
    #
    # Buy at 70 cents, watch it reach 90, and selling there returns 28% of
    # capital in about two minutes. Holding to settlement returns 43% but
    # ties the money up for ten more minutes and gives the move room to come
    # back. Three 28% turns in an hour beat one 43% hold. The engine is
    # capital turnover, not bigger wins.
    #
    # None of this is scalping a coin flip: the entry still requires the gap
    # to favour the position. It only changes when the position is released.
    # Refuse anything above the measured ceiling for the gap. Without this
    # the bot pays the typical asking price, which loses in every bucket.
    use_price_ceiling: bool = True

    exit_profit_target: float | None = None   # sell at this return on capital
    exit_stop_loss: float | None = None       # cut at this loss on capital

    # The model used to need a wide tolerance because its volatility input
    # was a raw trailing estimate that drifted 11 points between calm and
    # fast markets. With the blended estimate its calibration error is 1.2%,
    # so a disagreement with the table now means something real and the gate
    # can be tighter. vol_safety is gone: inflating sigma by a quarter was a
    # crude stand-in for the bias that blending actually removes, and it
    # pushed every probability toward 50% rather than toward the truth.
    vol_shrink: float = 0.5
    vol_safety: float = 1.0
    require_model_agreement: bool = True
    model_tolerance: float = 0.15


PROFILES: dict[str, dict] = {
    # Kept for comparison, not recommended. It rests orders below the market
    # and at real quoted prices almost never fills, because the market is
    # already cheaper than the price it waits for. It took 0 trades over 720
    # windows against the manual profile's 437.
    "patient": {},

    # A discretionary method, reconstructed from 12 settled trades: enter
    # on a decisive move with 2-4 minutes left and pay the quoted price, up
    # to the 96% was observed being paid. No edge calculation, because he was
    # not doing one. This turned the starting balance into the peak in ten hours by hand.
    #
    # Every safety still applies: the ring-fence, the profit ratchet, the
    # cooldown after a loss, the capital floor and the deep exit. The
    # strategy is his. The brakes are not negotiable.
    "manual": {
        "gap_min": 25.0,
        "gap_max": 100.0,
        "maker": False,          # take the offer, as he did
        # -1.0 means no edge check at all. He was not computing one; he
        # looked at the move, decided it was decisive, and paid the quoted
        # price. A floor of 0.0 would silently filter every trade, because
        # the market price exceeds the measured win rate in every bucket.
        # Measured against real recorded quotes rather than inferred ones,
        # taking the offer pays in three buckets: $25-50 at about 75 cents
        # for a bet worth 81.5%, $50-75 at 88 for 93.6%, and $75-100 at 90
        # for 98.1%. Above $100 the market charges 98 to 99 and the edge is
        # gone, which is also where the largest observed losses were.
        "min_edge": 0.02,
        "max_price": 0.93,
        "require_model_agreement": False,
        "maker_only_below_contracts": 0.0,
    },

    # The same method with the one filter his own results argue for. His
    # losses clustered in high-priced entries: 82% and 92% cost him about
    # $44 each, and a 95% winner paid a few dollars on $129.78 at risk. Capping the
    # price keeps the trades whose wins are worth having.
    # Full replication of what the video shows, measured from 41 buys, 20
    # settled wins, 3 settled losses and 9 early sells over three days.
    #
    # The distinguishing features are not the entry rule, they are the
    # sizing and the stacking. Up to 5 separate buys in a single
    # window, staked up to $177 at a time, and took prices from 56% to 99%.
    # That is how $65 became the peak in ten hours. It is also how the peak became
    # near zero over the day that followed: a $44 loss, then $2 bets, then a
    # $4.41 loss, then 13 cents.
    #
    # This profile exists to be run in paper against the other one on the
    # same windows. It is not recommended for live money, and the safeties
    # are deliberately left on even though the original had none.
    "replica": {
        "gap_min": 10.0,            # he entered at 56% odds, a small gap
        "gap_max": 400.0,
        "maker": False,             # paid the quoted price
        "min_edge": -1.0,           # no edge calculation at all
        "max_price": 0.93,
        "require_model_agreement": False,
        "maker_only_below_contracts": 0.0,
        "one_trade_per_window": False,
        "max_entries_per_window": 3,
        # The record this replicates used 25-80% of balance per bet, which is
        # what took the account to zero. Kept at 3% so the profile can be
        # studied and raced without being able to repeat that.
        "max_fraction": 0.03,
        "kelly_scale": 1.0,
        "exit_fair_floor": 0.45,    # he sold out of losers, sometimes
    },

    # For steady income rather than growth. Same entry read as "replica", but
    # staked at 1% a trade instead of 40%.
    #
    # The difference is not subtle. Over 720 windows and 150 bootstrap paths
    # starting at $50: at 1% a trade the median finish is $90, the worst 5%
    # is $74, the typical drawdown is 5% and it never once hit the stop. At
    # 8% the median is higher but it stopped out in 37% of paths; at 15%, in
    # 61%. The aggressive settings have a better median and a worse life.
    #
    # The measured edge is about 4.6 cents per dollar staked across roughly
    # 140 trades a day. At 1% of bankroll that is a bit over 6% a day, which
    # is already an extraordinary number and should be treated as an upper
    # bound: it is measured over 7.5 days, the win rates are partly fitted to
    # that stretch, and one period in four in the sample lost money.
    "income": {
        "gap_min": 10.0,
        "gap_max": 400.0,
        "maker": False,
        "min_edge": -1.0,
        # 90 cents, not 99. At 99 you need a 99.1% win rate just to break
        # even, and capping here tested better on every measure.
        "max_price": 0.90,
        "require_model_agreement": False,
        "maker_only_below_contracts": 0.0,
        "one_trade_per_window": False,
        "max_entries_per_window": 3,
        # 3%, not 1%. At a $5 balance 1% is five cents, which is the
        # minimum order, so the safe fraction is too small to place and
        # anything placeable is too big to be safe. 3% is the smallest
        # stake that both works at $5 and keeps the stop-out rate at 3%.
        "max_fraction": 0.03,
        "kelly_scale": 1.0,
        "exit_fair_floor": 0.45,
    },

    # Sell into strength and redeploy, rather than holding to settlement.
    # Reconstructed from a session of 22 round trips: 18 winners averaging
    # +25% on capital, held one to five minutes, against 4 losers. The two
    # worst losers ran to -78% and -89% because nothing cut them, which is
    # what the stop here is for.
    "turnover": {
        # $50 up, not $20. At the measured ceilings the $10-25 and $25-50
        # buckets still lose 3.7 and 4.4 cents; $50 and above pay 3 to 6.
        "gap_min": 50.0,
        "gap_max": 250.0,
        "maker": False,
        "min_edge": -1.0,
        "max_price": 0.93,
        "require_model_agreement": False,
        "maker_only_below_contracts": 0.0,
        "one_trade_per_window": False,
        "max_entries_per_window": 2,
        # Entry timing is the whole edge and it is not close. Holding
        # everything else fixed and moving only when the position is opened:
        # 14 minutes left wins 50% and the account grinds down; 7 minutes
        # wins 76%; 4 minutes wins 87% and it compounds. Entering early does
        # not give the trade room to run, it just removes the information
        # that made it a good trade.
        "entry_secs_max": 240,
        "entry_secs_min": 120,
        "max_fraction": 0.03,
        "kelly_scale": 1.0,
        "exit_profit_target": 0.25,
        "exit_stop_loss": 0.30,
        "exit_fair_floor": 0.30,
        "exit_min_hold": 10,
    },

    "manual_capped": {
        "gap_min": 25.0,
        "gap_max": 100.0,
        "maker": False,
        "min_edge": 0.02,
        "max_price": 0.85,
        "require_model_agreement": False,
        "maker_only_below_contracts": 0.0,
    },
}


def profile(name: str, **overrides) -> "Config":
    """Build a Config from a named profile."""
    if name not in PROFILES:
        raise ValueError(f"unknown profile {name!r}; "
                         f"choose from {sorted(PROFILES)}")
    return Config(**{**PROFILES[name], **overrides})


_LIMITS: dict | None = None


def price_ceiling(gap: float, db: str = "data/live.db") -> float | None:
    """The most worth paying for this gap, from recorded quotes.

    Measured over 70 hours, the median ask loses money in every bucket. The
    cheap end of the same distribution pays 3 to 6 cents in the larger gaps.
    So the binding constraint is price, not the gap: the bot waits for a
    good moment instead of taking whatever is on screen, which means sitting
    out roughly seven qualifying moments in eight.
    """
    global _LIMITS
    if _LIMITS is None:
        try:
            from .prices import buy_limits
            _LIMITS = buy_limits(db)
        except Exception:
            _LIMITS = {}
    for (lo, hi), px in _LIMITS.items():
        if lo <= abs(gap) < hi:
            return px
    return None


def table_win_rate(gap: float) -> float | None:
    g = abs(gap)
    for lo, hi, wr in WIN_RATE_TABLE:
        if lo <= g < hi:
            return wr
    return None


def entry_cost(price: float, maker: bool) -> float:
    """What one contract really costs, fee or rebate included."""
    adj = price * (1.0 - price)
    return price - THETA_MAKER_REBATE * adj if maker else price + THETA_TAKER * adj


def limit_for_edge(win_rate: float, need: float, maker: bool,
                   tick: float = 0.01) -> float:
    """Highest price that still leaves `need` of edge, rounded down to a tick.

    This is the whole point of resting rather than crossing. The book's spread
    is one tick, so joining the bid saves half a cent and the market's price
    is often ten cents above what the bet is worth. Anchoring the order to the
    current quote would just buy the overpricing a hair cheaper. Anchoring it
    to fair value means the order sits where it is worth being filled, and
    simply does not trade on the days the market never comes down.

    Solves cost(L) = win_rate - need for L, where cost includes the maker
    rebate or the taker fee.
    """
    target = win_rate - need
    if target <= 0:
        return 0.0
    th = -THETA_MAKER_REBATE if maker else THETA_TAKER
    # th*L^2*(-1) ... cost(L) = L + th*L*(1-L) = L(1+th) - th*L^2
    # => th*L^2 - L(1+th) + target = 0
    if abs(th) < 1e-12:
        L = target
    else:
        disc = (1 + th) ** 2 - 4 * th * target
        L = target if disc < 0 else ((1 + th) - math.sqrt(disc)) / (2 * th)
    return max(0.0, math.floor(L / tick) * tick)


@dataclass
class Signal:
    take: bool
    reason: str
    side: str | None = None
    limit_price: float | None = None
    win_rate: float | None = None
    model_p: float | None = None
    edge: float | None = None
    cost: float | None = None


def evaluate(gap: float, secs_left: float, best_bid: float | None,
             best_ask: float | None, model_p_up: float | None,
             cfg: Config, min_edge: float | None = None) -> Signal:
    """Decide whether this moment is worth an order, and at what price."""
    if not (cfg.entry_secs_min <= secs_left <= cfg.entry_secs_max):
        return Signal(False, f"outside entry window ({secs_left:.0f}s left)")

    g = abs(gap)
    if g < cfg.gap_min:
        return Signal(False, f"gap ${g:.0f} below minimum ${cfg.gap_min:.0f}")
    if g >= cfg.gap_max:
        return Signal(False,
                      f"gap ${g:.0f} is in the overpriced zone; every observed "
                      f"price above ${cfg.gap_max:.0f} loses money")

    wr = table_win_rate(g)
    if wr is None:
        return Signal(False, "no measured win rate for this gap")

    side = "UP" if gap > 0 else "DOWN"

    # Prices are quoted for the UP side. DOWN costs 1 minus the UP price,
    # so the two books are mirror images of each other.
    if side == "UP":
        ask, bid = best_ask, best_bid
    else:
        ask = (1.0 - best_bid) if best_bid is not None else None
        bid = (1.0 - best_ask) if best_ask is not None else None
    if ask is None:
        return Signal(False, "no offer on the side we want")

    need = cfg.min_edge if min_edge is None else min_edge

    if cfg.maker:
        # Price the order off what the bet is worth, not off the quote.
        limit = round(limit_for_edge(wr, need, True, cfg.tick), 2)
        if limit <= 0:
            return Signal(False, "no price leaves the required edge")
        if limit >= ask:
            # The market is already cheap enough to just take it.
            limit = round(ask, 2)
    else:
        limit = round(ask, 2)

    if not (0.02 <= limit <= cfg.max_price):
        return Signal(False, f"price {limit:.2f} outside 0.02-{cfg.max_price}")

    if cfg.use_price_ceiling:
        ceil = price_ceiling(gap)
        if ceil is not None and limit > ceil:
            return Signal(False,
                          f"asking {limit:.2f}, worth paying at most "
                          f"{ceil:.2f} for a ${abs(gap):.0f} gap",
                          side=side, limit_price=limit, win_rate=wr)

    # The TWAP model is a second opinion. The table is the measured truth, but
    # it is blind to how fast the market is moving right now. If the two
    # disagree badly, something is unusual and the safe move is to pass.
    model_side_p = None
    if model_p_up is not None:
        model_side_p = model_p_up if side == "UP" else 1.0 - model_p_up
        if cfg.require_model_agreement and abs(model_side_p - wr) > cfg.model_tolerance:
            return Signal(False,
                          f"table says {wr:.0%} but live model says "
                          f"{model_side_p:.0%}; too far apart to trust",
                          side=side, win_rate=wr, model_p=model_side_p)

    # Use the more cautious of the two estimates.
    p = min(wr, model_side_p) if model_side_p is not None else wr
    cost = entry_cost(limit, cfg.maker)
    edge = p - cost

    if edge < need:
        return Signal(False,
                      f"edge {edge * 100:+.1f}c below the {need * 100:.0f}c "
                      f"minimum at {limit:.2f}",
                      side=side, limit_price=limit, win_rate=wr,
                      model_p=model_side_p, edge=edge, cost=cost)

    return Signal(True, f"{side} at {limit:.2f}, edge {edge * 100:+.1f}c",
                  side=side, limit_price=limit, win_rate=wr,
                  model_p=model_side_p, edge=edge, cost=cost)


def working_balance(account: float, cfg: Config,
                    equity: float | None = None) -> float:
    """The balance the bot may size against.

    With no bankroll set, that is the whole account. With one set, it is
    whatever the bot's own equity has become: the allocation plus its wins,
    minus its losses, and never more than the account actually holds.

    The floor is deliberately NOT the original allocation. Topping a losing
    bot back up to its starting stake would be funding it from money that
    was set aside for something else, which is the opposite of ring-fencing.
    A bot that has lost two of its five dollars is a three dollar bot.
    """
    if not cfg.bankroll:
        return account
    allowed = cfg.bankroll if equity is None else equity
    return max(0.0, min(account, allowed))


def stake_for(balance: float, win_p: float, limit: float, cfg: Config) -> float:
    """Dollars to commit. A fraction of the balance, so wins compound."""
    balance = working_balance(balance, cfg)
    f_full = kelly_fraction(win_p, limit,
                            -THETA_MAKER_REBATE if cfg.maker else THETA_TAKER)
    frac = min(f_full * cfg.kelly_scale, cfg.max_fraction)
    return max(0.0, balance * frac)


def should_exit(fair_now: float, secs_left: float, held: float,
                cfg: Config, entry_cost_per: float | None = None,
                sell_value: float | None = None) -> str | None:
    """Whether to close a position early.

    Deliberately has no price-based stop. Cutting at 10 or 20 cents below
    entry tested worse than holding, by 3.1 and 2.1 cents a contract, because
    a position that dips usually recovers before settlement. The only exit
    that paid was waiting until the position was genuinely broken.

    Also refuses to sell in the last few seconds. By then the settlement
    average is mostly fixed, the book is thin, and whatever the position is
    worth you will not get it.
    """
    if not cfg.exit_enabled:
        return None
    if held < cfg.exit_min_hold:
        return None
    if secs_left <= cfg.exit_blackout:
        return None
    if fair_now <= cfg.exit_fair_floor:
        return f"worth only {fair_now:.0%} now, closing"

    if entry_cost_per and sell_value and entry_cost_per > 0:
        ret = sell_value / entry_cost_per - 1.0
        if cfg.exit_profit_target is not None and ret >= cfg.exit_profit_target:
            return f"up {ret:+.0%}, taking it"
        if cfg.exit_stop_loss is not None and ret <= -abs(cfg.exit_stop_loss):
            return f"down {ret:+.0%}, cutting it"
    return None


@dataclass
class RiskState:
    peak_balance: float = 0.0
    equity: float = 0.0          # allocation plus realised profit
    trades: int = 0
    wins: int = 0
    loss_streak: int = 0
    cooldown_until_window: int = 0
    edge_penalty: float = 0.0
    halted: str = ""
    last_review: str = ""


class Risk:
    """Keeps the bot rolling, but makes it stop and look after a loss.

    There is no cap on trades per day. A loss triggers a cooldown of a few
    windows, and consecutive losses lengthen it. Before resuming, the bot
    compares how it has actually been doing against the win rate its own
    table predicted. If it is running behind, it demands more edge per trade
    until the gap closes. That is the "analyse the situation" step: the
    cooldown is not just a pause, it changes how picky the bot is.

    The one hard stop is a capital floor. Below a set share of peak balance
    it stops and waits for a person, because by then something is wrong that
    a cooldown will not fix.
    """

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.s = RiskState()
        self.history: list[tuple[float, bool]] = []   # (expected_p, won)

    def note_balance(self, balance: float) -> None:
        self.s.peak_balance = max(self.s.peak_balance, balance)

    def floor(self) -> float:
        """The balance at which the bot stops for good.

        Two rules, whichever is higher. A plain trailing stop at a share of
        the high water mark, and, once the bot has made real progress, a
        ratchet that keeps part of what it made. The ratchet is what stops a
        winning run from becoming a losing one.
        """
        c, s = self.cfg, self.s
        trailing = s.peak_balance * c.hard_floor_frac
        alloc = c.bankroll or 0.0
        if alloc and s.peak_balance >= alloc * c.profit_lock_at:
            locked = alloc + (s.peak_balance - alloc) * c.profit_lock_keep
            return max(trailing, locked)
        return trailing

    def blocked(self, balance: float, window_start: int) -> str | None:
        c, s = self.cfg, self.s
        if s.halted:
            return s.halted
        fl = self.floor()
        if s.peak_balance > 0 and balance < fl:
            s.halted = (f"balance ${balance:.2f} is below the ${fl:.2f} floor "
                        f"(peak was ${s.peak_balance:.2f}); stopping for a "
                        f"human to look")
            return s.halted
        if window_start < s.cooldown_until_window:
            left = (s.cooldown_until_window - window_start) // 900
            return f"cooling off for {left} more window(s) after a loss"
        return None

    def min_edge(self) -> float:
        """Edge required right now, raised while the bot is underperforming."""
        return self.cfg.min_edge + self.s.edge_penalty

    def record(self, expected_p: float, won: bool, window_start: int) -> str:
        c, s = self.cfg, self.s
        s.trades += 1
        s.wins += int(won)
        self.history.append((expected_p, won))

        if won:
            s.loss_streak = 0
            # Earn back pickiness slowly as results recover.
            s.edge_penalty = max(0.0, s.edge_penalty - c.edge_bump / 2)
            return ""

        s.loss_streak += 1
        n = c.cooldown_windows
        if c.cooldown_escalate:
            n = min(c.cooldown_max, c.cooldown_windows * (2 ** (s.loss_streak - 1)))
        s.cooldown_until_window = window_start + n * 900
        return self.review(n)

    def review(self, cooldown_windows: int) -> str:
        """Compare recent results against what the model expected."""
        c, s = self.cfg, self.s
        recent = self.history[-c.review_trades:]
        if len(recent) < 5:
            s.last_review = (f"loss {s.loss_streak} in a row, sitting out "
                             f"{cooldown_windows} windows "
                             f"({len(recent)} trades so far, too few to judge)")
            return s.last_review

        expected = sum(p for p, _ in recent) / len(recent)
        actual = sum(w for _, w in recent) / len(recent)
        shortfall = expected - actual

        if shortfall > c.review_tolerance:
            s.edge_penalty = min(c.edge_bump_max, s.edge_penalty + c.edge_bump)
            verdict = (f"running {shortfall * 100:.0f} points below the model "
                       f"({actual:.0%} actual vs {expected:.0%} expected over "
                       f"{len(recent)} trades); now demanding "
                       f"{self.min_edge() * 100:.0f}c of edge")
        else:
            verdict = (f"{actual:.0%} actual vs {expected:.0%} expected over "
                       f"{len(recent)} trades, within tolerance")
        s.last_review = (f"loss {s.loss_streak} in a row, sitting out "
                         f"{cooldown_windows} windows. {verdict}")
        return s.last_review

    def summary(self) -> str:
        s = self.s
        wr = (s.wins / s.trades) if s.trades else 0.0
        return (f"{s.trades} trades, {s.wins} wins ({wr:.0%}), "
                f"streak {s.loss_streak}, edge required "
                f"{self.min_edge() * 100:.0f}c")
