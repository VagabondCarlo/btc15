"""Public Polymarket US gateway client. No credentials required.

Every endpoint here was verified against the live host on 2026-10-02.

  GET /v1/events?slug={event_slug}
      event_slug = btc-updown-15m-YYYY-MM-DD-HHMMz   (UTC window start)
      -> event.markets[0].assetPriceTerms carries priceToBeat,
         settlementPrice, windowStart, windowEnd, indexSymbol (BRTI)
      -> market.feeCoefficient (0.0695), minimumTradeQty (0.01),
         orderPriceMinTickSize (0.01)

  GET /v1/asset-prices/history?assets=crypto:btc
      &bucket=ASSET_PRICE_BUCKET_1S&from={unix}&to={unix}
      -> per-second BRTI candles. The bucket must be the full enum name;
         the short form `_1S` from the docs returns HTTP 400.

  GET /v1/markets/{market_slug}/bbo
      -> best bid / best ask. Yes = Up.

Settlement timing, established empirically rather than from the docs:
the 60 prints that form both priceToBeat and settlementPrice are the ones
stamped [T-60, T-1], NOT [T-59, T]. Reconstructing window 2026-10-02 04:00z
with [T-60, T-1] reproduced the venue's published 85497.31 and 85450.85 to
the cent; the documented reading was off by about 35 cents. One second of
misalignment changes every number downstream, so this offset matters.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

GATEWAY = "https://gateway.polymarket.us"
WINDOW_SECS = 900
TWAP_N = 60

# The print at T-1 is the last one inside the averaging window.
TWAP_END_OFFSET = -1


def event_slug(window_start: datetime) -> str:
    return "btc-updown-15m-" + window_start.strftime("%Y-%m-%d-%H%M") + "z"


def market_slug(window_start: datetime) -> str:
    return "cpc-" + event_slug(window_start)


def window_starts(n: int, before: datetime | None = None):
    """The `n` most recent completed 15-minute window starts, newest first."""
    now = before or datetime.now(timezone.utc)
    epoch = int(now.timestamp()) // WINDOW_SECS * WINDOW_SECS
    for i in range(1, n + 1):
        yield datetime.fromtimestamp(epoch - i * WINDOW_SECS, timezone.utc)


class Gateway:
    def __init__(self, base: str = GATEWAY, timeout: float = 25.0,
                 min_interval: float = 0.05) -> None:
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.min_interval = min_interval
        self._last = 0.0

    def _get(self, path: str, retries: int = 3):
        gap = time.monotonic() - self._last
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap)
        url = f"{self.base}{path}"
        err: Exception | None = None
        for attempt in range(retries):
            try:
                req = urllib.request.Request(
                    url, headers={"Accept": "application/json",
                                  "User-Agent": "btc15/0.1"})
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    self._last = time.monotonic()
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 504):
                    err = e
                    time.sleep(0.5 * (2 ** attempt))
                    continue
                raise
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
                err = e
                time.sleep(0.4 * (attempt + 1))
        raise RuntimeError(f"GET {path} failed after {retries}: {err}")

    # ---- endpoints ---------------------------------------------------

    def event(self, slug: str) -> dict | None:
        evs = self._get(f"/v1/events?slug={slug}").get("events") or []
        return evs[0] if evs else None

    def bbo(self, mkt_slug: str) -> dict:
        return self._get(f"/v1/markets/{mkt_slug}/bbo").get("marketData", {})

    def index_candles(self, start: int, end: int) -> dict[int, float]:
        """Per-second BRTI closes, keyed by unix second. Max span 3600."""
        if end - start > 3600:
            raise ValueError(f"span {end - start}s exceeds the 3600 cap")
        payload = self._get(
            "/v1/asset-prices/history?assets=crypto:btc"
            f"&bucket=ASSET_PRICE_BUCKET_1S&from={start}&to={end}")
        series = payload.get("series") or []
        if not series:
            return {}
        return {int(c["timestamp"]): float(c["close"])
                for c in series[0].get("candles", [])}

    def index_range(self, start: int, end: int,
                    chunk: int = 3500) -> dict[int, float]:
        """Per-second BRTI over any span, fetched in legal chunks."""
        out: dict[int, float] = {}
        a = start
        while a <= end:
            b = min(a + chunk, end)
            out.update(self.index_candles(a, b))
            a = b + 1
        return out


def twap_at(prints: dict[int, float], boundary_ts: int) -> tuple[float | None, int]:
    """The venue's 60-print average ending at `boundary_ts`.

    Returns (value rounded to 2dp, number of prints found). A window missing
    prints is reported rather than silently averaged over fewer, because a
    partial average is not the number the venue settles on.
    """
    last = boundary_ts + TWAP_END_OFFSET
    vals = [prints[t] for t in range(last - TWAP_N + 1, last + 1) if t in prints]
    if not vals:
        return None, 0
    return round(sum(vals) / len(vals), 2), len(vals)


def parse_terms(event: dict) -> dict | None:
    """Flatten the bits of an event we care about."""
    mkts = event.get("markets") or []
    if not mkts:
        return None
    m = mkts[0]
    t = m.get("assetPriceTerms") or {}
    if t.get("marketType") != "ASSET_PRICE_MARKET_TYPE_UP_DOWN":
        return None

    def money(k):
        v = t.get(k)
        if isinstance(v, dict) and v.get("value") is not None:
            try:
                return float(v["value"])
            except (TypeError, ValueError):
                return None
        return None

    def iso(s):
        """Parse an ISO timestamp, returning None rather than raising.

        A malformed timestamp from the API would otherwise crash window
        setup, and the bot would restart into the same bad payload every
        time. Better to treat the window as unusable and sit it out.
        """
        if not s:
            return None
        try:
            return int(datetime.fromisoformat(
                str(s).replace("Z", "+00:00")).timestamp())
        except (ValueError, TypeError, AttributeError):
            return None

    return {
        "event_slug": event.get("slug"),
        "market_slug": m.get("slug"),
        "market_id": str(m.get("id")),
        "closed": bool(m.get("closed")),
        "status": m.get("status"),
        "window_start": iso(t.get("windowStart")),
        "window_end": iso(t.get("windowEnd")),
        "price_to_beat": money("priceToBeat"),
        "settlement": money("settlementPrice"),
        "index_symbol": t.get("indexSymbol"),
        "fee_coefficient": float(m.get("feeCoefficient") or 0.0695),
        "min_qty": float(m.get("minimumTradeQty") or 0.01),
        "tick": float(m.get("orderPriceMinTickSize") or 0.01),
        "horizon": t.get("horizon"),
    }
