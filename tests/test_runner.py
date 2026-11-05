from __future__ import annotations

import dataclasses

import pytest

from src.config import AssetConfig, load_settings
from src.market.market_ws import PriceCache
from src.models import Fill, MarketWindow, Position, Side
from src.runner import AssetRunner, MultiAssetRunner
from src.runtime_config import RuntimeConfig
from src.store import StateStore

T0 = 1_800_000_000


def _window() -> MarketWindow:
    return MarketWindow(
        asset="BTC",
        slug=f"btc-updown-5m-{T0}",
        start_ts=T0,
        end_ts=T0 + 300,
        up_token="up-token",
        down_token="down-token",
    )


class FakeMarketService:
    def __init__(self, window):
        self.window = window

    def current(self, asset, now):
        return self.window if self.window.contains(now) else None

    def active_tokens(self, extra=()):
        return set()

    def stats(self):
        return {}


class FakeBroker:
    mode = "paper"

    def __init__(self):
        self.buys = []
        self.sells = []

    async def buy(self, token_id, usd_amount, limit_price, tick_size):
        self.buys.append((token_id, usd_amount, limit_price))
        shares = usd_amount / 0.24
        return Fill(
            token_id, "BUY", shares, usd_amount, 0.24, 0.08, T0 + 10
        )

    async def sell(self, token_id, shares, limit_price, tick_size):
        self.sells.append((token_id, shares, limit_price))
        price = 0.30 if limit_price < 0.30 else limit_price
        return Fill(
            token_id, "SELL", shares, shares * price, price, 0.05, T0 + 70
        )

    async def token_balance(self, token_id):
        return None


def _settings():
    s = load_settings()
    return dataclasses.replace(
        s,
        assets=(
            AssetConfig(
                asset="BTC",
                enabled=True,
                sides=(Side.DOWN,),
                entry_thresh=0.25,
                tp_pct=0.10,
            ),
        ),
        execution=dataclasses.replace(
            s.execution, entry_retry_s=0.001, exit_retry_s=0.001
        ),
    )


@pytest.mark.asyncio
async def test_entry_then_take_profit(tmp_path):
    settings = _settings()
    cache = PriceCache()
    cache.set_best_bid_ask("down-token", 0.23, 0.24, ts=T0 + 10)
    broker = FakeBroker()
    trades = []
    runner = AssetRunner(
        settings.assets[0],
        settings,
        broker,
        cache,
        FakeMarketService(_window()),
        StateStore(tmp_path),
        can_enter=lambda side: (True, "ok"),
        on_trade=trades.append,
        on_state_change=lambda: None,
    )

    await runner.tick(T0 + 10)
    assert len(broker.buys) == 1
    assert runner.sides[0].position is not None

    # Price is above the 0.264 target at t=70s.
    cache.set_best_bid_ask("down-token", 0.30, 0.31, ts=T0 + 70)
    await runner.tick(T0 + 70)
    assert len(broker.sells) == 1
    assert len(trades) == 1
    assert trades[0].reason == "tp"
    assert trades[0].pnl_net > 0


class RaisingBroker(FakeBroker):
    async def buy(self, token_id, usd_amount, limit_price, tick_size):
        raise RuntimeError("network down")


@pytest.mark.asyncio
async def test_broker_exception_resets_entry_state(tmp_path):
    settings = _settings()
    cache = PriceCache()
    cache.set_best_bid_ask("down-token", 0.23, 0.24, ts=T0 + 10)
    runner = AssetRunner(
        settings.assets[0],
        settings,
        RaisingBroker(),
        cache,
        FakeMarketService(_window()),
        StateStore(tmp_path),
        can_enter=lambda side: (True, "ok"),
        on_trade=lambda r: None,
        on_state_change=lambda: None,
    )
    await runner.tick(T0 + 10)
    # A failed broker call must not leave the side stuck in ENTERING.
    assert runner.sides[0].status.value == "waiting"


class GapBroker(FakeBroker):
    """Sell fill at the observed gapped bid (0.42), not at the target."""

    async def sell(self, token_id, shares, limit_price, tick_size):
        self.sells.append((token_id, shares, limit_price))
        price = 0.42
        return Fill(
            token_id, "SELL", shares, shares * price, price, 0.05, T0 + 70
        )


