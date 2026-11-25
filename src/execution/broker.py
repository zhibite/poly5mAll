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
        self,
        token_id: str,
        usd_amount: float,
        limit_price: float,
        tick_size: float,
        fee_rate: float | None = None,
        *,
        position_id: str = "",
    ) -> Fill | None: ...

    async def sell(
        self,
        token_id: str,
        shares: float,
        limit_price: float,
        tick_size: float,
        fee_rate: float | None = None,
        *,
        position_id: str = "",
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
        fee_rate: float = 0.01,
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
    def _fee(
        self, shares: float, price: float, fee_rate: float | None
    ) -> float:
        """Taker fee for a fill; ``fee_rate`` overrides the broker default."""
        if not self.include_fees:
            return 0.0
        rate = self.fee_rate if fee_rate is None else float(fee_rate)
        return taker_fee(shares, price, rate)

    async def buy(
        self,
        token_id: str,
        usd_amount: float,
        limit_price: float,
        tick_size: float,
        fee_rate: float | None = None,
        *,
        position_id: str = "",
    ) -> Fill | None:
        # ``position_id`` is a signing detail for live orders; paper fills
        # are keyed by token id only.
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
        fee = self._fee(walk.shares, avg, fee_rate)
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
        self,
        token_id: str,
        shares: float,
        limit_price: float,
        tick_size: float,
        fee_rate: float | None = None,
        *,
        position_id: str = "",
    ) -> Fill | None:
        # ``position_id`` is a signing detail for live orders; paper fills
        # are keyed by token id only.
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
        fee = self._fee(walk.shares, avg, fee_rate)
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
        self,
        token_id: str,
        usd_amount: float,
        limit_price: float,
        tick_size: float,
        fee_rate: float | None = None,
        *,
        position_id: str = "",
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
            token_id,
            usd_amount,
            limit_price,
            tick_size,
            fee_rate=fee_rate,
            position_id=position_id,
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
        self,
        token_id: str,
        shares: float,
        limit_price: float,
        tick_size: float,
        fee_rate: float | None = None,
        *,
        position_id: str = "",
    ) -> Fill | None:
        return await self._clob.place_fak_sell(
            token_id,
            shares,
            limit_price,
            tick_size,
            fee_rate=fee_rate,
            position_id=position_id,
        )

    async def token_balance(self, token_id: str) -> float | None:
        return await self._clob.get_token_balance(token_id)

    async def collateral(self) -> float | None:
        """USDC (pUSD) balance, for a startup sanity check."""
        return await self._clob.get_collateral()


# ---------------------------------------------------------------------------
# Runtime paper/live switch
# ---------------------------------------------------------------------------


class MarketBroker:
    """A per-market view over a shared :class:`ModeBroker`.

    ``mode`` resolves through the parent's ``mode_for`` callback, so the
    config page can switch one market between paper and live without
    affecting the others.  Exits pass the position's own mode explicitly.
    """

    def __init__(self, parent: "ModeBroker", market: str) -> None:
        self._parent = parent
        self.market = market

    @property
    def mode(self) -> str:
        return self._parent.mode_for(self.market)

    @property
    def live_available(self) -> bool:
        return self._parent.live_available

    async def buy(
        self,
        token_id: str,
        usd_amount: float,
        limit_price: float,
        tick_size: float,
        fee_rate: float | None = None,
        *,
        position_id: str = "",
        mode: str | None = None,
    ) -> Fill | None:
        return await self._parent._buy(
            mode or self.mode,
            token_id,
            usd_amount,
            limit_price,
            tick_size,
            fee_rate=fee_rate,
            position_id=position_id,
        )

    async def sell(
        self,
        token_id: str,
        shares: float,
        limit_price: float,
        tick_size: float,
        fee_rate: float | None = None,
        *,
        position_id: str = "",
        mode: str | None = None,
    ) -> Fill | None:
        return await self._parent._sell(
            mode or self.mode,
            token_id,
            shares,
            limit_price,
            tick_size,
            fee_rate=fee_rate,
            position_id=position_id,
        )

    async def token_balance(
        self, token_id: str, *, mode: str | None = None
    ) -> float | None:
        return await self._parent._token_balance(
            mode or self.mode, token_id
        )


