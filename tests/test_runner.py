from __future__ import annotations

import dataclasses
import json
import time

import pytest

from src.config import AssetConfig, load_settings
from src.infra.timeutil import day_key
from src.market.market_ws import PriceCache
from src.models import (
    DailyStats,
    Fill,
    MarketWindow,
    Position,
    Quote,
    Side,
    SideStatus,
)
from src.runner import AssetRunner, MultiAssetRunner
from src.runtime_config import RuntimeConfig
from src.store import StateStore

T0 = 1_800_000_000


def _window(interval: str = "5m") -> MarketWindow:
    span = 900 if interval == "15m" else 300
    return MarketWindow(
        asset="BTC",
        slug=f"btc-updown-{interval}-{T0}",
        start_ts=T0,
        end_ts=T0 + span,
        up_token="up-token",
        down_token="down-token",
        interval=interval,
    )


class FakePos:
    """Minimal position shape used by the exposure-cap unit tests."""

    def __init__(
        self,
        side: Side,
        window_start: int,
        *,
        window_sec: int = 300,
        cost_usd: float = 0.0,
    ) -> None:
        self.side = side
        self.window_start = window_start
        self.window_sec = window_sec
        self.window_end = window_start + window_sec
        self.cost_usd = cost_usd

    @property
    def window_key(self) -> tuple[int, int]:
        return (self.window_sec, self.window_start)


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
        self.buy_position_ids = []
        self.sell_position_ids = []

    async def buy(
        self,
        token_id,
        usd_amount,
        limit_price,
        tick_size,
        fee_rate=None,
        *,
        position_id="",
    ):
        self.buys.append((token_id, usd_amount, limit_price))
        self.buy_position_ids.append(position_id)
        shares = usd_amount / 0.24
        return Fill(
            token_id, "BUY", shares, usd_amount, 0.24, 0.08, T0 + 10
        )

    async def sell(
        self,
        token_id,
        shares,
        limit_price,
        tick_size,
        fee_rate=None,
        *,
        position_id="",
    ):
        self.sells.append((token_id, shares, limit_price))
        self.sell_position_ids.append(position_id)
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
                interval="5m",
                window_sec=300,
                entry_window_end_s=60.0,
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
    pos = runner.sides[0].position
    assert pos is not None
    assert pos.entry_quote_mid == pytest.approx(0.235)
    assert pos.entry_quote_bid == pytest.approx(0.23)
    assert pos.entry_quote_ask == pytest.approx(0.24)
    assert pos.entry_quote_spread == pytest.approx(0.01)
    assert pos.entry_quote_source == "ws_bba"

    # Price is above the 0.264 target at t=70s.
    cache.set_best_bid_ask("down-token", 0.30, 0.31, ts=T0 + 70)
    await runner.tick(T0 + 70)
    assert len(broker.sells) == 1
    assert len(trades) == 1
    assert trades[0].reason == "tp"
    assert trades[0].pnl_net > 0
    assert trades[0].entry_quote_spread == pytest.approx(0.01)
    assert trades[0].entry_quote_bid == pytest.approx(0.23)
    assert trades[0].entry_quote_ask == pytest.approx(0.24)


@pytest.mark.asyncio
async def test_take_profit_before_the_60s_boundary(tmp_path):
    """TP is armed at the fill, so it can fire inside the entry window."""
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
    await runner.tick(T0 + 10)  # entry @0.24, TP 0.264
    cache.set_best_bid_ask("down-token", 0.30, 0.31, ts=T0 + 20)
    await runner.tick(T0 + 20)  # t=20s, still inside the entry window
    assert len(broker.sells) == 1
    assert len(trades) == 1
    assert trades[0].reason == "tp"


@pytest.mark.asyncio
async def test_stop_loss_fires_inside_the_entry_window(tmp_path):
    """A price stop set below the fill is armed immediately."""
    settings = _settings()
    asset = dataclasses.replace(settings.assets[0], stop_loss_pct=0.20)
    settings = dataclasses.replace(settings, assets=(asset,))
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
    await runner.tick(T0 + 10)  # entry @0.24, stop 0.192
    pos = runner.sides[0].position
    assert pos is not None
    assert pos.sl_price == pytest.approx(0.192)

    cache.set_best_bid_ask("down-token", 0.19, 0.20, ts=T0 + 20)
    await runner.tick(T0 + 20)  # bid 0.19 <= stop, still inside entry window
    assert len(broker.sells) == 1
    assert len(trades) == 1
    assert trades[0].reason == "sl"
    assert trades[0].sl_price == pytest.approx(0.192)


