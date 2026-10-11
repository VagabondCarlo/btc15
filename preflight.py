#!/usr/bin/env python3
"""Check a live account works before letting the bot near it. Places no orders.

Everything in btc15_bot.py that reads public data has been exercised against
the real venue. The authenticated half has not, because no API keys existed
while it was written. The request shapes come from the documentation, and
documentation is often wrong in small ways that matter.

So this probes each authenticated endpoint read-only, reports which paths
answer and what fields they return, and writes what it learns to
data/endpoints.json so the bot uses the real paths rather than the guessed
ones. Run it the moment you have keys, and again any time orders start
failing.

  PM_KEY_ID=... PM_SECRET=... ./.venv/bin/python preflight.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from btc15.auth import Credentials, Signer
from btc15.client import ApiError, PolymarketUS

# Each entry: purpose -> candidate paths, in order of likelihood.
CANDIDATES: dict[str, list[str]] = {
    "positions": ["/v1/portfolio/positions", "/v1/positions",
                  "/v1/account/positions"],
    "balance": ["/v1/account/balances", "/v1/portfolio/balances",
                "/v1/balances", "/v1/account/balance"],
    "open_orders": ["/v1/orders", "/v1/orders/open", "/v1/order"],
    "fills": ["/v1/fills", "/v1/trades", "/v1/report/trades"],
}

MONEYISH = ("buyingPower", "availableBalance", "available", "cash",
            "balance", "equity", "withdrawable", "withdrawableBalance",
            "cashBalance", "promoBalance", "incentiveBalance",
            "pendingDeposits", "unclearedBalance", "value")

# Fields that are NOT your money, or not yet your money. The balance on
# screen is not the number you can take out.
NOT_YOURS = ("promoBalance", "incentiveBalance", "promotionalCredit",
             "bonusBalance")
NOT_YET = ("pendingDeposits", "unclearedBalance", "fundsInFlight")


def walk(o, depth=0):
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from walk(v, depth + 1)
    elif isinstance(o, list):
        for v in o[:5]:
            yield from walk(v, depth + 1)


def find_money(payload):
    out = {}
    for d in walk(payload):
        for k in MONEYISH:
            if k in d:
                v = d[k]
                if isinstance(v, dict) and "value" in v:
                    v = v["value"]
                try:
                    out[k] = float(v)
                except (TypeError, ValueError):
                    pass
    return out


def main() -> int:
    try:
        creds = Credentials.from_env()
    except RuntimeError as e:
        print(f"{e}\n")
        print("Get keys at polymarket.us/developer after finishing ID checks")
        print("in the app, then put them in keys.env (copy keys.env.example).")
        return 1

    try:
        signer = Signer(creds)
    except Exception as e:
        print(f"Could not load the secret key: {e}")
        print("PM_SECRET should be the base64 string the site gave you, "
              "unquoted and on one line.")
        return 1

    api = PolymarketUS(signer)
    print(f"key id {creds.key_id[:8]}... signing OK\n")

    found: dict[str, str] = {}
    balance = None

    for purpose, paths in CANDIDATES.items():
        print(f"{purpose}:")
        for path in paths:
            try:
                r = api.request("GET", path, retries=0)
            except ApiError as e:
                flag = "auth rejected" if e.status in (401, 403) else f"HTTP {e.status}"
                print(f"   {path:<34} {flag}")
                if e.status in (401, 403):
                    print("\n   Signing was rejected. Either the key is wrong, "
                          "or this machine's\n   clock is off by more than 30 "
                          "seconds. Check with: date -u")
                    return 1
                continue
            except Exception as e:
                print(f"   {path:<34} {type(e).__name__}")
                continue
            print(f"   {path:<34} OK")
            found[purpose] = path
            money = find_money(r)
            if money:
                print(f"      money fields: {money}")
                if purpose == "balance":
                    balance = max(money.values())
                    promo = sum(v for k, v in money.items() if k in NOT_YOURS)
                    held = sum(v for k, v in money.items() if k in NOT_YET)
                    if promo:
                        print(f"      ${promo:.2f} of that is promotional "
                              f"credit, which can never be withdrawn")
                    if held:
                        print(f"      ${held:.2f} is a deposit still "
                              f"clearing; it cannot be withdrawn for 3-4 "
                              f"business days")
                    withdrawable = money.get("withdrawable") or \
                        money.get("withdrawableBalance")
                    if withdrawable is not None:
                        print(f"      WITHDRAWABLE RIGHT NOW: "
                              f"${withdrawable:.2f}")
            keys = sorted({k for d in walk(r) for k in d})[:12]
            if keys:
                print(f"      fields seen: {', '.join(keys)}")
            break
        else:
            print(f"   none of {len(paths)} candidates worked")
        print()

    out = Path("data/endpoints.json")
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"verified": found}, indent=2))
    print(f"wrote {out}\n")

    ok = {"positions", "balance"} <= set(found)
    print("READY FOR LIVE" if ok else "NOT READY")
    if balance is not None:
        print(f"  account balance reads as ${balance:,.2f}")
    if not ok:
        missing = {"positions", "balance"} - set(found)
        print(f"  could not find: {', '.join(sorted(missing))}")
        print("  add the correct path to CANDIDATES in this file and rerun")
        return 1

    print("\nBefore trading anything, prove you can get money OUT.")
    print("Deposit, wait for it to clear, withdraw a small amount, and")
    print("watch it arrive. Three rules stack against a first withdrawal:")
    print("  - a debit card deposit takes 3-4 business days to clear")
    print("  - promotional credit can never be withdrawn, even after it")
    print("    settles; only profit made on top of it can")
    print("  - ACH has a $50 minimum, though debit card does not")
    print("A balance you cannot withdraw is a score, not money.")
    print("\nThen place ONE order by hand and check it in the app:")
    print("  ./.venv/bin/python first_order.py --dry")
    return 0


if __name__ == "__main__":
    sys.exit(main())
