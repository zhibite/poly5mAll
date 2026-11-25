"""Market discovery and window bookkeeping for 5m and 15m series.

One ``MarketService`` is shared by every market (6 coins x 2 timeframes
by default).  It keeps the current and next window per market so the
runner can:

  * subscribe the next market's tokens before its entry window,
  * serve the current window to the strategy,
  * keep the previous window object alive while a position settles.

Gamma is polled, not pushed.  ``refresh`` is called by the discovery loop
at ``MARKET_DISCOVERY_S``; the per-market lock prevents overlapping
discovery for the same market.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Iterable

from src.infra.logging import get_logger
from src.market.pm_client import PolymarketClient
from src.models import MarketWindow

log = get_logger("market_service")


@dataclass(frozen=True)
class MarketSpec:
    """One Gamma series: coin + timeframe (slug interval + window size)."""

    market: str       # "ETH-5M" / "ETH-15M"
    asset: str        # "ETH"
    interval: str     # "5m" / "15m"
    window_sec: int   # 300 / 900


@dataclass
class AssetWindows:
    current: MarketWindow | None = None
    next: MarketWindow | None = None
    known: dict[int, MarketWindow] = field(default_factory=dict)
    last_discovery_at: float = 0.0
    last_warn_at: float = 0.0
    last_error: str = ""


class MarketService:
    def __init__(
        self,
        pm: PolymarketClient,
        *,
        specs: Iterable[MarketSpec] | None = None,
        window_sec: int = 300,
        pre_discovery_lead_s: float = 60.0,
        min_discovery_interval_s: float = 1.0,
    ) -> None:
        self._pm = pm
        self._default_window_sec = int(window_sec)
        self._specs: dict[str, MarketSpec] = {
            spec.market.upper(): spec for spec in (specs or ())
        }
        self._pre_discovery_lead_s = float(pre_discovery_lead_s)
        self._min_discovery_interval_s = float(min_discovery_interval_s)
        self._by_market: dict[str, AssetWindows] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _spec(self, market: str) -> MarketSpec:
        """Registered spec, or a 5m single-market fallback (tests / legacy)."""
        key = market.upper()
        spec = self._specs.get(key)
        if spec is not None:
            return spec
        return MarketSpec(
            market=key,
            asset=key,
            interval="5m",
            window_sec=self._default_window_sec,
        )

    # ── reads ──────────────────────────────────────────────────────────
    def current(self, market: str, now: float) -> MarketWindow | None:
        state = self._by_market.get(market.upper())
        if state is None:
            return None
        if state.current and state.current.contains(now):
            return state.current
        # A boundary may have passed between discovery ticks; promote from
        # the known map immediately so the entry window is not lost.
        for window in state.known.values():
            if window.contains(now):
                state.current = window
                return window
        return None

    def next(self, market: str) -> MarketWindow | None:
        state = self._by_market.get(market.upper())
        return state.next if state else None

    def known(self, market: str) -> list[MarketWindow]:
        state = self._by_market.get(market.upper())
        if state is None:
            return []
        return sorted(state.known.values(), key=lambda w: w.start_ts)

    def poll_tokens(
        self,
        extra: Iterable[str] = (),
        *,
        markets: set[str] | None = None,
        now: float | None = None,
    ) -> set[str]:
        """Tokens that need a REST midpoint/book poll right now.

        Only the current window (plus open positions) is polled.  The next
        window is subscribed on the WebSocket for a fast t=0 BBO, but its
        REST price is not needed until it becomes current.

        ``current()`` is called here so a boundary is promoted even if the
        discovery loop has not run since the clock crossed it.
        """
        now_f = time.time() if now is None else float(now)
        tokens: set[str] = {t for t in extra if t}
        wanted = (
            {m.upper() for m in markets} if markets is not None else None
        )
        for market in self._by_market:
            if wanted is not None and market not in wanted:
                continue
            window = self.current(market, now_f)
            if window is not None:
                tokens.add(window.up_token)
                tokens.add(window.down_token)
        return {t for t in tokens if t}

    def active_tokens(
        self,
        extra: Iterable[str] = (),
        *,
        include_recent: bool = False,
        markets: set[str] | None = None,
    ) -> set[str]:
        tokens: set[str] = {t for t in extra if t}
        wanted = (
            {m.upper() for m in markets} if markets is not None else None
        )
        now = time.time()
        for market, state in self._by_market.items():
            if wanted is not None and market not in wanted:
                continue
            for window in (state.current, state.next):
                if window is not None:
                    tokens.add(window.up_token)
                    tokens.add(window.down_token)
            # Old-window tokens are useful for a late settlement, but they
            # should not double the REST polling load.
            if include_recent:
                recent_span = self._spec(market).window_sec
                for window in state.known.values():
                    if window.end_ts >= now - recent_span:
                        tokens.add(window.up_token)
                        tokens.add(window.down_token)
        return {t for t in tokens if t}

    # ── discovery ──────────────────────────────────────────────────────
    async def refresh(self, market: str, now: float | None = None) -> bool:
        """Discover as needed.  Returns True when the current window changed."""
        key = market.upper()
        spec = self._spec(key)
        now_f = time.time() if now is None else float(now)
        state = self._by_market.setdefault(key, AssetWindows())
        lock = self._locks.setdefault(key, asyncio.Lock())

        async with lock:
            current = self.current(key, now_f)
            old_start = current.start_ts if current else None

            need = current is None
            if current is not None:
                # The next window is needed before it opens so its tokens
                # are already subscribed at t=0.
                next_missing = (
                    state.next is None
                    or state.next.start_ts != current.start_ts + spec.window_sec
                )
                if next_missing and now_f >= (
                    current.end_ts - self._pre_discovery_lead_s
                ):
                    need = True
            if not need:
                return False

            if now_f - state.last_discovery_at < self._min_discovery_interval_s:
                return False
            state.last_discovery_at = now_f

            try:
                raw = await self._pm.get_asset_updown_markets(
                    spec.asset,
                    slug_interval=spec.interval,
                    window_sec=spec.window_sec,
                    limit=6,
                )
            except Exception as exc:  # noqa: BLE001
                state.last_error = str(exc)
                log.warning(
                    "[{}] discovery failed error={}", key, exc
                )
                return False

            added = await self._ingest(spec, raw, state, now_f)
            self._recompute(state, spec.window_sec, now_f)
            if added:
                log.info(
                    "[{}] discovered {} window(s); current={} next={}",
                    key,
                    added,
                    state.current.slug if state.current else "-",
                    state.next.slug if state.next else "-",
                )
            if state.current is None and now_f - state.last_warn_at >= 30.0:
                state.last_warn_at = now_f
                log.warning(
                    "[{}] Gamma returned {} {} market(s) but none covers "
                    "now; retrying",
                    key,
                    len(raw),
                    spec.interval,
                )
            return bool(state.current and state.current.start_ts != old_start)

    async def _ingest(
        self,
        spec: MarketSpec,
        raw_markets: list[dict],
        state: AssetWindows,
        now: float,
    ) -> int:
        candidates: list[tuple[int, str, str, str, str, str, str]] = []
        for item in raw_markets:
            try:
                start = int(item.get("_window_start_ts") or 0)
            except (TypeError, ValueError):
                continue
            if start <= 0:
                continue
            slug = str(item.get("_event_slug") or "")
            tokens = item.get("tokens") or []
            if len(tokens) < 2 or not slug:
                continue
            up_token = str(tokens[0].get("token_id") or "")
            down_token = str(tokens[1].get("token_id") or "")
            if not up_token or not down_token:
                continue
            end = start + spec.window_sec
            if end < now - 2 * spec.window_sec:
                continue
            if start in state.known:
                continue
            candidates.append(
                (
                    start,
                    slug,
                    up_token,
                    down_token,
                    str(item.get("conditionId") or ""),
                    str(tokens[0].get("position_id") or ""),
                    str(tokens[1].get("position_id") or ""),
                )
            )

        if not candidates:
            return 0

        # Crypto Up/Down tokens can quote on a 0.001 grid, not just 0.01.
        # Both outcomes of a market share the same tick, so one call per
        # new window is enough.
        ticks = await asyncio.gather(
            *(self._tick_size(up) for _, _, up, *_ in candidates),
            return_exceptions=True,
        )

        added = 0
        for (
            start,
            slug,
            up_token,
            down_token,
            condition_id,
            up_position,
            down_position,
        ), tick in zip(candidates, ticks):
            if not isinstance(tick, (int, float)) or float(tick) <= 0:
                tick = 0.01
            window = MarketWindow(
                asset=spec.asset,
                slug=slug,
                start_ts=start,
                end_ts=start + spec.window_sec,
                up_token=up_token,
                down_token=down_token,
                interval=spec.interval,
                up_position_id=up_position,
                down_position_id=down_position,
                condition_id=condition_id,
                tick_size=float(tick),
                fee_rate_bps=-1,
            )
            state.known[start] = window
            added += 1
        return added

    async def _tick_size(self, token_id: str) -> float:
        """Exchange tick for a token, falling back to the old 0.01 grid."""
        getter = getattr(self._pm, "get_tick_size", None)
        if getter is None:
            return 0.01
        try:
            tick = await getter(token_id)
        except Exception:  # noqa: BLE001
            return 0.01
        if tick is None:
            return 0.01
        tick = float(tick)
        return tick if tick > 0 else 0.01

    async def tick_size(
        self, token_id: str, *, timeout_s: float = 1.0
    ) -> float | None:
        """Fresh exchange tick, or None when the query fails.

        Discovery caches a tick per window, but the CLOB can change it
        mid-window.  Exits use this to re-read the live value; on failure
        they keep the cached tick rather than falling back to 0.01.
        """
        getter = getattr(self._pm, "get_tick_size", None)
        if getter is None:
            return None
        try:
            tick = await getter(token_id, timeout_s=timeout_s, default=None)
        except Exception:  # noqa: BLE001
            return None
        if tick is None:
            return None
        tick = float(tick)
        return tick if tick > 0 else None

    def _recompute(
        self, state: AssetWindows, window_sec: int, now: float
    ) -> None:
        windows = sorted(state.known.values(), key=lambda w: w.start_ts)
        state.current = None
        state.next = None
        for window in windows:
            if window.contains(now):
                state.current = window
            elif window.start_ts > now and state.next is None:
                state.next = window
        # Drop windows older than two periods; open positions hold their
        # own MarketWindow copy, so this cannot orphan a live settlement.
        cutoff = now - 2 * window_sec
        for start in [s for s, w in state.known.items() if w.end_ts < cutoff]:
            state.known.pop(start, None)

    def stats(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for market, state in self._by_market.items():
            out[market] = {
                "current": state.current.slug if state.current else None,
                "next": state.next.slug if state.next else None,
                "known": len(state.known),
                "error": state.last_error,
            }
        return out
