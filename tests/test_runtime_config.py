from __future__ import annotations

import json

import pytest

from src.config import load_settings
from src.models import Side
from src.runtime_config import RuntimeConfig


def _runtime(tmp_path) -> RuntimeConfig:
    s = load_settings()
    return RuntimeConfig(tmp_path / "runtime_config.json", s.assets)


def test_defaults_six_5m_markets_15m_off(tmp_path):
    rt = _runtime(tmp_path)
    assert rt.asset("BTC-5M").enabled is False
    assert rt.asset("BTC-15M").enabled is False
    assert rt.asset("ETH-5M").enabled is True
    assert rt.asset("ETH-15M").enabled is False
    assert rt.asset("HYPE-15M").enabled is False
    assert rt.asset("BTC-5M").stop_loss_pct == pytest.approx(0.0)
    assert rt.asset("ETH-15M").stop_loss_pct == pytest.approx(0.0)
    assert rt.asset("ETH-5M").stop_loss_pct == pytest.approx(0.10)
    assert rt.asset("SOL-5M").stop_loss_pct == pytest.approx(0.0)
    assert rt.asset("XRP-15M").sides == (Side.UP, Side.DOWN)
    assert rt.asset("XRP-5M").entry_thresh == pytest.approx(0.06)
    assert rt.asset("XRP-15M").entry_thresh == pytest.approx(0.10)
    assert rt.asset("ETH-5M").window_sec == 300
    assert rt.asset("ETH-15M").window_sec == 900
    assert rt.asset("ETH-15M").entry_window_end_s == pytest.approx(180.0)
    # A coin alias resolves to the 5m market for backwards compatibility.
    assert rt.asset("ETH").market == "ETH-5M"


def test_update_market_and_persist(tmp_path):
    rt = _runtime(tmp_path)
    rt.update(
        {
            "assets": {
                "ETH-15M": {
                    "enabled": True,
                    "sides": "BOTH",
                    "entry_thresh": 0.20,
                    "tp_pct": 0.30,
                    "stop_loss_pct": 0.15,
                    "stake_usd": 5.0,
                }
            }
        }
    )
    eth15 = rt.asset("ETH-15M")
    assert eth15.enabled is True
    assert eth15.sides == (Side.UP, Side.DOWN)
    assert eth15.entry_thresh == pytest.approx(0.20)
    assert eth15.tp_pct == pytest.approx(0.30)
    assert eth15.stop_loss_pct == pytest.approx(0.15)
    assert eth15.stake_usd == pytest.approx(5.0)
    # The other timeframe keeps the 5m backtest parameters.
    assert rt.asset("ETH-5M").entry_thresh == pytest.approx(0.06)
    assert rt.asset("ETH-5M").stop_loss_pct == pytest.approx(0.10)

    reloaded = RuntimeConfig(
        tmp_path / "runtime_config.json", load_settings().assets
    )
    assert reloaded.asset("ETH-15M").enabled is True
    assert reloaded.asset("ETH-15M").entry_thresh == pytest.approx(0.20)
    assert reloaded.asset("ETH-15M").stop_loss_pct == pytest.approx(0.15)
    assert reloaded.version >= 1


def test_coin_alias_update_applies_to_both_timeframes(tmp_path):
    rt = _runtime(tmp_path)
    rt.update({"assets": {"BNB": {"enabled": False, "tp_pct": 0.30}}})
    assert rt.asset("BNB-5M").enabled is False
    assert rt.asset("BNB-15M").enabled is False
    assert rt.asset("BNB-5M").tp_pct == pytest.approx(0.30)
    assert rt.asset("BNB-15M").tp_pct == pytest.approx(0.30)


