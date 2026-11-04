"""Paper and live brokers.

Both expose the same three operations the strategy needs: FAK-like BUY,
FAK-like SELL, and a token-balance query for restart reconciliation.

``PaperBroker`` fills against the live CLOB order book (walking the ladder
up to the limit), falls back to the WS top-of-book when the REST snapshot
is unavailable, and charges the same taker fee as a live order.  This is
deliberately conservative: a paper order that could not have filled on the
book is not counted as a fill.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Protocol

from src.economics import taker_fee
from src.infra.logging import get_logger
from src.market.market_ws import PriceCache
from src.models import Fill

log = get_logger("broker")


class Broker(Protocol):
    mode: str

    async def buy(
        self, token_id: str, usd_amount: float, limit_price: float, tick_size: float
    ) -> Fill | None: ...

    async def sell(
        self, token_id: str, shares: float, limit_price: float, tick_size: float
    ) -> Fill | None: ...

    async def token_balance(self, token_id: str) -> float | None: ...


# ---------------------------------------------------------------------------
# Book walking
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Walk:
    shares: float
    notional: float

    @property
    def avg_price(self) -> float:
        return self.notional / self.shares if self.shares > 0 else 0.0


def parse_levels(rows: Any) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            price = float(row.get("price", 0))
            size = float(row.get("size", 0))
        except (TypeError, ValueError):
            continue
        if 0.0 < price < 1.0 and size > 0:
            out.append((price, size))
    return out


def walk_budget(
    asks: list[tuple[float, float]], budget: float, max_price: float
) -> Walk:
    """Walk asks spending at most ``budget`` USDC up to ``max_price``."""
    remaining = max(0.0, float(budget))
    shares = 0.0
    notional = 0.0
    for price, size in sorted(asks, key=lambda x: x[0]):
        if price > max_price + 1e-12:
            break
        take = min(size, remaining / price)
        if take <= 0:
            continue
        shares += take
        notional += take * price
        remaining -= take * price
        if remaining <= 1e-9:
            break
    return Walk(shares=shares, notional=notional)


def walk_shares(
    bids: list[tuple[float, float]], shares_wanted: float, min_price: float
) -> Walk:
    """Walk bids selling at most ``shares_wanted`` down to ``min_price``."""
    remaining = max(0.0, float(shares_wanted))
    shares = 0.0
    notional = 0.0
    for price, size in sorted(bids, key=lambda x: x[0], reverse=True):
        if price < min_price - 1e-12:
            break
        take = min(size, remaining)
        if take <= 0:
            continue
        shares += take
        notional += take * price
        remaining -= take
        if remaining <= 1e-9:
            break
    return Walk(shares=shares, notional=notional)


# ---------------------------------------------------------------------------
# Paper
# ---------------------------------------------------------------------------


class PaperBroker:
    mode = "paper"

    def __init__(
        self,
        pm: Any,
        price_cache: PriceCache,
        *,
        fee_rate: float = 0.07,
        include_fees: bool = True,
        tick_size: float = 0.01,
    ) -> None:
        self._pm = pm
        self._cache = price_cache
        self.fee_rate = float(fee_rate)
        self.include_fees = bool(include_fees)
        self.default_tick = float(tick_size)
        self.holdings: dict[str, float] = {}
        self.fills = 0
        self.last_error = ""

    # ── broker API ─────────────────────────────────────────────────────
    async def buy(
        self, token_id: str, usd_amount: float, limit_price: float, tick_size: float
    ) -> Fill | None:
        asks = await self._asks(token_id, limit_price, tick_size)
        if not asks:
            return None
        walk = walk_budget(asks, usd_amount, limit_price)
        if walk.shares <= 0 or walk.notional <= 0:
            return None
        avg = walk.avg_price
        fee = (
            taker_fee(walk.shares, avg, self.fee_rate)
            if self.include_fees
            else 0.0
        )
        self.holdings[token_id] = self.holdings.get(token_id, 0.0) + walk.shares
        self.fills += 1
        return Fill(
            token_id=token_id,
            side="BUY",
            shares=walk.shares,
            usdc=walk.notional,
            price=avg,
            fee=fee,
            ts=time.time(),
            note="paper-book",
        )

    async def sell(
        self, token_id: str, shares: float, limit_price: float, tick_size: float
    ) -> Fill | None:
        bids = await self._bids(token_id, limit_price, tick_size)
        if not bids:
            return None
        walk = walk_shares(bids, shares, limit_price)
        if walk.shares <= 0 or walk.notional <= 0:
            return None
        avg = walk.avg_price
        fee = (
            taker_fee(walk.shares, avg, self.fee_rate)
            if self.include_fees
            else 0.0
        )
        self.holdings[token_id] = max(
            0.0, self.holdings.get(token_id, 0.0) - walk.shares
        )
        self.fills += 1
        return Fill(
            token_id=token_id,
            side="SELL",
            shares=walk.shares,
            usdc=walk.notional,
            price=avg,
            fee=fee,
            ts=time.time(),
            note="paper-book",
        )

    async def token_balance(self, token_id: str) -> float | None:
        return self.holdings.get(token_id, 0.0)

    def adopt(self, token_id: str, shares: float) -> None:
        """Re-register a position restored from the state file."""
        if shares > 0:
            self.holdings[token_id] = self.holdings.get(token_id, 0.0) + shares

    # ── book access ────────────────────────────────────────────────────
    async def _asks(
        self, token_id: str, limit_price: float, tick_size: float
    ) -> list[tuple[float, float]]:
        book = await self._pm.get_order_book(token_id)
        if isinstance(book, dict):
            asks = parse_levels(book.get("asks"))
            if asks:
                return asks
        # Fallback: the WS top-of-book, if it is on the executable side.
        quote = self._cache.quote(token_id)
        if quote is not None and quote.ask > 0 and quote.ask <= limit_price + 1e-12:
            return [(quote.ask, float("inf"))]
        return []

    async def _bids(
        self, token_id: str, limit_price: float, tick_size: float
    ) -> list[tuple[float, float]]:
        book = await self._pm.get_order_book(token_id)
        if isinstance(book, dict):
            bids = parse_levels(book.get("bids"))
            if bids:
                return bids
        quote = self._cache.quote(token_id)
        if quote is not None and quote.bid > 0 and quote.bid >= limit_price - 1e-12:
            return [(quote.bid, float("inf"))]
        return []


# ---------------------------------------------------------------------------
# Live
# ---------------------------------------------------------------------------


class LiveBroker:
    mode = "live"

    def __init__(self, clob: Any) -> None:
        if not clob.is_configured:
            raise RuntimeError(f"live broker unavailable: {clob.init_error}")
        self._clob = clob

    async def buy(
        self, token_id: str, usd_amount: float, limit_price: float, tick_size: float
    ) -> Fill | None:
        return await self._clob.place_fak_buy(
            token_id, usd_amount, limit_price, tick_size
        )

    async def sell(
        self, token_id: str, shares: float, limit_price: float, tick_size: float
    ) -> Fill | None:
        return await self._clob.place_fak_sell(
            token_id, shares, limit_price, tick_size
        )

    async def token_balance(self, token_id: str) -> float | None:
        return await self._clob.get_token_balance(token_id)

    async def collateral(self) -> float | None:
        """USDC (pUSD) balance, for a startup sanity check."""
        return await self._clob.get_collateral()