class ModeBroker:
    """Routes fills to the paper or live broker, per market.

    The live broker is built once at startup (when the environment allows
    live trading and credentials are present).  ``mode`` is the default for
    markets without an override; ``mode_for(market)`` supplies the
    per-market choice and falls back to paper when live is unavailable.
    Open positions are always routed by the mode they were opened on.
    """

    def __init__(
        self,
        paper: PaperBroker,
        live: LiveBroker | None = None,
        mode: str = "paper",
        mode_for=None,
    ) -> None:
        self.paper = paper
        self.live = live
        self.mode = mode if mode in ("paper", "live") else "paper"
        if self.mode == "live" and self.live is None:
            self.mode = "paper"
        self._mode_for = mode_for

    @property
    def is_live(self) -> bool:
        return self.mode == "live" and self.live is not None

    @property
    def live_available(self) -> bool:
        return self.live is not None

    def mode_for(self, market: str = "") -> str:
        """Effective mode for one market (live falls back to paper)."""
        mode = self.mode
        if market and self._mode_for is not None:
            try:
                candidate = str(self._mode_for(market)).strip().lower()
            except Exception:  # noqa: BLE001 - config must not break routing
                candidate = ""
            if candidate in ("paper", "live"):
                mode = candidate
        if mode == "live" and self.live is None:
            mode = "paper"
        return mode

    def wrap(self, market: str) -> MarketBroker:
        return MarketBroker(self, market)

    def set_mode(self, mode: str) -> str:
        """Set the default mode (used when no per-market override exists)."""
        mode = str(mode).strip().lower()
        if mode not in ("paper", "live"):
            raise ValueError("mode must be 'paper' or 'live'")
        if mode == "live" and self.live is None:
            raise RuntimeError("live broker is not configured")
        self.mode = mode
        return self.mode

    def _select(self, mode: str):
        return self.live if (mode == "live" and self.live is not None) else self.paper

    async def _buy(
        self,
        mode: str,
        token_id: str,
        usd_amount: float,
        limit_price: float,
        tick_size: float,
        fee_rate: float | None = None,
        *,
        position_id: str = "",
    ) -> Fill | None:
        return await self._select(mode).buy(
            token_id,
            usd_amount,
            limit_price,
            tick_size,
            fee_rate=fee_rate,
            position_id=position_id,
        )

    async def _sell(
        self,
        mode: str,
        token_id: str,
        shares: float,
        limit_price: float,
        tick_size: float,
        fee_rate: float | None = None,
        *,
        position_id: str = "",
    ) -> Fill | None:
        # Never paper-sell a position that was opened live: the paper
        # ledger has no real shares and booking it would hide the loss.
        if mode == "live" and self.live is None:
            raise RuntimeError("live broker unavailable for a live position")
        return await self._select(mode).sell(
            token_id,
            shares,
            limit_price,
            tick_size,
            fee_rate=fee_rate,
            position_id=position_id,
        )

    async def _token_balance(
        self, mode: str, token_id: str
    ) -> float | None:
        return await self._select(mode).token_balance(token_id)

    async def buy(
        self,
        token_id: str,
        usd_amount: float,
        limit_price: float,
        tick_size: float,
        fee_rate: float | None = None,
        *,
        position_id: str = "",
        market: str = "",
        mode: str | None = None,
    ) -> Fill | None:
        return await self._buy(
            mode or self.mode_for(market),
            token_id,
            usd_amount,
            limit_price,
            tick_size,
            fee_rate=fee_rate,
            position_id=position_id,
        )

    async def sell(
        self,
        token_id: str,
        shares: float,
        limit_price: float,
        tick_size: float,
        fee_rate: float | None = None,
        *,
        position_id: str = "",
        market: str = "",
        mode: str | None = None,
    ) -> Fill | None:
        return await self._sell(
            mode or self.mode_for(market),
            token_id,
            shares,
            limit_price,
            tick_size,
            fee_rate=fee_rate,
            position_id=position_id,
        )

    async def token_balance(
        self,
        token_id: str,
        *,
        market: str = "",
        mode: str | None = None,
    ) -> float | None:
        return await self._token_balance(
            mode or self.mode_for(market), token_id
        )

    async def token_balance_for(
        self, market: str, mode: str, token_id: str
    ) -> float | None:
        return await self._token_balance(
            mode or self.mode_for(market), token_id
        )

    async def collateral(self) -> float | None:
        if self.live is None:
            return None
        return await self.live.collateral()
