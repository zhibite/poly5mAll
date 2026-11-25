from __future__ import annotations

import dataclasses

import pytest

from src.config import ConfigError, load_settings, validate_settings
from src.models import Side


def test_doc_defaults_load():
    s = load_settings()
    by_market = {a.market: a for a in s.assets}
    assert set(by_market) == {
        f"{coin}-{interval}"
        for coin in ("BTC", "ETH", "BNB", "SOL", "DOGE", "XRP", "HYPE")
        for interval in ("5M", "15M")
    }
    assert [i.name for i in s.intervals] == ["5m", "15m"]
    assert s.interval("5m").window_sec == 300
    assert s.interval("5m").entry_window_end_s == pytest.approx(60.0)
    assert s.interval("15m").window_sec == 900
    assert s.interval("15m").entry_window_end_s == pytest.approx(180.0)

    assert by_market["BTC-5M"].sides == (Side.UP, Side.DOWN)
    assert by_market["BTC-5M"].entry_thresh == pytest.approx(0.25)
    assert by_market["BTC-5M"].tp_pct == pytest.approx(0.10)
    assert by_market["BTC-15M"].entry_thresh == pytest.approx(0.25)
    # Latest 5m backtest: "absolute minimum drawdown + zero negative
    # months" tier, with a per-coin price stop.
    expected_5m = {
        "ETH": (0.06, 0.15, 0.10),
        "BNB": (0.06, 0.15, 0.10),
        "SOL": (0.08, 0.10, 0.00),
        "DOGE": (0.06, 0.15, 0.10),
        "XRP": (0.06, 0.15, 0.10),
        "HYPE": (0.10, 0.10, 0.10),
    }
    for coin, (entry, tp, sl) in expected_5m.items():
        cfg = by_market[f"{coin}-5M"]
        assert cfg.entry_thresh == pytest.approx(entry)
        assert cfg.tp_pct == pytest.approx(tp)
        assert cfg.stop_loss_pct == pytest.approx(sl)

    # 15m still uses the coin-level defaults until it gets its own backtest.
    assert by_market["ETH-15M"].entry_thresh == pytest.approx(0.18)
    assert by_market["ETH-15M"].tp_pct == pytest.approx(0.15)
    assert by_market["ETH-15M"].stop_loss_pct == pytest.approx(0.0)
    assert by_market["HYPE-15M"].entry_thresh == pytest.approx(0.14)
    assert by_market["HYPE-15M"].tp_pct == pytest.approx(0.25)
    assert by_market["BTC-5M"].stop_loss_pct == pytest.approx(0.0)

    # Default deployment enables only the six non-BTC 5m markets.  BTC is
    # off (its 5% taker fee is still negative) and 15m is opt-in after the
    # disappointing backtest.
    assert by_market["BTC-5M"].enabled is False
    assert by_market["BTC-15M"].enabled is False
    assert by_market["ETH-15M"].enabled is False
    assert by_market["HYPE-15M"].enabled is False
    enabled = [a.market for a in s.assets if a.enabled]
    assert enabled == [
        "ETH-5M", "BNB-5M", "SOL-5M",
        "DOGE-5M", "XRP-5M", "HYPE-5M",
    ]
    assert [p.market for p in s.plans()] == [
        m for m in enabled for _ in (0, 1)
    ]
    assert s.execution.paper_trading is True
    assert s.execution.stake_usd == pytest.approx(2.0)
    assert s.execution.require_two_sided_book is True
    assert s.execution.min_entry_bid_depth_usd == pytest.approx(5.0)
    assert s.execution.max_same_side_positions == 6
    assert s.execution.max_open_positions == 6
    assert s.execution.max_open_notional_usd == pytest.approx(10.0)
    assert s.execution.max_same_side_per_window == 2
    assert s.execution.max_daily_loss_usd == pytest.approx(40.0)
    assert s.execution.max_intraday_drawdown_usd == pytest.approx(16.0)
    assert s.execution.intraday_drawdown_pause_min == pytest.approx(60.0)
    assert s.execution.max_positions_per_window == 3
    assert s.execution.max_window_notional_usd == pytest.approx(6.0)
    assert s.execution.asset_perf_lookback_days == pytest.approx(7.0)
    assert s.execution.asset_perf_min_pnl_usd == pytest.approx(-3.0)
    assert s.execution.asset_perf_stake_mult == pytest.approx(0.5)
    assert s.execution.asset_perf_disable_pnl_usd == pytest.approx(-6.0)
    assert s.feed.ws_shards == 7
    assert s.feed.book_poll_s == pytest.approx(3.0)
    assert s.mode == "paper"
    assert s.web.port == 8033
    assert s.web.enabled is True


def test_markets_are_ordered_5m_first_then_15m():
    s = load_settings()
    markets = [a.market for a in s.assets]
    assert markets == [
        "BTC-5M", "ETH-5M", "BNB-5M", "SOL-5M",
        "DOGE-5M", "XRP-5M", "HYPE-5M",
        "BTC-15M", "ETH-15M", "BNB-15M", "SOL-15M",
        "DOGE-15M", "XRP-15M", "HYPE-15M",
    ]


def test_per_interval_overrides(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        "[ASSETS.ETH]\n"
        "ENABLED = true\n"
        "ENTRY_THRESH = 0.18\n"
        "TP_PCT = 0.15\n"
        "[ASSETS.ETH.15M]\n"
        "ENTRY_THRESH = 0.22\n"
        "TP_PCT = 0.20\n"
        "STOP_LOSS_PCT = 0.30\n"
        "[ASSETS.ETH.5M]\n"
        "TP_PCT = 0.10\n",
        encoding="utf-8",
    )
    s = load_settings(path)
    eth5 = s.asset("ETH-5M")
    eth15 = s.asset("ETH-15M")
    assert eth5.entry_thresh == pytest.approx(0.18)
    assert eth5.tp_pct == pytest.approx(0.10)
    assert eth5.stop_loss_pct == pytest.approx(0.0)
    assert eth15.entry_thresh == pytest.approx(0.22)
    assert eth15.tp_pct == pytest.approx(0.20)
    assert eth15.stop_loss_pct == pytest.approx(0.30)
    # Coin defaults still apply to markets without an override.
    assert s.asset("BNB-15M").entry_thresh == pytest.approx(0.15)


