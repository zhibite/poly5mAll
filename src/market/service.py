"""5m market discovery and window bookkeeping.

One ``MarketService`` is shared by all seven assets.  It keeps the current
and next window per asset so the runner can:

  * subscribe the next market's tokens before its 0–60 s entry window,
  * serve the current window to the strategy,
  * keep the previous window object alive while an open position is
    being force-exited.

Gamma is polled, not pushed.  ``refresh`` is called by the discovery loop
at ``MARKET_DISCOVERY_S``; the per-asset lock prevents overlapping
discovery for the same asset.
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
        window_sec: int = 300,
        pre_discovery_lead_s: float = 60.0,
        min_discovery_interval_s: float = 1.0,
    ) -> None:
        self._pm = pm
        self._window_sec = int(window_sec)
        self._pre_discovery_lead_s = float(pre_discovery_lead_s)
        self._min_discovery_interval_s = float(min_discovery_interval_s)
        self._by_asset: dict[str, AssetWindows] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # ── reads ──────────────────────────────────────────────────────────
    def current(self, asset: str, now: float) -> MarketWindow | None:
        state = self._by_asset.get(asset.upper())
        if state is None:
            return None
        if state.current and state.current.contains(now):
            return state.current
        # A boundary may have passed between discovery ticks; promote from
        # the known map immediately so the 0–60 s window is not lost.
        for window in state.known.values():
            if window.contains(now):
                state.current = window
                return window
        return None

    def next(self, asset: str) -> MarketWindow | None:
        state = self._by_asset.get(asset.upper())
        return state.next if state else None

    def known(self, asset: str) -> list[MarketWindow]:
        state = self._by_asset.get(asset.upper())
        if state is None:
            return []
        return sorted(state.known.values(), key=lambda w: w.start_ts)

    def poll_tokens(
        self,
        extra: Iterable[str] = (),
        *,
        assets: set[str] | None = None,
    ) -> set[str]:
        """Tokens that need a REST midpoint poll right now.

        Only the current window (plus open positions) is polled.  The next
        window is subscribed on the WebSocket for a fast t=0 BBO, but its
        REST midpoint is not needed until it becomes current; this keeps the
        CLOB request rate at ~14/2 s instead of ~28/2 s.
        """
        tokens: set[str] = {t for t in extra if t}
        for asset, state in self._by_asset.items():
            if assets is not None and asset not in assets:
                continue
            if state.current is not None:
                tokens.add(state.current.up_token)
                tokens.add(state.current.down_token)
        return {t for t in tokens if t}

    def active_tokens(
        self,
        extra: Iterable[str] = (),
        *,
        include_recent: bool = False,
        assets: set[str] | None = None,
    ) -> set[str]:
        tokens: set[str] = {t for t in extra if t}
        for asset, state in self._by_asset.items():
            if assets is not None and asset not in assets:
                continue
            for window in (state.current, state.next):
                if window is not None:
                    tokens.add(window.up_token)
                    tokens.add(window.down_token)
            # Old-window tokens are useful for a late force exit, but they
            # should not double the REST midpoint polling load.
            if include_recent:
                now = time.time()
                for window in state.known.values():
                    if window.end_ts >= now - self._window_sec:
                        tokens.add(window.up_token)
                        tokens.add(window.down_token)
        return {t for t in tokens if t}

    # ── discovery ──────────────────────────────────────────────────────
    async def refresh(self, asset: str, now: float | None = None) -> bool:
        """Discover as needed.  Returns True when the current window changed."""
        asset_u = asset.upper()
        now_f = time.time() if now is None else float(now)
        state = self._by_asset.setdefault(asset_u, AssetWindows())
        lock = self._locks.setdefault(asset_u, asyncio.Lock())

        async with lock:
            current = self.current(asset_u, now_f)
            old_start = current.start_ts if current else None

            need = current is None
            if current is not None:
                # The next window is needed before it opens so its tokens
                # are already subscribed at t=0.
                next_missing = (
                    state.next is None
                    or state.next.start_ts != current.start_ts + self._window_sec
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
                    asset_u,
                    slug_interval="5m",
                    window_sec=self._window_sec,
                    limit=6,
                )
            except Exception as exc:  # noqa: BLE001
                state.last_error = str(exc)
                log.warning("[{}] discovery failed error={}", asset_u, exc)
                return False

            added = self._ingest(asset_u, raw, state, now_f)
            self._recompute(asset_u, state, now_f)
            if added:
                log.info(
                    "[{}] discovered {} window(s); current={} next={}",
                    asset_u,
                    added,
                    state.current.slug if state.current else "-",
                    state.next.slug if state.next else "-",
                )
            if state.current is None and now_f - state.last_warn_at >= 30.0:
                state.last_warn_at = now_f
                log.warning(
                    "[{}] Gamma returned {} 5m market(s) but none covers "
                    "now; retrying",
                    asset_u,
                    len(raw),
                )
            return bool(state.current and state.current.start_ts != old_start)

    def _ingest(
        self,
        asset: str,
        raw_markets: list[dict],
        state: AssetWindows,
        now: float,
    ) -> int:
        added = 0
        for market in raw_markets:
            try:
                start = int(market.get("_window_start_ts") or 0)
            except (TypeError, ValueError):
                continue
            if start <= 0:
                continue
            slug = str(market.get("_event_slug") or "")
            tokens = market.get("tokens") or []
            if len(tokens) < 2 or not slug:
                continue
            up_token = str(tokens[0].get("token_id") or "")
            down_token = str(tokens[1].get("token_id") or "")
            if not up_token or not down_token:
                continue
            end = start + self._window_sec
            if end < now - 2 * self._window_sec:
                continue
            if start in state.known:
                continue

            # 5m crypto Up/Down tokens quote on a 0.01 grid.  Fetching
            # /tick-size and /fee-rate for every window would put four
            # extra round trips on the critical path at t=0; the strategy
            # uses 0.01 and the configured fee rate instead.
            window = MarketWindow(
                asset=asset,
                slug=slug,
                start_ts=start,
                end_ts=end,
                up_token=up_token,
                down_token=down_token,
                condition_id=str(market.get("conditionId") or ""),
                tick_size=0.01,
                fee_rate_bps=-1,
            )
            state.known[start] = window
            added += 1
        return added

    def _recompute(self, asset: str, state: AssetWindows, now: float) -> None:
        windows = sorted(state.known.values(), key=lambda w: w.start_ts)
        state.current = None
        state.next = None
        for window in windows:
            if window.contains(now):
                state.current = window
            elif window.start_ts > now and state.next is None:
                state.next = window
        # Drop windows older than two periods; open positions hold their own
        # MarketWindow copy, so this cannot orphan a live exit.
        cutoff = now - 2 * self._window_sec
        for start in [s for s, w in state.known.items() if w.end_ts < cutoff]:
            state.known.pop(start, None)

    def stats(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for asset, state in self._by_asset.items():
            out[asset] = {
                "current": state.current.slug if state.current else None,
                "next": state.next.slug if state.next else None,
                "known": len(state.known),
                "error": state.last_error,
            }
        return out
