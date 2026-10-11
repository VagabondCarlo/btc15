"""The venue's payloads are the bot's only input. None may crash it."""
import pytest

from btc15.gateway import parse_terms, twap_at
from btc15.shapes import find_updown_15m, parse_candles, parse_top_of_book

HOSTILE = [None, {}, [], "", "not json", 0, {"markets": None},
           {"events": [{"markets": [{}]}]},
           {"markets": [{"assetPriceTerms": {
               "marketType": "ASSET_PRICE_MARKET_TYPE_UP_DOWN",
               "priceToBeat": {"value": "not-a-number"},
               "windowStart": "garbage"}}]},
           {"series": [{"candles": [{"timestamp": "x", "close": None}]}]},
           {"bids": [{"px": {"value": "NaN"}}], "offers": [[None, None]]},
           {"a": {"b": {"c": [{"d": float("inf")}]}}}]


@pytest.mark.parametrize("payload", HOSTILE)
def test_parsers_never_raise(payload):
    find_updown_15m(payload)
    parse_candles(payload)
    parse_top_of_book(payload)
    if isinstance(payload, dict):
        parse_terms(payload)


def test_malformed_timestamp_yields_none_not_a_crash():
    t = parse_terms({"markets": [{"assetPriceTerms": {
        "marketType": "ASSET_PRICE_MARKET_TYPE_UP_DOWN",
        "windowStart": "garbage", "windowEnd": 12}}]})
    assert t is not None and t["window_start"] is None


def test_twap_reports_how_many_prints_it_had():
    """A partial average is not the number the venue settles on, so the
    caller must be able to tell and refuse to trade."""
    assert twap_at({}, 1790000000) == (None, 0)
    partial = {t: 86000.0 for t in range(1790000000 - 30, 1790000000)}
    value, n = twap_at(partial, 1790000000)
    assert n == 30 and value == pytest.approx(86000.0)
