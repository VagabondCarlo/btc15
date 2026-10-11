"""Phase 0: record everything, trade nothing.

Each pass stores the per-second reference index, the top of book for the live
15-minute BTC up/down market, and what the model believed at that instant.
After expiry it stores the settlement value and the outcome.

Nothing here places an order. The purpose is to build the dataset that decides
whether the TWAP lock-in effect is actually mispriced by this venue, because
that question cannot be answered by reasoning about it. At 96 cycles a day a
week of recording gives roughly 670 cycles, which is enough to measure a
one-cent edge rather than guess at it.
"""

from __future__ import annotations

import json
import logging
import time

import numpy as np

from . import shapes
from .client import ApiError, PolymarketUS
from .storage import connect, index_series, upsert_index
from .twap import forecast_settlement, realized_sigma_per_sec

log = logging.getLogger("btc15.recorder")

VOL_LOOKBACK = 900      # seconds of index history for the vol estimate
BOOK_FAR_HZ = 0.2       # polls/sec when expiry is far away
BOOK_NEAR_HZ = 2.0      # polls/sec inside the final two minutes
NEAR_WINDOW = 120


class Recorder:
    def __init__(self, api: PolymarketUS, db_path: str = "data/btc15.db",
                 mode: str = "record") -> None:
        self.api = api
        self.con = connect(db_path)
        self.mode = mode
        self._last_index_pull = 0.0
        self._markets_cached: list[dict] = []
        self._markets_at = 0.0

    # ---- index -------------------------------------------------------

    def pull_index(self, lookback: int = 300) -> int:
        """Fetch recent per-second index prints and store new ones."""
        now = int(time.time())
        try:
            payload = self.api.index_prices(start=now - lookback, end=now)
        except ApiError as e:
            log.warning("index pull failed: %s", e)
            return 0
        rows = shapes.parse_candles(payload)
        if not rows:
            log.warning("index pull returned no parseable candles")
            return 0
        n = upsert_index(self.con, rows)
        self._last_index_pull = time.time()
        return n

    def backfill_index(self, seconds: int = 3600) -> int:
        """Warm the history so the vol estimate is usable immediately."""
        now = int(time.time())
        total = 0
        start = now - seconds
        while start < now:
            chunk_end = min(start + 3500, now)
            try:
                payload = self.api.index_prices(start=start, end=chunk_end)
                total += upsert_index(self.con, shapes.parse_candles(payload))
            except ApiError as e:
                log.warning("backfill chunk failed: %s", e)
            start = chunk_end + 1
        return total

    def sigma(self) -> float:
        now = int(time.time())
        _, vals = index_series(self.con, now - VOL_LOOKBACK, now)
        return realized_sigma_per_sec(np.asarray(vals))

    # ---- markets -----------------------------------------------------

    def markets(self, max_age: float = 20.0) -> list[dict]:
        if time.time() - self._markets_at < max_age and self._markets_cached:
            return self._markets_cached
        try:
            payload = self.api.call("markets", params={"limit": 200})
        except ApiError as e:
            log.warning("market list failed: %s", e)
            return self._markets_cached
        found = shapes.find_updown_15m(payload, asset="BTC")
        if found:
            self._markets_cached = found
            self._markets_at = time.time()
            for m in found:
                self._record_cycle(m)
        return self._markets_cached

    def _record_cycle(self, m: dict) -> None:
        self.con.execute(
            """INSERT INTO cycles(market_id, slug, interval_start, interval_end,
                                  price_to_beat, index_symbol, first_seen)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(market_id) DO UPDATE SET
                 price_to_beat = COALESCE(excluded.price_to_beat, price_to_beat),
                 slug = COALESCE(excluded.slug, slug)""",
            (m["market_id"], m.get("slug"), m.get("interval_start"),
             m.get("interval_end"), m.get("price_to_beat"),
             m.get("index_symbol"), int(time.time())),
        )
        self.con.commit()

    def active(self) -> dict | None:
        """The market currently inside its 15-minute window."""
        now = time.time()
        live = [m for m in self.markets()
                if m.get("interval_start") and m.get("interval_end")
                and m["interval_start"] <= now < m["interval_end"]]
        return min(live, key=lambda m: m["interval_end"]) if live else None

    # ---- sampling ----------------------------------------------------

    def sample(self, market: dict) -> None:
        now_ms = int(time.time() * 1000)
        end = market["interval_end"]
        secs_left = end - time.time()

        try:
            book_raw = self.api.call("market_book", market_id=market["market_id"])
        except ApiError as e:
            log.warning("book fetch failed: %s", e)
            return
        tob = shapes.parse_top_of_book(book_raw)

        ts, vals = index_series(self.con, int(end) - 59 - 5, int(time.time()))
        index_last = vals[-1] if vals else None
        window = [v for t, v in zip(ts, vals) if t >= end - 59]

        sd = sig = model_p = locked = None
        strike = market.get("price_to_beat")
        if index_last is not None:
            sig = self.sigma()
            if sig == sig:  # not NaN
                f = forecast_settlement(np.asarray(window), secs_left,
                                        index_last, sig)
                sd, locked = f.sd, f.locked_fraction
                if strike:
                    model_p = f.prob_up(strike)

        self.con.execute(
            """INSERT INTO book_snaps(ts_ms, market_id, secs_left, up_bid,
                   up_bid_sz, up_ask, up_ask_sz, index_last, raw)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (now_ms, market["market_id"], secs_left, tob["bid"], tob["bid_sz"],
             tob["ask"], tob["ask_sz"], index_last,
             json.dumps(book_raw)[:20000]),
        )

        if model_p is not None:
            bid, ask = tob["bid"], tob["ask"]
            edge_up = (model_p - ask) if ask else None
            edge_dn = ((1.0 - model_p) - (1.0 - bid)) if bid else None
            self.con.execute(
                """INSERT INTO decisions(ts_ms, market_id, secs_left, model_p,
                       model_sd, locked_frac, mkt_bid, mkt_ask, edge_up,
                       edge_down, action, reason, size, mode)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (now_ms, market["market_id"], secs_left, model_p, sd, locked,
                 bid, ask, edge_up, edge_dn, "OBSERVE",
                 "record mode: no orders", 0, self.mode),
            )
        self.con.commit()

    def resolve_finished(self) -> int:
        """Fill in settlement and outcome for cycles past expiry."""
        now = int(time.time())
        pending = self.con.execute(
            """SELECT market_id FROM cycles
               WHERE outcome IS NULL AND interval_end < ? - 20""",
            (now,),
        ).fetchall()
        done = 0
        for row in pending:
            try:
                payload = self.api.call("market_by_id",
                                        market_id=row["market_id"])
            except ApiError:
                continue
            found = shapes.find_updown_15m(payload, asset="BTC")
            if not found:
                continue
            m = found[0]
            settle, ptb = m.get("settlement"), m.get("price_to_beat")
            if settle is None or ptb is None:
                continue
            outcome = "UP" if settle >= ptb else "DOWN"   # ties resolve UP
            self.con.execute(
                """UPDATE cycles SET settlement=?, price_to_beat=?, outcome=?,
                       resolved_at=? WHERE market_id=?""",
                (settle, ptb, outcome, now, row["market_id"]),
            )
            done += 1
        self.con.commit()
        return done

    # ---- loop --------------------------------------------------------

    def run(self, duration: float | None = None) -> None:
        started = time.time()
        log.info("recorder start, mode=%s", self.mode)
        self.backfill_index(VOL_LOOKBACK)
        next_resolve = 0.0

        while duration is None or time.time() - started < duration:
            loop_t = time.time()
            if loop_t - self._last_index_pull > 3.0:
                self.pull_index(lookback=120)

            mkt = self.active()
            if mkt:
                self.sample(mkt)
                secs_left = mkt["interval_end"] - time.time()
                hz = BOOK_NEAR_HZ if secs_left <= NEAR_WINDOW else BOOK_FAR_HZ
            else:
                hz = BOOK_FAR_HZ

            if loop_t > next_resolve:
                self.resolve_finished()
                next_resolve = loop_t + 60.0

            time.sleep(max(0.05, (1.0 / hz) - (time.time() - loop_t)))
