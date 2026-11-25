"""Multi-market orchestrator.

One process, 6 coins x 2 timeframes (12 markets), one shared price layer:

    MarketService   Gamma discovery (current + next window per market)
    MarketWS        CLOB market WebSocket (BBO + last trade)
    midpoint loop   CLOB /midpoint every 2 s as the stable fallback
    AssetRunner ×12 one strategy loop per market
    StateStore      state.json / trades.jsonl / status.json

Each market has its own asyncio task, so a slow FAK order for one market
cannot delay another market's entry window (60 s for 5m, 180 s for 15m).
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any

from src.config import AssetConfig, Settings
from src.economics import fee_line, floor_tick
from src.runtime_config import RuntimeConfig
from src.execution.broker import Broker, PaperBroker
from src.infra.logging import get_logger
from src.infra.timeutil import day_key, utc_hms, utc_iso, window_start
from src.market.market_ws import MarketWS, MarketWSManager, PriceCache
from src.market.service import MarketService
from src.models import (
    MIN_LIMIT_PRICE,
    MIN_ORDER_SHARES,
    MIN_ORDER_USD,
    SIZE_PRECISION,
    DailyStats,
    MarketWindow,
    Phase,
    Position,
    Quote,
    Side,
    SidePlan,
    SideStatus,
    TradeRecord,
)
from src.store import StateStore
from src.strategy.rebound import SideRuntime

log = get_logger("runner")

# Re-read the exchange tick this often during a forced exit.  The CLOB
# lowers the tick mid-window (0.01 -> 0.001 was observed in the final
# minute), so the value cached at discovery is not enough for exits.
_TICK_REFRESH_S = 5.0

# After a partial SELL the CLOB can keep the matched shares locked for a
# moment.  Retrying the full remainder immediately is rejected with
# "not enough balance / allowance"; wait this long for the lock to clear.
_PARTIAL_EXIT_COOLDOWN_S = 3.5


async def _always_reserve_entry(
    side: Side, window_key: tuple[int, int], notional: float = 0.0
) -> tuple[bool, str]:
    return True, ""


async def _release_entry_noop(
    side: Side, window_key: tuple[int, int]
) -> None:
    return None


def _default_stake_policy(asset: str) -> tuple[bool, float, str]:
    """(allowed, stake multiplier, reason) when no perf policy is wired."""
    return True, 1.0, ""


class AssetRunner:
    """The strategy loop for one market (coin × timeframe)."""

    def __init__(
        self,
        asset_cfg: AssetConfig,
        settings: Settings,
        broker: Broker,
        cache: PriceCache,
        market_service: MarketService,
        store: StateStore,
        *,
        can_enter,
        on_trade,
        on_state_change,
        runtime: RuntimeConfig | None = None,
        reserve_entry=None,
        release_entry=None,
        stake_policy=None,
    ) -> None:
        self.asset = asset_cfg.asset
        self.interval = asset_cfg.interval
        self.market = asset_cfg.market
        self.cfg = asset_cfg
        self.runtime = runtime
        self.settings = settings
        self.broker = broker
        self.cache = cache
        self.market_service = market_service
        self.store = store
        self._can_enter = can_enter
        self._on_trade = on_trade
        self._on_state_change = on_state_change
        self._reserve_entry_cb = reserve_entry or _always_reserve_entry
        self._release_entry_cb = release_entry or _release_entry_noop
        self._stake_policy = stake_policy or _default_stake_policy

        self.window: MarketWindow | None = None
        self.sides: list[SideRuntime] = []
        self.retired: list[SideRuntime] = []
        self.pending_positions: list[Position] = []
        self.last_error = ""
        self.last_skip_log = 0.0
        self.last_skip_reason = ""

    def current_cfg(self) -> AssetConfig:
        """Current runtime config for this market (falls back to startup)."""
        if self.runtime is not None:
            try:
                return self.runtime.asset(self.market)
            except KeyError:
                pass
        return self.cfg

    @property
    def mode(self) -> str:
        """Active execution mode (paper/live) from the broker."""
        return getattr(self.broker, "mode", "paper")

    @property
    def is_live(self) -> bool:
        return self.mode == "live"

    # ── lifecycle ──────────────────────────────────────────────────────
    async def tick(self, now: float | None = None) -> None:
        now_f = time.time() if now is None else float(now)
        self.cfg = self.current_cfg()
        if (
            not self.cfg.enabled
            and not self.positions()
            and not self.pending_positions
        ):
            # Disabled with nothing to manage: drop the stale window so the
            # dashboard shows it as off, and pick up the next window if the
            # operator re-enables it.
            self.window = None
            self.sides = []
            return
        window = self.market_service.current(self.market, now_f)
        if window is None:
            # The market closed before the next window was discovered (or
            # discovery is temporarily failing).  Move any open position
            # to settlement now instead of leaving it in HOLDING forever.
            self._settle_after_close(now_f)
            return
        if self.window is None or self.window.start_ts != window.start_ts:
            await self._bind(window, now_f)
        for side in list(self.sides):
            try:
                await self._tick_side(side, now_f)
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                log.exception("[{}] side tick failed: {}", self.market, exc)

    def _settle_after_close(self, now: float) -> None:
        """Mark open positions SETTLE_PENDING once their window has ended."""
        if self.window is None or now < self.window.end_ts:
            return
        changed = False
        for side in self.sides:
            pos = side.position
            if (
                pos is not None
                and not pos.is_flat
                and side.status is not SideStatus.SETTLE_PENDING
            ):
                side.status = SideStatus.SETTLE_PENDING
                changed = True
                log.critical(
                    "[{}] {} {} window closed with {:.4f} shares open; "
                    "waiting for Gamma settlement",
                    self.market,
                    side.plan.side.value,
                    pos.position_id,
                    pos.remaining_shares,
                )
        if changed:
            self._on_state_change()

    async def _bind(self, window: MarketWindow, now: float) -> None:
        self.window = window
        # Keep any open position from the previous window; it is waiting
        # for Gamma settlement.
        self.retired = [
            s
            for s in self.retired
            if s.position is not None and not s.position.is_flat
        ]
        for side in self.sides:
            if side.position is not None and not side.position.is_flat:
                # The old window has ended, so this position can no longer
                # be exited.  Mark it SETTLE_PENDING explicitly: a position
                # retired while still HOLDING would otherwise never be
                # picked up by the settlement loop.
                side.status = SideStatus.SETTLE_PENDING
                self.retired.append(side)
                log.warning(
                    "[{}] retiring open {} position {} into settlement tracking",
                    self.market,
                    side.plan.side.value,
                    side.position.position_id,
                )
        self.sides = []
        plans = (
            self.cfg.plans(self.settings.execution.stake_usd)
            if self.cfg.enabled
            else []
        )
        for plan in plans:
            if self.settings.execution.skip_fee_negative:
                line = fee_line(
                    plan,
                    include_fees=self.settings.execution.include_fees,
                )
                if line.net_per_trade <= 0:
                    log.warning(
                        "[{}] skipping {}: TP does not cover fees "
                        "(net/10U={:+.3f})",
                        self.market,
                        plan.side.value,
                        line.net_per_trade,
                    )
                    continue
            self.sides.append(SideRuntime(plan=plan, window=window))

        # Adopt positions restored from state.json.
        adopted: list[Position] = []
        for pos in list(self.pending_positions):
            if pos.window_start != window.start_ts:
                continue
            if pos.interval != self.interval:
                continue
            pos.interval = self.interval
            side_rt = next(
                (s for s in self.sides if s.plan.side is pos.side), None
            )
            if side_rt is None:
                side_rt = SideRuntime(
                    plan=SidePlan(
                        asset=self.asset,
                        side=pos.side,
                        entry_thresh=0.0,
                        tp_pct=0.0,
                        stake_usd=pos.stake_usd,
                        market=self.market,
                        interval=self.interval,
                        window_sec=window.window_sec,
                        entry_window_end_s=self.current_cfg().entry_window_end_s,
                        taker_fee_rate=self.current_cfg().taker_fee_rate,
                    ),
                    window=window,
                )
                self.sides.append(side_rt)
                log.warning(
                    "[{}] adopting {} position on a side no longer configured",
                    self.market,
                    pos.side.value,
                )
            side_rt.position = pos
            side_rt.status = SideStatus.HOLDING
            side_rt.last_exit_attempt = 0.0
            # State written before the tick fix carries 0.01 even though
            # these markets may quote on 0.001.
            if window.tick_size > 0:
                pos.tick_size = window.tick_size
            # State written before the Exchange V3 routing fix carries no
            # position id; the freshly discovered window supplies it so the
            # live exit is not signed with the legacy token-id path.
            if not pos.exchange_position_id:
                pos.exchange_position_id = window.position(pos.side)
            if pos.sl_price <= 0 and side_rt.plan.stop_loss_pct > 0:
                # A position restored from a state file written while the
                # stop was off gets the currently configured stop armed.
                pos.sl_price = pos.entry_price * (
                    1.0 - float(side_rt.plan.stop_loss_pct)
                )
                log.info(
                    "[{}] armed price stop for restored {} at {:.4f}",
                    self.market,
                    pos.position_id,
                    pos.sl_price,
                )
            adopted.append(pos)
            log.info(
                "[{}] adopted {} {} shares={:.4f} entry={:.4f} tp={:.4f}",
                self.market,
                pos.side.value,
                pos.position_id,
                pos.remaining_shares,
                pos.entry_price,
                pos.tp_price,
            )
        for pos in adopted:
            if pos in self.pending_positions:
                self.pending_positions.remove(pos)

        log.info(
            "[{}] window {} start={} tick={:g} sides={}",
            self.market,
            window.slug,
            utc_hms(window.start_ts),
            window.tick_size,
            "+".join(s.plan.side.value for s in self.sides) or "-",
        )
        if self.is_live:
            known = {
                s.position.token_id
                for s in self.sides
                if s.position is not None
            }
            asyncio.create_task(
                self._warn_unknown_balances(window, known),
                name=f"balance-check-{self.market}",
            )
        self._on_state_change()

    async def _warn_unknown_balances(
        self, window: MarketWindow, known_tokens: set[str]
    ) -> None:
        """Warn about wallet tokens this process is not managing.

        A crash between a FAK fill and the state write can leave a real
        position with no local record.  We never auto-sell it (that could
        close a position the operator opened manually), but the log line is
        critical and names the token.
        """
        for side in (Side.UP, Side.DOWN):
            token = window.token(side)
            if token in known_tokens:
                continue
            try:
                balance = await self.broker.token_balance(token)
            except Exception:  # noqa: BLE001
                continue
            if balance is not None and balance > 0.5:
                log.critical(
                    "[{}] unknown {} balance {:.4f} on {} ({}) — no local "
                    "position tracks it; check the wallet manually",
                    self.market,
                    side.value,
                    balance,
                    window.slug,
                    token[:16],
                )

    # ── per-side decisions ─────────────────────────────────────────────
    async def _tick_side(self, side: SideRuntime, now: float) -> None:
        if self.window is None:
            return
        pos = side.position
        token = self.window.token(side.plan.side) if pos is None else pos.token_id
        quote = self.cache.quote(
            token,
            now=now,
            max_age=self.settings.feed.exit_stale_price_max_age_s,
        )

        if pos is None:
            await self._maybe_enter(side, now, quote)
            return

        if side.status is SideStatus.SETTLE_PENDING:
            return

        if side.window_ended(now):
            # The market is closed: no more exits are possible.  Any
            # remaining shares settle at the Gamma 0/1 outcome.
            side.status = SideStatus.SETTLE_PENDING
            log.critical(
                "[{}] {} {} window closed with {:.4f} shares open; "
                "waiting for Gamma settlement",
                self.market,
                side.plan.side.value,
                pos.position_id,
                pos.remaining_shares,
            )
            self._on_state_change()
            return

        if side.sl_due(
            now,
            quote,
            self.settings.execution,
            self.settings.feed,
        ):
            await self._exit(side, now, quote, reason="sl")
            return

        if side.tp_due(
            now,
            quote,
            self.settings.execution,
            self.settings.feed,
            mode=self.settings.execution.tp_execution_mode,
        ):
            await self._exit(side, now, quote, reason="tp")
            return

    async def _maybe_enter(
        self, side: SideRuntime, now: float, quote: Quote | None
    ) -> None:
        if not self.current_cfg().enabled:
            side.last_reason = "asset-disabled"
            self._log_skip("asset-disabled", side, now)
            return
        reason = side.entry_block_reason(
            now,
            quote,
            self.settings.execution,
            self.settings.feed,
        )
        if reason is not None:
            side.last_reason = reason
            if reason not in ("not-waiting", "outside-entry-window", "above-threshold"):
                self._log_skip(reason, side, now)
            return

        allowed, block = self._can_enter(side.plan.side)
        if not allowed:
            side.last_reason = block
            self._log_skip(block, side, now)
            return

        # Per-asset rolling performance: a coin that keeps losing gets a
        # smaller stake, and one past the hard threshold stops entering.
        perf_allowed, stake_mult, perf_reason = self._stake_policy(self.asset)
        if not perf_allowed:
            side.last_reason = perf_reason
            self._log_skip(perf_reason, side, now)
            return
        stake_usd = float(side.plan.stake_usd) * float(stake_mult)
        if stake_usd <= 0:
            side.last_reason = "asset-perf-disabled"
            self._log_skip("asset-perf-disabled", side, now)
            return

        window_key = self.window.key
        reserved, reserve_reason = await self._reserve_entry_cb(
            side.plan.side, window_key, stake_usd
        )
        if not reserved:
            side.last_reason = reserve_reason or "entry-limit"
            self._log_skip(side.last_reason, side, now)
            return
        try:
            side.status = SideStatus.ENTERING
            side.last_entry_attempt = now
            limit = floor_tick(side.plan.entry_thresh, self.window.tick_size)
            if limit <= 0:
                side.on_entry_missed(now)
                return
            try:
                fill = await self.broker.buy(
                    self.window.token(side.plan.side),
                    stake_usd,
                    limit,
                    self.window.tick_size,
                    fee_rate=self.current_cfg().taker_fee_rate,
                    position_id=self.window.position(side.plan.side),
                )
            except Exception as exc:  # noqa: BLE001
                side.on_entry_missed(now)
                log.exception("[{}] buy failed: {}", self.market, exc)
                return
            if fill is None or fill.shares <= 0:
                side.last_reason = "no-fill"
                self._log_skip("no-fill", side, now)
                side.on_entry_missed(now)
                return

            try:
                position = side.on_entry_fill(
                    fill, now, self.settings.execution
                )
            except Exception as exc:  # noqa: BLE001
                side.on_entry_missed(now)
                log.error(
                    "[{}] invalid entry fill {}: {}",
                    self.market,
                    fill,
                    exc,
                )
                return
            # The fill resolves the effective stake (plan stake scaled by
            # the rolling-performance multiplier), not the unscaled plan.
            position.stake_usd = stake_usd
            if quote is not None:
                # Snapshot the quote that triggered the entry.  The fill
                # is a taker ask; these fields record the spread it paid
                # (and whether a valid BBO existed at decision time).
                position.entry_quote_mid = float(quote.mid)
                position.entry_quote_bid = float(quote.bid)
                position.entry_quote_ask = float(quote.ask)
                position.entry_quote_spread = float(quote.spread)
                position.entry_quote_source = quote.source
                position.entry_quote_ts = float(quote.updated_at)
            log.info(
                "[{}] ENTRY {} {} shares={:.4f} px={:.4f} cost={:.4f} "
                "fee={:.4f} tp={:.4f} mid={:.4f} bid={:.4f} ask={:.4f} "
                "spread={:.4f} src={}",
                self.market,
                side.plan.side.value,
                position.position_id,
                position.shares,
                position.entry_price,
                position.cost_usd,
                position.entry_fee,
                position.tp_price,
                position.entry_quote_mid,
                position.entry_quote_bid,
                position.entry_quote_ask,
                position.entry_quote_spread,
                position.entry_quote_source,
            )
            self._on_state_change()
        finally:
            await self._release_entry_cb(side.plan.side, window_key)

    async def _exit(
        self,
        side: SideRuntime,
        now: float,
        quote: Quote | None,
        *,
        reason: str,
    ) -> None:
        pos = side.position
        if pos is None or pos.remaining_shares <= 0:
            return
        remaining = pos.remaining_shares
        if remaining < SIZE_PRECISION - 1e-9:
            # Below one size lot: worth <1 cent, write it off and close.
            record = side.on_dust(now, reason, self.mode)
            log.warning(
                "[{}] {} {} dust {:.6f} written off (reason={})",
                self.market,
                side.plan.side.value,
                pos.position_id,
                remaining,
                reason,
            )
            if record is not None:
                self._on_trade(record)
            self._on_state_change()
            return
        # The CLOB accepts a SELL at 5+ shares OR $1+ notional; only a
        # remainder below both floors cannot be posted.  Anything else
        # still gets a live attempt (the old AND check wrongly settled
        # 5-share / sub-$1 remainders).
        side.status = SideStatus.EXITING
        side.last_exit_attempt = now

        if reason == "tp" and quote is not None:
            # Snapshot the observed quote at the (last) TP attempt.  The
            # trade record compares this with the actual fill price.
            pos.tp_observed_price = float(quote.mid)
            pos.tp_observed_bid = float(quote.bid)
            pos.tp_observed_ask = float(quote.ask)
            pos.tp_observed_source = quote.source
            pos.tp_observed_ts = now

        # The position carries its own market's tick: a runner can be
        # exiting a retired position from the previous window, and ticks
        # may differ between 5m markets (0.01 vs 0.001).
        tick = pos.tick_size if pos.tick_size > 0 else self.window.tick_size

        # Refresh the tick when the bid is below the cached grid (the CLOB
        # has lowered it since discovery), and periodically during the
        # forced exit (also covers a missing quote, where the limit is the
        # tick itself).  A failed refresh keeps the cached value.
        if (
            (quote is not None and 0 < quote.bid < tick)
            or (
                reason != "tp"
                and now - side.last_tick_refresh >= _TICK_REFRESH_S
            )
        ):
            side.last_tick_refresh = now
            fresh = await self._fresh_tick(pos)
            if fresh is not None:
                tick = fresh

        if reason == "tp":
            if self.settings.execution.tp_execution_mode == "market":
                # Doc's option B: cross the book at market after the
                # observed mid crosses the target.  This can fill below
                # the target if the bid has not caught up.
                limit = self._market_exit_limit(quote, tick)
            else:
                # Observed fill with a target floor: only fill at bids at
                # or above the target; a gap to 0.42 fills at 0.42.
                limit = max(
                    float(MIN_LIMIT_PRICE),
                    tick,
                    floor_tick(pos.tp_price, tick),
                )
        else:
            limit = self._market_exit_limit(quote, tick)

        if (
            remaining < MIN_ORDER_SHARES - 1e-9
            and remaining * limit < MIN_ORDER_USD - 1e-9
        ):
            # Both floors fail: the CLOB will not accept this sell.  Leave
            # the value for Gamma settlement instead of retrying.
            side.on_dust_pending(now, reason)
            log.warning(
                "[{}] {} {} shares {:.4f} < {:.2f} and notional {:.4f}U "
                "< {:.2f}U; waiting for settlement",
                self.market,
                side.plan.side.value,
                pos.position_id,
                remaining,
                MIN_ORDER_SHARES,
                remaining * limit,
                MIN_ORDER_USD,
            )
            self._on_state_change()
            return

        try:
            fill = await self.broker.sell(
                pos.token_id,
                pos.remaining_shares,
                limit,
                tick,
                fee_rate=self.current_cfg().taker_fee_rate,
                position_id=pos.exchange_position_id,
            )
        except Exception as exc:  # noqa: BLE001
            side.on_exit_missed(now)
            log.exception("[{}] sell failed: {}", self.market, exc)
            return
        if fill is None or fill.shares <= 0:
            side.on_exit_missed(now)
            return

        record = side.on_exit_fill(
            fill, now, reason, self.settings.execution, self.mode
        )
        log.info(
            "[{}] EXIT {} {} reason={} mode={} tp_target={:.4f} "
            "shares={:.4f} px={:.4f} usdc={:.4f}",
            self.market,
            side.plan.side.value,
            pos.position_id,
            reason,
            self.settings.execution.tp_execution_mode
            if reason == "tp"
            else "-",
            pos.tp_price,
            fill.shares,
            fill.price,
            fill.usdc,
        )
        sold = remaining - pos.remaining_shares
        if record is None and sold > 0 and not pos.is_flat:
            # The CLOB may still count the matched shares as locked.
            # Selling the full remainder now is rejected for balance, so
            # hold the next attempt until the fill has settled.
            side.exit_cooldown_until = now + _PARTIAL_EXIT_COOLDOWN_S
            log.info(
                "[{}] EXIT {} {} partial sold={:.4f} remaining={:.4f}; "
                "next sell in {:.1f}s (matched shares settling)",
                self.market,
                side.plan.side.value,
                pos.position_id,
                sold,
                pos.remaining_shares,
                _PARTIAL_EXIT_COOLDOWN_S,
            )
        if record is not None:
            self._on_trade(record)
        self._on_state_change()

    async def _fresh_tick(self, pos: Position) -> float | None:
        """Re-read the exchange tick and update the position."""
        getter = getattr(self.market_service, "tick_size", None)
        if getter is None:
            return None
        try:
            tick = await getter(pos.token_id)
        except Exception:  # noqa: BLE001
            return None
        if tick is None or tick <= 0:
            return None
        tick = float(tick)
        if abs(tick - pos.tick_size) > 1e-12:
            log.info(
                "[{}] {} tick changed {:.4g} -> {:.4g}",
                self.market,
                pos.position_id,
                pos.tick_size,
                tick,
            )
            pos.tick_size = tick
        return tick

    def _market_exit_limit(self, quote: Quote | None, tick: float) -> float:
        """Marketable SELL limit inside the CLOB price range.

        Round the slippage-adjusted bid down to the tick and never place
        the limit above the bid (a higher FAK cannot cross).  The CLOB
        rejects any price below 0.01 even when the tick is 0.001, so the
        limit is clamped into [0.01, max(bid, 0.01)]: near zero the order
        may not fill, but it stays valid and can cross if the bid recovers
        before the window closes.
        """
        tick_f = float(tick) if tick and tick > 0 else 0.01
        low = max(float(MIN_LIMIT_PRICE), tick_f)
        slip = max(0.0, self.settings.execution.slippage_pct) / 100.0
        if quote is not None and quote.bid > 0:
            bid = float(quote.bid)
            limit = floor_tick(bid * (1.0 - slip), tick_f)
            if limit <= 0:
                # The slippage-adjusted price is below one tick: sell at
                # the best bid itself rather than not at all.
                limit = floor_tick(bid, tick_f)
            if limit <= 0:
                # Book price is below the assumed tick (stale/unknown
                # tick); the exchange minimum keeps the order valid.
                limit = bid
            return min(max(limit, low), max(bid, low))
        return low

    def _log_skip(
        self, reason: str, side: SideRuntime, now: float
    ) -> None:
        if reason == self.last_skip_reason and now - self.last_skip_log < 5.0:
            return
        self.last_skip_reason = reason
        self.last_skip_log = now
        log.debug(
            "[{}] {} entry skipped: {}",
            self.market,
            side.plan.side.value,
            reason,
        )

    # ── flatten on shutdown ────────────────────────────────────────────
    async def flatten(self, now: float) -> None:
        for side in self.all_sides():
            if side.position is None or side.position.is_flat:
                continue
            pos = side.position
            remaining = pos.remaining_shares
            if remaining < SIZE_PRECISION - 1e-9:
                record = side.on_dust(now, "shutdown", self.mode)
                log.warning(
                    "[{}] {} dust {:.6f} written off (shutdown)",
                    self.market,
                    pos.position_id,
                    remaining,
                )
                if record is not None:
                    self._on_trade(record)
                continue
            tick = pos.tick_size if pos.tick_size > 0 else (
                self.window.tick_size if self.window else 0.01
            )
            fresh = await self._fresh_tick(pos)
            if fresh is not None:
                tick = fresh
            if (
                remaining < MIN_ORDER_SHARES - 1e-9
                and remaining * tick < MIN_ORDER_USD - 1e-9
            ):
                side.on_dust_pending(now, "shutdown")
                log.warning(
                    "[{}] {} shares {:.4f} < {:.2f} and notional "
                    "{:.4f}U < {:.2f}U; left for settlement",
                    self.market,
                    pos.position_id,
                    remaining,
                    MIN_ORDER_SHARES,
                    remaining * tick,
                    MIN_ORDER_USD,
                )
                continue
            try:
                fill = await self.broker.sell(
                    pos.token_id,
                    pos.remaining_shares,
                    tick,
                    tick,
                    fee_rate=self.current_cfg().taker_fee_rate,
                    position_id=pos.exchange_position_id,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("[{}] flatten sell error: {}", self.market, exc)
                fill = None
            if fill is None or fill.shares <= 0:
                log.error(
                    "[{}] flatten failed for {}", self.market, pos.position_id
                )
                continue
            record = side.on_exit_fill(
                fill,
                now,
                "shutdown",
                self.settings.execution,
                self.mode,
            )
            log.info(
                "[{}] FLATTEN {} shares={:.4f} px={:.4f}",
                self.market,
                pos.position_id,
                fill.shares,
                fill.price,
            )
            if record is not None:
                self._on_trade(record)
        self._on_state_change()

    # ── introspection ──────────────────────────────────────────────────
    def all_sides(self) -> list[SideRuntime]:
        return self.sides + self.retired

    def positions(self) -> list[Position]:
        return [
            s.position
            for s in self.all_sides()
            if s.position is not None and not s.position.is_flat
            and s.status is not SideStatus.CLOSED
        ]

    def open_tokens(self) -> set[str]:
        return {
            p.token_id
            for p in self.positions()
            if p.token_id
        }

    def describe(self, now: float) -> str:
        cfg = self.current_cfg()
        if not cfg.enabled and not self.positions():
            return f"{self.market}: disabled"
        if self.window is None:
            return f"{self.market}: no window"
        parts = [f"{self.market} {utc_hms(self.window.start_ts)}"]
        for side in self.sides:
            quote = self.cache.quote(
                self.window.token(side.plan.side), now=now, max_age=30.0
            )
            parts.append(side.describe(now, quote))
        for side in self.retired:
            parts.append(f"[retired]{side.plan.side.value}:{side.status.value}")
        return " | ".join(parts)

    def snapshot(self, now: float) -> dict[str, Any]:
        window = self.window
        cfg = self.current_cfg()
        sides: dict[str, Any] = {}
        for side in self.all_sides():
            pos = side.position
            quote = (
                self.cache.quote(pos.token_id, now=now, max_age=30.0)
                if pos is not None and pos.token_id
                else (
                    self.cache.quote(
                        window.token(side.plan.side), now=now, max_age=30.0
                    )
                    if window is not None
                    else None
                )
            )
            sides[f"{side.plan.side.value}{'[retired]' if side in self.retired else ''}"] = {
                "status": side.status.value,
                "phase": (
                    side.phase(now).value if window is not None else None
                ),
                "entry_thresh": side.plan.entry_thresh,
                "tp_pct": side.plan.tp_pct,
                "stop_loss_pct": side.plan.stop_loss_pct,
                "sl_price": round(pos.sl_price, 4) if pos is not None else None,
                "mid": round(quote.mid, 4) if quote else None,
                "bid": round(quote.bid, 4) if quote else None,
                "ask": round(quote.ask, 4) if quote else None,
                "quote_source": quote.source if quote else None,
                "quote_age_s": round(quote.age(now), 2) if quote else None,
                "position": pos.to_dict() if pos is not None else None,
                "unrealized_usd": round(side.unrealized(quote), 4)
                if pos is not None
                else 0.0,
                "entry_attempts": side.entry_attempts,
                "exit_attempts": side.exit_attempts,
                "last_reason": side.last_reason,
            }
        return {
            "market": self.market,
            "asset": self.asset,
            "interval": cfg.interval,
            "window_sec": cfg.window_sec,
            "entry_window_end_s": cfg.entry_window_end_s,
            "window": {
                "slug": window.slug,
                "start_ts": window.start_ts,
                "end_ts": window.end_ts,
                "interval": window.interval,
                "window_sec": window.window_sec,
                "phase": (
                    phase_for(
                        now,
                        window,
                        cfg.entry_window_end_s,
                    ).value
                    if window is not None
                    else None
                ),
            }
            if window is not None
            else None,
            "sides": sides,
            "enabled": cfg.enabled,
            "config": {
                "enabled": cfg.enabled,
                "sides": [s.value for s in cfg.sides],
                "entry_thresh": cfg.entry_thresh,
                "tp_pct": cfg.tp_pct,
                "stop_loss_pct": cfg.stop_loss_pct,
                "stake_usd": cfg.stake_usd
                or self.settings.execution.stake_usd,
                "window_sec": cfg.window_sec,
                "entry_window_end_s": cfg.entry_window_end_s,
            },
            "last_error": self.last_error,
        }


def phase_for(
    now: float,
    window: MarketWindow,
    entry_window_end_s: float,
) -> Phase:
    t = window.seconds_since(now)
    if t < 0:
        return Phase.PRE
    if t <= entry_window_end_s:
        return Phase.ENTRY
    if t < window.window_sec:
        return Phase.TP
    return Phase.ENDED


class MultiAssetRunner:
    def __init__(
        self,
        settings: Settings,
        pm,
        cache: PriceCache,
        market_service: MarketService,
        broker: Broker,
        store: StateStore,
        *,
        market_ws: MarketWSManager | MarketWS | None = None,
        runtime: RuntimeConfig | None = None,
    ) -> None:
        self.settings = settings
        self.pm = pm
        self.cache = cache
        self.market_service = market_service
        self.broker = broker
        self.store = store
        self.market_ws = market_ws
        self.runtime = runtime or RuntimeConfig.from_settings(
            settings.data_dir / "runtime_config.json", settings
        )

        self.positions, self.daily = store.load()
        self._stale: list[Position] = []
        self._tasks: list[asyncio.Task] = []
        self._running = False
        self._pause_reason = ""
        self.pause_new_entries = settings.execution.pause_new_entries
        self.pause_reason = "paused" if self.pause_new_entries else ""
        self._last_kill_state: bool | None = None
        self._last_daily_warn = 0.0
        self._started_at = time.time()
        self._entry_lock = asyncio.Lock()
        # (side, (window_sec, window_start)) -> in-flight entry notionals.
        # Keyed per window so a settled position from the previous window
        # cannot block entries in the next one, and a 5m and a 15m window
        # with the same start timestamp stay separate.
        self._entry_reservations: dict[
            tuple[Side, tuple[int, int]], list[float]
        ] = {}
        # (exit_ts, asset, pnl_net) from trades.jsonl, for the rolling
        # per-asset performance rule.  Loaded once so a restart keeps the
        # lookback instead of starting from zero.
        self._asset_pnl: deque[tuple[float, str, float]] = deque()
        self._load_asset_history()

        # Build every configured market, including disabled ones: the Web
        # config page can turn a market on without restarting the process.
        self.markets: dict[str, AssetRunner] = {}
        for asset_cfg in settings.assets:
            self.markets[asset_cfg.market] = AssetRunner(
                asset_cfg,
                settings,
                broker,
                cache,
                market_service,
                store,
                can_enter=self.entries_allowed,
                on_trade=self._on_trade,
                on_state_change=self._save_state,
                runtime=self.runtime,
                reserve_entry=self.reserve_entry,
                release_entry=self.release_entry,
                stake_policy=self.asset_perf_policy,
            )

    def same_side_open(self, side: Side) -> int:
        return sum(
            1 for pos in self.all_positions() if pos.side is side
        )

    def same_side_open_in_window(
        self, side: Side, window_key: tuple[int, int]
    ) -> int:
        """Open same-side positions opened in one market window.

        Positions from earlier windows that are still in settlement do not
        count: they are no longer at risk and must not block the new window.
        """
        return sum(
            1
            for pos in self.all_positions()
            if pos.side is side and pos.window_key == window_key
        )

    def positions_in_window(
        self, window_key: tuple[int, int]
    ) -> list[Position]:
        """Open positions (both sides, all markets) in one window.

        ``window_key`` is ``(window_sec, start_ts)``: a 5m and a 15m
        window that start together are counted separately.
        """
        return [
            pos
            for pos in self.all_positions()
            if pos.window_key == window_key
        ]

    def window_open_notional(self, window_key: tuple[int, int]) -> float:
        """Capital deployed by the open positions of one market window."""
        return sum(
            float(getattr(pos, "cost_usd", 0.0) or 0.0)
            for pos in self.positions_in_window(window_key)
        )

    def _same_side_reserved(self, side: Side) -> int:
        return sum(
            len(amounts)
            for (reserved_side, _), amounts in self._entry_reservations.items()
            if reserved_side is side
        )

    def _window_reserved(
        self, window_key: tuple[int, int]
    ) -> tuple[int, float]:
        """(count, notional) of in-flight entries reserved for a window."""
        count = 0
        notional = 0.0
        for (_, reserved_window), amounts in self._entry_reservations.items():
            if reserved_window == window_key:
                count += len(amounts)
                notional += sum(amounts)
        return count, notional

    def _all_reserved(self) -> tuple[int, float]:
        """(count, notional) of every in-flight entry, all intervals."""
        count = 0
        notional = 0.0
        for amounts in self._entry_reservations.values():
            count += len(amounts)
            notional += sum(amounts)
        return count, notional

    def global_open_exposure(self) -> tuple[int, float]:
        """(open positions + in-flight entries, cost + reserved notional).

        Covers BOTH timeframes and both sides.  Positions waiting for
        settlement still count: their 0/1 outcome is not known yet.
        """
        positions = self.all_positions()
        reserved_count, reserved_notional = self._all_reserved()
        notional = sum(
            float(getattr(pos, "cost_usd", 0.0) or 0.0)
            for pos in positions
        )
        return (
            len(positions) + reserved_count,
            notional + reserved_notional,
        )

    async def reserve_entry(
        self,
        side: Side,
        window_key: tuple[int, int],
        notional: float = 0.0,
    ) -> tuple[bool, str]:
        """Reserve one entry slot for ``window_key``.

        Six caps apply (0 disables each): the global concurrent open
        position / notional caps across both timeframes, the global
        concurrent same-side cap, the same-side per-window cap, the total
        entries per window across both sides / all markets, and the total
        entry notional per window.  In-flight attempts count too, so two
        markets cannot both pass the check at the same moment.  Returns
        ``(ok, reason)``.
        """
        e = self.settings.execution
        limit_total = int(e.max_same_side_positions)
        limit_open = self.max_open_positions
        limit_open_notional = self.max_open_notional_usd
        limit_side_window = self.max_same_side_per_window
        limit_window_count = self.max_positions_per_window
        limit_window_notional = self.max_window_notional_usd
        if (
            limit_total <= 0
            and limit_open <= 0
            and limit_open_notional <= 0
            and limit_side_window <= 0
            and limit_window_count <= 0
            and limit_window_notional <= 0
        ):
            return True, ""
        window_key = (int(window_key[0]), int(window_key[1]))
        notional = max(0.0, float(notional or 0.0))
        key = (side, window_key)
        async with self._entry_lock:
            if limit_open > 0 or limit_open_notional > 0:
                used_open, used_open_notional = self.global_open_exposure()
                if limit_open > 0 and used_open >= limit_open:
                    return False, "open-position-limit"
                if (
                    limit_open_notional > 0
                    and used_open_notional + notional
                    > limit_open_notional + 1e-9
                ):
                    return False, "open-notional-limit"
            if limit_total > 0:
                used_total = (
                    self.same_side_open(side) + self._same_side_reserved(side)
                )
                if used_total >= limit_total:
                    return False, "same-side-limit"
            if limit_side_window > 0:
                used_window = (
                    self.same_side_open_in_window(side, window_key)
                    + len(self._entry_reservations.get(key, ()))
                )
                if used_window >= limit_side_window:
                    return False, "same-side-limit"
            if limit_window_count > 0 or limit_window_notional > 0:
                reserved_count, reserved_notional = self._window_reserved(
                    window_key
                )
                if limit_window_count > 0:
                    used_count = (
                        len(self.positions_in_window(window_key))
                        + reserved_count
                    )
                    if used_count >= limit_window_count:
                        return False, "window-position-limit"
                if limit_window_notional > 0:
                    used_notional = (
                        self.window_open_notional(window_key)
                        + reserved_notional
                    )
                    if used_notional + notional > limit_window_notional + 1e-9:
                        return False, "window-notional-limit"
            self._entry_reservations.setdefault(key, []).append(notional)
            return True, ""

    async def release_entry(
        self, side: Side, window_key: tuple[int, int]
    ) -> None:
        key = (side, (int(window_key[0]), int(window_key[1])))
        async with self._entry_lock:
            amounts = self._entry_reservations.get(key)
            if not amounts:
                return
            amounts.pop()
            if not amounts:
                self._entry_reservations.pop(key, None)

    # ── runtime paper/live mode ────────────────────────────────────────
    @property
    def mode(self) -> str:
        return getattr(self.broker, "mode", "paper")

    @property
    def is_live(self) -> bool:
        return self.mode == "live"

    @property
    def live_available(self) -> bool:
        return bool(getattr(self.broker, "live_available", False))

    @property
    def live_allowed(self) -> bool:
        return bool(self.settings.live)

    def _runtime_value(self, field: str, default: Any) -> Any:
        """Runtime override when one is set, else the startup config."""
        if self.runtime is not None:
            value = getattr(self.runtime, field, None)
            if value is not None:
                return value
        return default

    @property
    def max_daily_loss_usd(self) -> float:
        """Effective daily loss cap (runtime override or startup config)."""
        return float(
            self._runtime_value(
                "max_daily_loss_usd",
                self.settings.execution.max_daily_loss_usd,
            )
        )

    @property
    def max_open_positions(self) -> int:
        """Effective global concurrent position cap (runtime override)."""
        return int(
            self._runtime_value(
                "max_open_positions",
                self.settings.execution.max_open_positions,
            )
        )

    @property
    def max_open_notional_usd(self) -> float:
        """Effective global concurrent notional cap (runtime override)."""
        return float(
            self._runtime_value(
                "max_open_notional_usd",
                self.settings.execution.max_open_notional_usd,
            )
        )

    @property
    def max_same_side_per_window(self) -> int:
        """Effective same-side per-window cap (runtime override)."""
        return int(
            self._runtime_value(
                "max_same_side_per_window",
                self.settings.execution.max_same_side_per_window,
            )
        )

    @property
    def max_intraday_drawdown_usd(self) -> float:
        """Effective intraday drawdown pause (0 = off)."""
        return float(
            self._runtime_value(
                "max_intraday_drawdown_usd",
                self.settings.execution.max_intraday_drawdown_usd,
            )
        )

    @property
    def intraday_drawdown_pause_min(self) -> float:
        """Cooldown after a drawdown trigger (0 = drawdown pause off)."""
        return float(
            self._runtime_value(
                "intraday_drawdown_pause_min",
                self.settings.execution.intraday_drawdown_pause_min,
            )
        )

    @property
    def max_positions_per_window(self) -> int:
        """Effective per-window total position cap (0 = off)."""
        return int(
            self._runtime_value(
                "max_positions_per_window",
                self.settings.execution.max_positions_per_window,
            )
        )

    @property
    def max_window_notional_usd(self) -> float:
        """Effective per-window total notional cap (0 = off)."""
        return float(
            self._runtime_value(
                "max_window_notional_usd",
                self.settings.execution.max_window_notional_usd,
            )
        )

    @property
    def asset_perf_lookback_days(self) -> float:
        """Rolling-performance lookback (0 = rule off)."""
        return float(
            self._runtime_value(
                "asset_perf_lookback_days",
                self.settings.execution.asset_perf_lookback_days,
            )
        )

    @property
    def asset_perf_min_pnl_usd(self) -> float:
        return float(
            self._runtime_value(
                "asset_perf_min_pnl_usd",
                self.settings.execution.asset_perf_min_pnl_usd,
            )
        )

    @property
    def asset_perf_stake_mult(self) -> float:
        return float(
            self._runtime_value(
                "asset_perf_stake_mult",
                self.settings.execution.asset_perf_stake_mult,
            )
        )

    @property
    def asset_perf_disable_pnl_usd(self) -> float:
        return float(
            self._runtime_value(
                "asset_perf_disable_pnl_usd",
                self.settings.execution.asset_perf_disable_pnl_usd,
            )
        )

    # ── rolling per-asset performance ──────────────────────────────────
    def _load_asset_history(self) -> None:
        """Seed the rolling performance window from trades.jsonl."""
        try:
            rows = self.store.all_trades()
        except Exception as exc:  # noqa: BLE001 - audit file must not block startup
            log.warning("runner.asset_history_failed error={}", exc)
            return
        for row in rows:
            try:
                ts = float(row.get("exit_ts") or 0.0)
                pnl = float(row.get("pnl_net") or 0.0)
            except (TypeError, ValueError):
                continue
            asset = str(row.get("asset") or "").strip().upper()
            if asset and ts > 0:
                self._asset_pnl.append((ts, asset, pnl))
        if self._asset_pnl:
            log.info("runner.asset_history trades={}", len(self._asset_pnl))

    def asset_rolling_pnl(
        self, asset: str, now: float | None = None
    ) -> float:
        """Realized net PnL of ``asset`` inside the lookback window."""
        lookback = self.asset_perf_lookback_days
        if lookback <= 0:
            return 0.0
        now_f = time.time() if now is None else float(now)
        cutoff = now_f - lookback * 86400.0
        while self._asset_pnl and self._asset_pnl[0][0] < cutoff:
            self._asset_pnl.popleft()
        name = asset.upper()
        return sum(
            pnl for ts, a, pnl in self._asset_pnl
            if a == name and ts >= cutoff
        )

    def asset_perf_policy(
        self, asset: str, now: float | None = None
    ) -> tuple[bool, float, str]:
        """(allowed, stake multiplier, reason) for this asset's entries.

        Downweights once the rolling PnL is at or below the soft threshold;
        skips the asset entirely at or below the hard (disable) threshold.
        """
        if self.asset_perf_lookback_days <= 0:
            return True, 1.0, ""
        pnl = self.asset_rolling_pnl(asset, now)
        disable_at = self.asset_perf_disable_pnl_usd
        if disable_at < 0 and pnl <= disable_at:
            return False, 0.0, "asset-perf-disabled"
        if pnl <= self.asset_perf_min_pnl_usd:
            mult = float(self.asset_perf_stake_mult)
            if mult <= 0:
                return False, 0.0, "asset-perf-disabled"
            return True, mult, "asset-perf-downweight"
        return True, 1.0, ""

    def asset_perf_snapshot(
        self, now: float | None = None
    ) -> dict[str, dict[str, Any]]:
        """Per-market rolling PnL / stake multiplier for the dashboard.

        Realized PnL is still grouped per coin: a losing ETH 5m record
        also downweights ETH 15m.  The snapshot is keyed by market so the
        UI can show the tag on both rows.
        """
        if self.asset_perf_lookback_days <= 0:
            return {}
        out: dict[str, dict[str, Any]] = {}
        for market, runner in self.markets.items():
            allowed, mult, reason = self.asset_perf_policy(
                runner.asset, now
            )
            out[market] = {
                "asset": runner.asset,
                "rolling_pnl": round(
                    self.asset_rolling_pnl(runner.asset, now), 4
                ),
                "stake_mult": mult,
                "blocked": not allowed,
                "reason": reason,
            }
        return out

    def mode_switch_block_reason(self) -> str:
        """Why the paper/live switch is not allowed right now ("" = ok)."""
        if self.all_positions():
            return "open positions"
        if any(self._entry_reservations.values()):
            return "entry in flight"
        for runner in self.markets.values():
            for side in runner.all_sides():
                if side.status is SideStatus.ENTERING:
                    return "entry in flight"
        return ""

    def set_mode(self, mode: str) -> str:
        mode = str(mode).strip().lower()
        if mode not in ("paper", "live"):
            raise ValueError("mode must be 'paper' or 'live'")
        reason = self.mode_switch_block_reason()
        if reason:
            raise ValueError(f"cannot switch mode: {reason}")
        if mode == "live" and not self.live_allowed:
            raise ValueError("live mode is not enabled in .env")
        if mode == "live" and not self.live_available:
            raise ValueError("live broker is not configured")
        self.broker.set_mode(mode)
        if self.runtime is not None:
            self.runtime.set_mode(mode)
        log.warning("runtime mode switched to {}", mode)
        return mode

    def enabled_markets(self) -> set[str]:
        out: set[str] = set()
        for name in self.markets:
            try:
                if self.runtime.asset(name).enabled:
                    out.add(name)
            except KeyError:
                continue
        return out

    # ── risk ───────────────────────────────────────────────────────────
    @property
    def paused(self) -> bool:
        return bool(self.pause_new_entries)

    def set_pause(self, paused: bool, reason: str = "") -> None:
        """Runtime pause/resume, for the Web backend.

        This only blocks NEW entries; open positions still take profit and
        force-exit normally.
        """
        self.pause_new_entries = bool(paused)
        self.pause_reason = (
            (reason or "paused") if self.pause_new_entries else ""
        )
        log.warning(
            "runtime pause={} reason={}",
            self.pause_new_entries,
            self.pause_reason or "-",
        )

    async def flatten_open_positions(self, now: float | None = None) -> int:
        """Market-sell every open position (best effort).  Returns count."""
        now_f = time.time() if now is None else float(now)
        sold = 0
        for asset in self.markets.values():
            before = len(asset.positions())
            await asset.flatten(now_f)
            sold += max(0, before - len(asset.positions()))
        self._save_state()
        log.warning("runtime flatten_open_positions sold={}", sold)
        return sold

    def intraday_drawdown(self, now: float | None = None) -> float:
        """How far today's realized PnL sits below its high-water mark."""
        now_f = time.time() if now is None else float(now)
        stats = self.daily.get(day_key(now_f))
        if stats is None:
            return 0.0
        return max(0.0, stats.peak_pnl - stats.realized_pnl)

    def _drawdown_paused(self, today: DailyStats, now_f: float) -> bool:
        """Apply or expire the intraday drawdown cooldown.

        Returns True while entries must stay paused.  A trigger pauses
        entries for ``intraday_drawdown_pause_min`` minutes; when that
        expires the drawdown anchor is rebased to the current PnL, so the
        next pause needs a fresh drawdown of the same size instead of
        re-triggering on the old high-water mark.
        """
        limit = self.max_intraday_drawdown_usd
        cooldown_min = self.intraday_drawdown_pause_min
        if limit <= 0 or cooldown_min <= 0:
            return False
        if today.drawdown_pause_until > now_f:
            return True
        if today.drawdown_pause_until > 0:
            today.peak_pnl = today.realized_pnl
            today.drawdown_pause_until = 0.0
            self._save_state()
            log.warning(
                "intraday drawdown cooldown over; entries resumed "
                "(anchor={:+.4f}U)",
                today.peak_pnl,
            )
            return False
        drawdown = today.peak_pnl - today.realized_pnl
        if drawdown >= limit - 1e-9:
            today.drawdown_pause_until = now_f + cooldown_min * 60.0
            self._save_state()
            log.warning(
                "intraday drawdown {:.4f}U >= {:.4f}U; entries paused "
                "for {:g} min (until {})",
                drawdown,
                limit,
                cooldown_min,
                utc_iso(today.drawdown_pause_until),
            )
            return True
        return False

    def entries_allowed(
        self, side: Side | None = None, now: float | None = None
    ) -> tuple[bool, str]:
        e = self.settings.execution
        if self.pause_new_entries:
            return False, self.pause_reason or "paused"
        kill_path = self.settings.resolve_path(e.kill_switch_file)
        if e.kill_switch_file and kill_path.exists():
            return False, "kill-switch"
        now_f = time.time() if now is None else float(now)
        today = self.daily.get(day_key(now_f))
        if today is None:
            return True, "ok"
        limit = self.max_daily_loss_usd
        if limit > 0 and today.realized_pnl <= -limit:
            return False, "daily-loss-limit"
        if self._drawdown_paused(today, now_f):
            return False, "intraday-drawdown"
        return True, "ok"

    def _on_trade(self, record: TradeRecord) -> None:
        self.store.append_trade(record)
        self._asset_pnl.append(
            (float(record.exit_ts), record.asset.upper(), float(record.pnl_net))
        )
        day = day_key(record.exit_ts)
        stats = self.daily.setdefault(day, DailyStats())
        stats.add(
            record.pnl_net,
            record.pnl_gross,
            record.entry_fee + record.exit_fee,
        )
        log.info(
            "[TRADE] {} {} {} reason={} pnl_net={:+.4f}U pnl_gross={:+.4f}U "
            "day={:+.4f}U trades={}",
            record.asset,
            record.side,
            record.position_id,
            record.reason,
            record.pnl_net,
            record.pnl_gross,
            stats.realized_pnl,
            stats.trades,
        )
        self._save_state()

    def _save_state(self) -> None:
        self.store.save(self.all_positions(), self.daily)

    def all_positions(self) -> list[Position]:
        out: list[Position] = []
        seen: set[str] = set()
        for asset in self.markets.values():
            for pos in asset.positions() + asset.pending_positions:
                if pos.position_id not in seen and not pos.is_flat:
                    seen.add(pos.position_id)
                    out.append(pos)
        for pos in self._stale:
            if pos.position_id not in seen and not pos.is_flat:
                seen.add(pos.position_id)
                out.append(pos)
        return out

    def open_tokens(self) -> set[str]:
        tokens: set[str] = set()
        for asset in self.markets.values():
            tokens.update(asset.open_tokens())
        tokens.update(p.token_id for p in self._stale if p.token_id)
        return tokens

    # ── lifecycle ──────────────────────────────────────────────────────
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        await self._prepare_restored_positions()

        if self.market_ws is not None and self.settings.feed.use_ws:
            await self.market_ws.start()

        self._tasks = [
            asyncio.create_task(self._discovery_loop(), name="discovery"),
            asyncio.create_task(self._midpoint_loop(), name="midpoint"),
            asyncio.create_task(self._book_loop(), name="book"),
            asyncio.create_task(self._status_loop(), name="status"),
            asyncio.create_task(self._reconcile_loop(), name="reconcile"),
        ]
        for market, runner in self.markets.items():
            self._tasks.append(
                asyncio.create_task(
                    self._market_loop(market, runner),
                    name=f"market-{market}",
                )
            )
        log.info(
            "runner.started mode={} live_available={} markets={}",
            self.mode,
            self.live_available,
            ",".join(self.markets),
        )

    async def stop(self, *, flatten: bool | None = None) -> None:
        if not self._running:
            return
        self._running = False
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                pass
        self._tasks = []

        do_flatten = (
            self.settings.execution.flatten_on_exit
            if flatten is None
            else flatten
        )
        if do_flatten:
            now = time.time()
            for asset in self.markets.values():
                try:
                    await asset.flatten(now)
                except Exception as exc:  # noqa: BLE001
                    log.warning("[{}] flatten error: {}", asset.asset, exc)
        self._save_state()
        if self.market_ws is not None:
            await self.market_ws.stop()
        log.info("runner.stopped")

    async def _prepare_restored_positions(self) -> None:
        """Adopt active positions; queue ended ones for settlement."""
        now = time.time()
        for pos in list(self.positions):
            runner = self.markets.get(pos.market)
            if runner is None:
                # The market is no longer configured.  Keep it for settlement.
                self._stale.append(pos)
                continue
            if pos.window_end > now:
                # Live: the wallet balance is the source of truth.  Paper:
                # re-register the holding in the paper ledger.
                if self.is_live:
                    balance = await self.broker.token_balance(pos.token_id)
                    if balance is None:
                        log.warning(
                            "[{}] balance query failed for {}; keeping stored shares",
                            pos.asset,
                            pos.position_id,
                        )
                    elif balance <= 0:
                        log.warning(
                            "[{}] {} has zero on-chain balance; dropping",
                            pos.asset,
                            pos.position_id,
                        )
                        continue
                    else:
                        pos.shares = pos.sold_shares + balance
                else:
                    paper = getattr(self.broker, "paper", None)
                    if paper is None and isinstance(self.broker, PaperBroker):
                        paper = self.broker
                    if paper is not None:
                        paper.adopt(pos.token_id, pos.remaining_shares)
                runner.pending_positions.append(pos)
            else:
                self._stale.append(pos)
        if self._stale:
            log.info(
                "runner.restored_stale n={} (will resolve)", len(self._stale)
            )
        # The loaded list has been distributed to runners/pending/stale;
        # keeping it would duplicate positions in all_positions().
        self.positions = []

    # ── loops ──────────────────────────────────────────────────────────
    async def _market_loop(self, market: str, runner: AssetRunner) -> None:
        while self._running:
            try:
                await runner.tick(time.time())
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                runner.last_error = str(exc)
                log.exception("[{}] tick failed: {}", market, exc)
            await asyncio.sleep(max(0.05, self.settings.general.tick_s))

    async def _discovery_loop(self) -> None:
        while self._running:
            try:
                now = time.time()
                markets = list(self.markets)
                results = await asyncio.gather(
                    *(self.market_service.refresh(m, now) for m in markets),
                    return_exceptions=True,
                )
                for market, result in zip(markets, results):
                    if isinstance(result, Exception):
                        log.warning(
                            "[{}] discovery error: {}", market, result
                        )
                self._update_ws_tokens()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception("discovery loop error: {}", exc)
            await asyncio.sleep(max(0.5, self.settings.feed.market_discovery_s))

    async def _midpoint_loop(self) -> None:
        sem = asyncio.Semaphore(8)
        while self._running:
            try:
                # Current window + open positions.  Next-window tokens are
                # WS-subscribed early but not REST-polled until they start.
                tokens = list(
                    self.market_service.poll_tokens(
                        self.open_tokens(), markets=self.enabled_markets()
                    )
                )
                if tokens:
                    async def one(token: str) -> None:
                        async with sem:
                            mid = await self.pm.get_midpoint(token)
                            if mid > 0:
                                self.cache.set_midpoint(token, mid)

                    await asyncio.gather(
                        *(one(t) for t in tokens), return_exceptions=True
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception("midpoint loop error: {}", exc)
            await asyncio.sleep(max(0.5, self.settings.feed.rest_midpoint_poll_s))

    async def _book_loop(self) -> None:
        """Poll REST /book for current tokens.

        This keeps bid/ask (and depth) available when the market WS is
        down, which matters because TP/entry decisions and the paper
        broker all use the bid side for execution.
        """
        sem = asyncio.Semaphore(4)
        while self._running:
            try:
                tokens = list(
                    self.market_service.poll_tokens(
                        self.open_tokens(), markets=self.enabled_markets()
                    )
                )
                if tokens:
                    async def one(token: str) -> None:
                        async with sem:
                            book = await self.pm.get_order_book(token)
                            if isinstance(book, dict):
                                self.cache.set_book(
                                    token,
                                    book.get("bids"),
                                    book.get("asks"),
                                    source="rest_book",
                                )

                    await asyncio.gather(
                        *(one(t) for t in tokens), return_exceptions=True
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception("book loop error: {}", exc)
            await asyncio.sleep(max(1.0, self.settings.feed.book_poll_s))

    async def _reconcile_loop(self) -> None:
        while self._running:
            try:
                self._promote_expired_pending()
                await self._resolve_stale()
            except Exception as exc:  # noqa: BLE001
                log.warning("reconcile error: {}", exc)
            await asyncio.sleep(10.0)

    def _promote_expired_pending(self) -> None:
        """Move restored positions whose window ended before they bound.

        A restart with only 1–2 s left in a window can queue a position as
        'pending', then discovery takes longer than that and the window is
        gone.  Such a position can no longer be traded; send it to the
        settlement path instead of leaving it stranded.
        """
        now = time.time()
        for runner in self.markets.values():
            for pos in list(runner.pending_positions):
                if pos.window_end > now:
                    continue
                runner.pending_positions.remove(pos)
                self._stale.append(pos)
                log.warning(
                    "[{}] pending position {} expired before adoption; "
                    "moving to settlement",
                    pos.asset,
                    pos.position_id,
                )

    async def _resolve_stale(self) -> None:
        # Positions land here when they could not be sold before the market
        # closed: restored from disk, on an asset that is no longer enabled,
        # or marked SETTLE_PENDING by a live AssetRunner.
        candidates: list[Position] = []
        seen: set[str] = set()
        for pos in self._stale:
            if not pos.is_flat and pos.position_id not in seen:
                seen.add(pos.position_id)
                candidates.append(pos)
        for runner in self.markets.values():
            for side in runner.all_sides():
                pos = side.position
                if (
                    side.status is SideStatus.SETTLE_PENDING
                    and pos is not None
                    and not pos.is_flat
                    and pos.position_id not in seen
                ):
                    seen.add(pos.position_id)
                    candidates.append(pos)
        if not candidates:
            return

        now = time.time()
        remaining: list[Position] = []
        for pos in candidates:
            slug = pos.slug or (
                f"{pos.asset.lower()}-updown-{pos.interval}-"
                f"{pos.window_start}"
            )
            resolution = await self.pm.get_event_resolution(slug)
            if resolution is None:
                if now - pos.window_end > 600:
                    log.warning(
                        "[{}] {} resolution unavailable 10m after window end",
                        pos.asset,
                        pos.position_id,
                    )
                if pos in self._stale:
                    remaining.append(pos)
                continue
            if not resolution.get("resolved"):
                if now - pos.window_end > 600:
                    log.warning(
                        "[{}] {} unresolved 10m after window end",
                        pos.asset,
                        pos.position_id,
                    )
                if pos in self._stale:
                    remaining.append(pos)
                continue
            payout = (
                float(resolution.get("UP", 0.0))
                if pos.side is Side.UP
                else float(resolution.get("DOWN", 0.0))
            )
            payout = 1.0 if payout >= 0.99 else 0.0
            runner = self.markets.get(pos.market)
            runtime = (
                next(
                    (
                        s
                        for s in runner.all_sides()
                        if s.position is not None
                        and s.position.position_id == pos.position_id
                    ),
                    None,
                )
                if runner is not None
                else None
            )
            if runtime is not None:
                record = runtime.on_settled(
                    payout, now, self.mode, note="gamma resolution"
                )
            else:
                record = _settle_orphan(pos, payout, now, self.mode)
            paper = getattr(self.broker, "paper", None)
            if paper is None and isinstance(self.broker, PaperBroker):
                paper = self.broker
            if paper is not None:
                paper.holdings[pos.token_id] = 0.0
            if record is not None:
                self._on_trade(record)
            log.info(
                "[SETTLE] {} {} payout={:.2f} pnl_net={:+.4f}",
                pos.asset,
                pos.position_id,
                payout,
                record.pnl_net if record else 0.0,
            )
        self._stale = remaining
        self._save_state()

    def _update_ws_tokens(self) -> None:
        if self.market_ws is None:
            return
        tokens = self.market_service.active_tokens(
            self.open_tokens(),
            include_recent=True,
            markets=self.enabled_markets(),
        )
        self.market_ws.set_active_tokens(tokens)

    async def _status_loop(self) -> None:
        tick = 0
        while self._running:
            try:
                now = time.time()
                allowed, reason = self.entries_allowed()
                if reason != "ok" and reason != self._pause_reason:
                    log.warning("new entries blocked: {}", reason)
                    self._pause_reason = reason
                elif reason == "ok":
                    self._pause_reason = ""

                # status.json every 5 s; human-readable lines every 15 s.
                if tick % 3 == 0:
                    for runner in self.markets.values():
                        log.info("[STATUS] {}", runner.describe(now))
                payload = self.status_payload(now)
                self.store.write_status(payload)
                tick += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception("status loop error: {}", exc)
            await asyncio.sleep(5.0)

    def status_payload(self, now: float | None = None) -> dict[str, Any]:
        now_f = time.time() if now is None else float(now)
        allowed, reason = self.entries_allowed(now=now_f)
        today_stats = self.daily.get(day_key(now_f))
        pause_until = (
            today_stats.drawdown_pause_until
            if today_stats is not None
            else 0.0
        )
        global_count, global_notional = self.global_open_exposure()
        exposure: dict[str, dict[str, Any]] = {}
        for interval in self.settings.intervals:
            start = window_start(now_f, interval.window_sec)
            key = (int(interval.window_sec), int(start))
            reserved_count, reserved_notional = self._window_reserved(key)
            exposure[interval.name] = {
                "window_sec": int(interval.window_sec),
                "start_ts": start,
                "end_ts": start + int(interval.window_sec),
                "positions": len(self.positions_in_window(key)),
                "notional_usd": round(
                    self.window_open_notional(key), 4
                ),
                "reserved_positions": reserved_count,
                "reserved_notional_usd": round(reserved_notional, 4),
            }
        return {
            "ts": now_f,
            "iso": utc_iso(now_f),
            "mode": self.mode,
            "live_available": self.live_available,
            "live_allowed": self.live_allowed,
            "uptime_s": round(now_f - self._started_at, 1),
            "config_version": self.runtime.version,
            "max_same_side_positions": self.settings.execution.max_same_side_positions,
            "max_same_side_per_window": self.max_same_side_per_window,
            "max_daily_loss_usd": self.max_daily_loss_usd,
            "max_intraday_drawdown_usd": self.max_intraday_drawdown_usd,
            "intraday_drawdown_pause_min": (
                self.intraday_drawdown_pause_min
            ),
            "intraday_drawdown_usd": round(self.intraday_drawdown(now_f), 4),
            "drawdown_pause_until_ts": round(pause_until, 3),
            "drawdown_pause_remaining_s": round(
                max(0.0, pause_until - now_f), 1
            ),
            "max_positions_per_window": self.max_positions_per_window,
            "max_window_notional_usd": self.max_window_notional_usd,
            "max_open_positions": self.max_open_positions,
            "max_open_notional_usd": self.max_open_notional_usd,
            "open_exposure": {
                "count": global_count,
                "notional_usd": round(global_notional, 4),
            },
            "intervals": [
                {
                    "name": interval.name,
                    "window_sec": int(interval.window_sec),
                    "entry_window_end_s": float(
                        interval.entry_window_end_s
                    ),
                }
                for interval in self.settings.intervals
            ],
            "window_exposure": exposure,
            "same_side_open": {
                "UP": self.same_side_open(Side.UP),
                "DOWN": self.same_side_open(Side.DOWN),
            },
            "paused": self.paused,
            "pause_reason": self.pause_reason,
            "entries_allowed": allowed,
            "entries_block_reason": reason,
            "asset_perf": self.asset_perf_snapshot(now_f),
            "daily": {
                day: stats.to_dict() for day, stats in sorted(self.daily.items())
            },
            "assets": {
                market: runner.snapshot(now_f)
                for market, runner in self.markets.items()
            },
            "market_service": self.market_service.stats(),
        }

    # ── status helpers ─────────────────────────────────────────────────
    def daily_pnl(self, now: float | None = None) -> float:
        now_f = time.time() if now is None else float(now)
        stats = self.daily.get(day_key(now_f))
        return stats.realized_pnl if stats else 0.0


def _settle_orphan(
    pos: Position, payout: float, now: float, mode: str
) -> TradeRecord:
    """Settle a position that has no live SideRuntime (asset disabled)."""
    exit_usdc = pos.remaining_shares * payout
    pnl_gross = pos.exit_proceeds + exit_usdc - pos.cost_usd
    pnl_net = pnl_gross - pos.entry_fee - pos.exit_fees
    return TradeRecord(
        position_id=pos.position_id,
        asset=pos.asset,
        side=pos.side.value,
        window_start=pos.window_start,
        slug=pos.slug,
        entry_ts=pos.entry_ts,
        entry_price=pos.entry_price,
        shares=pos.shares,
        cost_usd=pos.cost_usd,
        entry_fee=pos.entry_fee,
        tp_price=pos.tp_price,
        exit_ts=now,
        exit_price=payout,
        exit_usdc=pos.exit_proceeds + exit_usdc,
        exit_fee=pos.exit_fees,
        reason="settle",
        pnl_gross=pnl_gross,
        pnl_net=pnl_net,
        stake_usd=pos.stake_usd,
        mode=mode,
        interval=pos.interval,
        sl_price=pos.sl_price,
        entry_quote_mid=pos.entry_quote_mid,
        entry_quote_bid=pos.entry_quote_bid,
        entry_quote_ask=pos.entry_quote_ask,
        entry_quote_spread=pos.entry_quote_spread,
        entry_quote_source=pos.entry_quote_source,
        entry_quote_ts=pos.entry_quote_ts,
        note="orphan settlement",
    )
