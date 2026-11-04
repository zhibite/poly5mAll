from __future__ import annotations

import pytest

from src.config import ExecutionConfig, FeedConfig, GeneralConfig
from src.models import (
    Fill,
    MarketWindow,
    Quote,
    Side,
    SidePlan,
    SideStatus,
)
from src.strategy.rebound import SideRuntime

T0 = 1_800_000_000  # divisible by 300


def _window() -> MarketWindow:
    return MarketWindow(
        asset="BTC",
        slug=f"btc-updown-5m-{T0}",
        start_ts=T0,
        end_ts=T0 + 300,
        up_token="up-token",
        down_token="down-token",
    )


def _plan() -> SidePlan:
    return SidePlan(
        asset="BTC", side=Side.DOWN, entry_thresh=0.25, tp_pct=0.10,
        stake_usd=10.0,
    )


def _quote(now: float, mid: float, bid: float = 0.0, ask: float = 0.0) -> Quote:
    return Quote(
        token_id="down-token",
        mid=mid,
        bid=bid,
        ask=ask,
        last=mid,
        updated_at=now,
        source="ws_bba" if bid and ask else "rest_mid",
    )


def test_entry_only_in_first_60s():
    side = SideRuntime(_plan(), _window())
    g, e, f = GeneralConfig(), ExecutionConfig(), FeedConfig()

    assert side.entry_block_reason(T0 + 10, _quote(T0 + 10, 0.24), g, e, f) is None
    assert (
        side.entry_block_reason(T0 + 61, _quote(T0 + 61, 0.10), g, e, f)
        == "outside-entry-window"
    )
    assert (
        side.entry_block_reason(T0 + 10, _quote(T0 + 10, 0.30), g, e, f)
        == "above-threshold"
    )


def test_stale_quote_blocks_entry():
    side = SideRuntime(_plan(), _window())
    g, e, f = GeneralConfig(), ExecutionConfig(), FeedConfig()
    quote = Quote(
        token_id="down-token", mid=0.20, bid=0.19, ask=0.21,
        last=0.20, updated_at=T0, source="ws_bba",
    )
    assert (
        side.entry_block_reason(T0 + 30, quote, g, e, f) == "stale-price"
    )


def test_tp_waits_until_60s_and_uses_bid():
    side = SideRuntime(_plan(), _window())
    g, e, f = GeneralConfig(), ExecutionConfig(), FeedConfig()
    fill = Fill("down-token", "BUY", 41.6667, 10.0, 0.24, 0.0, T0 + 10)
    pos = side.on_entry_fill(fill, T0 + 10, e)
    assert pos.tp_price == pytest.approx(0.264)
    assert side.status is SideStatus.HOLDING

    high = _quote(T0 + 55, 0.30, bid=0.30, ask=0.31)
    assert not side.tp_due(T0 + 55, high, g, e, f)
    assert side.tp_due(T0 + 60, high, g, e, f)

    # Mid above target but the executable bid is below it: no TP yet.
    wide = _quote(T0 + 70, 0.30, bid=0.25, ask=0.31)
    assert not side.tp_due(T0 + 70, wide, g, e, f)


def test_stop_loss_starts_after_60s_and_uses_mid():
    side = SideRuntime(_plan(), _window())
    g, e, f = GeneralConfig(), ExecutionConfig(), FeedConfig()
    fill = Fill("down-token", "BUY", 40.0, 10.0, 0.25, 0.0, T0 + 10)
    pos = side.on_entry_fill(fill, T0 + 10, e)
    assert pos.sl_price == pytest.approx(0.20)  # 0.25 * (1 - 0.20)

    # 0–60s is the observation window: no price stop.
    low_before = _quote(T0 + 50, 0.19, bid=0.18, ask=0.20)
    assert not side.sl_due(T0 + 50, low_before, g, e, f)
    # From 60s, an observed mid at/below the stop triggers even if bid > sl.
    wide = _quote(T0 + 61, 0.19, bid=0.25, ask=0.30)
    assert side.sl_due(T0 + 61, wide, g, e, f)


