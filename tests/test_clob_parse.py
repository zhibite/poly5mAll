from __future__ import annotations

import pytest

from src.execution.clob_client import CLOBClient


def _client() -> CLOBClient:
    # Bypass __init__ so the test needs neither credentials nor network.
    c = object.__new__(CLOBClient)
    c.fee_rate = 0.07
    c.include_fees = True
    c.fill_callback = None
    return c


def test_parse_fak_buy_aggregates():
    fill = _client()._parse_fill(
        "tok",
        "BUY",
        {"takingAmount": "41.6667", "makingAmount": "10.00", "orderID": "abc"},
        0.24,
        0.01,
    )
    assert fill is not None
    assert fill.shares == pytest.approx(41.6667)
    assert fill.usdc == pytest.approx(10.0)
    assert fill.price == pytest.approx(0.24, rel=1e-3)
    assert fill.fee > 0
    assert fill.order_id == "abc"


def test_parse_fak_sell_aggregates():
    fill = _client()._parse_fill(
        "tok",
        "SELL",
        {"makingAmount": "41.6667", "takingAmount": "11.25"},
        0.26,
        0.01,
    )
    assert fill is not None
    assert fill.shares == pytest.approx(41.6667)
    assert fill.usdc == pytest.approx(11.25)
    assert fill.price == pytest.approx(0.27, rel=1e-3)


def test_parse_base_units_and_fills_fallback():
    c = _client()
    fill = c._parse_fill(
        "tok",
        "BUY",
        {"fills": [{"size": "41666666", "price": "0.24"}]},
        0.24,
        0.01,
    )
    assert fill is not None
    assert fill.shares == pytest.approx(41.666666, rel=1e-5)
    assert fill.usdc == pytest.approx(10.0, rel=1e-4)


def test_parse_empty_response_returns_none():
    assert _client()._parse_fill("tok", "BUY", {}, 0.24, 0.01) is None
