from __future__ import annotations

import time

from src.market.market_ws import PriceCache, MarketWSManager


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


def test_rest_book_source_is_visible():
    cache = PriceCache()
    cache.set_book(
        "tok",
        [{"price": "0.20", "size": "100"}],
        [{"price": "0.30", "size": "100"}],
        source="rest_book",
    )
    quote = cache.quote("tok", now=time.time())
    assert quote is not None
    assert quote.source == "rest_book"
    assert quote.bid == 0.20 and quote.ask == 0.30


def test_ws_manager_shards_tokens():
    manager = MarketWSManager(PriceCache(), shards=7)
    tokens = {f"token-{i}" for i in range(70)}
    manager.set_active_tokens(tokens)
    assert manager.subscribed_tokens == frozenset(tokens)
    assert len(manager.shards) == 7
    assert sum(manager.shard_sizes) == 70
    assert all(size > 0 for size in manager.shard_sizes)