@pytest.mark.asyncio
async def test_position_ids_are_forwarded_to_the_broker(tmp_path):
    """v2 markets: entry and exit must sign with the window's position id."""
    settings = _settings()
    window = dataclasses.replace(
        _window(), up_position_id="pos-up", down_position_id="pos-down"
    )
    cache = PriceCache()
    cache.set_best_bid_ask("down-token", 0.23, 0.24, ts=T0 + 10)
    broker = FakeBroker()
    runner = AssetRunner(
        settings.assets[0],
        settings,
        broker,
        cache,
        FakeMarketService(window),
        StateStore(tmp_path),
        can_enter=lambda side: (True, "ok"),
        on_trade=lambda r: None,
        on_state_change=lambda: None,
    )

    await runner.tick(T0 + 10)
    assert broker.buy_position_ids == ["pos-down"]
    pos = runner.sides[0].position
    assert pos is not None and pos.exchange_position_id == "pos-down"

    cache.set_best_bid_ask("down-token", 0.30, 0.31, ts=T0 + 70)
    await runner.tick(T0 + 70)
    assert broker.sell_position_ids == ["pos-down"]


@pytest.mark.asyncio
async def test_restored_position_backfills_exchange_position_id(tmp_path):
    """A state file written before the V3 fix must still exit on v2 markets."""
    settings = _settings()
    window = dataclasses.replace(_window(), down_position_id="pos-down")
    cache = PriceCache()
    cache.set_best_bid_ask("down-token", 0.23, 0.24, ts=T0 + 10)
    broker = FakeBroker()
    runner = AssetRunner(
        settings.assets[0],
        settings,
        broker,
        cache,
        FakeMarketService(window),
        StateStore(tmp_path),
        can_enter=lambda side: (True, "ok"),
        on_trade=lambda r: None,
        on_state_change=lambda: None,
    )
    restored = Position(
        position_id=f"BTC-DOWN-{T0}",
        asset="BTC",
        side=Side.DOWN,
        window_start=T0,
        window_end=T0 + 300,
        slug=f"btc-updown-5m-{T0}",
        token_id="down-token",
        entry_ts=T0 + 5,
        entry_price=0.24,
        shares=40.0,
        cost_usd=9.6,
        entry_fee=0.0,
        tp_price=0.264,
        exchange_position_id="",  # old state file
    )
    runner.pending_positions.append(restored)

    await runner.tick(T0 + 10)

    assert runner.sides[0].position is restored
    assert restored.exchange_position_id == "pos-down"


class RaisingBroker(FakeBroker):
    async def buy(
        self,
        token_id,
        usd_amount,
        limit_price,
        tick_size,
        fee_rate=None,
        *,
        position_id="",
    ):
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

    async def sell(
        self,
        token_id,
        shares,
        limit_price,
        tick_size,
        fee_rate=None,
        *,
        position_id="",
    ):
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
    settings = dataclasses.replace(
        settings,
        execution=dataclasses.replace(
            settings.execution,
            max_open_positions=0,
            max_open_notional_usd=0.0,
            max_positions_per_window=0,
            max_window_notional_usd=0.0,
        ),
    )
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

    runner.all_positions = lambda: [  # type: ignore[method-assign]
        FakePos(Side.UP, T0) for _ in range(6)
    ]
    assert runner.same_side_open(Side.UP) == 6
    assert await runner.reserve_entry(Side.UP, (300, T0)) == (
        False, "same-side-limit",
    )
    assert await runner.reserve_entry(Side.DOWN, (300, T0)) == (True, "")


