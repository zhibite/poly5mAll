from __future__ import annotations

import pytest

from src.models import DailyStats, Position, Side, TradeRecord
from src.store import StateStore


def _position() -> Position:
    return Position(
        position_id="BTC-DOWN-1800000000",
        asset="BTC",
        side=Side.DOWN,
        window_start=1_800_000_000,
        window_end=1_800_000_300,
        slug="btc-updown-5m-1800000000",
        token_id="down-token",
        entry_ts=1_800_000_010.0,
        entry_price=0.24,
        shares=40.0,
        cost_usd=9.6,
        entry_fee=0.08,
        tp_price=0.264,
        stake_usd=10.0,
        up_token="up-token",
        down_token="down-token",
        exchange_position_id="pos-down",
        entry_quote_mid=0.235,
        entry_quote_bid=0.23,
        entry_quote_ask=0.24,
        entry_quote_spread=0.01,
        entry_quote_source="ws_bba",
        entry_quote_ts=1_800_000_010.0,
    )


def test_state_roundtrip(tmp_path):
    store = StateStore(tmp_path)
    daily = {"2026-11-04": DailyStats(realized_pnl=1.2, trades=2)}
    store.save([_position()], daily)
    positions, loaded_daily = store.load()
    assert len(positions) == 1
    assert positions[0].position_id == "BTC-DOWN-1800000000"
    assert positions[0].side is Side.DOWN
    assert positions[0].exchange_position_id == "pos-down"
    assert positions[0].entry_quote_spread == pytest.approx(0.01)
    assert positions[0].entry_quote_source == "ws_bba"
    assert positions[0].entry_quote_ts == pytest.approx(1_800_000_010.0)
    assert loaded_daily["2026-11-04"].realized_pnl == 1.2


def test_daily_stats_peak_pnl_roundtrip_and_legacy(tmp_path):
    store = StateStore(tmp_path)
    daily = {
        "2026-11-04": DailyStats(
            realized_pnl=-2.0,
            peak_pnl=5.0,
            trades=4,
            drawdown_pause_until=1_800_003_600.0,
        )
    }
    store.save([], daily)
    _, loaded = store.load()
    assert loaded["2026-11-04"].peak_pnl == pytest.approx(5.0)
    assert loaded["2026-11-04"].drawdown_pause_until == pytest.approx(
        1_800_003_600.0
    )
    # A legacy row without peak_pnl must not look like a drawdown of its
    # whole profit: the high-water mark is at least the current PnL.
    legacy = DailyStats.from_dict({"realized_pnl": 3.0, "trades": 2})
    assert legacy.peak_pnl == pytest.approx(3.0)


def test_trade_append(tmp_path):
    store = StateStore(tmp_path)
    record = TradeRecord(
        position_id="p",
        asset="BTC",
        side="DOWN",
        window_start=1,
        slug="s",
        entry_ts=2.0,
        entry_price=0.2,
        shares=50.0,
        cost_usd=10.0,
        entry_fee=0.0,
        tp_price=0.22,
        exit_ts=3.0,
        exit_price=0.22,
        exit_usdc=11.0,
        exit_fee=0.0,
        reason="tp",
        pnl_gross=1.0,
        pnl_net=1.0,
        stake_usd=10.0,
        mode="paper",
        entry_quote_spread=0.01,
    )
    store.append_trade(record)
    lines = store.trades_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert '"pnl_net": 1.0' in lines[0]
    assert '"entry_quote_spread": 0.01' in lines[0]
