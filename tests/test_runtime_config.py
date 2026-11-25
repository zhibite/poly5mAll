from __future__ import annotations

import pytest

from src.config import load_settings
from src.models import Side
from src.runtime_config import RuntimeConfig


def _runtime(tmp_path) -> RuntimeConfig:
    s = load_settings()
    return RuntimeConfig(tmp_path / "runtime_config.json", s.assets)


def test_defaults_six_enabled_btc_off(tmp_path):
    rt = _runtime(tmp_path)
    assert rt.asset("BTC").enabled is False
    assert rt.asset("ETH").enabled is True
    assert rt.asset("BTC").stop_loss_pct == pytest.approx(0.0)
    assert rt.asset("ETH").stop_loss_pct == pytest.approx(0.0)
    assert rt.asset("XRP").sides == (Side.UP, Side.DOWN)
    assert rt.asset("XRP").entry_thresh == pytest.approx(0.10)
    assert rt.asset("ETH").force_exit_s == pytest.approx(240.0)
    assert rt.asset("ETH").force_exit_deadline_s == pytest.approx(295.0)


def test_update_enable_eth_and_persist(tmp_path):
    rt = _runtime(tmp_path)
    rt.update(
        {
            "assets": {
                "ETH": {
                    "enabled": True,
                    "sides": "BOTH",
                    "entry_thresh": 0.20,
                    "tp_pct": 0.30,
                    "stop_loss_pct": 0.15,
                    "stake_usd": 5.0,
                    "force_exit_s": 120.0,
                    "force_exit_deadline_s": 135.0,
                }
            }
        }
    )
    eth = rt.asset("ETH")
    assert eth.enabled is True
    assert eth.sides == (Side.UP, Side.DOWN)
    assert eth.entry_thresh == pytest.approx(0.20)
    assert eth.tp_pct == pytest.approx(0.30)
    assert eth.stop_loss_pct == pytest.approx(0.15)
    assert eth.stake_usd == pytest.approx(5.0)
    assert eth.force_exit_s == pytest.approx(120.0)
    assert eth.force_exit_deadline_s == pytest.approx(135.0)

    reloaded = RuntimeConfig(tmp_path / "runtime_config.json", load_settings().assets)
    assert reloaded.asset("ETH").enabled is True
    assert reloaded.asset("ETH").sides == (Side.UP, Side.DOWN)
    assert reloaded.asset("ETH").force_exit_s == pytest.approx(120.0)
    assert reloaded.asset("ETH").force_exit_deadline_s == pytest.approx(135.0)
    assert reloaded.version >= 1


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
    "max_intraday_drawdown_usd": 8.0,
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


def test_invalid_force_exit_rejected(tmp_path):
    rt = _runtime(tmp_path)
    with pytest.raises(ValueError):
        rt.update({"assets": {"ETH": {"force_exit_s": 30}}})
    with pytest.raises(ValueError):
        rt.update(
            {
                "assets": {
                    "ETH": {
                        "force_exit_s": 200,
                        "force_exit_deadline_s": 150,
                    }
                }
            }
        )


def test_partial_update_preserves_other_fields(tmp_path):
    rt = _runtime(tmp_path)
    rt.update(
        {
            "assets": {
                "ETH": {
                    "enabled": True,
                    "entry_thresh": 0.20,
                    "tp_pct": 0.30,
                    "stake_usd": 5.0,
                }
            }
        }
    )
    rt.update({"assets": {"ETH": {"enabled": False}}})
    eth = rt.asset("ETH")
    assert eth.enabled is False
    assert eth.entry_thresh == pytest.approx(0.20)
    assert eth.tp_pct == pytest.approx(0.30)
    assert eth.stake_usd == pytest.approx(5.0)


def test_reset_returns_to_baseline(tmp_path):
    rt = _runtime(tmp_path)
    rt.update({"assets": {"ETH": {"enabled": False}}})
    assert rt.asset("ETH").enabled is False
    rt.reset()
    assert rt.asset("ETH").enabled is True
    assert rt.asset("BTC").enabled is False
    assert not (tmp_path / "runtime_config.json").exists()


def test_invalid_values_rejected(tmp_path):
    rt = _runtime(tmp_path)
    with pytest.raises(ValueError):
        rt.update({"assets": {"BTC": {"entry_thresh": 1.2}}})
    with pytest.raises(ValueError):
        rt.update({"assets": {"BTC": {"tp_pct": -0.1}}})
    with pytest.raises(ValueError):
        rt.update({"assets": {"BTC": {"entry_thresh": 0.9, "tp_pct": 0.5}}})
    with pytest.raises(ValueError):
        rt.update({"assets": {"NOPE": {"enabled": True}}})
