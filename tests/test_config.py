from __future__ import annotations

import dataclasses

import pytest

from src.config import ConfigError, load_settings, validate_settings
from src.models import Side


def test_doc_defaults_load():
    s = load_settings()
    by_name = {a.asset: a for a in s.assets}
    assert set(by_name) == {"BTC", "ETH", "BNB", "SOL", "DOGE", "XRP", "HYPE"}
    assert by_name["BTC"].sides == (Side.UP, Side.DOWN)
    assert by_name["BTC"].entry_thresh == pytest.approx(0.25)
    assert by_name["BTC"].tp_pct == pytest.approx(0.10)
    assert by_name["ETH"].entry_thresh == pytest.approx(0.18)
    assert by_name["XRP"].sides == (Side.UP, Side.DOWN)
    assert by_name["XRP"].entry_thresh == pytest.approx(0.10)
    assert by_name["DOGE"].entry_thresh == pytest.approx(0.12)
    assert by_name["XRP"].entry_thresh == pytest.approx(0.10)
    assert by_name["HYPE"].entry_thresh == pytest.approx(0.14)
    assert by_name["HYPE"].tp_pct == pytest.approx(0.25)
    assert by_name["BTC"].stop_loss_pct == pytest.approx(0.0)
    assert by_name["ETH"].stop_loss_pct == pytest.approx(0.0)
    assert by_name["XRP"].stop_loss_pct == pytest.approx(0.0)
    # Default deployment enables the six non-BTC assets and leaves BTC off
    # (BTC's 5% taker fee is still negative for 0.25/10% in the recheck).
    assert by_name["BTC"].enabled is False
    assert all(by_name[n].enabled for n in by_name if n != "BTC")
    assert [p.asset for p in s.plans()] == [
        "ETH", "ETH", "BNB", "BNB", "SOL", "SOL",
        "DOGE", "DOGE", "XRP", "XRP", "HYPE", "HYPE",
    ]
    assert s.execution.paper_trading is True
    assert s.execution.stake_usd == pytest.approx(2.0)
    assert s.execution.require_two_sided_book is True
    assert s.execution.min_entry_bid_depth_usd == pytest.approx(5.0)
    assert s.execution.max_same_side_positions == 6
    assert s.execution.max_same_side_per_window == 2
    assert s.feed.ws_shards == 7
    assert s.feed.book_poll_s == pytest.approx(3.0)
    assert s.mode == "paper"
    assert s.web.port == 8033
    assert s.web.enabled is True


def test_per_asset_force_exit_defaults_and_override(tmp_path):
    s = load_settings()
    by_name = {a.asset: a for a in s.assets}
    assert by_name["ETH"].force_exit_s == pytest.approx(240.0)
    assert by_name["ETH"].force_exit_deadline_s == pytest.approx(295.0)

    path = tmp_path / "config.toml"
    path.write_text(
        "[GENERAL]\n"
        "FORCE_EXIT_S = 240\n"
        "FORCE_EXIT_DEADLINE_S = 295\n"
        "[ASSETS.ETH]\n"
        "FORCE_EXIT_S = 120\n",
        encoding="utf-8",
    )
    s = load_settings(path)
    eth = s.asset("ETH")
    assert eth.force_exit_s == pytest.approx(120.0)
    # No explicit deadline: min(global 295, 120 + 15)
    assert eth.force_exit_deadline_s == pytest.approx(135.0)


def test_defaults_without_config_file(tmp_path):
    # Even with no config.toml, the six non-BTC assets are on, the price
    # stop is off, and the built-in official fee schedule still applies.
    s = load_settings(tmp_path / "missing.toml")
    assert [a.asset for a in s.assets if a.enabled] == [
        "ETH", "BNB", "SOL", "DOGE", "XRP", "HYPE",
    ]
    by_name = {a.asset: a for a in s.assets}
    assert by_name["BTC"].stop_loss_pct == pytest.approx(0.0)
    assert by_name["ETH"].stop_loss_pct == pytest.approx(0.0)
    assert by_name["BTC"].taker_fee_rate == pytest.approx(0.05)
    assert by_name["ETH"].taker_fee_rate == pytest.approx(0.01)


def test_invalid_threshold_rejected():
    s = load_settings()
    bad_asset = dataclasses.replace(s.assets[0], entry_thresh=1.2)
    bad = dataclasses.replace(s, assets=(bad_asset,))
    with pytest.raises(ConfigError):
        validate_settings(bad)


def test_per_asset_fee_rates():
    from src.config import fee_negative_plans
    from src.economics import fee_line

    s = load_settings()
    by_name = {a.asset: a for a in s.assets}
    # Official crypto 5m schedule: BTC 5%, every other asset 1%.
    assert by_name["BTC"].taker_fee_rate == pytest.approx(0.05)
    for name in ("ETH", "BNB", "SOL", "DOGE", "XRP", "HYPE"):
        assert by_name[name].taker_fee_rate == pytest.approx(0.01)

    plans = {p.key: p for p in s.all_plans()}
    assert "XRP-UP" in plans
    assert plans["BTC-DOWN"].taker_fee_rate == pytest.approx(0.05)
    assert plans["ETH-DOWN"].taker_fee_rate == pytest.approx(0.01)

    # Under the new schedule the doc's BTC 0.25/10% TP covers its fees.
    line = fee_line(plans["BTC-DOWN"])
    assert line.fee_rate == pytest.approx(0.05)
    assert line.net_per_trade > 0
    assert fee_negative_plans(s) == []
