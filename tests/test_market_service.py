from __future__ import annotations

import pytest

from src.market.service import MarketService

T0 = 1_800_000_000


def _market(start: int, tag: str) -> dict:
    return {
        "tokens": [
            {"token_id": f"up-{tag}", "outcome": "Up"},
            {"token_id": f"down-{tag}", "outcome": "Down"},
        ],
        "_event_slug": f"btc-updown-5m-{start}",
        "_window_start_ts": start,
        "conditionId": f"cond-{tag}",
    }


class FakePM:
    def __init__(self, markets):
        self.markets = markets
        self.calls = 0

    async def get_asset_updown_markets(self, asset, **kwargs):
        self.calls += 1
        return list(self.markets)


class TickPM(FakePM):
    """CLOB stub that reports a sub-cent tick (the 5m market's real grid)."""

    def __init__(self, markets, tick):
        super().__init__(markets)
        self.tick = tick
        self.tick_calls: list[str] = []

    async def get_tick_size(self, token_id):
        self.tick_calls.append(token_id)
        return self.tick


@pytest.mark.asyncio
async def test_current_and_next_discovered_together():
    pm = FakePM([_market(T0, "a"), _market(T0 + 300, "b")])
    ms = MarketService(pm, window_sec=300, pre_discovery_lead_s=60,
                       min_discovery_interval_s=0)
    await ms.refresh("BTC", T0 + 10)
    cur = ms.current("BTC", T0 + 10)
    nxt = ms.next("BTC")
    assert cur is not None and cur.start_ts == T0
    assert nxt is not None and nxt.start_ts == T0 + 300
    assert ms.active_tokens() == {"up-a", "down-a", "up-b", "down-b"}
    # No get_tick_size on the client -> keep the old 0.01 grid.
    assert cur.tick_size == pytest.approx(0.01)


@pytest.mark.asyncio
async def test_tick_size_comes_from_clob():
    pm = TickPM([_market(T0, "a"), _market(T0 + 300, "b")], tick=0.001)
    ms = MarketService(pm, window_sec=300, pre_discovery_lead_s=60,
                       min_discovery_interval_s=0)
    await ms.refresh("BTC", T0 + 10)
    assert ms.current("BTC", T0 + 10).tick_size == pytest.approx(0.001)
    assert ms.next("BTC").tick_size == pytest.approx(0.001)
    # One request per market, using the Up token.
    assert pm.tick_calls == ["up-a", "up-b"]


@pytest.mark.asyncio
async def test_current_promotes_at_boundary():
    pm = FakePM([_market(T0, "a"), _market(T0 + 300, "b")])
    ms = MarketService(pm, window_sec=300, pre_discovery_lead_s=60,
                       min_discovery_interval_s=0)
    await ms.refresh("BTC", T0 + 10)
    cur = ms.current("BTC", T0 + 301)
    assert cur is not None and cur.start_ts == T0 + 300
    assert cur.up_token == "up-b"


@pytest.mark.asyncio
async def test_pre_discovery_of_next_next_window():
    pm = FakePM(
        [
            _market(T0, "a"),
            _market(T0 + 300, "b"),
            _market(T0 + 600, "c"),
        ]
    )
    ms = MarketService(pm, window_sec=300, pre_discovery_lead_s=60,
                       min_discovery_interval_s=0)
    await ms.refresh("BTC", T0 + 10)
    # Before the last minute of the T0+300 window, the next-next window is
    # not fetched (the next window is already known).
    await ms.refresh("BTC", T0 + 320)
    assert ms.next("BTC").start_ts == T0 + 300
    # Inside the 60 s lead, discovery picks up T0+600 as the new next.
    await ms.refresh("BTC", T0 + 550)
    assert ms.next("BTC").start_ts == T0 + 600
