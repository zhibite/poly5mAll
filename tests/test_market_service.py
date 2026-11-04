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
