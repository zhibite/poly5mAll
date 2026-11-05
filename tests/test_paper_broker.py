from __future__ import annotations

import pytest

from src.execution.broker import PaperBroker, walk_budget, walk_shares
from src.market.market_ws import PriceCache


class FakePM:
    def __init__(self, book):
        self.book = book

    async def get_order_book(self, token_id):
        return self.book


def test_walk_budget_respects_limit():
    # 10 U at the 0.10 level buys all 100 shares without reaching 0.14.
    asks = [(0.10, 100.0), (0.14, 100.0), (0.16, 100.0)]
    walk = walk_budget(asks, 10.0, 0.15)
    assert walk.shares == pytest.approx(100.0)
    assert walk.notional == pytest.approx(10.0)
    assert walk.avg_price == pytest.approx(0.10)

    # A larger budget walks the ladder but stops at the 0.15 limit.
    walk = walk_budget(asks, 25.0, 0.15)
    assert walk.notional == pytest.approx(24.0)
    assert walk.avg_price < 0.15


def test_walk_shares_respects_floor():
    bids = [(0.20, 10.0), (0.17, 10.0), (0.15, 10.0)]
    walk = walk_shares(bids, 25.0, 0.16)
    assert walk.shares == pytest.approx(20.0)
    assert walk.notional == pytest.approx(10 * 0.20 + 10 * 0.17)


@pytest.mark.asyncio
async def test_paper_buy_then_sell():
    pm = FakePM(
        {
            "asks": [{"price": "0.14", "size": "100"}],
            "bids": [{"price": "0.17", "size": "100"}],
        }
    )
    cache = PriceCache()
    broker = PaperBroker(pm, cache, fee_rate=0.07, include_fees=True)
    buy = await broker.buy("tok", 10.0, 0.15, 0.01)
    assert buy is not None
    assert buy.price == pytest.approx(0.14)
    assert buy.shares == pytest.approx(71.428571, rel=1e-5)
    assert buy.fee > 0
    assert await broker.token_balance("tok") == pytest.approx(buy.shares)

    sell = await broker.sell("tok", buy.shares, 0.16, 0.01)
    assert sell is not None
    assert sell.price == pytest.approx(0.17)
    assert sell.usdc == pytest.approx(buy.shares * 0.17)
    assert await broker.token_balance("tok") == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_paper_buy_rejects_ask_above_limit():
    pm = FakePM({"asks": [{"price": "0.16", "size": "100"}], "bids": []})
    broker = PaperBroker(pm, PriceCache())
    assert await broker.buy("tok", 10.0, 0.15, 0.01) is None


@pytest.mark.asyncio
async def test_paper_falls_back_to_ws_top_of_book_when_filter_off():
    pm = FakePM(None)
    cache = PriceCache()
    cache.set_best_bid_ask("tok", 0.13, 0.14)
    broker = PaperBroker(
        pm, cache, require_two_sided_book=False, min_entry_bid_depth_usd=0
    )
    buy = await broker.buy("tok", 10.0, 0.15, 0.01)
    assert buy is not None
    assert buy.price == pytest.approx(0.14)


@pytest.mark.asyncio
async def test_paper_rejects_no_bids():
    pm = FakePM({"asks": [{"price": "0.14", "size": "100"}], "bids": []})
    broker = PaperBroker(pm, PriceCache())
    assert await broker.buy("tok", 10.0, 0.15, 0.01) is None


@pytest.mark.asyncio
async def test_paper_rejects_thin_bids():
    pm = FakePM(
        {
            "asks": [{"price": "0.14", "size": "100"}],
            "bids": [{"price": "0.13", "size": "1"}],
        }
    )
    broker = PaperBroker(pm, PriceCache(), min_entry_bid_depth_usd=5.0)
    assert await broker.buy("tok", 10.0, 0.15, 0.01) is None


@pytest.mark.asyncio
async def test_paper_rejects_missing_book_when_filter_on():
    broker = PaperBroker(FakePM(None), PriceCache())
    assert await broker.buy("tok", 10.0, 0.15, 0.01) is None
