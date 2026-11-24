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

    paper.holdings["t"] = 5.0
    broker.set_mode("paper")
    assert paper.holdings == {}


@pytest.mark.asyncio
async def test_mode_broker_refuses_live_without_broker():
    broker = ModeBroker(_FakeBroker(), None, mode="paper")
    assert broker.live_available is False
    assert broker.mode == "paper"
    with pytest.raises(RuntimeError):
        broker.set_mode("live")
    with pytest.raises(ValueError):
        broker.set_mode("bogus")