@pytest.mark.asyncio
async def test_same_side_per_window_limit(tmp_path):
    """At most MAX_SAME_SIDE_PER_WINDOW same-side entries per window.
    Positions still settling from an earlier window do not block the next.
    """
    settings = _risk_settings(
        max_open_positions=0, max_open_notional_usd=0.0
    )
    limit = settings.execution.max_same_side_per_window
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

    runner.all_positions = lambda: [  # type: ignore[method-assign]
        FakePos(Side.UP, T0) for _ in range(limit)
    ]
    assert runner.same_side_open_in_window(Side.UP, (300, T0)) == limit
    assert runner.same_side_open_in_window(Side.UP, (300, T0 - 300)) == 0
    assert await runner.reserve_entry(Side.UP, (300, T0)) == (
        False, "same-side-limit",
    )
    assert await runner.reserve_entry(Side.DOWN, (300, T0)) == (True, "")
    # Positions still settling from the previous window must not block the
    # next one.
    assert await runner.reserve_entry(Side.UP, (300, T0 + 300)) == (True, "")

    # In-flight attempts reserve slots too, so concurrent markets cannot all
    # pass the check before any fill lands.
    for _ in range(limit):
        assert await runner.reserve_entry(
            Side.DOWN, (300, T0 + 600)
        ) == (True, "")
    assert await runner.reserve_entry(Side.DOWN, (300, T0 + 600)) == (
        False, "same-side-limit",
    )
    await runner.release_entry(Side.DOWN, (300, T0 + 600))
    assert await runner.reserve_entry(
        Side.DOWN, (300, T0 + 600)
    ) == (True, "")


def _risk_settings(**overrides):
    """Settings tuned for one entry path, with the other caps disabled."""
    settings = _settings()
    return dataclasses.replace(
        settings,
        execution=dataclasses.replace(settings.execution, **overrides),
    )


def _multi_runner(settings, tmp_path):
    runtime = RuntimeConfig(tmp_path / "runtime_config.json", settings.assets)
    return MultiAssetRunner(
        settings,
        None,
        PriceCache(),
        FakeMarketService(_window()),
        FakeBroker(),
        StateStore(tmp_path),
        runtime=runtime,
    )


@pytest.mark.asyncio
async def test_window_total_position_limit(tmp_path):
    """The per-window cap counts both sides of every asset."""
    settings = _risk_settings(
        max_same_side_positions=0,
        max_same_side_per_window=0,
        max_positions_per_window=2,
        max_window_notional_usd=0.0,
    )
    runner = _multi_runner(settings, tmp_path)

    runner.all_positions = lambda: [  # type: ignore[method-assign]
        FakePos(Side.UP, T0)
    ]
    assert await runner.reserve_entry(Side.DOWN, (300, T0)) == (True, "")
    # One open + one in-flight = 2, so a third entry is blocked even
    # though it is a different side.
    assert await runner.reserve_entry(Side.UP, (300, T0)) == (
        False, "window-position-limit",
    )
    # The next window starts with a clean slate.
    assert await runner.reserve_entry(Side.UP, (300, T0 + 300)) == (
        True, "",
    )
    # A 15m window that starts at the same second is a different window.
    assert await runner.reserve_entry(Side.UP, (900, T0)) == (True, "")


@pytest.mark.asyncio
async def test_window_notional_limit(tmp_path):
    """Open cost plus in-flight stake cannot exceed the window budget."""
    settings = _risk_settings(
        max_same_side_positions=0,
        max_same_side_per_window=0,
        max_positions_per_window=0,
        max_window_notional_usd=3.0,
    )
    runner = _multi_runner(settings, tmp_path)

    runner.all_positions = lambda: [  # type: ignore[method-assign]
        FakePos(Side.UP, T0, cost_usd=2.0)
    ]
    assert await runner.reserve_entry(Side.DOWN, (300, T0), 1.0) == (
        True, "",
    )
    assert await runner.reserve_entry(Side.DOWN, (300, T0), 0.5) == (
        False, "window-notional-limit",
    )
    await runner.release_entry(Side.DOWN, (300, T0))
    assert await runner.reserve_entry(Side.DOWN, (300, T0), 1.0) == (
        True, "",
    )


@pytest.mark.asyncio
async def test_global_open_position_limit_counts_inflight_and_both_intervals(
    tmp_path,
):
    """One aggregate cap covers 5m + 15m and in-flight entries."""
    settings = _risk_settings(
        max_same_side_positions=0,
        max_same_side_per_window=0,
        max_positions_per_window=0,
        max_window_notional_usd=0.0,
        max_open_positions=2,
        max_open_notional_usd=0.0,
    )
    runner = _multi_runner(settings, tmp_path)
    runner.all_positions = lambda: [  # type: ignore[method-assign]
        FakePos(Side.UP, T0, cost_usd=2.0)
    ]
    # One open + one in-flight 15m reservation = 2, so a 5m entry is blocked.
    assert await runner.reserve_entry(Side.DOWN, (900, T0), 2.0) == (
        True, "",
    )
    assert runner.global_open_exposure()[0] == 2
    assert await runner.reserve_entry(Side.DOWN, (300, T0), 2.0) == (
        False, "open-position-limit",
    )
    await runner.release_entry(Side.DOWN, (900, T0))
    assert await runner.reserve_entry(Side.DOWN, (300, T0), 2.0) == (
        True, "",
    )


