"""Polymarket public REST client.

Adapted from ``02.poly1h/src/market/pm_client.py`` for the 5m and 15m
series:

  * Gamma ``/events?slug={asset}-updown-{5m|15m}-{epoch}`` discovers the
    market and its UP/DOWN token ids.
  * CLOB ``/midpoint`` supplies the stable strategy reference price.
  * CLOB ``/orderbook`` supplies executable depth for paper fills.
  * ``/tick-size`` is queried once per new window and re-read by exits:
    these tokens can quote on a 0.001 grid, and the CLOB can lower the
    tick mid-window (observed 0.01 -> 0.001 near expiry).  A hard-coded
    0.01 made exits unsellable below one cent.  ``/fee-rate`` remains
    diagnostic; the configured fee rate is used on the critical path.
  * ``/events?slug=`` is reused after settlement to resolve a position
    whose 240 s market exit could not be filled.

No authentication is required for any of these calls.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Optional

import httpx

from src.infra.logging import get_logger

log = get_logger("pm_client")

# Polymarket has used both the ticker and the full name in event slugs.
# Probe the ticker first (the historical 5m form) and fall back to aliases;
# the first prefix that returns a market is cached for the process.
_UPDOWN_SLUG_NAMES: dict[str, tuple[str, ...]] = {
    "BTC": ("btc", "bitcoin"),
    "ETH": ("eth", "ethereum"),
    "BNB": ("bnb", "binance-coin", "binancecoin"),
    "XRP": ("xrp", "ripple"),
    "SOL": ("sol", "solana"),
    "DOGE": ("doge", "dogecoin"),
    "HYPE": ("hype", "hyperliquid"),
}


class PolymarketClient:
    def __init__(
        self,
        gamma_api: str = "https://gamma-api.polymarket.com",
        polymarket_host: str = "https://clob.polymarket.com",
        timeout_s: float = 5.0,
    ) -> None:
        self.gamma_api = gamma_api.rstrip("/")
        self.polymarket_host = polymarket_host.rstrip("/")
        self.timeout_s = float(timeout_s)
        self._http: Optional[httpx.AsyncClient] = None
        # (asset, interval) -> working slug prefix.  The interval must be
        # part of the key: "eth-updown-5m" is not a valid prefix for the
        # 15m series.
        self._prefix_cache: dict[tuple[str, str], str] = {}

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout_s, connect=3.0),
                limits=httpx.Limits(
                    max_connections=20, max_keepalive_connections=10
                ),
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/126.0.0.0 Safari/537.36"
                    ),
                    "Accept": "application/json",
                },
            )
        return self._http

    async def close(self) -> None:
        if self._http and not self._http.is_closed:
            await self._http.aclose()
        self._http = None

    # ------------------------------------------------------------------
    # Market discovery
    # ------------------------------------------------------------------

    async def get_asset_updown_markets(
        self,
        asset: str,
        *,
        slug_interval: str = "5m",
        window_sec: int = 300,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Return active/next markets for ``asset`` at one timeframe.

        ``slug_interval`` is "5m" or "15m"; ``window_sec`` is the matching
        window length (300 / 900).  The same slug pattern is used for
        both series.

        The returned dicts carry:
            ``tokens``            [{"token_id", "outcome"}] in Up/Down order
            ``_event_slug``       exact Gamma event slug
            ``_window_start_ts``  unix seconds
            ``conditionId``       CLOB condition id (if supplied)
        """
        asset_u = asset.upper()
        c = await self._client()
        now = int(time.time())
        base = (now // window_sec) * window_sec

        # Current first, then the previous/next window.  The extra window is
        # what lets the discovery loop pre-subscribe the next market before
        # its 0–60 s entry window starts.
        starts = [base, base - window_sec, base + window_sec]

        # Query the candidate slugs concurrently: a slow Gamma request for
        # one window must not delay the current window's 0–60 s entry.
        async def fetch(slug: str) -> tuple[list[dict[str, Any]], bool]:
            """Return (events, reachable).  ``reachable`` is False only when
            the HTTP call itself failed, which lets the alias loop stop
            early instead of waiting on every naming variant."""
            try:
                resp = await c.get(
                    f"{self.gamma_api}/events",
                    params={"slug": slug},
                    timeout=self.timeout_s,
                )
                if resp.status_code != 200:
                    return [], True
                events = resp.json()
                if isinstance(events, dict):
                    events = [events] if events else []
                return (events if isinstance(events, list) else []), True
            except Exception as exc:  # noqa: BLE001
                log.debug("gamma.events failed slug={} error={}", slug, exc)
                return [], False

        for prefix in self._slug_prefixes(asset_u, slug_interval):
            slugs = [f"{prefix}-{ts}" for ts in starts]
            results = await asyncio.gather(*(fetch(slug) for slug in slugs))
            pages = [events for events, _ in results]
            reachable = any(ok for _, ok in results)
            found = self._parse_pages(slugs, starts, pages, limit)
            if found:
                self._prefix_cache[(asset_u, slug_interval)] = prefix
                return found
            if not reachable:
                log.warning(
                    "gamma.unreachable asset={} interval={}",
                    asset_u,
                    slug_interval,
                )
                break

        log.debug("gamma.no_market asset={} interval={}", asset_u, slug_interval)
        return []

    def _slug_prefixes(self, asset: str, slug_interval: str) -> list[str]:
        cached = self._prefix_cache.get((asset, slug_interval))
        names = _UPDOWN_SLUG_NAMES.get(asset, (asset.lower(),))
        prefixes: list[str] = []
        if cached:
            prefixes.append(cached)
        for name in names:
            for form in ("updown", "up-or-down"):
                prefix = f"{name}-{form}-{slug_interval}"
                if prefix not in prefixes:
                    prefixes.append(prefix)
        return prefixes

    @staticmethod
    def _parse_pages(
        slugs: list[str],
        starts: list[int],
        pages: list[list[dict[str, Any]]],
        limit: int,
    ) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        seen_tokens: set[str] = set()
        for start_ts, slug, events in zip(starts, slugs, pages):
            for ev in events:
                if not isinstance(ev, dict):
                    continue
                ev_slug = str(ev.get("slug") or "").strip()
                if not ev_slug:
                    continue
                if ev_slug != slug and not ev_slug.startswith(slug + "-"):
                    continue
                if _truthy(ev.get("closed")) or _truthy(ev.get("archived")):
                    continue

                for market in ev.get("markets") or []:
                    if not isinstance(market, dict):
                        continue
                    if not _market_is_live(market):
                        continue
                    tokens = _tokens_from_market(market)
                    if not tokens:
                        continue
                    up_token, down_token = tokens
                    if up_token in seen_tokens or down_token in seen_tokens:
                        continue
                    seen_tokens.update((up_token, down_token))
                    # Newer ("v2") markets carry position ids and must be
                    # signed against Exchange V3; legacy markets leave these
                    # empty and keep the clob token-id path.
                    up_position, down_position = _positions_from_market(market)
                    found.append(
                        {
                            "tokens": [
                                {
                                    "token_id": up_token,
                                    "outcome": "Up",
                                    "position_id": up_position,
                                },
                                {
                                    "token_id": down_token,
                                    "outcome": "Down",
                                    "position_id": down_position,
                                },
                            ],
                            "_event_slug": ev_slug,
                            "_window_start_ts": int(start_ts),
                            "conditionId": str(
                                market.get("conditionId")
                                or market.get("condition_id")
                                or ""
                            ),
                            "outcomes": _outcome_labels(market),
                        }
                    )
        return found[:limit]

    # ------------------------------------------------------------------
    # CLOB prices / books
    # ------------------------------------------------------------------

    async def get_midpoint(self, token_id: str) -> float:
        """CLOB midpoint (bid+ask)/2, or 0.0 on failure."""
        c = await self._client()
        try:
            resp = await c.get(
                f"{self.polymarket_host}/midpoint",
                params={"token_id": token_id},
                timeout=3.0,
            )
            resp.raise_for_status()
            return float((resp.json() or {}).get("mid", 0.0) or 0.0)
        except Exception:  # noqa: BLE001
            return 0.0

    async def get_order_book(self, token_id: str) -> dict[str, Any] | None:
        """Raw CLOB order book: ``{"bids": [...], "asks": [...]}``.

        The current CLOB serves depth at ``/book``.  ``/orderbook`` was the
        older path used by 02.poly1h; it now returns 404, so try ``/book``
        first and keep the legacy path as a harmless fallback.
        """
        c = await self._client()
        last_error = ""
        for path in ("/book", "/orderbook"):
            try:
                resp = await c.get(
                    f"{self.polymarket_host}{path}",
                    params={"token_id": token_id},
                    timeout=3.0,
                )
                if resp.status_code != 200:
                    last_error = f"{path} HTTP {resp.status_code}"
                    continue
                data = resp.json()
                if isinstance(data, dict):
                    return data
                last_error = f"{path} non-object response"
            except Exception as exc:  # noqa: BLE001
                last_error = f"{path} {exc}"
        log.debug(
            "clob.orderbook failed token={} error={}",
            token_id[:12],
            last_error,
        )
        return None

    async def get_tick_size(
        self,
        token_id: str,
        *,
        timeout_s: float = 3.0,
        default: float | None = 0.01,
    ) -> float | None:
        """Current ``minimum_tick_size`` for a token.

        The CLOB can lower a market's tick while it is live (observed
        0.01 -> 0.001 near expiry), so callers that need certainty can
        pass ``default=None`` to distinguish a failed query from 0.01.
        """
        c = await self._client()
        try:
            resp = await c.get(
                f"{self.polymarket_host}/tick-size",
                params={"token_id": token_id},
                timeout=timeout_s,
            )
            resp.raise_for_status()
            raw = (resp.json() or {}).get("minimum_tick_size")
            if raw is None:
                return default
            tick = float(raw)
            return tick if tick > 0 else default
        except Exception:  # noqa: BLE001
            return default

    async def get_fee_rate_bps(self, token_id: str) -> int:
        """Taker fee in basis points, or -1 when the endpoint is unavailable."""
        c = await self._client()
        try:
            resp = await c.get(
                f"{self.polymarket_host}/fee-rate",
                params={"token_id": token_id},
                timeout=3.0,
            )
            resp.raise_for_status()
            data = resp.json() or {}
            raw = data.get("base_fee", data.get("fee_rate_bps", -1))
            return int(raw)
        except Exception:  # noqa: BLE001
            return -1

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    async def get_event_resolution(self, slug: str) -> dict[str, Any] | None:
        """Return ``{"UP": p, "DOWN": p, "resolved": bool}`` for a slug.

        Gamma publishes resolved 0/1 ``outcomePrices`` after settlement.
        ``resolved`` is true once the two prices are effectively binary.
        """
        c = await self._client()
        try:
            resp = await c.get(
                f"{self.gamma_api}/events",
                params={"slug": slug},
                timeout=self.timeout_s,
            )
            resp.raise_for_status()
            events = resp.json()
            if isinstance(events, dict):
                events = [events] if events else []
        except Exception as exc:  # noqa: BLE001
            log.debug("gamma.resolution failed slug={} error={}", slug, exc)
            return None

        for ev in events:
            if not isinstance(ev, dict):
                continue
            ev_slug = str(ev.get("slug") or "")
            if ev_slug != slug and not ev_slug.startswith(slug + "-"):
                continue
            for market in ev.get("markets") or []:
                if not isinstance(market, dict):
                    continue
                outcomes = _outcome_labels(market)
                prices = _outcome_prices(market)
                if len(outcomes) < 2 or len(prices) < 2:
                    continue
                mapping = {
                    str(outcome).strip().upper(): float(price)
                    for outcome, price in zip(outcomes, prices)
                }
                up = mapping.get("UP", mapping.get("YES", 0.0))
                down = mapping.get("DOWN", mapping.get("NO", 0.0))
                if up <= 0 and down <= 0:
                    continue
                resolved = (
                    (up >= 0.99 and down <= 0.01)
                    or (down >= 0.99 and up <= 0.01)
                )
                return {
                    "UP": up,
                    "DOWN": down,
                    "resolved": resolved,
                    "closed": _truthy(ev.get("closed"))
                    or _truthy(market.get("closed")),
                }
        return None


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes")
    return False


def _market_is_live(market: dict[str, Any]) -> bool:
    if _truthy(market.get("closed")) or _truthy(market.get("archived")):
        return False
    status = str(market.get("status") or "").strip().lower()
    if status in ("closed", "resolved", "archived"):
        return False
    return True


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def _outcome_labels(market: dict[str, Any]) -> list[str]:
    raw = _json_list(market.get("outcomes"))
    return [str(x) for x in raw]


def _outcome_prices(market: dict[str, Any]) -> list[float]:
    raw = _json_list(market.get("outcomePrices"))
    out: list[float] = []
    for item in raw:
        try:
            out.append(float(item))
        except (TypeError, ValueError):
            out.append(0.0)
    return out


def _updown_indices(market: dict[str, Any]) -> tuple[int, int]:
    """Outcome-array indices of the Up and Down legs.

    ``clobTokenIds`` and ``positionIds`` are parallel to ``outcomes``.
    When Gamma omits the labels, the historical positional order is Up, Down.
    """
    outcomes = [x.strip().lower() for x in _outcome_labels(market)]
    up_idx = down_idx = None
    for idx, outcome in enumerate(outcomes):
        if outcome in ("up", "yes") and up_idx is None:
            up_idx = idx
        elif outcome in ("down", "no") and down_idx is None:
            down_idx = idx
    if up_idx is None or down_idx is None:
        up_idx, down_idx = 0, 1
    return up_idx, down_idx


def _tokens_from_market(market: dict[str, Any]) -> tuple[str, str] | None:
    """Map Gamma's ``clobTokenIds`` to (up_token, down_token)."""
    token_ids = _json_list(market.get("clobTokenIds"))
    if len(token_ids) < 2:
        return None
    up_idx, down_idx = _updown_indices(market)
    if up_idx >= len(token_ids) or down_idx >= len(token_ids):
        return None
    up = str(token_ids[up_idx])
    down = str(token_ids[down_idx])
    if not up or not down or up == down:
        return None
    return up, down


def _positions_from_market(market: dict[str, Any]) -> tuple[str, str]:
    """Map Gamma's ``positionIds`` to (up_position, down_position).

    Newer ("v2") markets -- SOL/XRP/DOGE/BNB/HYPE -- are addressed by
    position id and must be signed against Exchange V3.  Legacy markets
    (BTC/ETH) omit the field; empty strings keep the token-id path.
    """
    position_ids = _json_list(market.get("positionIds"))
    if len(position_ids) < 2:
        if str(market.get("version") or "").strip().lower() == "v2":
            # A v2 market without position ids cannot be signed for
            # Exchange V3; the order would be rejected after posting, so
            # surface the discovery problem here instead.
            log.warning(
                "gamma.v2_market_missing_position_ids slug={}",
                market.get("slug"),
            )
        return "", ""
    up_idx, down_idx = _updown_indices(market)
    if up_idx >= len(position_ids) or down_idx >= len(position_ids):
        return "", ""
    up = str(position_ids[up_idx])
    down = str(position_ids[down_idx])
    if not up or not down or up == down:
        return "", ""
    return up, down
