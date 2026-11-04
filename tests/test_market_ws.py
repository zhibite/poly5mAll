from __future__ import annotations

from src.market.market_ws import PriceCache


def test_quote_prefers_fresh_bba():
    cache = PriceCache()
    now = 1_000_000.0
    cache.set_midpoint("tok", 0.40, ts=now - 1)
    cache.set_best_bid_ask("tok", 0.44, 0.46, ts=now - 0.5)
    quote = cache.quote("tok", now=now)
    assert quote is not None
    assert quote.source == "ws_bba"
    assert quote.mid == 0.45
    assert quote.bid == 0.44 and quote.ask == 0.46


def test_quote_falls_back_to_rest_mid_when_bba_stale():
    cache = PriceCache()
    now = 1_000_000.0
    cache.set_best_bid_ask("tok", 0.44, 0.46, ts=now - 10)
    cache.set_midpoint("tok", 0.40, ts=now - 1)
    quote = cache.quote("tok", now=now)
    assert quote is not None
    assert quote.source == "rest_mid"
    assert quote.mid == 0.40
    assert quote.bid == 0.0 and quote.ask == 0.0


def test_quote_none_when_everything_stale():
    cache = PriceCache()
    now = 1_000_000.0
    cache.set_midpoint("tok", 0.40, ts=now - 60)
    assert cache.quote("tok", now=now, max_age=15.0) is None