def test_legacy_coin_key_file_migrates_to_both_timeframes(tmp_path):
    """A runtime file written before 15m support keys overrides by coin."""
    path = tmp_path / "runtime_config.json"
    path.write_text(
        json.dumps(
            {
                "version": 3,
                "mode": "paper",
                "assets": {
                    "ETH": {
                        "enabled": True,
                        "sides": "DOWN",
                        "entry_thresh": 0.21,
                        "tp_pct": 0.18,
                        "stake_usd": 4.0,
                        "stop_loss_pct": 0.2,
                        # Removed field: must be ignored, not fatal.
                        "force_exit_s": 120,
                        "force_exit_deadline_s": 135,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    rt = RuntimeConfig(path, load_settings().assets)
    for market in ("ETH-5M", "ETH-15M"):
        cfg = rt.asset(market)
        assert cfg.enabled is True
        assert cfg.sides == (Side.DOWN,)
        assert cfg.entry_thresh == pytest.approx(0.21)
        assert cfg.tp_pct == pytest.approx(0.18)
        assert cfg.stake_usd == pytest.approx(4.0)
        assert cfg.stop_loss_pct == pytest.approx(0.2)


def test_daily_loss_cap_update_and_persist(tmp_path):
    s = load_settings()
    path = tmp_path / "runtime_config.json"
    rt = RuntimeConfig(path, s.assets, max_daily_loss_usd=20.0)
    assert rt.max_daily_loss_usd == pytest.approx(20.0)

    rt.update({"max_daily_loss_usd": 10.0})
    assert rt.max_daily_loss_usd == pytest.approx(10.0)

    reloaded = RuntimeConfig(path, s.assets, max_daily_loss_usd=20.0)
    assert reloaded.max_daily_loss_usd == pytest.approx(10.0)

    with pytest.raises(ValueError):
        rt.update({"max_daily_loss_usd": -1})

    reloaded.reset()
    assert reloaded.max_daily_loss_usd == pytest.approx(20.0)


def test_same_side_per_window_cap_update_and_persist(tmp_path):
    s = load_settings()
    path = tmp_path / "runtime_config.json"
    rt = RuntimeConfig(path, s.assets, max_same_side_per_window=2)
    assert rt.max_same_side_per_window == 2

    rt.update({"max_same_side_per_window": 1})
    assert rt.max_same_side_per_window == 1

    reloaded = RuntimeConfig(path, s.assets, max_same_side_per_window=2)
    assert reloaded.max_same_side_per_window == 1

    with pytest.raises(ValueError):
        rt.update({"max_same_side_per_window": -1})

    reloaded.reset()
    assert reloaded.max_same_side_per_window == 2


_RISK_DEFAULTS = {
    "max_open_positions": 6,
    "max_open_notional_usd": 10.0,
    "max_intraday_drawdown_usd": 16.0,
    "intraday_drawdown_pause_min": 60.0,
    "max_positions_per_window": 3,
    "max_window_notional_usd": 6.0,
    "asset_perf_lookback_days": 7.0,
    "asset_perf_min_pnl_usd": -3.0,
    "asset_perf_stake_mult": 0.5,
    "asset_perf_disable_pnl_usd": -6.0,
}


def test_new_risk_caps_update_persist_and_reset(tmp_path):
    s = load_settings()
    path = tmp_path / "runtime_config.json"
    rt = RuntimeConfig(path, s.assets, **_RISK_DEFAULTS)
    for field, value in _RISK_DEFAULTS.items():
        assert getattr(rt, field) == pytest.approx(value)

    rt.update(
        {
            "max_open_positions": 4,
            "max_open_notional_usd": 8,
            "max_intraday_drawdown_usd": 12,
            "intraday_drawdown_pause_min": 120,
            "max_positions_per_window": 2,
            "max_window_notional_usd": 5,
            "asset_perf_lookback_days": 14,
            "asset_perf_min_pnl_usd": -2,
            "asset_perf_stake_mult": 0.25,
            "asset_perf_disable_pnl_usd": -9,
        }
    )
    reloaded = RuntimeConfig(path, s.assets, **_RISK_DEFAULTS)
    assert reloaded.max_open_positions == 4
    assert reloaded.max_open_notional_usd == pytest.approx(8.0)
    assert reloaded.max_intraday_drawdown_usd == pytest.approx(12.0)
    assert reloaded.intraday_drawdown_pause_min == pytest.approx(120.0)
    assert reloaded.max_positions_per_window == 2
    assert reloaded.max_window_notional_usd == pytest.approx(5.0)
    assert reloaded.asset_perf_lookback_days == pytest.approx(14.0)
    assert reloaded.asset_perf_min_pnl_usd == pytest.approx(-2.0)
    assert reloaded.asset_perf_stake_mult == pytest.approx(0.25)
    assert reloaded.asset_perf_disable_pnl_usd == pytest.approx(-9.0)

    with pytest.raises(ValueError):
        rt.update({"asset_perf_stake_mult": 1.5})
    with pytest.raises(ValueError):
        rt.update({"max_positions_per_window": 1.5})
    with pytest.raises(ValueError):
        rt.update({"max_open_positions": 1.5})
    with pytest.raises(ValueError):
        rt.update({"max_open_notional_usd": -1})
    with pytest.raises(ValueError):
        rt.update({"max_window_notional_usd": -1})
    with pytest.raises(ValueError):
        rt.update({"intraday_drawdown_pause_min": -1})

    reloaded.reset()
    for field, value in _RISK_DEFAULTS.items():
        assert getattr(reloaded, field) == pytest.approx(value)


def test_new_risk_caps_default_to_none_without_startup_values(tmp_path):
    """None means 'use config.toml' rather than 'cap disabled'."""
    rt = _runtime(tmp_path)
    for field in _RISK_DEFAULTS:
        assert getattr(rt, field) is None


def test_market_modes_persist_and_reset(tmp_path):
    s = load_settings()
    path = tmp_path / "runtime_config.json"
    rt = RuntimeConfig(path, s.assets, live_allowed=True)
    assert rt.mode_for("ETH-5M") == "paper"
    assert rt.mode_for("ETH-15M") == "paper"

    rt.set_market_mode("ETH-15M", "live")
    assert rt.mode_for("ETH-15M") == "live"
    assert rt.mode_for("ETH-5M") == "paper"

    reloaded = RuntimeConfig(path, s.assets, live_allowed=True)
    assert reloaded.mode_for("ETH-15M") == "live"
    assert reloaded.mode_for("ETH-5M") == "paper"

    # Resetting strategy/risk parameters keeps the operator's paper/live
    # choices (the global switch clears overrides instead).
    reloaded.reset()
    assert reloaded.mode_for("ETH-15M") == "live"
    assert reloaded.mode_for("ETH-5M") == "paper"


def test_market_mode_live_gate_and_global_switch_clears_overrides(tmp_path):
    s = load_settings()
    path = tmp_path / "runtime_config.json"
    blocked = RuntimeConfig(path, s.assets, live_allowed=False)
    with pytest.raises(ValueError):
        blocked.set_market_mode("ETH-15M", "live")
    with pytest.raises(ValueError):
        blocked.set_market_mode("ETH-15M", "bogus")
    with pytest.raises(ValueError):
        blocked.set_market_mode("NOPE", "paper")

    rt = RuntimeConfig(path, s.assets, live_allowed=True)
    rt.set_market_mode("ETH-15M", "live")
    assert rt.mode_for("ETH-15M") == "live"
    rt.set_mode("paper")
    assert rt.mode_for("ETH-15M") == "paper"
    assert rt.market_modes == {}


def test_mode_default_persisted_and_live_gate(tmp_path):
    s = load_settings()
    path = tmp_path / "runtime_config.json"
    rt = RuntimeConfig(path, s.assets, default_mode="paper",
                       live_allowed=True)
    assert rt.mode == "paper"
    assert rt.set_mode("live") == "live"

    reloaded = RuntimeConfig(path, s.assets, default_mode="paper",
                             live_allowed=True)
    assert reloaded.mode == "live"

    # A persisted live mode is ignored when .env does not allow live.
    blocked = RuntimeConfig(path, s.assets, default_mode="paper",
                            live_allowed=False)
    assert blocked.mode == "paper"
    with pytest.raises(ValueError):
        blocked.set_mode("live")


def test_partial_update_after_reload_keeps_enum_sides(tmp_path):
    """A patch without 'sides' must not reparse tuple[Side] as strings."""
    rt = _runtime(tmp_path)
    rt.update(
        {
            "assets": {
                "ETH-5M": {
                    "sides": "DOWN",
                    "entry_thresh": 0.20,
                    "tp_pct": 0.30,
                }
            }
        }
    )
    rt.update({"assets": {"ETH-5M": {"stop_loss_pct": 0.10}}})
    eth = rt.asset("ETH-5M")
    assert eth.sides == (Side.DOWN,)
    assert eth.entry_thresh == pytest.approx(0.20)
    assert eth.stop_loss_pct == pytest.approx(0.10)

    reloaded = RuntimeConfig(
        tmp_path / "runtime_config.json", load_settings().assets
    )
    reloaded.update({"assets": {"ETH-5M": {"tp_pct": 0.25}}})
    assert reloaded.asset("ETH-5M").sides == (Side.DOWN,)


def test_partial_update_preserves_other_fields(tmp_path):
    rt = _runtime(tmp_path)
    rt.update(
        {
            "assets": {
                "ETH-5M": {
                    "enabled": True,
                    "entry_thresh": 0.20,
                    "tp_pct": 0.30,
                    "stake_usd": 5.0,
                }
            }
        }
    )
    rt.update({"assets": {"ETH-5M": {"enabled": False}}})
    eth = rt.asset("ETH-5M")
    assert eth.enabled is False
    assert eth.entry_thresh == pytest.approx(0.20)
    assert eth.tp_pct == pytest.approx(0.30)
    assert eth.stake_usd == pytest.approx(5.0)


def test_reset_returns_to_baseline(tmp_path):
    rt = _runtime(tmp_path)
    rt.update({"assets": {"ETH-15M": {"enabled": True}}})
    assert rt.asset("ETH-15M").enabled is True
    rt.reset()
    assert rt.asset("ETH-15M").enabled is False
    assert rt.asset("ETH-5M").enabled is True
    assert rt.asset("BTC-5M").enabled is False
    assert rt.asset("BTC-15M").enabled is False
    assert not (tmp_path / "runtime_config.json").exists()


def test_invalid_values_rejected(tmp_path):
    rt = _runtime(tmp_path)
    with pytest.raises(ValueError):
        rt.update({"assets": {"BTC-5M": {"entry_thresh": 1.2}}})
    with pytest.raises(ValueError):
        rt.update({"assets": {"BTC-15M": {"tp_pct": -0.1}}})
    with pytest.raises(ValueError):
        rt.update(
            {"assets": {"BTC-5M": {"entry_thresh": 0.9, "tp_pct": 0.5}}}
        )
    with pytest.raises(ValueError):
        rt.update({"assets": {"NOPE": {"enabled": True}}})