@pytest.mark.asyncio
async def test_global_open_notional_limit(tmp_path):
    settings = _risk_settings(
        max_same_side_positions=0,
        max_same_side_per_window=0,
        max_positions_per_window=0,
        max_window_notional_usd=0.0,
        max_open_positions=0,
        max_open_notional_usd=5.0,
    )
    runner = _multi_runner(settings, tmp_path)
    runner.all_positions = lambda: [  # type: ignore[method-assign]
        FakePos(Side.UP, T0, cost_usd=3.0)
    ]
    assert await runner.reserve_entry(Side.DOWN, (300, T0), 1.0) == (
        True, "",
    )
    assert await runner.reserve_entry(Side.DOWN, (300, T0), 1.5) == (
        False, "open-notional-limit",
    )
    await runner.release_entry(Side.DOWN, (300, T0))
    assert await runner.reserve_entry(Side.DOWN, (300, T0), 2.0) == (
        True, "",
    )


def test_intraday_drawdown_pauses_then_resumes(tmp_path):
    settings = _risk_settings(max_intraday_drawdown_usd=8.0)
    runner = _multi_runner(settings, tmp_path)
    now = float(T0)
    today = day_key(now)
    runner.daily[today] = DailyStats(realized_pnl=1.0, peak_pnl=10.0)
    assert runner.intraday_drawdown(now) == pytest.approx(9.0)
    assert settings.execution.max_intraday_drawdown_usd == pytest.approx(8.0)
    assert settings.execution.intraday_drawdown_pause_min == pytest.approx(
        60.0
    )

    # Trigger: entries pause for the configured cooldown.
    assert runner.entries_allowed(now=now) == (False, "intraday-drawdown")
    assert runner.daily[today].drawdown_pause_until == pytest.approx(
        now + 3600.0
    )
    assert runner.entries_allowed(now=now + 3599.0) == (
        False, "intraday-drawdown",
    )

    # Cooldown over: entries resume and the anchor is rebased to the
    # current PnL instead of re-triggering on the old peak.
    assert runner.entries_allowed(now=now + 3601.0) == (True, "ok")
    assert runner.daily[today].peak_pnl == pytest.approx(1.0)
    assert runner.daily[today].drawdown_pause_until == 0.0

    # A fresh drawdown of the same size triggers a new pause.
    runner.daily[today].add(-8.0, -8.0, 0.0)
    assert runner.daily[today].realized_pnl == pytest.approx(-7.0)
    assert runner.entries_allowed(now=now + 4200.0) == (
        False, "intraday-drawdown",
    )


def test_intraday_drawdown_pause_zero_disables(tmp_path):
    settings = _risk_settings(
        intraday_drawdown_pause_min=0.0, max_daily_loss_usd=0.0
    )
    runner = _multi_runner(settings, tmp_path)
    now = float(T0)
    runner.daily[day_key(now)] = DailyStats(
        realized_pnl=-20.0, peak_pnl=10.0
    )
    assert runner.entries_allowed(now=now) == (True, "ok")


def _seed_trade(store: StateStore, asset: str, pnl_net: float) -> None:
    row = {
        "asset": asset,
        "exit_ts": time.time() - 60.0,
        "pnl_net": pnl_net,
    }
    store.trades_path.write_text(json.dumps(row) + "\n", encoding="utf-8")


