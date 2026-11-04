from __future__ import annotations

import pytest

from src.config import load_settings
from src.models import Side
from src.runtime_config import RuntimeConfig


def _runtime(tmp_path) -> RuntimeConfig:
    s = load_settings()
    return RuntimeConfig(tmp_path / "runtime_config.json", s.assets)


def test_defaults_only_btc_enabled(tmp_path):
    rt = _runtime(tmp_path)
    assert rt.asset("BTC").enabled is True
    assert rt.asset("ETH").enabled is False
    assert rt.asset("BTC").stop_loss_pct == pytest.approx(0.20)
    assert rt.asset("ETH").stop_loss_pct == pytest.approx(0.0)
    assert rt.asset("XRP").sides == (Side.UP, Side.DOWN)
    assert rt.asset("XRP").entry_thresh == pytest.approx(0.10)


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

    reloaded = RuntimeConfig(tmp_path / "runtime_config.json", load_settings().assets)
    assert reloaded.asset("ETH").enabled is True
    assert reloaded.asset("ETH").sides == (Side.UP, Side.DOWN)
    assert reloaded.version >= 1


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
    rt.update({"assets": {"ETH": {"enabled": True}}})
    assert rt.asset("ETH").enabled is True
    rt.reset()
    assert rt.asset("ETH").enabled is False
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
