#!/usr/bin/env python3
"""Why the fee is not the problem, and why a high win rate is not enough.

Two tables. The first splits the loss on a market-price entry into the fee
and the overpricing. The second shows what any win rate is worth at any
price. Both are the same point: these contracts pay $1, so the price you pay
is almost the whole of the bet. Winning 99 times out of 100 is a losing
strategy if you paid 99 cents each time.
"""
from btc15.fees import THETA_TAKER

ROWS = [("$25-50", 0.815, 0.925), ("$50-75", 0.936, 0.990),
        ("$75-100", 0.981, 0.990), ("$100-150", 0.978, 0.990),
        ("$150-250", 0.988, 0.990)]


def main() -> None:
    print("Taking the market price at 2-4 minutes left:\n")
    print(f"{'gap':>10}{'worth':>8}{'asks':>8}{'fee':>8}{'overpricing':>13}"
          f"{'net':>9}")
    for lbl, wr, px in ROWS:
        fee = THETA_TAKER * px * (1 - px)
        print(f"{lbl:>10}{wr:>8.1%}{px:>8.1%}{fee * 100:>7.2f}c"
              f"{(px - wr) * 100:>+12.2f}c{-(px - wr + fee) * 100:>+8.2f}c")

    print("\nWhat a win rate is worth, by price paid:\n")
    prices = (0.75, 0.85, 0.92, 0.96, 0.99)
    print(f"{'win rate':>10}" + "".join(f"{p:>11.0%}" for p in prices))
    print("-" * 66)
    for wr in (0.80, 0.86, 0.90, 0.95, 0.98, 0.99):
        print(f"{wr:>10.0%}" + "".join(
            f"{(wr - (p + THETA_TAKER * p * (1 - p))) * 100:>+10.1f}c"
            for p in prices))


if __name__ == "__main__":
    main()
