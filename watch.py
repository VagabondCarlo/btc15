#!/usr/bin/env python3
"""Live settlement monitor. Shows the number that decides the contract.

The app shows the live index against the strike. That is not what settles.
Settlement is the average of the 60 index prints ending at expiry, so with
30 seconds left half of the result is already fixed and unchangeable, and
the live price can sit above the strike while the average sits below it.

That is not a hypothetical. On 2026-10-02 23:30z the strike was 84,545.77,
the screen read 84,549.15 in green, and the contract settled DOWN at
84,543.92 because the index had dipped to 84,527.08 earlier in that same
final minute. Anyone watching the live number saw a win and got a loss.

This prints the running average instead, with how much of it is locked.
Run it beside the app while trading by hand.

    PYTHONPATH=. ./.venv/bin/python watch.py
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from datetime import datetime, timezone

import numpy as np

from btc15.gateway import (Gateway, WINDOW_SECS, event_slug, parse_terms,
                           twap_at)
from btc15.twap import blended_sigma, forecast_settlement

RUN = True
C = {"g": "\033[32m", "r": "\033[31m", "y": "\033[33m", "b": "\033[1m",
     "d": "\033[2m", "x": "\033[0m", "inv": "\033[7m"}


def stop(*_):
    global RUN
    RUN = False


def bar(frac: float, width: int = 20) -> str:
    n = int(round(frac * width))
    return "#" * n + "." * (width - n)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plain", action="store_true", help="no colour")
    a = ap.parse_args()
    if a.plain:
        for k in C:
            C[k] = ""

    signal.signal(signal.SIGINT, stop)
    gw = Gateway()
    ws = terms = None
    prints: dict[int, float] = {}
    sigma = float("nan")

    while RUN:
        now = int(time.time())
        cur = now // WINDOW_SECS * WINDOW_SECS
        if cur != ws:
            ws = cur
            d = datetime.fromtimestamp(ws, timezone.utc)
            try:
                terms = parse_terms(gw.event(event_slug(d)) or {})
                prints = gw.index_range(ws - 3500, now)
                v = np.array([prints[t] for t in sorted(prints)], float)
                sigma = blended_sigma(v[-600:], v)
            except Exception:
                terms = None
            print(f"\n{C['b']}=== {d:%H:%M}z window ==={C['x']}")

        if not terms or terms.get("price_to_beat") is None:
            try:
                terms = parse_terms(gw.event(event_slug(
                    datetime.fromtimestamp(ws, timezone.utc))) or {})
            except Exception:
                pass
            time.sleep(2)
            continue

        we = ws + WINDOW_SECS
        left = we - time.time()
        try:
            prints.update(gw.index_candles(now - 10, now))
        except Exception:
            time.sleep(1)
            continue

        strike = terms["price_to_beat"]
        live = prints.get(max(prints))
        if live is None:
            time.sleep(1)
            continue

        # The running settlement average: prints already inside the final
        # minute, with the rest still to come.
        window_prints = [prints[t] for t in range(we - 60, now + 1)
                         if t in prints]
        locked = len(window_prints) / 60.0
        running = (sum(window_prints) / len(window_prints)
                   if window_prints else live)
        f = forecast_settlement(np.asarray(window_prints), left, live,
                                sigma if sigma == sigma else 1.0)
        p_up = f.prob_up(strike)

        live_d = live - strike
        set_d = f.mean - strike
        agree = (live_d >= 0) == (set_d >= 0)

        col = C["g"] if p_up >= 0.80 else (C["r"] if p_up <= 0.20 else C["y"])
        side = "UP" if p_up >= 0.5 else "DOWN"
        conf = max(p_up, 1 - p_up)

        warn = ""
        if not agree and locked > 0.1:
            warn = (f"  {C['inv']}{C['r']} SCREEN DISAGREES WITH SETTLEMENT "
                    f"{C['x']}")

        sys.stdout.write(
            f"\r{C['d']}{left:5.0f}s{C['x']} "
            f"live {live:>10,.2f} {live_d:>+7.2f} | "
            f"{C['b']}settles {f.mean:>10,.2f} {set_d:>+7.2f}{C['x']} "
            f"[{bar(locked)}] {locked:>3.0%} locked | "
            f"{col}{side:<4} {conf:>5.1%}{C['x']}{warn}   ")
        sys.stdout.flush()
        time.sleep(1.0)

    print("\nstopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
