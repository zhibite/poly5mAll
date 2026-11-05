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
        require_two_sided_book: bool = True,
        min_entry_bid_depth_usd: float = 5.0,
    ) -> None:
        self._pm = pm
        self._cache = price_cache
        self.fee_rate = float(fee_rate)
        self.include_fees = bool(include_fees)
        self.default_tick = float(tick_size)
        self.require_two_sided_book = bool(require_two_sided_book)
        self.min_entry_bid_depth_usd = float(min_entry_bid_depth_usd)
        self.holdings: dict[str, float] = {}
        self.fills = 0
        self.last_error = ""
        self._last_no_fill_log: dict[str, float] = {}

    # ── broker API ─────────────────────────────────────────────────────
    async def buy(
        self, token_id: str, usd_amount: float, limit_price: float, tick_size: float
    ) -> Fill | None:
        # The entry gate needs a two-sided REST book.  Do not fall back to
        # the WS top-of-book here: its depth is unknown, which is exactly
        # how the strategy ended up in tokens it could not exit.
        bids, asks = await self._book(
            token_id, allow_bba_fallback=not self.require_two_sided_book
        )
        if self.require_two_sided_book:
            if not bids or not asks:
                self._log_no_fill(
                    token_id, "buy", "book not two-sided (no bid or no ask)"
                )
                return None
            best_bid = max((p for p, _ in bids), default=0.0)
            bid_depth_usd = sum(p * s for p, s in bids)
            if best_bid <= 0:
                self._log_no_fill(token_id, "buy", "best_bid <= 0")
                return None
            if bid_depth_usd < self.min_entry_bid_depth_usd:
                self._log_no_fill(
                    token_id,
                    "buy",
                    f"bid depth {bid_depth_usd:.2f}U < "
                    f"{self.min_entry_bid_depth_usd:.2f}U",
                )
                return None
        if not asks:
            self._log_no_fill(
                token_id, "buy", "no asks (book empty / WS unavailable)"
            )
            return None
        walk = walk_budget(asks, usd_amount, limit_price)
        if walk.shares <= 0 or walk.notional <= 0:
            best = min((p for p, _ in asks), default=0.0)
            self._log_no_fill(
                token_id,
                "buy",
                f"best_ask={best:.4f} > limit={limit_price:.4f}",
            )
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
        # Exits may use the WS top-of-book fallback; refusing to exit is
        # worse than exiting on a single fresh bid.
        bids, _ = await self._book(token_id, allow_bba_fallback=True)
        if not bids:
            self._log_no_fill(
                token_id, "sell", "no bids (book empty / WS unavailable)"
            )
            return None
        walk = walk_shares(bids, shares, limit_price)
        if walk.shares <= 0 or walk.notional <= 0:
            best = max((p for p, _ in bids), default=0.0)
            self._log_no_fill(
                token_id,
                "sell",
                f"best_bid={best:.4f} < limit={limit_price:.4f}",
            )
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

    def _log_no_fill(self, token_id: str, side: str, reason: str) -> None:
        now = time.time()
        last = self._last_no_fill_log.get(token_id, 0.0)
        if now - last < 5.0:
            return
        self._last_no_fill_log[token_id] = now
        log.info(
            "paper.{}_no_fill token={} reason={}",
            side,
            token_id[:12],
            reason,
        )

    # ── book access ────────────────────────────────────────────────────
    async def _book(
        self, token_id: str, *, allow_bba_fallback: bool
    ) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
        book = await self._pm.get_order_book(token_id)
        if isinstance(book, dict):
            bids = parse_levels(book.get("bids"))
            asks = parse_levels(book.get("asks"))
        else:
            bids, asks = [], []
        if allow_bba_fallback:
            quote = self._cache.quote(token_id)
            if quote is not None:
                if not bids and quote.bid > 0:
                    bids = [(quote.bid, float("inf"))]
                if not asks and quote.ask > 0:
                    asks = [(quote.ask, float("inf"))]
        return bids, asks


# ---------------------------------------------------------------------------
# Live
# ---------------------------------------------------------------------------


class LiveBroker:
    mode = "live"

    def __init__(
        self,
        clob: Any,
        pm: Any = None,
        *,
        require_two_sided_book: bool = True,
        min_entry_bid_depth_usd: float = 5.0,
    ) -> None:
        if not clob.is_configured:
            raise RuntimeError(f"live broker unavailable: {clob.init_error}")
        self._clob = clob
        self._pm = pm
        self.require_two_sided_book = bool(require_two_sided_book)
        self.min_entry_bid_depth_usd = float(min_entry_bid_depth_usd)
        self._last_no_fill_log: dict[str, float] = {}

    async def buy(
        self, token_id: str, usd_amount: float, limit_price: float, tick_size: float
    ) -> Fill | None:
        if self.require_two_sided_book:
            if self._pm is None:
                log.warning("live.buy_no_fill reason=no_pm_for_liquidity_check")
                return None
            book = await self._pm.get_order_book(token_id)
            bids = parse_levels(book.get("bids")) if isinstance(book, dict) else []
            asks = parse_levels(book.get("asks")) if isinstance(book, dict) else []
            if not bids or not asks:
                self._log_no_fill(token_id, "book not two-sided")
                return None
            depth = sum(p * s for p, s in bids)
            if depth < self.min_entry_bid_depth_usd:
                self._log_no_fill(
                    token_id,
                    f"bid depth {depth:.2f}U < "
                    f"{self.min_entry_bid_depth_usd:.2f}U",
                )
                return None
        return await self._clob.place_fak_buy(
            token_id, usd_amount, limit_price, tick_size
        )

    def _log_no_fill(self, token_id: str, reason: str) -> None:
        now = time.time()
        if now - self._last_no_fill_log.get(token_id, 0.0) < 5.0:
            return
        self._last_no_fill_log[token_id] = now
        log.info(
            "live.buy_no_fill token={} reason={}", token_id[:12], reason
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