@pytest.mark.asyncio
async def test_asset_perf_downweights_stake(tmp_path):
    settings = _risk_settings()
    store = StateStore(tmp_path)
    # -4 is below the -3 soft threshold but above the -6 disable line.
    _seed_trade(store, "BTC", -4.0)
    runtime = RuntimeConfig(tmp_path / "runtime_config.json", settings.assets)
    cache = PriceCache()
    cache.set_best_bid_ask("down-token", 0.23, 0.24, ts=T0 + 10)
    broker = FakeBroker()
    runner = MultiAssetRunner(
        settings,
        None,
        cache,
        FakeMarketService(_window()),
        broker,
        store,
        runtime=runtime,
    )
    assert runner.asset_rolling_pnl("BTC") == pytest.approx(-4.0)
    assert runner.asset_perf_policy("BTC") == (
        True, 0.5, "asset-perf-downweight",
    )
    await runner.markets["BTC-5M"].tick(T0 + 10)
    assert len(broker.buys) == 1
    # Global stake is 2 U; the losing coin trades at half size.
    assert broker.buys[0][1] == pytest.approx(1.0)
    pos = runner.markets["BTC-5M"].sides[0].position
    assert pos is not None
    assert pos.stake_usd == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_asset_perf_disables_entries(tmp_path):
    settings = _risk_settings()
    store = StateStore(tmp_path)
    _seed_trade(store, "BTC", -7.0)
    runtime = RuntimeConfig(tmp_path / "runtime_config.json", settings.assets)
    cache = PriceCache()
    cache.set_best_bid_ask("down-token", 0.23, 0.24, ts=T0 + 10)
    broker = FakeBroker()
    runner = MultiAssetRunner(
        settings,
        None,
        cache,
        FakeMarketService(_window()),
        broker,
        store,
        runtime=runtime,
    )
    assert runner.asset_perf_policy("BTC") == (
        False, 0.0, "asset-perf-disabled",
    )
    await runner.markets["BTC-5M"].tick(T0 + 10)
    assert broker.buys == []
    assert runner.markets["BTC-5M"].sides[0].last_reason == "asset-perf-disabled"


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


def _settings_15m():
    settings = _settings()
    asset = dataclasses.replace(
        settings.assets[0],
        interval="15m",
        window_sec=900,
        entry_window_end_s=180.0,
    )
    return dataclasses.replace(settings, assets=(asset,))


@pytest.mark.asyncio
async def test_15m_market_enters_in_the_first_three_minutes(tmp_path):
    settings = _settings_15m()
    cache = PriceCache()
    cache.set_best_bid_ask("down-token", 0.23, 0.24, ts=T0 + 170)
    broker = FakeBroker()
    runner = AssetRunner(
        settings.assets[0],
        settings,
        broker,
        cache,
        FakeMarketService(_window("15m")),
        StateStore(tmp_path),
        can_enter=lambda side: (True, "ok"),
        on_trade=lambda r: None,
        on_state_change=lambda: None,
    )
    await runner.tick(T0 + 170)
    assert len(broker.buys) == 1
    pos = runner.sides[0].position
    assert pos is not None
    assert pos.interval == "15m"
    assert pos.window_end == T0 + 900
    assert pos.position_id.startswith("BTC-15M-")


@pytest.mark.asyncio
async def test_15m_market_does_not_enter_after_180s(tmp_path):
    settings = _settings_15m()
    cache = PriceCache()
    cache.set_best_bid_ask("down-token", 0.10, 0.11, ts=T0 + 181)
    broker = FakeBroker()
    runner = AssetRunner(
        settings.assets[0],
        settings,
        broker,
        cache,
        FakeMarketService(_window("15m")),
        StateStore(tmp_path),
        can_enter=lambda side: (True, "ok"),
        on_trade=lambda r: None,
        on_state_change=lambda: None,
    )
    await runner.tick(T0 + 181)
    assert broker.buys == []


def test_5m_and_15m_positions_use_separate_window_keys(tmp_path):
    settings = _settings()
    runner = _multi_runner(settings, tmp_path)
    runner.all_positions = lambda: [  # type: ignore[method-assign]
        FakePos(Side.UP, T0, window_sec=300, cost_usd=2.0),
        FakePos(Side.DOWN, T0, window_sec=900, cost_usd=3.0),
    ]
    assert len(runner.positions_in_window((300, T0))) == 1
    assert len(runner.positions_in_window((900, T0))) == 1
    assert runner.window_open_notional((300, T0)) == pytest.approx(2.0)
    assert runner.window_open_notional((900, T0)) == pytest.approx(3.0)


def test_market_exit_limit_respects_exchange_price_floor(tmp_path):
    """A 0.001 tick does not allow sub-cent orders: clamp to 0.01.

    A live stop-loss sent 0.001/0.009 limits, the CLOB rejected them as
    "invalid price (min 0.01)", and the position settled worthless.
    """
    settings = _settings()
    runner = AssetRunner(
        settings.assets[0],
        settings,
        FakeBroker(),
        PriceCache(),
        FakeMarketService(_window()),
        StateStore(tmp_path),
        can_enter=lambda side: (True, "ok"),
        on_trade=lambda r: None,
        on_state_change=lambda: None,
    )

    def quote(bid: float) -> Quote:
        return Quote(
            token_id="down-token",
            mid=bid,
            bid=bid,
            ask=bid + 0.001,
            last=bid,
            updated_at=T0,
            source="test",
        )

    # 2% below 0.02 at a 0.001 tick stays inside the valid range.
    assert runner._market_exit_limit(quote(0.02), 0.001) == pytest.approx(
        0.019
    )
    # Below the exchange minimum: use 0.01 rather than an invalid 0.005.
    assert runner._market_exit_limit(quote(0.005), 0.001) == pytest.approx(
        0.01
    )
    # No quote: a valid minimum-price order instead of the 0.001 tick.
    assert runner._market_exit_limit(None, 0.001) == pytest.approx(0.01)


