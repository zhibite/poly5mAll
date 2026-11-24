"""Live order routing: v2 markets sign against Exchange V3 via position ids.

Gamma's newer 5m markets (SOL/XRP/DOGE/BNB/HYPE) expose ``positionIds`` and
reject orders signed off the clob token id.  The SDK switches to Exchange V3
only when ``MarketOrderArgs`` carries a position id, so these tests pin both
sides of the branch: v2 markets must reach the SDK as ``position_id`` with no
``token_id``, legacy markets (BTC/ETH) must keep the token-id path.
"""
from __future__ import annotations

import pytest

from src.execution.broker import LiveBroker
from src.execution.clob_client import CLOBClient
from src.market.pm_client import PolymarketClient
from src.market.service import MarketService
from src.models import Fill, MarketWindow, Side

T0 = 1_800_000_000


class FakeSdk:
    """Stands in for ``py_clob_client_v2.ClobClient``."""

    def __init__(self) -> None:
        self.market_orders: list[tuple] = []

    def create_and_post_market_order(self, args, **kwargs):
        self.market_orders.append((args, kwargs))
        return {"orderID": "0xbeef", "takingAmount": "20", "makingAmount": "5"}


def _sdk_client() -> tuple[CLOBClient, FakeSdk]:
    # Bypass __init__ so the test needs neither credentials nor network.
    client = object.__new__(CLOBClient)
    client.fee_rate = 0.01
    client.include_fees = True
    client.fill_callback = None
    fake = FakeSdk()
    client._client = fake
    return client, fake


class FakeClob:
    is_configured = True

    def __init__(self) -> None:
        self.buys: list[dict] = []
        self.sells: list[dict] = []

    async def place_fak_buy(
        self,
        token_id,
        amount,
        limit_price,
        tick_size,
        fee_rate=None,
        *,
        position_id="",
    ):
        self.buys.append({"token_id": token_id, "position_id": position_id})
        return Fill(token_id, "BUY", 20.0, 5.0, 0.25, 0.0, T0)

    async def place_fak_sell(
        self,
        token_id,
        shares,
        limit_price,
        tick_size,
        fee_rate=None,
        *,
        position_id="",
    ):
        self.sells.append({"token_id": token_id, "position_id": position_id})
        return Fill(token_id, "SELL", shares, shares * limit_price,
                    limit_price, 0.0, T0 + 60)


# ---------------------------------------------------------------------------
# CLOB client: exactly one of token_id / position_id reaches the SDK
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clob_buy_signs_v2_market_with_position_id():
    client, fake = _sdk_client()
    fill = await client.place_fak_buy(
        "token-up", 5.0, 0.24, 0.01, position_id="pos-up"
    )

    assert fill is not None
    args, _kwargs = fake.market_orders[0]
    assert args.position_id == "pos-up"
    assert args.token_id is None


@pytest.mark.asyncio
async def test_clob_buy_keeps_token_id_for_legacy_market():
    client, fake = _sdk_client()
    await client.place_fak_buy("token-up", 5.0, 0.24, 0.01)

    args, _kwargs = fake.market_orders[0]
    assert args.token_id == "token-up"
    assert args.position_id is None


@pytest.mark.asyncio
async def test_clob_sell_signs_v2_market_with_position_id():
    client, fake = _sdk_client()
    fill = await client.place_fak_sell(
        "token-down", 20.0, 0.25, 0.01, position_id="pos-down"
    )

    assert fill is not None
    args, _kwargs = fake.market_orders[0]
    assert args.position_id == "pos-down"
    assert args.token_id is None


@pytest.mark.asyncio
async def test_clob_sell_keeps_token_id_for_legacy_market():
    client, fake = _sdk_client()
    await client.place_fak_sell("token-down", 20.0, 0.25, 0.01)

    args, _kwargs = fake.market_orders[0]
    assert args.token_id == "token-down"
    assert args.position_id is None


# ---------------------------------------------------------------------------
# Live broker forwards the position id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_broker_forwards_position_id():
    clob = FakeClob()
    broker = LiveBroker(clob, require_two_sided_book=False)

    await broker.buy(
        "token-up", 5.0, 0.24, 0.01, position_id="pos-up"
    )
    await broker.sell(
        "token-down", 20.0, 0.25, 0.01, position_id="pos-down"
    )

    assert clob.buys == [{"token_id": "token-up", "position_id": "pos-up"}]
    assert clob.sells == [{"token_id": "token-down", "position_id": "pos-down"}]


# ---------------------------------------------------------------------------
# Discovery: positionIds are parallel to outcomes
# ---------------------------------------------------------------------------


def test_discovery_maps_position_ids_by_outcome():
    slug = f"sol-updown-5m-{T0}"
    market = {
        # Down first: the helpers must re-order by outcome label, not index.
        "outcomes": '["Down", "Up"]',
        "clobTokenIds": '["tok-down", "tok-up"]',
        "positionIds": '["pos-down", "pos-up"]',
        "conditionId": "cond",
    }
    found = PolymarketClient._parse_pages(
        [slug], [T0], [[{"slug": slug, "markets": [market]}]], 5
    )

    assert found[0]["tokens"] == [
        {"token_id": "tok-up", "outcome": "Up", "position_id": "pos-up"},
        {"token_id": "tok-down", "outcome": "Down", "position_id": "pos-down"},
    ]


def test_discovery_keeps_legacy_market_without_position_ids():
    slug = f"btc-updown-5m-{T0}"
    market = {
        "outcomes": '["Up", "Down"]',
        "clobTokenIds": '["tok-up", "tok-down"]',
    }
    found = PolymarketClient._parse_pages(
        [slug], [T0], [[{"slug": slug, "markets": [market]}]], 5
    )

    assert [t["position_id"] for t in found[0]["tokens"]] == ["", ""]


class FakePM:
    def __init__(self, markets):
        self.markets = markets

    async def get_asset_updown_markets(self, asset, **kwargs):
        return list(self.markets)


@pytest.mark.asyncio
async def test_market_service_propagates_position_ids():
    market = {
        "tokens": [
            {"token_id": "tok-up", "outcome": "Up", "position_id": "pos-up"},
            {"token_id": "tok-down", "outcome": "Down", "position_id": "pos-down"},
        ],
        "_event_slug": f"sol-updown-5m-{T0}",
        "_window_start_ts": T0,
        "conditionId": "cond",
    }
    service = MarketService(
        FakePM([market]),
        window_sec=300,
        pre_discovery_lead_s=60,
        min_discovery_interval_s=0,
    )
    await service.refresh("SOL", T0 + 10)
    window = service.current("SOL", T0 + 10)

    assert window is not None
    assert window.position(Side.UP) == "pos-up"
    assert window.position(Side.DOWN) == "pos-down"
    assert window.token(Side.UP) == "tok-up"


def test_window_position_falls_back_to_empty_on_legacy_markets():
    window = MarketWindow(
        asset="BTC",
        slug="s",
        start_ts=T0,
        end_ts=T0 + 300,
        up_token="tok-up",
        down_token="tok-down",
    )
    assert window.position(Side.UP) == ""
    assert window.position(Side.DOWN) == ""
