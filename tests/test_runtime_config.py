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