def _sl_settings(stop_loss_pct: float = 0.20):
    settings = _settings()
    asset = dataclasses.replace(
        settings.assets[0], stop_loss_pct=stop_loss_pct
    )
    return dataclasses.replace(settings, assets=(asset,))


@pytest.mark.asyncio
async def test_stop_loss_exits_with_a_marketable_limit(tmp_path):
    settings = _sl_settings()
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
    await runner.tick(T0 + 10)  # entry @0.24, stop 0.192
    cache.set_best_bid_ask("down-token", 0.19, 0.20, ts=T0 + 70)
    await runner.tick(T0 + 70)
    assert len(broker.sells) == 1
    assert trades and trades[0].reason == "sl"


@pytest.mark.asyncio
async def test_open_position_settles_at_window_end_without_forced_exit(tmp_path):
    """No time exit: an open position waits at the close for settlement."""
    settings = _settings()
    cache = PriceCache()
    cache.set_best_bid_ask("down-token", 0.23, 0.24, ts=T0 + 10)
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
    await runner.tick(T0 + 10)
    # A low bid late in the window must not trigger a sell (the price stop
    # is off by default and there is no time-based exit).
    cache.set_best_bid_ask("down-token", 0.10, 0.11, ts=T0 + 299)
    await runner.tick(T0 + 299)
    assert broker.sells == []
    # The window closes: hand the remainder to settlement.
    await runner.tick(T0 + 300)
    assert broker.sells == []
    assert runner.sides[0].status is SideStatus.SETTLE_PENDING


class BidBookBroker(FakeBroker):
    """Fill a SELL only when the limit crosses the current top bid."""

    def __init__(self, bid: float):
        super().__init__()
        self.bid = bid
        self.rejected: list[float] = []
        self.invalid: list[float] = []

    async def sell(
        self,
        token_id,
        shares,
        limit_price,
        tick_size,
        fee_rate=None,
        *,
        position_id="",
    ):
        self.sells.append((token_id, shares, limit_price, tick_size))
        if limit_price < 0.01 - 1e-12:
            # The live CLOB rejects sub-cent prices even on a 0.001 tick.
            self.invalid.append(limit_price)
            return None
        if limit_price > self.bid + 1e-12:
            self.rejected.append(limit_price)
            return None
        price = self.bid
        return Fill(
            token_id, "SELL", shares, shares * price, price, 0.0, T0 + 241
        )


@pytest.mark.asyncio
async def test_stop_loss_uses_subcent_tick_grid(tmp_path):
    """A 0.001 tick still allows valid prices at or above 0.01."""
    settings = _sl_settings()
    window = dataclasses.replace(_window(), tick_size=0.001)
    cache = PriceCache()
    cache.set_best_bid_ask("down-token", 0.23, 0.24, ts=T0 + 10)
    broker = BidBookBroker(0.015)
    trades = []
    runner = AssetRunner(
        settings.assets[0],
        settings,
        broker,
        cache,
        FakeMarketService(window),
        StateStore(tmp_path),
        can_enter=lambda side: (True, "ok"),
        on_trade=trades.append,
        on_state_change=lambda: None,
    )
    await runner.tick(T0 + 10)  # entry ~0.24
    cache.set_best_bid_ask("down-token", 0.015, 0.016, ts=T0 + 70)
    await runner.tick(T0 + 70)

    assert broker.invalid == []
    assert broker.rejected == []
    assert broker.sells and broker.sells[-1][2] == pytest.approx(0.014)
    assert len(trades) == 1
    assert trades[0].reason == "sl"
    assert trades[0].exit_price == pytest.approx(0.015)


