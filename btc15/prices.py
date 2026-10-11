"""What the market actually charges, measured from the live order book.

The first version of this was inferred from each finished window's published
high and low paired with the index gap at that second. Those are the two
worst moments in the window by construction, and the result was 10 to 20
cents too pessimistic. It said the market charges 99 cents where real
continuous quotes say 75 to 90, and on that basis it concluded that taking
the offer could never work. Observed real fills at 80 to 96 percent were
the evidence against it the whole time.

This reads the recorded book instead. Rebuild it as the recorder collects
more:

    PYTHONPATH=. ./.venv/bin/python -m btc15.prices
"""

from __future__ import annotations

import json
import sqlite3
import statistics as st
from pathlib import Path

CACHE = Path("data/market_prices.json")

# Fallback for a machine with no recorded quotes of its own.
#
# These are the MEDIAN ask, measured 2026-10-06 from 29,120 samples at 2-4
# minutes left across 70 hours. An earlier version of this table came from
# about 6 hours and read 0.75, 0.88 and 0.90 for the middle buckets; the
# longer sample says 0.95, 0.98 and 0.99, and its two halves agree with each
# other. The short sample was noise and anything derived from it was wrong.
DEFAULT: dict[tuple[float, float], float] = {
    (10.0, 25.0): 0.85,
    (25.0, 50.0): 0.95,
    (50.0, 75.0): 0.98,
    (75.0, 100.0): 0.99,
    (100.0, 150.0): 0.98,
    (150.0, 250.0): 0.99,
    (250.0, float("inf")): 0.99,
}

# The most worth paying in each bucket: the cheap end of that same
# distribution. Taking the median ask loses in every bucket; these clear by
# 3 to 6 cents in the larger gaps. A fresh install uses these until it has
# collected enough of its own quotes to measure them again.
DEFAULT_CEILINGS: dict[tuple[float, float], float] = {
    (10.0, 25.0): 0.73,
    (25.0, 50.0): 0.85,
    (50.0, 75.0): 0.90,
    (75.0, 100.0): 0.92,
    (100.0, 150.0): 0.94,
    (150.0, 250.0): 0.95,
    (250.0, float("inf")): 0.99,
}


def measure(db: str = "data/live.db", secs_lo: int = 120,
            secs_hi: int = 240, min_samples: int = 25,
            percentile: float = 0.50) -> dict:
    """Asking price by gap bucket, from recorded quotes.

    `percentile` picks which part of the distribution to read. The median is
    what you pay if you take whatever is on screen. A low percentile is what
    you pay if you wait for a good moment, and over 70 hours of quotes that
    distinction is the whole strategy: at the median every bucket loses,
    while in the cheapest tenth four of them pay 3 to 7 cents a contract.
    """
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """SELECT ABS(gap) g,
                  CASE WHEN gap > 0 THEN best_ask ELSE 1.0 - best_bid END px
           FROM live_book
           WHERE best_ask IS NOT NULL AND best_bid IS NOT NULL
             AND gap IS NOT NULL AND secs_left BETWEEN ? AND ?""",
        (secs_lo, secs_hi)).fetchall()
    con.close()

    out = {}
    for lo, hi in DEFAULT:
        v = sorted(r["px"] for r in rows if lo <= r["g"] < hi and r["px"])
        if len(v) >= min_samples:
            out[(lo, hi)] = round(v[int(percentile * (len(v) - 1))], 3)
    return out


def buy_limits(db: str = "data/live.db", percentile: float = 0.12) -> dict:
    """The most you should pay in each gap bucket.

    Not a forecast of what will be offered, a ceiling on what is worth
    taking. Measured at the cheap end of the distribution, so the bot sits
    out the roughly nine moments in ten where the market is asking more than
    the bet is worth.
    """
    out = dict(DEFAULT_CEILINGS)
    try:
        out.update(measure(db, percentile=percentile))
    except sqlite3.Error:
        pass
    return out


def load(db: str = "data/live.db") -> dict[tuple[float, float], float]:
    """Measured prices where there is enough data, defaults elsewhere."""
    prices = dict(DEFAULT)
    try:
        prices.update(measure(db))
    except sqlite3.Error:
        pass
    return prices


def ask_for(gap: float, prices=None) -> float | None:
    prices = prices or load()
    for (lo, hi), px in prices.items():
        if lo <= abs(gap) < hi:
            return px
    return None


if __name__ == "__main__":
    m = measure()
    print(f"{'gap':>12}{'median ask':>12}{'source':>10}")
    for (lo, hi), px in sorted(DEFAULT.items()):
        lbl = f"${lo:.0f}+" if hi > 1e8 else f"${lo:.0f}-{hi:.0f}"
        got = m.get((lo, hi))
        print(f"{lbl:>12}{(got or px):>12.3f}"
              f"{'measured' if got else 'default':>10}")
    CACHE.parent.mkdir(exist_ok=True)
    CACHE.write_text(json.dumps(
        {f"{lo}-{hi}": px for (lo, hi), px in load().items()}, indent=2))
    print(f"\nwrote {CACHE}")