def test_interval_sub_table_can_disable_one_timeframe(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        "[ASSETS.ETH]\n"
        "ENABLED = true\n"
        "[ASSETS.ETH.15M]\n"
        "ENABLED = false\n",
        encoding="utf-8",
    )
    s = load_settings(path)
    assert s.asset("ETH-5M").enabled is True
    assert s.asset("ETH-15M").enabled is False


def test_defaults_without_config_file(tmp_path):
    # Even with no config.toml, the six non-BTC coins run on 5m only
    # (15m is opt-in), the price stop is off, and the built-in official
    # fee schedule still applies.
    s = load_settings(tmp_path / "missing.toml")
    enabled = [a.market for a in s.assets if a.enabled]
    assert enabled == [
        "ETH-5M", "BNB-5M", "SOL-5M",
        "DOGE-5M", "XRP-5M", "HYPE-5M",
    ]
    assert s.asset("BTC-5M").stop_loss_pct == pytest.approx(0.0)
    assert s.asset("ETH-15M").stop_loss_pct == pytest.approx(0.0)
    assert s.asset("BTC-5M").taker_fee_rate == pytest.approx(0.05)
    assert s.asset("ETH-15M").taker_fee_rate == pytest.approx(0.01)


def test_trade_intervals_env_selects_one_timeframe(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADE_INTERVALS", "15m")
    s = load_settings(tmp_path / "missing.toml")
    assert [i.name for i in s.intervals] == ["15m"]
    assert all(a.interval == "15m" for a in s.assets)
    assert {a.window_sec for a in s.assets} == {900}
    assert {a.entry_window_end_s for a in s.assets} == {180.0}
    # Selecting only 15m makes it the default timeframe for the coins.
    assert all(
        a.enabled for a in s.assets if a.asset != "BTC"
    )


def test_15m_off_by_default_and_opt_in_per_market(tmp_path):
    assert load_settings().asset("ETH-15M").enabled is False

    path = tmp_path / "config.toml"
    path.write_text(
        "[ASSETS.ETH]\n"
        "ENABLED = true\n"
        "[ASSETS.ETH.15M]\n"
        "ENABLED = true\n",
        encoding="utf-8",
    )
    s = load_settings(path)
    assert s.asset("ETH-5M").enabled is True
    assert s.asset("ETH-15M").enabled is True


def test_invalid_threshold_rejected():
    s = load_settings()
    bad_asset = dataclasses.replace(s.assets[0], entry_thresh=1.2)
    bad = dataclasses.replace(s, assets=(bad_asset,))
    with pytest.raises(ConfigError):
        validate_settings(bad)


def test_invalid_interval_rejected():
    s = load_settings()
    bad_interval = dataclasses.replace(
        s.intervals[0], entry_window_end_s=s.intervals[0].window_sec + 1
    )
    bad = dataclasses.replace(
        s, intervals=(bad_interval, s.intervals[1])
    )
    with pytest.raises(ConfigError):
        validate_settings(bad)


def test_invalid_risk_caps_rejected():
    s = load_settings()
    for bad_exec in (
        dataclasses.replace(s.execution, asset_perf_stake_mult=1.5),
        dataclasses.replace(s.execution, max_intraday_drawdown_usd=-1.0),
        dataclasses.replace(s.execution, intraday_drawdown_pause_min=-1.0),
        dataclasses.replace(s.execution, max_open_positions=-1),
        dataclasses.replace(s.execution, max_open_notional_usd=-1.0),
        dataclasses.replace(s.execution, max_positions_per_window=-1),
        dataclasses.replace(s.execution, max_window_notional_usd=-1.0),
        dataclasses.replace(s.execution, asset_perf_lookback_days=-1.0),
    ):
        with pytest.raises(ConfigError):
            validate_settings(dataclasses.replace(s, execution=bad_exec))


def test_per_asset_fee_rates():
    from src.config import fee_negative_plans
    from src.economics import fee_line

    s = load_settings()
    # Official crypto schedule: BTC 5%, every other asset 1%, both
    # timeframes.
    assert s.asset("BTC-5M").taker_fee_rate == pytest.approx(0.05)
    assert s.asset("BTC-15M").taker_fee_rate == pytest.approx(0.05)
    for market in (
        "ETH-5M", "ETH-15M", "BNB-5M", "BNB-15M", "SOL-5M", "SOL-15M",
        "DOGE-5M", "DOGE-15M", "XRP-5M", "XRP-15M", "HYPE-5M", "HYPE-15M",
    ):
        assert s.asset(market).taker_fee_rate == pytest.approx(0.01)

    plans = {p.key: p for p in s.all_plans()}
    assert "XRP-5M-UP" in plans
    assert "XRP-15M-UP" in plans
    assert plans["BTC-5M-DOWN"].taker_fee_rate == pytest.approx(0.05)
    assert plans["ETH-15M-DOWN"].taker_fee_rate == pytest.approx(0.01)

    # Under the new schedule the doc's BTC 0.25/10% TP covers its fees.
    line = fee_line(plans["BTC-5M-DOWN"])
    assert line.fee_rate == pytest.approx(0.05)
    assert line.net_per_trade > 0
    assert fee_negative_plans(s) == []
