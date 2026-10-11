"""Tolerant extraction from API payloads.

The crypto instrument field names are documented
(docs.polymarket.us/trader-guide/crypto-schema) but their nesting is not, and
the live shapes could not be checked without API keys. Rather than hardcode a
guess at the envelope, these helpers search the payload for known field names
at any depth. If Polymarket wraps results in `data`, `results`, `markets`, or
nothing at all, this still works.
"""

from __future__ import annotations

from typing import Any, Iterator

# Field names straight from the documented crypto schema.
F_MARKET_TYPE = ("crypto_market_type",)
F_HORIZON = ("crypto_horizon",)
F_PRICE_TO_BEAT = ("price_to_beat", "priceToBeat")
F_SETTLEMENT = ("crypto_settlement_price", "cryptoSettlementPrice")
F_START = ("interval_start", "intervalStart")
F_END = ("interval_end", "intervalEnd")
F_INDEX_SYM = ("crypto_index_symbol", "cryptoIndexSymbol")
F_SUBCAT = ("event_subcategory", "eventSubcategory")
F_ID = ("market_id", "marketId", "id", "ticker", "symbol")
F_SLUG = ("slug",)


def walk(obj: Any) -> Iterator[dict]:
    """Yield every dict nested anywhere in the payload."""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v)


def pick(d: dict, names: tuple[str, ...], default=None):
    for n in names:
        if n in d and d[n] is not None:
            return d[n]
    return default


def as_float(v) -> float | None:
    try:
        f = float(v)
        return f if f == f else None   # reject NaN
    except (TypeError, ValueError):
        return None


def as_int(v) -> int | None:
    f = as_float(v)
    return int(f) if f is not None else None


def find_updown_15m(payload: Any, asset: str = "BTC") -> list[dict]:
    """Every 15-minute up/down market for `asset` found in the payload.

    Matches on the two documented discriminators rather than on slug text,
    so a naming change upstream does not silently stop the bot.
    """
    out, seen = [], set()
    for d in walk(payload):
        mt = pick(d, F_MARKET_TYPE)
        hz = pick(d, F_HORIZON)
        if mt != "updown" or hz != "15m":
            continue
        sub = pick(d, F_SUBCAT, "")
        if asset and str(sub).upper() not in (asset.upper(), ""):
            continue
        mid = pick(d, F_ID)
        if mid is None or mid in seen:
            continue
        seen.add(mid)
        out.append({
            "market_id": str(mid),
            "slug": pick(d, F_SLUG),
            "interval_start": as_int(pick(d, F_START)),
            "interval_end": as_int(pick(d, F_END)),
            "price_to_beat": as_float(pick(d, F_PRICE_TO_BEAT)),
            "settlement": as_float(pick(d, F_SETTLEMENT)),
            "index_symbol": pick(d, F_INDEX_SYM),
            "_raw": d,
        })
    return out


def parse_candles(payload: Any) -> list[tuple[int, float]]:
    """Extract (unix_second, close) pairs from asset-price candle output.

    Accepts OHLC dicts, {t,c} pairs, or bare [ts, value] arrays.
    """
    rows: list[tuple[int, float]] = []
    for d in walk(payload):
        ts = as_int(pick(d, ("t", "ts", "time", "timestamp", "start", "bucket_start")))
        val = as_float(pick(d, ("c", "close", "value", "price", "v")))
        if ts is None or val is None:
            continue
        if ts > 10_000_000_000:       # milliseconds
            ts //= 1000
        rows.append((ts, val))

    if not rows and isinstance(payload, (list, tuple)):
        for item in payload:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                ts, val = as_int(item[0]), as_float(item[1])
                if ts is not None and val is not None:
                    if ts > 10_000_000_000:
                        ts //= 1000
                    rows.append((ts, val))

    rows.sort()
    dedup: dict[int, float] = {}
    for ts, v in rows:
        dedup[ts] = v
    return sorted(dedup.items())


def parse_top_of_book(payload: Any) -> dict[str, float | None]:
    """Best bid/ask and sizes for the UP side.

    Looks for explicit bid/ask fields first, then falls back to the deepest
    bids/asks arrays it can find.
    """
    for d in walk(payload):
        bid = as_float(pick(d, ("best_bid", "bestBid", "bid", "bid_price")))
        ask = as_float(pick(d, ("best_ask", "bestAsk", "ask", "ask_price", "best_offer")))
        if bid is not None or ask is not None:
            return {
                "bid": bid, "ask": ask,
                "bid_sz": as_float(pick(d, ("bid_size", "bidSize", "best_bid_size"))),
                "ask_sz": as_float(pick(d, ("ask_size", "askSize", "best_ask_size"))),
            }

    for d in walk(payload):
        bids, asks = d.get("bids"), d.get("asks")
        if isinstance(bids, list) and isinstance(asks, list) and (bids or asks):
            def top(levels, best_high: bool):
                parsed = []
                for lv in levels:
                    if isinstance(lv, dict):
                        p = as_float(pick(lv, ("price", "p", "px")))
                        s = as_float(pick(lv, ("size", "s", "quantity", "qty")))
                    elif isinstance(lv, (list, tuple)) and len(lv) >= 2:
                        p, s = as_float(lv[0]), as_float(lv[1])
                    else:
                        continue
                    if p is not None:
                        parsed.append((p, s))
                if not parsed:
                    return None, None
                return max(parsed) if best_high else min(parsed)

            b = top(bids, True) or (None, None)
            a = top(asks, False) or (None, None)
            return {"bid": b[0], "bid_sz": b[1], "ask": a[0], "ask_sz": a[1]}

    return {"bid": None, "ask": None, "bid_sz": None, "ask_sz": None}