@pytest.mark.asyncio
async def test_stop_loss_limit_never_above_bid(tmp_path):
    """Even with a stale 0.01 tick, do not clamp the limit above the bid."""
    settings = _sl_settings()
    cache = PriceCache()
    cache.set_best_bid_ask("down-token", 0.23, 0.24, ts=T0 + 10)
    broker = BidBookBroker(0.015)
    trades = []
    runner = AssetRunner(
        settings.assets[0],
        settings,
        broker,
        cache,
        FakeMarketService(_window()),  # tick_size default 0.01
        StateStore(tmp_path),
        can_enter=lambda side: (True, "ok"),
        on_trade=trades.append,
        on_state_change=lambda: None,
    )
    await runner.tick(T0 + 10)
    cache.set_best_bid_ask("down-token", 0.015, 0.016, ts=T0 + 70)
    await runner.tick(T0 + 70)

    assert broker.invalid == []
    assert broker.rejected == []
    assert broker.sells and broker.sells[-1][2] == pytest.approx(0.01)
    assert trades and trades[0].reason == "sl"


class DynamicTickService(FakeMarketService):
    """Market service whose CLOB tick has changed since discovery."""

    def __init__(self, window, tick):
        super().__init__(window)
        self.tick = tick
        self.tick_calls = 0

    async def tick_size(self, token_id):
        self.tick_calls += 1
        return self.tick


@pytest.mark.asyncio
async def test_stop_loss_refreshes_midwindow_tick(tmp_path):
    """The CLOB lowers the tick mid-window; the exit must follow it."""
    settings = _sl_settings()
    cache = PriceCache()
    cache.set_best_bid_ask("down-token", 0.23, 0.24, ts=T0 + 10)
    broker = BidBookBroker(0.015)
    trades = []
    service = DynamicTickService(_window(), 0.001)
    runner = AssetRunner(
        settings.assets[0],
        settings,
        broker,
        cache,
        service,
        StateStore(tmp_path),
        can_enter=lambda side: (True, "ok"),
        on_trade=trades.append,
        on_state_change=lambda: None,
    )
    await runner.tick(T0 + 10)
    pos = runner.sides[0].position
    assert pos is not None and pos.tick_size == pytest.approx(0.01)

    # By the stop-loss the exchange grid is 0.001, not the 0.01 cached
    # when the window was discovered.
    cache.set_best_bid_ask("down-token", 0.015, 0.016, ts=T0 + 70)
    await runner.tick(T0 + 70)

    assert service.tick_calls >= 1
    assert pos.tick_size == pytest.approx(0.001)
    assert broker.invalid == []
    assert broker.sells[-1][2] == pytest.approx(0.014)
    assert broker.sells[-1][3] == pytest.approx(0.001)
    assert trades and trades[0].reason == "sl"


class PartialSellBroker(FakeBroker):
    """The first SELL fills one third; later sells fill the remainder."""

    def __init__(self, price: float = 0.10):
        super().__init__()
        self.price = price
        self._partial_done = False

    async def sell(
        self,
        token_id,
        shares,
        limit_price,
        tick_size,
        fee_rate=None,
        *,
        position_id="",
    ):
        self.sells.append((token_id, shares, limit_price))
        sold = shares if self._partial_done else shares / 3.0
        self._partial_done = True
        return Fill(
            token_id,
            "SELL",
            sold,
            sold * self.price,
            self.price,
            0.0,
            T0 + 245,
        )


@pytest.mark.asyncio
async def test_partial_exit_cools_down_before_retry(tmp_path):
    """A partial SELL locks matched shares; do not retry until settled."""
    settings = _sl_settings()
    cache = PriceCache()
    cache.set_best_bid_ask("down-token", 0.23, 0.24, ts=T0 + 10)
    broker = PartialSellBroker()
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
    await runner.tick(T0 + 10)  # entry 8.33 shares @0.24
    cache.set_best_bid_ask("down-token", 0.10, 0.11, ts=T0 + 70)
    await runner.tick(T0 + 70)  # stop loss: sells one third

    side = runner.sides[0]
    assert len(broker.sells) == 1
    assert trades == []
    assert side.position is not None and side.position.sold_shares > 0
    assert side.exit_cooldown_until == pytest.approx(T0 + 70 + 3.5)

    # exit_retry_s is 1 ms in this fixture; the cooldown must still hold
    # the next sell back instead of overselling the locked balance.
    await runner.tick(T0 + 71)
    assert len(broker.sells) == 1

    # After the cooldown the remainder is sold and booked once.
    await runner.tick(T0 + 74)
    assert len(broker.sells) == 2
    assert len(trades) == 1
    assert trades[0].reason == "sl"


