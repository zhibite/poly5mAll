from __future__ import annotations

import pytest

from src.market.service import MarketService, MarketSpec

T0 = 1_800_000_000  # divisible by 300 and 900


def _market(start: int, tag: str, interval: str = "5m") -> dict:
    return {
        "tokens": [
            {"token_id": f"up-{tag}", "outcome": "Up"},
            {"token_id": f"down-{tag}", "outcome": "Down"},
        ],
        "_event_slug": f"btc-updown-{interval}-{start}",
        "_window_start_ts": start,
        "conditionId": f"cond-{tag}",
    }


class FakePM:
    def __init__(self, markets):
        self.markets = markets
        self.calls = 0
        self.kwargs = []

    async def get_asset_updown_markets(self, asset, **kwargs):
        self.calls += 1
        self.kwargs.append(kwargs)
        return list(self.markets)


class TickPM(FakePM):
    """CLOB stub that reports a sub-cent tick (the 5m market's real grid)."""

    def __init__(self, markets, tick):
        super().__init__(markets)
        self.tick = tick
        self.tick_calls: list[str] = []

    async def get_tick_size(self, token_id, *, timeout_s=3.0, default=0.01):
        self.tick_calls.append(token_id)
        return self.tick if self.tick is not None else default


@pytest.mark.asyncio
async def test_15m_spec_uses_its_slug_interval_and_window():
    pm = FakePM(
        [
            _market(T0, "a15", "15m"),
            _market(T0 + 900, "b15", "15m"),
        ]
    )
    ms = MarketService(
        pm,
        specs=[MarketSpec("BTC-15M", "BTC", "15m", 900)],
        pre_discovery_lead_s=60,
        min_discovery_interval_s=0,
    )
    await ms.refresh("BTC-15M", T0 + 10)
    assert pm.kwargs[0]["slug_interval"] == "15m"
    assert pm.kwargs[0]["window_sec"] == 900
    cur = ms.current("BTC-15M", T0 + 10)
    nxt = ms.next("BTC-15M")
    assert cur is not None and cur.start_ts == T0
    assert cur.interval == "15m" and cur.end_ts == T0 + 900
    assert nxt is not None and nxt.start_ts == T0 + 900
    # A 15m market is invisible to the 5m market key.
    assert ms.current("BTC-5M", T0 + 10) is None


class IntervalPM(FakePM):
    """Returns the market matching the requested slug interval."""

    async def get_asset_updown_markets(self, asset, **kwargs):
        self.calls += 1
        self.kwargs.append(kwargs)
        if kwargs.get("slug_interval") == "15m":
            return [_market(T0, "a15", "15m")]
        return [_market(T0, "a5", "5m")]


@pytest.mark.asyncio
async def test_5m_and_15m_windows_are_tracked_separately():
    pm = IntervalPM([])
    ms = MarketService(
        pm,
        specs=[
            MarketSpec("BTC-5M", "BTC", "5m", 300),
            MarketSpec("BTC-15M", "BTC", "15m", 900),
        ],
        pre_discovery_lead_s=60,
        min_discovery_interval_s=0,
    )
    await ms.refresh("BTC-5M", T0 + 10)
    await ms.refresh("BTC-15M", T0 + 10)
    assert ms.current("BTC-5M", T0 + 10).window_sec == 300
    assert ms.current("BTC-15M", T0 + 10).window_sec == 900
    assert ms.poll_tokens(markets={"BTC-15M"}, now=T0 + 10) == {
        "up-a15", "down-a15",
    }
    assert ms.poll_tokens(markets={"BTC-5M"}, now=T0 + 10) == {
        "up-a5", "down-a5",
    }


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
async def test_fresh_tick_size_is_requeried():
    pm = TickPM([], tick=0.001)
    ms = MarketService(pm)
    assert await ms.tick_size("up-a") == pytest.approx(0.001)


@pytest.mark.asyncio
async def test_fresh_tick_size_failure_returns_none():
    pm = TickPM([], tick=None)
    ms = MarketService(pm)
    # None lets the exit keep its cached tick instead of falling back to
    # 0.01 (which could be coarser than the live grid).
    assert await ms.tick_size("up-a") is None


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
async def test_poll_tokens_promotes_at_boundary():
    pm = FakePM([_market(T0, "a"), _market(T0 + 300, "b")])
    ms = MarketService(pm, window_sec=300, pre_discovery_lead_s=60,
                       min_discovery_interval_s=0)
    await ms.refresh("BTC", T0 + 10)
    assert ms.poll_tokens(now=T0 + 10) == {"up-a", "down-a"}
    # No refresh between the boundary and the poll: poll_tokens itself
    # must promote the known next window and return its tokens.
    assert ms.poll_tokens(now=T0 + 301) == {"up-b", "down-b"}


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
