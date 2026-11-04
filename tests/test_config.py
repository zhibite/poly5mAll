from __future__ import annotations

import dataclasses

import pytest

from src.config import ConfigError, load_settings, validate_settings
from src.models import Side


def test_doc_defaults_load():
    s = load_settings()
    by_name = {a.asset: a for a in s.assets}
    assert set(by_name) == {"BTC", "ETH", "BNB", "SOL", "DOGE", "XRP", "HYPE"}
    assert by_name["BTC"].sides == (Side.DOWN,)
    assert by_name["BTC"].entry_thresh == pytest.approx(0.25)
    assert by_name["BTC"].tp_pct == pytest.approx(0.10)
    assert by_name["ETH"].entry_thresh == pytest.approx(0.18)
    assert by_name["XRP"].sides == (Side.UP,)
    assert by_name["XRP"].entry_thresh == pytest.approx(0.10)
    assert by_name["HYPE"].entry_thresh == pytest.approx(0.30)
    # Default deployment only enables BTC; the Web config page turns others on.
    assert by_name["BTC"].enabled is True
    assert all(not by_name[n].enabled for n in by_name if n != "BTC")
    assert [p.asset for p in s.plans()] == ["BTC"]
    assert s.execution.paper_trading is True
    assert s.mode == "paper"
    assert s.web.port == 8033
    assert s.web.enabled is True


def test_defaults_without_config_file(tmp_path):
    # Even with no config.toml, the safe default is BTC-only.
    s = load_settings(tmp_path / "missing.toml")
    assert [a.asset for a in s.assets if a.enabled] == ["BTC"]


def test_invalid_threshold_rejected():
    s = load_settings()
    bad_asset = dataclasses.replace(s.assets[0], entry_thresh=1.2)
    bad = dataclasses.replace(s, assets=(bad_asset,))
    with pytest.raises(ConfigError):
        validate_settings(bad)


def test_fee_negative_plans_reported():
    s = load_settings()
    keys = {p.key for p in s.all_plans()}
    assert "XRP-UP" in keys
    # The doc's BTC 0.25/10% TP does not cover the 0.07 round-trip fee.
    from src.economics import fee_line

    btc = next(p for p in s.all_plans() if p.key == "BTC-DOWN")
    assert fee_line(btc).net_per_trade < 0