@pytest.mark.asyncio
async def test_open_position_retired_at_window_flip_goes_to_settlement(tmp_path):
    """A position still open when the next window binds must not be stranded."""
    settings = _settings()
    cache = PriceCache()
    cache.set_best_bid_ask("down-token", 0.23, 0.24, ts=T0 + 10)
    service = FakeMarketService(_window())
    runner = AssetRunner(
        settings.assets[0],
        settings,
        FakeBroker(),
        cache,
        service,
        StateStore(tmp_path),
        can_enter=lambda side: (True, "ok"),
        on_trade=lambda r: None,
        on_state_change=lambda: None,
    )
    await runner.tick(T0 + 10)
    assert runner.sides[0].position is not None

    # The next window opens before this side was ever marked for settlement.
    service.window = dataclasses.replace(
        _window(),
        start_ts=T0 + 300,
        end_ts=T0 + 600,
        slug=f"btc-updown-5m-{T0 + 300}",
    )
    await runner.tick(T0 + 301)

    retired = [s for s in runner.retired if s.position is not None]
    assert len(retired) == 1
    assert retired[0].status is SideStatus.SETTLE_PENDING


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


@pytest.mark.asyncio
async def test_stop_loss_writes_off_dust_without_sell(tmp_path):
    """A sub-0.01 remainder must not be sent to the CLOB as a sell."""
    settings = _sl_settings()
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
    pos = runner.sides[0].position
    assert pos is not None
    pos.sold_shares = pos.shares - 0.001

    cache.set_best_bid_ask("down-token", 0.19, 0.20, ts=T0 + 70)
    await runner.tick(T0 + 70)
    assert broker.sells == []
    assert len(trades) == 1
    assert trades[0].reason == "sl"
    assert "dust" in trades[0].note


@pytest.mark.asyncio
async def test_stop_loss_below_min_size_waits_for_settlement(tmp_path):
    """A 2-share remainder cannot be sold; leave it for settlement."""
    settings = _sl_settings()
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
    pos = runner.sides[0].position
    assert pos is not None
    pos.sold_shares = pos.shares - 2.0

    cache.set_best_bid_ask("down-token", 0.19, 0.20, ts=T0 + 70)
    await runner.tick(T0 + 70)
    assert broker.sells == []
    assert trades == []
    assert runner.sides[0].status is SideStatus.SETTLE_PENDING


@pytest.mark.asyncio
class LiveFakeBroker(FakeBroker):
    """Fake broker that reports the live mode, like ModeBroker does."""

    mode = "live"


async def test_live_remainder_below_min_notional_waits(tmp_path):
    """Live SELLs also need $1 notional; 4 shares at 0.01 cannot post."""
    settings = _sl_settings()
    settings = dataclasses.replace(
        settings,
        execution=dataclasses.replace(
            settings.execution, paper_trading=False, live_enabled=True
        ),
    )
    cache = PriceCache()
    cache.set_best_bid_ask("down-token", 0.23, 0.24, ts=T0 + 10)
    broker = LiveFakeBroker()
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
    pos = runner.sides[0].position
    assert pos is not None
    # 4 shares AND $0.04: both floors fail, so the CLOB cannot accept it.
    pos.sold_shares = pos.shares - 4.0
    cache.set_best_bid_ask("down-token", 0.01, 0.02, ts=T0 + 70)

    await runner.tick(T0 + 70)
    assert broker.sells == []
    assert trades == []
    assert runner.sides[0].status is SideStatus.SETTLE_PENDING


async def test_live_five_share_remainder_is_posted(tmp_path):
    """5 shares meets the CLOB share floor even when notional < $1."""
    settings = _sl_settings()
    settings = dataclasses.replace(
        settings,
        execution=dataclasses.replace(
            settings.execution, paper_trading=False, live_enabled=True
        ),
    )
    cache = PriceCache()
    cache.set_best_bid_ask("down-token", 0.23, 0.24, ts=T0 + 10)
    broker = LiveFakeBroker()
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
    await runner.tick(T0 + 10)
    pos = runner.sides[0].position
    assert pos is not None
    pos.sold_shares = pos.shares - 5.0  # 5 shares x $0.01 = $0.05
    cache.set_best_bid_ask("down-token", 0.01, 0.02, ts=T0 + 70)

    await runner.tick(T0 + 70)
    assert len(broker.sells) == 1
    assert broker.sells[0][1] == pytest.approx(5.0)
