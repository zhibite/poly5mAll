from __future__ import annotations

import pytest

from src.execution.broker import ModeBroker


class _FakeBroker:
    mode = "paper"

    def __init__(self) -> None:
        self.holdings: dict[str, float] = {}
        self.calls: list[str] = []

    async def buy(self, token_id, usd_amount, limit_price, tick_size,
                  fee_rate=None, *, position_id=""):
        self.calls.append("buy")
        return None

    async def sell(self, token_id, shares, limit_price, tick_size,
                   fee_rate=None, *, position_id=""):
        self.calls.append("sell")
        return None

    async def token_balance(self, token_id):
        return 0.0


@pytest.mark.asyncio
async def test_mode_broker_delegates_and_switches():
    paper, live = _FakeBroker(), _FakeBroker()
    broker = ModeBroker(paper, live, mode="paper")

    await broker.buy("t", 1.0, 0.1, 0.01)
    assert paper.calls == ["buy"] and live.calls == []

    assert broker.set_mode("live") == "live"
    await broker.buy("t", 1.0, 0.1, 0.01)
    assert live.calls == ["buy"]

    # The paper ledger is per-position now; a global switch must not wipe
    # holdings that belong to still-open paper markets.
    paper.holdings["t"] = 5.0
    broker.set_mode("paper")
    assert paper.holdings == {"t": 5.0}


@pytest.mark.asyncio
async def test_mode_broker_routes_per_market():
    paper, live = _FakeBroker(), _FakeBroker()
    modes = {"ETH-15M": "live"}
    broker = ModeBroker(
        paper,
        live,
        mode="paper",
        mode_for=lambda market: modes.get(market, "paper"),
    )
    assert broker.mode_for("ETH-15M") == "live"
    assert broker.mode_for("ETH-5M") == "paper"

    w15 = broker.wrap("ETH-15M")
    w5 = broker.wrap("ETH-5M")
    await w15.buy("t", 1.0, 0.1, 0.01)
    await w5.buy("t", 1.0, 0.1, 0.01)
    assert live.calls == ["buy"]
    assert paper.calls == ["buy"]

    # An exiting position carries its own venue: a live market can have a
    # paper position restored from an earlier paper run.
    await w15.sell("t", 1.0, 0.1, 0.01, mode="paper")
    assert paper.calls == ["buy", "sell"]
    assert live.calls == ["buy"]

    modes["ETH-15M"] = "paper"
    assert w15.mode == "paper"
    await w15.buy("t", 1.0, 0.1, 0.01)
    assert paper.calls == ["buy", "sell", "buy"]


@pytest.mark.asyncio
async def test_mode_broker_refuses_live_without_broker():
    broker = ModeBroker(
        _FakeBroker(), None, mode="paper", mode_for=lambda m: "live"
    )
    assert broker.live_available is False
    assert broker.mode == "paper"
    # A per-market live override falls back to paper when no live broker
    # exists, instead of sending orders nowhere.
    assert broker.mode_for("ETH-15M") == "paper"
    # An explicit live exit (a restored live position) must not be paper-
    # booked when the live broker is missing.
    with pytest.raises(RuntimeError):
        await broker.sell("t", 1.0, 0.1, 0.01, mode="live")
    with pytest.raises(RuntimeError):
        broker.set_mode("live")
    with pytest.raises(ValueError):
        broker.set_mode("bogus")
