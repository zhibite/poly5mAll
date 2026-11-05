from __future__ import annotations

import time

import pytest

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
        [{"price": "0.24", "size": "100"}],
        [{"price": "0.26", "size": "100"}],
        source="rest_book",
    )
    quote = cache.quote("tok", now=time.time())
    assert quote is not None
    assert quote.source == "rest_book"
    assert quote.bid == 0.24 and quote.ask == 0.26


def test_wide_bba_uses_rest_midpoint():
    # 0.05/0.95 has a meaningless BBO midpoint of 0.5; the official
    # midpoint (0.23) must drive the strategy reference while the wide
    # bid/ask remain visible to the entry spread filter.
    cache = PriceCache()
    now = 1_000_000.0
    cache.set_midpoint("tok", 0.23, ts=now - 1)
    cache.set_best_bid_ask("tok", 0.05, 0.95, ts=now - 0.5)
    quote = cache.quote("tok", now=now)
    assert quote is not None
    assert quote.source == "rest_mid"
    assert quote.mid == 0.23
    assert quote.bid == 0.05 and quote.ask == 0.95
    assert quote.spread == pytest.approx(0.90)


def test_wide_bba_without_midpoint_returns_none():
    # Do not fall back to a meaningless 0.5 reference when no official
    # midpoint/last trade exists.
    cache = PriceCache()
    now = 1_000_000.0
    cache.set_best_bid_ask("tok", 0.05, 0.95, ts=now - 0.5)
    assert cache.quote("tok", now=now) is None


def test_ws_manager_shards_tokens():
    manager = MarketWSManager(PriceCache(), shards=7)
    tokens = {f"token-{i}" for i in range(70)}
    manager.set_active_tokens(tokens)
    assert manager.subscribed_tokens == frozenset(tokens)
    assert len(manager.shards) == 7
    assert sum(manager.shard_sizes) == 70
    assert all(size > 0 for size in manager.shard_sizes)
