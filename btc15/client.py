"""Polymarket US REST client.

Endpoint paths below come from the published documentation index. Only
`/v1/asset-prices/history` has been exercised against the live host so far
(it returns 401 without key headers, confirming host and path are right).
Anything marked INFERRED has the right documented name but an unverified
path shape. Run `python -m btc15.cli discover` once you have keys: it probes
each candidate and writes the ones that answer to data/endpoints.json, so the
guesswork is resolved by the server rather than by me.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests

from .auth import Signer

BASE = "https://api.polymarket.us"

# name -> (method, path template, verified?)
ENDPOINTS: dict[str, tuple[str, str, bool]] = {
    "asset_prices":   ("GET", "/v1/asset-prices/history", True),
    "markets":        ("GET", "/v1/markets", False),
    "market_by_id":   ("GET", "/v1/markets/{market_id}", False),
    "market_by_slug": ("GET", "/v1/markets/slug/{slug}", False),
    "market_book":    ("GET", "/v1/markets/{market_id}/book", False),
    "market_bbo":     ("GET", "/v1/markets/{market_id}/bbo", False),
    "instruments":    ("GET", "/v1/reference-data/instruments", False),
    "positions":      ("GET", "/v1/portfolio/positions", False),
    "preview_order":  ("POST", "/v1/orders/preview", False),
    "create_order":   ("POST", "/v1/orders", False),
    "cancel_order":   ("DELETE", "/v1/orders/{order_id}", False),
    "open_orders":    ("GET", "/v1/orders", False),
}


class ApiError(RuntimeError):
    def __init__(self, status: int, body: str, path: str) -> None:
        super().__init__(f"{status} on {path}: {body[:400]}")
        self.status = status
        self.body = body
        self.path = path


class PolymarketUS:
    def __init__(
        self,
        signer: Signer,
        base: str = BASE,
        timeout: float = 10.0,
        overrides: dict[str, str] | None = None,
    ) -> None:
        self.signer = signer
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.overrides = overrides or {}
        self._last_call = 0.0
        self.min_interval = 0.06  # stay under the 20 req/s public cap

    # ---- plumbing ----------------------------------------------------

    def _throttle(self) -> None:
        gap = time.monotonic() - self._last_call
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap)
        self._last_call = time.monotonic()

    def request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        retries: int = 2,
    ) -> Any:
        query = urlencode(params, safe=":") if params else ""
        url = f"{self.base}{path}" + (f"?{query}" if query else "")
        payload = json.dumps(body) if body is not None else None

        last: Exception | None = None
        for attempt in range(retries + 1):
            self._throttle()
            headers = self.signer.headers(method, path, query)
            try:
                r = self.session.request(
                    method, url, headers=headers, data=payload,
                    timeout=self.timeout,
                )
            except requests.RequestException as e:
                last = e
                time.sleep(0.4 * (attempt + 1))
                continue

            if r.status_code == 429 or 500 <= r.status_code < 600:
                last = ApiError(r.status_code, r.text, path)
                time.sleep(0.5 * (2 ** attempt))
                continue
            if not r.ok:
                raise ApiError(r.status_code, r.text, path)
            if not r.content:
                return None
            try:
                return r.json()
            except ValueError:
                return r.text

        raise last if last else RuntimeError("unreachable")

    def call(self, name: str, *, params=None, body=None, **fmt) -> Any:
        method, path, _ = ENDPOINTS[name]
        path = self.overrides.get(name, path)
        return self.request(method, path.format(**fmt), params=params, body=body)

    # ---- typed helpers -----------------------------------------------

    def index_prices(
        self, start: int, end: int | None = None, asset: str = "crypto:btc",
        bucket: str = "_1S",
    ) -> Any:
        """Per-second reference index candles. Max 3600 buckets per call.

        These are the exact values settlement is computed from, which is why
        this feed is used rather than any exchange's spot price.
        """
        span = (end or int(time.time())) - start
        if bucket == "_1S" and span > 3600:
            raise ValueError(f"{span}s exceeds the 3600 candle cap at 1s")
        params: dict[str, Any] = {"assets": asset, "bucket": bucket, "from": start}
        if end is not None:
            params["to"] = end
        return self.call("asset_prices", params=params)

    def preview_order(self, market_id: str, side: str, price: float,
                      size: int) -> Any:
        """Ask the venue what a fill would cost, fees included.

        Always prefer this over a local fee formula. The published theta
        (0.0695) is the general-markets figure and crypto appears to run
        lower, so a hardcoded constant would misstate every edge calculation.
        """
        return self.call("preview_order", body={
            "market_id": market_id, "side": side,
            "price": round(price, 4), "size": int(size),
        })


def load_overrides(path: Path) -> dict[str, str]:
    if path.exists():
        return json.loads(path.read_text()).get("overrides", {})
    return {}
