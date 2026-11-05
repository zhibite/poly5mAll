"""Polymarket CLOB market WebSocket + price cache.

Adapted from ``02.poly1h/src/market/market_ws.py``.  It keeps the same
wire-format handling (``last_trade_price``, ``best_bid_ask``/``bbo``,
``book`` and ``price_change``) and adds per-field timestamps so the strategy
can tell a fresh top-of-book from a stale one.

The strategy reads a single ``Quote`` per token:

    mid   reference price for entry/TP decisions
    bid   executable price for SELLs (when fresh)
    ask   executable price for BUYs  (when fresh)

Priority for ``mid``: fresh WS BBO midpoint, then fresh REST midpoint, then
fresh last trade.  The REST midpoint poller in ``runner.py`` keeps the
fallback alive when the WS has no book updates.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import websockets
from websockets.protocol import State as WsState

from src.infra.logging import get_logger
from src.models import Quote

log = get_logger("market_ws")

MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
_RECONNECT_AFTER_PRUNED = 128


@dataclass
class PriceCache:
    """Per-token price snapshot with timestamps (unix seconds)."""

    last_trade_price: dict[str, float] = field(default_factory=dict)
    last_trade_at: dict[str, float] = field(default_factory=dict)
    midpoint: dict[str, float] = field(default_factory=dict)
    midpoint_at: dict[str, float] = field(default_factory=dict)
    best_bid: dict[str, float] = field(default_factory=dict)
    best_bid_at: dict[str, float] = field(default_factory=dict)
    best_ask: dict[str, float] = field(default_factory=dict)
    best_ask_at: dict[str, float] = field(default_factory=dict)
    bid_depth: dict[str, float] = field(default_factory=dict)
    ask_depth: dict[str, float] = field(default_factory=dict)
    tick_size: dict[str, float] = field(default_factory=dict)
    # Which writer last set the BBO: "ws_bba" / "ws_book" / "rest_book".
    book_source: dict[str, str] = field(default_factory=dict)

    # ── writers ────────────────────────────────────────────────────────
    def set_midpoint(self, token: str, price: float, ts: float | None = None) -> None:
        if not token or not (0.0 < float(price) < 1.0):
            return
        now = time.time() if ts is None else float(ts)
        self.midpoint[token] = float(price)
        self.midpoint_at[token] = now

    def set_last_trade(self, token: str, price: float, ts: float | None = None) -> None:
        if not token or not (0.0 < float(price) < 1.0):
            return
        now = time.time() if ts is None else float(ts)
        self.last_trade_price[token] = float(price)
        self.last_trade_at[token] = now

    def set_best_bid_ask(
        self,
        token: str,
        bid: Any,
        ask: Any,
        ts: float | None = None,
    ) -> None:
        if not token:
            return
        now = time.time() if ts is None else float(ts)
        try:
            b = float(bid)
        except (TypeError, ValueError):
            b = 0.0
        try:
            a = float(ask)
        except (TypeError, ValueError):
            a = 0.0
        if 0.0 < b < 1.0:
            self.best_bid[token] = b
            self.best_bid_at[token] = now
        if 0.0 < a < 1.0:
            self.best_ask[token] = a
            self.best_ask_at[token] = now
        if 0.0 < b < 1.0 or 0.0 < a < 1.0:
            self.book_source[token] = "ws_bba"

    def set_book(
        self,
        token: str,
        bids: Any,
        asks: Any,
        levels: int = 5,
        ts: float | None = None,
        source: str = "ws_book",
    ) -> None:
        if not token:
            return
        now = time.time() if ts is None else float(ts)
        best_bid, bid_depth = _book_side(bids, levels, best_is_max=True)
        best_ask, ask_depth = _book_side(asks, levels, best_is_max=False)
        if best_bid > 0:
            self.best_bid[token] = best_bid
            self.best_bid_at[token] = now
            self.bid_depth[token] = bid_depth
        if best_ask > 0:
            self.best_ask[token] = best_ask
            self.best_ask_at[token] = now
            self.ask_depth[token] = ask_depth
        if best_bid > 0 or best_ask > 0:
            self.book_source[token] = source

    def clear_token(self, token: str) -> None:
        for mapping in (
            self.last_trade_price, self.last_trade_at,
            self.midpoint, self.midpoint_at,
            self.best_bid, self.best_bid_at,
            self.best_ask, self.best_ask_at,
            self.bid_depth, self.ask_depth, self.tick_size,
            self.book_source,
        ):
            mapping.pop(token, None)

    # ── reader ─────────────────────────────────────────────────────────
    def quote(
        self,
        token: str,
        now: float | None = None,
        max_age: float = 15.0,
        bba_max_age: float = 5.0,
    ) -> Quote | None:
        """Build the freshest quote, or None when every source is stale."""
        if not token:
            return None
        now_f = time.time() if now is None else float(now)
        max_age_f = max(0.05, float(max_age))

        bid = self.best_bid.get(token, 0.0)
        ask = self.best_ask.get(token, 0.0)
        bid_at = self.best_bid_at.get(token, 0.0)
        ask_at = self.best_ask_at.get(token, 0.0)
        bba_at = min(x for x in (bid_at, ask_at) if x > 0) if (
            bid_at > 0 and ask_at > 0
        ) else 0.0
        bba_age = now_f - bba_at if bba_at > 0 else 1e9
        bba_ok = (
            bid > 0 and ask > 0 and ask >= bid
            and bba_age <= min(max_age_f, bba_max_age)
        )

        if bba_ok:
            return Quote(
                token_id=token,
                mid=(bid + ask) / 2.0,
                bid=bid,
                ask=ask,
                last=self.last_trade_price.get(token, 0.0),
                updated_at=bba_at,
                source=self.book_source.get(token, "ws_bba"),
            )

        candidates: list[tuple[float, str, float]] = []
        mid = self.midpoint.get(token, 0.0)
        mid_at = self.midpoint_at.get(token, 0.0)
        if 0.0 < mid < 1.0 and mid_at > 0 and now_f - mid_at <= max_age_f:
            candidates.append((mid_at, "rest_mid", mid))

        last = self.last_trade_price.get(token, 0.0)
        last_at = self.last_trade_at.get(token, 0.0)
        if 0.0 < last < 1.0 and last_at > 0 and now_f - last_at <= max_age_f:
            candidates.append((last_at, "last_trade", last))

        if not candidates:
            return None
        updated_at, source, mid = max(candidates, key=lambda x: x[0])
        # Only expose executable BBO when it is fresh in its own right; a
        # 10 s-old bid must not be used as a TP trigger just because the
        # REST midpoint happens to be fresh.
        fresh_bid = bid_at > 0 and now_f - bid_at <= bba_max_age
        fresh_ask = ask_at > 0 and now_f - ask_at <= bba_max_age
        return Quote(
            token_id=token,
            mid=mid,
            bid=bid if fresh_bid else 0.0,
            ask=ask if fresh_ask else 0.0,
            last=last,
            updated_at=updated_at,
            source=source,
        )


def _book_side(
    rows: Any, levels: int, *, best_is_max: bool
) -> tuple[float, float]:
    if not isinstance(rows, list):
        return 0.0, 0.0
    prices: list[tuple[float, float]] = []
    for row in rows[:levels]:
        if not isinstance(row, dict):
            continue
        try:
            price = float(row.get("price", 0))
            size = float(row.get("size", 0))
        except (TypeError, ValueError):
            continue
        if 0.0 < price < 1.0 and size > 0:
            prices.append((price, size))
    if not prices:
        return 0.0, 0.0
    best = max(prices, key=lambda x: x[0])[0] if best_is_max else min(
        prices, key=lambda x: x[0]
    )[0]
    depth = sum(size for _, size in prices)
    return best, depth


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------


class MarketWS:
    """Async CLOB market-data consumer."""

    def __init__(
        self,
        cache: PriceCache,
        *,
        url: str = MARKET_WS_URL,
        name: str = "ws",
        on_connect: Callable[[], None] | None = None,
        on_disconnect: Callable[[], None] | None = None,
    ) -> None:
        self._cache = cache
        self._url = url
        self._name = name
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect
        self._pending_tokens: set[str] = set()
        self._ws: Any = None
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._pruned_since_connect = 0
        self._force_reconnect_pending = False
        self._raw_dump_remaining = 3
        self.last_message_at = 0.0

    # ── public API ─────────────────────────────────────────────────────
    def set_active_tokens(self, token_ids: set[str]) -> None:
        active = {t for t in token_ids if t}
        removed = self._pending_tokens - active
        new_tokens = active - self._pending_tokens
        self._pending_tokens = active
        self._pruned_since_connect += len(removed)
        if new_tokens and self._ws is not None and self._ws.state == WsState.OPEN:
            asyncio.create_task(self._subscribe(self._ws, list(new_tokens)))
        if (
            removed
            and self._pruned_since_connect >= _RECONNECT_AFTER_PRUNED
            and self._ws is not None
            and self._ws.state == WsState.OPEN
            and not self._force_reconnect_pending
        ):
            self._force_reconnect_pending = True
            asyncio.create_task(self._cycle_socket())

    @property
    def subscribed_tokens(self) -> frozenset[str]:
        return frozenset(self._pending_tokens)

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(
            self._run(), name=f"market-ws-{self._name}"
        )
        log.info(
            "market_ws.started shard={} url={}", self._name, self._url
        )

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        log.info("market_ws.stopped shard={}", self._name)

    # ── internals ──────────────────────────────────────────────────────
    async def _run(self) -> None:
        delay = 1.0
        max_delay = 30.0
        while self._running:
            try:
                async with websockets.connect(
                    self._url, ping_interval=10, close_timeout=3
                ) as ws:
                    self._ws = ws
                    delay = 1.0
                    log.info("market_ws.connected shard={}", self._name)
                    if self._pending_tokens:
                        await self._subscribe(ws, list(self._pending_tokens))
                    if self._on_connect:
                        self._on_connect()
                    async for raw in ws:
                        if not self._running:
                            break
                        await self._process(raw)
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "market_ws.error shard={} error={} retry_in={:.1f}s",
                    self._name,
                    exc,
                    delay,
                )
                if self._on_disconnect:
                    self._on_disconnect()
                await asyncio.sleep(delay)
                delay = min(delay * 2.0, max_delay)
            finally:
                self._ws = None

    async def _subscribe(self, ws: Any, tokens: list[str]) -> None:
        payload = {
            "type": "market",
            "assets_ids": tokens,
            "custom_feature_enabled": True,
        }
        await ws.send(json.dumps(payload))
        log.info(
            "market_ws.subscribed shard={} n={}", self._name, len(tokens)
        )

    async def _cycle_socket(self) -> None:
        ws = self._ws
        try:
            if ws is not None:
                await ws.close()
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._force_reconnect_pending = False
            self._pruned_since_connect = 0

    async def _process(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        if not isinstance(msg, dict):
            return
        self.last_message_at = time.time()

        if self._raw_dump_remaining > 0:
            self._raw_dump_remaining -= 1
            log.debug(
                "market_ws.raw keys={} event={}",
                sorted(msg.keys()),
                msg.get("event_type") or msg.get("type"),
            )

        event_type = str(msg.get("event_type") or msg.get("type") or "")
        token = str(
            msg.get("asset_id")
            or msg.get("token")
            or ""
        )
        market_field = msg.get("market")
        if not token and isinstance(market_field, dict):
            token = str(
                market_field.get("asset_id")
                or market_field.get("assetID")
                or ""
            )
        active = not self._pending_tokens or token in self._pending_tokens
        now = time.time()

        if event_type == "last_trade_price" and active and token:
            price = msg.get("price")
            if price not in (None, ""):
                self._cache.set_last_trade(token, _float(price), now)

        if event_type in ("best_bid_ask", "bbo") and active and token:
            bid = msg.get("best_bid", msg.get("bestBid"))
            ask = msg.get("best_ask", msg.get("bestAsk"))
            if bid is not None or ask is not None:
                self._cache.set_best_bid_ask(token, bid, ask, now)

        elif event_type == "book" and active and token:
            self._cache.set_book(
                token,
                msg.get("bids") or msg.get("buys"),
                msg.get("asks") or msg.get("sells"),
                ts=now,
            )

        # price_change carries one row per updated asset, each with its own
        # best_bid/best_ask and the price of the changed level.
        for pc in msg.get("price_changes") or []:
            if not isinstance(pc, dict):
                continue
            pc_token = str(pc.get("asset_id") or token or "")
            if not pc_token:
                continue
            if self._pending_tokens and pc_token not in self._pending_tokens:
                continue
            price = pc.get("price")
            if price not in (None, ""):
                self._cache.set_last_trade(pc_token, _float(price), now)
            bid = pc.get("best_bid")
            ask = pc.get("best_ask")
            if bid is not None or ask is not None:
                self._cache.set_best_bid_ask(pc_token, bid, ask, now)

        # Fallback for the older top-level last_trade_price field.
        if not active:
            return
        ltp = msg.get("last_trade_price")
        if isinstance(ltp, (str, int, float)) and token:
            self._cache.set_last_trade(token, _float(ltp), now)


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


class MarketWSManager:
    """Hash-shard the CLOB market WS across independent connections.

    One connection carrying all seven assets was regularly reset with
    ``1013 slow consumer: send buffer full``.  Tokens are assigned to a
    fixed shard by their token id, so each connection subscribes to a
    smaller subset and one bad shard cannot take down every asset.
    """

    def __init__(
        self,
        cache: PriceCache,
        *,
        shards: int = 7,
        url: str = MARKET_WS_URL,
    ) -> None:
        n = max(1, int(shards))
        self._cache = cache
        self._shards: list[MarketWS] = [
            MarketWS(cache, url=url, name=f"ws{i}") for i in range(n)
        ]
        self._running = False

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        await asyncio.gather(*(s.start() for s in self._shards))
        log.info(
            "market_ws_manager.started shards={}", len(self._shards)
        )

    async def stop(self) -> None:
        self._running = False
        await asyncio.gather(
            *(s.stop() for s in self._shards), return_exceptions=True
        )
        log.info("market_ws_manager.stopped")

    def set_active_tokens(self, token_ids: set[str]) -> None:
        n = len(self._shards)
        buckets: list[set[str]] = [set() for _ in range(n)]
        for token in token_ids:
            if not token:
                continue
            digest = hashlib.md5(token.encode("utf-8")).digest()
            idx = int.from_bytes(digest[:4], "big") % n
            buckets[idx].add(token)
        for shard, bucket in zip(self._shards, buckets):
            shard.set_active_tokens(bucket)

    @property
    def subscribed_tokens(self) -> frozenset[str]:
        out: set[str] = set()
        for shard in self._shards:
            out.update(shard.subscribed_tokens)
        return frozenset(out)

    @property
    def shard_sizes(self) -> list[int]:
        return [len(s.subscribed_tokens) for s in self._shards]

    @property
    def last_message_at(self) -> float:
        return max(
            (s.last_message_at for s in self._shards), default=0.0
        )

    @property
    def shards(self) -> tuple[MarketWS, ...]:
        return tuple(self._shards)
