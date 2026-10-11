#!/usr/bin/env python3
"""Place exactly one minimum-size order, by hand, and show everything.

The bot's live order path has never run against the venue. Before trusting
it with an account, send one order the smallest size allows, read the raw
request and response, and confirm in the app that what appeared there is
what you intended. Field names in the docs are sometimes wrong, and the way
you find out is this, not a running bot.

  ./.venv/bin/python first_order.py --dry      build it, send nothing
  ./.venv/bin/python first_order.py            send it, after confirming
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone

from btc15.auth import Credentials, Signer
from btc15.client import ApiError, PolymarketUS
from btc15.gateway import Gateway, WINDOW_SECS, event_slug, parse_terms


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="print, do not send")
    ap.add_argument("--side", choices=["UP", "DOWN"], default="UP")
    ap.add_argument("--price", type=float, default=0.10,
                    help="deliberately far from the market so it rests "
                         "unfilled and you can cancel it")
    ap.add_argument("--size", type=float, default=0.01)
    ap.add_argument("--tif", default="TIME_IN_FORCE_GOOD_TILL_CANCEL",
                    help="IMMEDIATE_OR_CANCEL never rests, so the maker "
                         "strategy needs a resting value. Try alternatives "
                         "here until one is accepted.")
    a = ap.parse_args()

    gw = Gateway()
    ws = int(time.time()) // WINDOW_SECS * WINDOW_SECS
    d = datetime.fromtimestamp(ws, timezone.utc)
    ev = gw.event(event_slug(d))
    terms = parse_terms(ev) if ev else None
    if not terms:
        print("no live window right now, try again in a minute")
        return 1

    print(f"market : {terms['market_slug']}")
    print(f"minimum: {terms['min_qty']} contracts, tick {terms['tick']}")
    try:
        md = gw.bbo(terms["market_slug"])
        bb = (md.get("bestBid") or {}).get("value")
        ba = (md.get("bestAsk") or {}).get("value")
        print(f"book   : bid {bb}  ask {ba}")
    except Exception:
        pass

    px = a.price if a.side == "UP" else round(1.0 - a.price, 2)
    body = {
        "marketSlug": terms["market_slug"],
        "intent": "ORDER_INTENT_BUY_LONG" if a.side == "UP"
                  else "ORDER_INTENT_BUY_SHORT",
        "timeInForce": a.tif,
        "price": {"value": f"{px:.2f}", "currency": "USD"},
        "quantity": f"{a.size:.2f}",
        "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
        "synchronousExecution": False,
    }
    print(f"\nPOST /v1/orders\n{json.dumps(body, indent=2)}")
    print(f"\nrisking at most ${a.price * a.size:.4f}")

    if a.dry:
        print("\n--dry, nothing sent.")
        return 0

    print("\nThis sends a REAL order to your account.")
    if input("Type SEND to continue: ").strip() != "SEND":
        print("aborted")
        return 1

    api = PolymarketUS(Signer(Credentials.from_env()))
    try:
        r = api.request("POST", "/v1/orders", body=body, retries=0)
    except ApiError as e:
        print(f"\nREJECTED {e.status}\n{e.body[:800]}\n")
        print("Common causes: a field name differs from the docs, the price "
              "is off-tick,\nthe size is below the minimum, or the account "
              "has no buying power.\nFix the body above and rerun.")
        return 1

    print(f"\nACCEPTED\n{json.dumps(r, indent=2)[:1500]}")
    print("\nNow check three things in the Polymarket US app:")
    print("  1. the order shows the side you intended")
    print("  2. the price matches")
    print("  3. the size matches")
    print("\nIf the side is inverted, the UP/DOWN intent mapping is wrong and")
    print("must be fixed in btc15_bot.py before running the bot. Cancel the")
    print("order in the app when you are done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