@pytest.mark.asyncio
async def test_take_profit_fills_at_observed_gap(tmp_path):
    settings = _settings()
    cache = PriceCache()
    cache.set_best_bid_ask("down-token", 0.23, 0.24, ts=T0 + 10)
    broker = GapBroker()
    trades = []
    runner = AssetRunner(
        settings.assets[0],
        settings,
        broker,
        cache,
        FakeMarketService(_window()),
        StateStore(tmp_path),
        can_enter=lambda side: (True, "ok"),
        on_trade=trades.append,
        on_state_change=lambda: None,
    )
    await runner.tick(T0 + 10)  # entry ~0.24, target ~0.264
    cache.set_best_bid_ask("down-token", 0.42, 0.43, ts=T0 + 70)
    await runner.tick(T0 + 70)
    assert len(trades) == 1
    assert trades[0].reason == "tp"
    assert trades[0].tp_price == pytest.approx(0.264)
    # The fill is the observed executable price, not the target.
    assert trades[0].exit_price == pytest.approx(0.42)
    assert trades[0].tp_observed_price == pytest.approx(0.425)
    assert trades[0].tp_observed_bid == pytest.approx(0.42)
    assert trades[0].tp_observed_ask == pytest.approx(0.43)
    assert trades[0].tp_observed_source == "ws_bba"
    assert trades[0].pnl_gross > 1.0


@pytest.mark.asyncio
async def test_same_side_limit(tmp_path):
    settings = _settings()
    runtime = RuntimeConfig(tmp_path / "runtime_config.json", settings.assets)
    runner = MultiAssetRunner(
        settings,
        None,
        PriceCache(),
        FakeMarketService(_window()),
        FakeBroker(),
        StateStore(tmp_path),
        runtime=runtime,
    )

    class _P:
        side = Side.UP

    runner.all_positions = lambda: [_P(), _P(), _P()]  # type: ignore[method-assign]
    assert runner.same_side_open(Side.UP) == 3
    assert await runner.reserve_entry(Side.UP) is False
    assert await runner.reserve_entry(Side.DOWN) is True


@pytest.mark.asyncio
async def test_no_entry_after_60s(tmp_path):
    settings = _settings()
    cache = PriceCache()
    cache.set_best_bid_ask("down-token", 0.10, 0.11, ts=T0 + 61)
    broker = FakeBroker()
    runner = AssetRunner(
        settings.assets[0],
        settings,
        broker,
        cache,
        FakeMarketService(_window()),
        StateStore(tmp_path),
        can_enter=lambda side: (True, "ok"),
        on_trade=lambda r: None,
        on_state_change=lambda: None,
    )
    await runner.tick(T0 + 61)
    assert broker.buys == []


@pytest.mark.asyncio
async def test_forced_exit_at_240s(tmp_path):
    settings = _settings()
    cache = PriceCache()
    cache.set_best_bid_ask("down-token", 0.23, 0.24, ts=T0 + 10)
    broker = FakeBroker()
    trades = []
    runner = AssetRunner(
        settings.assets[0],
        settings,
        broker,
        cache,
        FakeMarketService(_window()),
        StateStore(tmp_path),
        can_enter=lambda side: (True, "ok"),
        on_trade=trades.append,
        on_state_change=lambda: None,
    )
    await runner.tick(T0 + 10)
    cache.set_best_bid_ask("down-token", 0.20, 0.21, ts=T0 + 241)
    await runner.tick(T0 + 241)
    assert len(broker.sells) == 1
    assert trades and trades[0].reason == "time_exit"


class ResolvingPM:
    async def get_event_resolution(self, slug):
        return {"UP": 0.0, "DOWN": 1.0, "resolved": True}

    async def get_asset_updown_markets(self, *args, **kwargs):
        return []

    async def get_midpoint(self, token):
        return 0.0

    async def get_order_book(self, token):
        return None


@pytest.mark.asyncio
async def test_restored_ended_position_settles(tmp_path):
    settings = _settings()
    store = StateStore(tmp_path)
    import time as _time

    past = (int(_time.time()) // 300) * 300 - 600
    pos = Position(
        position_id=f"BTC-DOWN-{past}",
        asset="BTC",
        side=Side.DOWN,
        window_start=past,
        window_end=past + 300,
        slug=f"btc-updown-5m-{past}",
        token_id="down-token",
        entry_ts=past + 10,
        entry_price=0.25,
        shares=40.0,
        cost_usd=10.0,
        entry_fee=0.0,
        tp_price=0.275,
        stake_usd=10.0,
    )
    store.save([pos], {})
    runner = MultiAssetRunner(
        settings,
        ResolvingPM(),
        PriceCache(),
        FakeMarketService(_window()),
        FakeBroker(),
        store,
    )
    await runner._prepare_restored_positions()
    assert [p.position_id for p in runner._stale] == [pos.position_id]
    await runner._resolve_stale()
    assert runner._stale == []
    lines = store.trades_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert '"reason": "settle"' in lines[0]
    assert '"pnl_net": 30.0' in lines[0]
