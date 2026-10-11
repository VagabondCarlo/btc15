"""Fee model and the break-even arithmetic that decides every trade.

Published schedule (docs.polymarket.us/fees):

    taker fee     = theta_t * C * p * (1 - p)        theta_t = 0.0695
    maker rebate  = theta_m * C * p * (1 - p)        theta_m = 0.0125

rounded to the cent, banker's rounding.

CONFIRMED against the live venue: every BTC 15m up/down market carries
`feeCoefficient: 0.0695` in its public payload, so crypto uses the same
coefficient as everything else. Press coverage reporting a "1.80% peak" was
quoting the fee as a share of the $1 payout, not of notional:
0.0695 * 0.25 = $0.0174 per contract at p = 0.5, i.e. 1.74% of $1.

Still read `feeCoefficient` from the market payload rather than trusting this
constant, since it is per-market and the venue can change it.
"""

from __future__ import annotations

from dataclasses import dataclass

THETA_TAKER = 0.0695      # confirmed: market payload feeCoefficient
THETA_TAKER_GENERAL = THETA_TAKER   # back-compat alias
THETA_MAKER_REBATE = 0.0125


def _bankers(x: float) -> float:
    from decimal import Decimal, ROUND_HALF_EVEN
    return float(Decimal(repr(x)).quantize(Decimal("0.01"), ROUND_HALF_EVEN))


def taker_fee(contracts: int, price: float, theta: float = THETA_TAKER_GENERAL) -> float:
    return _bankers(theta * contracts * price * (1.0 - price))


def maker_rebate(contracts: int, price: float,
                 theta: float = THETA_MAKER_REBATE) -> float:
    return _bankers(theta * contracts * price * (1.0 - price))


def breakeven_prob(price: float, theta: float = THETA_TAKER_GENERAL) -> float:
    """Win probability needed for a taker buy at `price` to break even.

    Buying one contract costs p + theta*p*(1-p) and pays 1 on a win, so
    break-even q* = p + theta*p*(1-p).
    """
    return price + theta * price * (1.0 - price)


@dataclass(frozen=True)
class TradeEconomics:
    price: float
    contracts: int
    model_p: float
    fee: float
    cost: float
    breakeven_p: float
    edge: float          # model_p - breakeven_p, in probability points
    ev: float            # expected dollars
    risk: float          # dollars lost if wrong

    @property
    def ev_per_dollar_risked(self) -> float:
        return self.ev / self.risk if self.risk else 0.0


def evaluate_buy(price: float, contracts: int, model_p: float,
                 theta: float = THETA_TAKER_GENERAL) -> TradeEconomics:
    """Economics of taking `contracts` at `price` believing P(win) = model_p."""
    fee = taker_fee(contracts, price, theta)
    cost = contracts * price + fee
    be = breakeven_prob(price, theta)
    ev = model_p * contracts * 1.0 - cost
    return TradeEconomics(
        price=price, contracts=contracts, model_p=model_p, fee=fee, cost=cost,
        breakeven_p=be, edge=model_p - be, ev=ev, risk=cost,
    )


def kelly_fraction(model_p: float, price: float,
                   theta: float = THETA_TAKER_GENERAL) -> float:
    """Kelly stake as a fraction of bankroll for a binary at `price`.

    Net odds b = (1 - cost_per_contract) / cost_per_contract, so
    f* = (p*(b+1) - 1) / b. Clipped at zero; the caller applies a fractional
    multiplier. Full Kelly on a 95-cent contract is wildly overlevered for a
    model whose probabilities are themselves estimated, so never ship f*.
    """
    c = price + theta * price * (1.0 - price)
    if not (0.0 < c < 1.0):
        return 0.0
    b = (1.0 - c) / c
    f = (model_p * (b + 1.0) - 1.0) / b
    return max(0.0, min(1.0, f))
