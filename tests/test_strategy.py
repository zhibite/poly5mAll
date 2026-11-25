from __future__ import annotations

import dataclasses

import pytest

from src.config import ExecutionConfig, FeedConfig
from src.models import (
    Fill,
    MarketWindow,
    Phase,
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


def test_phase_uses_the_market_timeframe():
    side5 = SideRuntime(_plan(), _window())
    assert side5.phase(T0 + 100) is Phase.TP
    assert side5.phase(T0 + 300) is Phase.ENDED

    plan15 = dataclasses.replace(
        _plan(), interval="15m", window_sec=900, entry_window_end_s=180.0
    )
    window15 = dataclasses.replace(
        _window(),
        slug=f"btc-updown-15m-{T0}",
        end_ts=T0 + 900,
        interval="15m",
    )
    side15 = SideRuntime(plan15, window15)
    assert side15.phase(T0 + 100) is Phase.ENTRY
    assert side15.phase(T0 + 181) is Phase.TP
    assert side15.phase(T0 + 900) is Phase.ENDED


def test_entry_only_in_first_60s():
    side = SideRuntime(_plan(), _window())
    e, f = ExecutionConfig(), FeedConfig()

    assert side.entry_block_reason(T0 + 10, _quote(T0 + 10, 0.24), e, f) is None
    assert (
        side.entry_block_reason(T0 + 61, _quote(T0 + 61, 0.10), e, f)
        == "outside-entry-window"
    )
    assert (
        side.entry_block_reason(T0 + 10, _quote(T0 + 10, 0.30), e, f)
        == "above-threshold"
    )


def test_15m_entry_window_is_three_minutes():
    plan = dataclasses.replace(
        _plan(), interval="15m", window_sec=900, entry_window_end_s=180.0
    )
    window = dataclasses.replace(
        _window(),
        slug=f"btc-updown-15m-{T0}",
        end_ts=T0 + 900,
        interval="15m",
    )
    side = SideRuntime(plan, window)
    e, f = ExecutionConfig(), FeedConfig()
    assert side.entry_block_reason(
        T0 + 170, _quote(T0 + 170, 0.24), e, f
    ) is None
    assert (
        side.entry_block_reason(T0 + 181, _quote(T0 + 181, 0.10), e, f)
        == "outside-entry-window"
    )


def test_stale_quote_blocks_entry():
    side = SideRuntime(_plan(), _window())
    e, f = ExecutionConfig(), FeedConfig()
    quote = Quote(
        token_id="down-token", mid=0.20, bid=0.19, ask=0.21,
        last=0.20, updated_at=T0, source="ws_bba",
    )
    assert (
        side.entry_block_reason(T0 + 30, quote, e, f) == "stale-price"
    )


def test_wide_spread_and_missing_bbo_block_entry():
    side = SideRuntime(_plan(), _window())
    f = FeedConfig()
    e = ExecutionConfig(max_spread_abs=0.01)

    wide = _quote(T0 + 10, 0.23, bid=0.22, ask=0.24)  # spread 0.02
    assert side.entry_block_reason(T0 + 10, wide, e, f) == "wide-spread"

    # Missing BBO must not silently pass the spread filter.
    no_bbo = _quote(T0 + 10, 0.23, bid=0.0, ask=0.0)
    assert side.entry_block_reason(T0 + 10, no_bbo, e, f) == "wide-spread"

    tight = _quote(T0 + 10, 0.24, bid=0.235, ask=0.245)  # spread 0.01
    assert side.entry_block_reason(T0 + 10, tight, e, f) is None


def test_tp_starts_at_entry_and_uses_bid():
    side = SideRuntime(_plan(), _window())
    e, f = ExecutionConfig(), FeedConfig()
    fill = Fill("down-token", "BUY", 41.6667, 10.0, 0.24, 0.0, T0 + 10)
    pos = side.on_entry_fill(fill, T0 + 10, e)
    assert pos.tp_price == pytest.approx(0.264)
    assert side.status is SideStatus.HOLDING

    # TP is armed by the fill, not by a fixed 60 s window boundary.
    high = _quote(T0 + 20, 0.30, bid=0.30, ask=0.31)
    assert side.tp_due(T0 + 20, high, e, f)

    # Mid above target but the executable bid is below it: no TP yet.
    wide = _quote(T0 + 70, 0.30, bid=0.25, ask=0.31)
    assert not side.tp_due(T0 + 70, wide, e, f)


def test_sl_armed_at_entry_and_uses_bid():
    plan = dataclasses.replace(_plan(), stop_loss_pct=0.5)
    side = SideRuntime(plan, _window())
    e, f = ExecutionConfig(), FeedConfig()
    fill = Fill("down-token", "BUY", 41.6667, 10.0, 0.24, 0.0, T0 + 10)
    pos = side.on_entry_fill(fill, T0 + 10, e)
    assert pos.sl_price == pytest.approx(0.12)

    # Still above the stop line: no exit.
    assert not side.sl_due(
        T0 + 20, _quote(T0 + 20, 0.20, bid=0.20, ask=0.21), e, f
    )
    # Bid touches the line 10 s after the fill; no 60 s wait.
    assert side.sl_due(
        T0 + 20, _quote(T0 + 20, 0.11, bid=0.11, ask=0.12), e, f
    )
    # Mid below the line but the executable bid is above it: no trigger.
    assert not side.sl_due(
        T0 + 30, _quote(T0 + 30, 0.11, bid=0.13, ask=0.14), e, f
    )
    # The stop runs until the 5m window closes; after that settlement
    # takes over, not another sell attempt.
    assert side.sl_due(
        T0 + 299, _quote(T0 + 299, 0.01, bid=0.01, ask=0.02), e, f
    )
    assert not side.sl_due(
        T0 + 300, _quote(T0 + 300, 0.01, bid=0.01, ask=0.02), e, f
    )


def test_stop_loss_off_by_default():
    side = SideRuntime(_plan(), _window())
    e = ExecutionConfig()
    fill = Fill("down-token", "BUY", 40.0, 10.0, 0.25, 0.0, T0 + 10)
    pos = side.on_entry_fill(fill, T0 + 10, e)
    assert pos.sl_price == 0.0


def test_tp_trigger_modes():
    side = SideRuntime(_plan(), _window())
    e, f = ExecutionConfig(), FeedConfig()
    fill = Fill("down-token", "BUY", 40.0, 10.0, 0.25, 0.0, T0 + 10)
    side.on_entry_fill(fill, T0 + 10, e)
    # Mid is above target, but the executable bid is still below it.
    quote = _quote(T0 + 70, 0.276, bid=0.20, ask=0.35)
    assert not side.tp_due(T0 + 70, quote, e, f, mode="observed")
    assert side.tp_due(T0 + 70, quote, e, f, mode="market")


def test_no_forced_exit_at_the_old_240s_mark():
    side = SideRuntime(_plan(), _window())
    e, f = ExecutionConfig(), FeedConfig()
    fill = Fill("down-token", "BUY", 41.6667, 10.0, 0.24, 0.0, T0 + 10)
    side.on_entry_fill(fill, T0 + 10, e)
    quote = _quote(T0 + 240, 0.20, bid=0.20, ask=0.21)
    # No time-based exit: only TP/SL can close it before the window end.
    assert not side.tp_due(T0 + 240, quote, e, f)
    assert not side.sl_due(T0 + 240, quote, e, f)
    assert side.window_ended(T0 + 300)
    assert not side.window_ended(T0 + 299)


def test_full_exit_books_net_pnl():
    side = SideRuntime(_plan(), _window())
    e = ExecutionConfig()
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


def test_partial_tp_sell_writes_off_dust_and_closes():
    """A 0.0017-share remainder cannot be sold; close and book the trade."""
    side = SideRuntime(_plan(), _window())
    e = ExecutionConfig()
    entry = Fill(
        "down-token", "BUY", 29.411765, 5.0, 0.17, 0.0415, T0 + 10
    )
    side.on_entry_fill(entry, T0 + 10, e)
    fill = Fill(
        "down-token", "SELL", 29.41, 6.1761, 0.21, 0.04879119, T0 + 179
    )

    record = side.on_exit_fill(fill, T0 + 179, "tp", e, "live")

    assert record is not None
    assert side.status is SideStatus.CLOSED
    assert "dust" in record.note
    assert record.exit_usdc == pytest.approx(6.1761)
    assert record.pnl_net == pytest.approx(
        6.1761 - 5.0 - 0.0415 - 0.04879119
    )
    assert side.position is not None
    assert side.position.is_flat
    assert side.position.sold_shares == pytest.approx(29.411765)


def test_dust_only_position_closes_on_exit():
    side = SideRuntime(_plan(), _window())
    e = ExecutionConfig()
    entry = Fill("down-token", "BUY", 10.0, 1.7, 0.17, 0.014, T0 + 10)
    side.on_entry_fill(entry, T0 + 10, e)
    assert side.position is not None
    side.position.sold_shares = 9.999

    record = side.on_dust(T0 + 250, "time_exit", "live")

    assert record is not None
    assert side.status is SideStatus.CLOSED
    assert "dust" in record.note
    assert record.exit_usdc == pytest.approx(0.0)
    assert side.position.is_flat


def test_settlement_booked_as_binary_payout():
    side = SideRuntime(_plan(), _window())
    e = ExecutionConfig()
    entry = Fill("down-token", "BUY", 40.0, 10.0, 0.25, 0.0, T0 + 10)
    side.on_entry_fill(entry, T0 + 10, e)
    pos = side.position
    pos.entry_quote_mid = 0.235
    pos.entry_quote_bid = 0.23
    pos.entry_quote_ask = 0.24
    pos.entry_quote_spread = 0.01
    pos.entry_quote_source = "ws_bba"
    pos.entry_quote_ts = T0 + 10
    record = side.on_settled(1.0, T0 + 300, "paper")
    assert record is not None
    assert record.reason == "settle"
    assert record.pnl_gross == pytest.approx(30.0)
    # Settlement records keep the entry quote for spread analysis.
    assert record.entry_quote_mid == pytest.approx(0.235)
    assert record.entry_quote_bid == pytest.approx(0.23)
    assert record.entry_quote_spread == pytest.approx(0.01)
    assert record.entry_quote_source == "ws_bba"
    assert record.entry_quote_ts == pytest.approx(T0 + 10)
    assert side.status is SideStatus.CLOSED


def test_partial_tp_remainder_stays_holding_for_next_attempt():
    side = SideRuntime(_plan(), _window())
    e = ExecutionConfig()
    entry = Fill(
        "down-token", "BUY", 29.411765, 5.0, 0.17, 0.0415, T0 + 10
    )
    side.on_entry_fill(entry, T0 + 10, e)
    pos = side.position
    assert pos is not None
    pos.tp_observed_price = 0.205
    pos.tp_observed_bid = 0.20
    pos.tp_observed_source = "ws_bba"
    pos.tp_observed_ts = T0 + 179

    # Leave 2.001765 shares.  The CLOB accepts a sell at 5+ shares OR
    # $1+ notional, so the strategy keeps the remainder HOLDING and the
    # next exit attempt decides with the live quote.
    fill = Fill(
        "down-token", "SELL", 27.41, 27.41 * 0.21, 0.21, 0.04, T0 + 179
    )
    assert side.on_exit_fill(fill, T0 + 179, "tp", e, "live") is None
    assert side.status is SideStatus.HOLDING
    assert pos.remaining_shares == pytest.approx(2.001765)

    record = side.on_settled(1.0, T0 + 310, "live", note="gamma resolution")
    assert record is not None
    assert record.reason == "settle"
    assert "partial exit + remainder settlement" in record.note
    assert record.tp_observed_bid == pytest.approx(0.20)
    assert record.exit_usdc == pytest.approx(27.41 * 0.21 + 2.001765)
    assert record.exit_price == pytest.approx(
        record.exit_usdc / 29.411765
    )