def test_stop_loss_disabled_with_zero_pct():
    plan = SidePlan(
        asset="BTC", side=Side.DOWN, entry_thresh=0.25, tp_pct=0.10,
        stake_usd=10.0, stop_loss_pct=0.0,
    )
    side = SideRuntime(plan, _window())
    g, e, f = GeneralConfig(), ExecutionConfig(), FeedConfig()
    fill = Fill("down-token", "BUY", 40.0, 10.0, 0.25, 0.0, T0 + 10)
    pos = side.on_entry_fill(fill, T0 + 10, e)
    assert pos.sl_price == 0.0
    assert not side.sl_due(
        T0 + 61, _quote(T0 + 61, 0.10, bid=0.09, ask=0.11), g, e, f
    )


def test_tp_trigger_modes():
    side = SideRuntime(_plan(), _window())
    g, e, f = GeneralConfig(), ExecutionConfig(), FeedConfig()
    fill = Fill("down-token", "BUY", 40.0, 10.0, 0.25, 0.0, T0 + 10)
    side.on_entry_fill(fill, T0 + 10, e)
    # Mid is above target, but the executable bid is still below it.
    quote = _quote(T0 + 70, 0.276, bid=0.20, ask=0.35)
    assert not side.tp_due(T0 + 70, quote, g, e, f, mode="observed")
    assert side.tp_due(T0 + 70, quote, g, e, f, mode="market")


def test_force_exit_after_240s():
    side = SideRuntime(_plan(), _window())
    g, e, f = GeneralConfig(), ExecutionConfig(), FeedConfig()
    fill = Fill("down-token", "BUY", 41.6667, 10.0, 0.24, 0.0, T0 + 10)
    side.on_entry_fill(fill, T0 + 10, e)
    assert not side.force_exit_due(T0 + 239, g, e)
    assert side.force_exit_due(T0 + 240, g, e)
    assert side.deadline_passed(T0 + 296, g)


def test_full_exit_books_net_pnl():
    side = SideRuntime(_plan(), _window())
    g, e, f = GeneralConfig(), ExecutionConfig(), FeedConfig()
    entry = Fill("down-token", "BUY", 40.0, 10.0, 0.25, 0.10, T0 + 10)
    pos = side.on_entry_fill(entry, T0 + 10, e)

    exit_fill = Fill(
        "down-token", "SELL", 40.0, 40.0 * 0.275, 0.275, 0.08, T0 + 100
    )
    record = side.on_exit_fill(exit_fill, T0 + 100, "tp", e, "paper")
    assert record is not None
    assert record.reason == "tp"
    assert record.pnl_gross == pytest.approx(1.0)
    assert record.pnl_net == pytest.approx(1.0 - 0.10 - 0.08)
    assert side.status is SideStatus.CLOSED
    assert pos.remaining_shares == pytest.approx(0.0)


def test_partial_exit_stays_holding_until_flat():
    side = SideRuntime(_plan(), _window())
    e = ExecutionConfig()
    entry = Fill("down-token", "BUY", 40.0, 10.0, 0.25, 0.0, T0 + 10)
    side.on_entry_fill(entry, T0 + 10, e)
    half = Fill("down-token", "SELL", 20.0, 20.0 * 0.275, 0.275, 0.0, T0 + 100)
    assert side.on_exit_fill(half, T0 + 100, "tp", e, "paper") is None
    assert side.status is SideStatus.HOLDING
    rest = Fill("down-token", "SELL", 20.0, 20.0 * 0.275, 0.275, 0.0, T0 + 101)
    record = side.on_exit_fill(rest, T0 + 101, "tp", e, "paper")
    assert record is not None
    # 40 shares bought at 0.25 -> 10.00 cost; sold at 0.275 -> 11.00.
    assert record.pnl_gross == pytest.approx(1.0)


def test_settlement_booked_as_binary_payout():
    side = SideRuntime(_plan(), _window())
    e = ExecutionConfig()
    entry = Fill("down-token", "BUY", 40.0, 10.0, 0.25, 0.0, T0 + 10)
    side.on_entry_fill(entry, T0 + 10, e)
    record = side.on_settled(1.0, T0 + 300, "paper")
    assert record is not None
    assert record.reason == "settle"
    assert record.pnl_gross == pytest.approx(30.0)
    assert side.status is SideStatus.CLOSED
