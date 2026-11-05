"""Multi-asset orchestrator.

One process, seven assets, one shared price layer:

    MarketService   Gamma discovery (current + next 5m window)
    MarketWS        CLOB market WebSocket (BBO + last trade)
    midpoint loop   CLOB /midpoint every 2 s as the stable fallback
    AssetRunner ×7  one strategy loop per asset
    StateStore      state.json / trades.jsonl / status.json

Each asset has its own asyncio task, so a slow FAK order for one asset
cannot delay another asset's 0–60 s entry window.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from src.config import AssetConfig, Settings
from src.economics import fee_line, floor_tick
from src.runtime_config import RuntimeConfig
from src.execution.broker import Broker, PaperBroker
from src.infra.logging import get_logger
from src.infra.timeutil import day_key, utc_hms, utc_iso
from src.market.market_ws import MarketWS, PriceCache
from src.market.service import MarketService
from src.models import (
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


async def _always_reserve_entry(side: Side) -> bool:
    return True


async def _release_entry_noop(side: Side) -> None:
    return None


class AssetRunner:
    """The strategy loop for one asset."""

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
    ) -> None:
        self.asset = asset_cfg.asset
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

        self.window: MarketWindow | None = None
        self.sides: list[SideRuntime] = []
        self.retired: list[SideRuntime] = []
        self.pending_positions: list[Position] = []
        self.last_error = ""
        self.last_skip_log = 0.0
        self.last_skip_reason = ""

    def current_cfg(self) -> AssetConfig:
        """Current runtime config for this asset (falls back to startup)."""
        if self.runtime is not None:
            try:
                return self.runtime.asset(self.asset)
            except KeyError:
                pass
        return self.cfg

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
        window = self.market_service.current(self.asset, now_f)
        if window is None:
            return
        if self.window is None or self.window.start_ts != window.start_ts:
            await self._bind(window, now_f)
        for side in list(self.sides):
            try:
                await self._tick_side(side, now_f)
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                log.exception("[{}] side tick failed: {}", self.asset, exc)

    async def _bind(self, window: MarketWindow, now: float) -> None:
        self.window = window
        # Keep any open position from the previous window; it is either
        # being force-exited or waiting for settlement.
        self.retired = [
            s
            for s in self.retired
            if s.position is not None and not s.position.is_flat
        ]
        for side in self.sides:
            if side.position is not None and not side.position.is_flat:
                self.retired.append(side)
                log.warning(
                    "[{}] retiring open {} position {} into settlement tracking",
                    self.asset,
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
                    fee_rate=self.settings.execution.taker_fee_rate,
                )
                if line.net_per_trade <= 0:
                    log.warning(
                        "[{}] skipping {}: TP does not cover fees "
                        "(net/10U={:+.3f})",
                        self.asset,
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
                    ),
                    window=window,
                )
                self.sides.append(side_rt)
                log.warning(
                    "[{}] adopting {} position on a side no longer configured",
                    self.asset,
                    pos.side.value,
                )
            side_rt.position = pos
            side_rt.status = SideStatus.HOLDING
            side_rt.last_exit_attempt = 0.0
            adopted.append(pos)
            log.info(
                "[{}] adopted {} {} shares={:.4f} entry={:.4f} tp={:.4f}",
                self.asset,
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
            "[{}] window {} start={} sides={}",
            self.asset,
            window.slug,
            utc_hms(window.start_ts),
            "+".join(s.plan.side.value for s in self.sides) or "-",
        )
        if self.settings.live:
            known = {
                s.position.token_id
                for s in self.sides
                if s.position is not None
            }
            asyncio.create_task(
                self._warn_unknown_balances(window, known),
                name=f"balance-check-{self.asset}",
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
                    self.asset,
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

        if side.tp_due(
            now,
            quote,
            self.settings.general,
            self.settings.execution,
            self.settings.feed,
            mode=self.settings.execution.tp_execution_mode,
        ):
            await self._exit(side, now, quote, reason="tp")
            return

        if side.force_exit_due(
            now, self.settings.general, self.settings.execution
        ):
            await self._exit(side, now, quote, reason="time_exit")
            return

        if side.deadline_passed(now, self.settings.general):
            side.status = SideStatus.SETTLE_PENDING
            log.critical(
                "[{}] {} {} could not be sold by {}s; waiting for settlement",
                self.asset,
                side.plan.side.value,
                pos.position_id,
                self.settings.general.force_exit_deadline_s,
            )
            self._on_state_change()

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
            self.settings.general,
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

        reserved = await self._reserve_entry_cb(side.plan.side)
        if not reserved:
            side.last_reason = "same-side-limit"
            self._log_skip("same-side-limit", side, now)
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
                    side.plan.stake_usd,
                    limit,
                    self.window.tick_size,
                )
            except Exception as exc:  # noqa: BLE001
                side.on_entry_missed(now)
                log.exception("[{}] buy failed: {}", self.asset, exc)
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
                    self.asset,
                    fill,
                    exc,
                )
                return
            log.info(
                "[{}] ENTRY {} {} shares={:.4f} px={:.4f} cost={:.4f} "
                "fee={:.4f} tp={:.4f}",
                self.asset,
                side.plan.side.value,
                position.position_id,
                position.shares,
                position.entry_price,
                position.cost_usd,
                position.entry_fee,
                position.tp_price,
            )
            self._on_state_change()
        finally:
            await self._release_entry_cb(side.plan.side)

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

        if reason == "tp":
            if self.settings.execution.tp_execution_mode == "market":
                # Doc's option B: cross the book at market after the
                # observed mid crosses the target.  This can fill below
                # the target if the bid has not caught up.
                limit = self._force_exit_limit(quote, self.window.tick_size)
            else:
                # Observed fill with a target floor: only fill at bids at
                # or above the target; a gap to 0.42 fills at 0.42.
                limit = max(
                    self.window.tick_size,
                    floor_tick(pos.tp_price, self.window.tick_size),
                )
        else:
            limit = self._force_exit_limit(quote, self.window.tick_size)

        try:
            fill = await self.broker.sell(
                pos.token_id, pos.remaining_shares, limit, self.window.tick_size
            )
        except Exception as exc:  # noqa: BLE001
            side.on_exit_missed(now)
            log.exception("[{}] sell failed: {}", self.asset, exc)
            return
        if fill is None or fill.shares <= 0:
            side.on_exit_missed(now)
            return

        record = side.on_exit_fill(
            fill, now, reason, self.settings.execution, self.settings.mode
        )
        log.info(
            "[{}] EXIT {} {} reason={} mode={} tp_target={:.4f} "
            "shares={:.4f} px={:.4f} usdc={:.4f}",
            self.asset,
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
        if record is not None:
            self._on_trade(record)
        self._on_state_change()

    def _force_exit_limit(self, quote: Quote | None, tick: float) -> float:
        slip = max(0.0, self.settings.execution.slippage_pct) / 100.0
        if quote is not None and quote.bid > 0:
            floor = quote.bid * (1.0 - slip)
            return max(tick, floor_tick(floor, tick))
        return tick

    def _log_skip(
        self, reason: str, side: SideRuntime, now: float
    ) -> None:
        if reason == self.last_skip_reason and now - self.last_skip_log < 5.0:
            return
        self.last_skip_reason = reason
        self.last_skip_log = now
        log.debug(
            "[{}] {} entry skipped: {}",
            self.asset,
            side.plan.side.value,
            reason,
        )

    # ── flatten on shutdown ────────────────────────────────────────────
    async def flatten(self, now: float) -> None:
        for side in self.all_sides():
            if side.position is None or side.position.is_flat:
                continue
            pos = side.position
            try:
                fill = await self.broker.sell(
                    pos.token_id,
                    pos.remaining_shares,
                    self.window.tick_size if self.window else pos.tick_size,
                    pos.tick_size,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("[{}] flatten sell error: {}", self.asset, exc)
                fill = None
            if fill is None or fill.shares <= 0:
                log.error(
                    "[{}] flatten failed for {}", self.asset, pos.position_id
                )
                continue
            record = side.on_exit_fill(
                fill,
                now,
                "shutdown",
                self.settings.execution,
                self.settings.mode,
            )
            log.info(
                "[{}] FLATTEN {} shares={:.4f} px={:.4f}",
                self.asset,
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
            return f"{self.asset}: disabled"
        if self.window is None:
            return f"{self.asset}: no window"
        parts = [f"{self.asset} {utc_hms(self.window.start_ts)}"]
        for side in self.sides:
            quote = self.cache.quote(
                self.window.token(side.plan.side), now=now, max_age=30.0
            )
            parts.append(side.describe(now, self.settings.general, quote))
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
                    side.phase(now, self.settings.general).value
                    if window is not None
                    else None
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
            "asset": self.asset,
            "window": {
                "slug": window.slug,
                "start_ts": window.start_ts,
                "end_ts": window.end_ts,
                "phase": (
                    phase_for(now, window, self.settings).value
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
            },
            "last_error": self.last_error,
        }


def phase_for(now: float, window: MarketWindow, settings: Settings) -> Phase:
    t = window.seconds_since(now)
    g = settings.general
    if t < 0:
        return Phase.PRE
    if t <= g.entry_window_end_s:
        return Phase.ENTRY
    if t < g.force_exit_s:
        return Phase.TP
    if t <= g.force_exit_deadline_s:
        return Phase.FORCE_EXIT
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
        market_ws: MarketWS | None = None,
        runtime: RuntimeConfig | None = None,
    ) -> None:
        self.settings = settings
        self.pm = pm
        self.cache = cache
        self.market_service = market_service
        self.broker = broker
        self.store = store
        self.market_ws = market_ws
        self.runtime = runtime or RuntimeConfig(
            settings.data_dir / "runtime_config.json", settings.assets
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
        self._entry_reservations: dict[Side, int] = {
            Side.UP: 0,
            Side.DOWN: 0,
        }

        # Build every configured asset, including disabled ones: the Web
        # config page can turn a coin on without restarting the process.
        self.assets: dict[str, AssetRunner] = {}
        for asset_cfg in settings.assets:
            self.assets[asset_cfg.asset] = AssetRunner(
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
            )

    def same_side_open(self, side: Side) -> int:
        return sum(
            1 for pos in self.all_positions() if pos.side is side
        )

    async def reserve_entry(self, side: Side) -> bool:
        """Reserve one same-side slot, including in-flight entry attempts."""
        limit = int(self.settings.execution.max_same_side_positions)
        if limit <= 0:
            return True
        async with self._entry_lock:
            used = self.same_side_open(side) + self._entry_reservations[side]
            if used >= limit:
                return False
            self._entry_reservations[side] += 1
            return True

    async def release_entry(self, side: Side) -> None:
        async with self._entry_lock:
            self._entry_reservations[side] = max(
                0, self._entry_reservations[side] - 1
            )

    def enabled_assets(self) -> set[str]:
        out: set[str] = set()
        for name in self.assets:
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
        for asset in self.assets.values():
            before = len(asset.positions())
            await asset.flatten(now_f)
            sold += max(0, before - len(asset.positions()))
        self._save_state()
        log.warning("runtime flatten_open_positions sold={}", sold)
        return sold

    def entries_allowed(self, side: Side | None = None) -> tuple[bool, str]:
        e = self.settings.execution
        if self.pause_new_entries:
            return False, self.pause_reason or "paused"
        kill_path = self.settings.resolve_path(e.kill_switch_file)
        if e.kill_switch_file and kill_path.exists():
            return False, "kill-switch"
        if e.max_daily_loss_usd > 0:
            today = self.daily.get(day_key(time.time()))
            if today is not None and today.realized_pnl <= -e.max_daily_loss_usd:
                return False, "daily-loss-limit"
        return True, "ok"

    def _on_trade(self, record: TradeRecord) -> None:
        self.store.append_trade(record)
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
        for asset in self.assets.values():
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
        for asset in self.assets.values():
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
            asyncio.create_task(self._status_loop(), name="status"),
            asyncio.create_task(self._reconcile_loop(), name="reconcile"),
        ]
        for asset, runner in self.assets.items():
            self._tasks.append(
                asyncio.create_task(
                    self._asset_loop(asset, runner), name=f"asset-{asset}"
                )
            )
        log.info(
            "runner.started mode={} assets={}",
            self.settings.mode,
            ",".join(self.assets),
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
            for asset in self.assets.values():
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
            runner = self.assets.get(pos.asset)
            if runner is None:
                # The asset is no longer enabled.  Keep it for settlement.
                self._stale.append(pos)
                continue
            if pos.window_end > now:
                # Live: the wallet balance is the source of truth.  Paper:
                # re-register the holding in the paper ledger.
                if self.settings.live:
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
                elif isinstance(self.broker, PaperBroker):
                    self.broker.adopt(pos.token_id, pos.remaining_shares)
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
    async def _asset_loop(self, asset: str, runner: AssetRunner) -> None:
        while self._running:
            try:
                await runner.tick(time.time())
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                runner.last_error = str(exc)
                log.exception("[{}] tick failed: {}", asset, exc)
            await asyncio.sleep(max(0.05, self.settings.general.tick_s))

    async def _discovery_loop(self) -> None:
        while self._running:
            try:
                now = time.time()
                assets = list(self.assets)
                results = await asyncio.gather(
                    *(self.market_service.refresh(a, now) for a in assets),
                    return_exceptions=True,
                )
                for asset, result in zip(assets, results):
                    if isinstance(result, Exception):
                        log.warning("[{}] discovery error: {}", asset, result)
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
                        self.open_tokens(), assets=self.enabled_assets()
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
        for runner in self.assets.values():
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
        for runner in self.assets.values():
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
            slug = pos.slug or f"{pos.asset.lower()}-updown-5m-{pos.window_start}"
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
            runner = self.assets.get(pos.asset)
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
                    payout, now, self.settings.mode, note="gamma resolution"
                )
            else:
                record = _settle_orphan(pos, payout, now, self.settings.mode)
            if isinstance(self.broker, PaperBroker):
                self.broker.holdings[pos.token_id] = 0.0
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
            assets=self.enabled_assets(),
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
                    for runner in self.assets.values():
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
        allowed, reason = self.entries_allowed()
        return {
            "ts": now_f,
            "iso": utc_iso(now_f),
            "mode": self.settings.mode,
            "uptime_s": round(now_f - self._started_at, 1),
            "config_version": self.runtime.version,
            "max_same_side_positions": self.settings.execution.max_same_side_positions,
            "same_side_open": {
                "UP": self.same_side_open(Side.UP),
                "DOWN": self.same_side_open(Side.DOWN),
            },
            "paused": self.paused,
            "pause_reason": self.pause_reason,
            "entries_allowed": allowed,
            "entries_block_reason": reason,
            "daily": {
                day: stats.to_dict() for day, stats in sorted(self.daily.items())
            },
            "assets": {
                asset: runner.snapshot(now_f)
                for asset, runner in self.assets.items()
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
        sl_price=pos.sl_price,
        note="orphan settlement",
    )
