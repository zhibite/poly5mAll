"""Strategy 1 (rebound) — per asset/side state machine.

The doc's timeline, implemented literally:

    t = 0              market opens
    0 <= t <= 60 s     observe; first touch of entry_thresh -> BUY
    60 < t < 240 s     first touch of entry_price * (1 + tp_pct) -> SELL
    t >= 240 s         forced exit at market (the doc's "240s SL")
    t > 295 s          market exit deadline; leave to settlement if the
                       book is still empty

The class is pure: it never talks to the network.  ``runner.py`` supplies a
``Quote`` and a ``Fill``, and the state machine decides whether an action
is due and how to book the result.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.config import ExecutionConfig, FeedConfig, GeneralConfig
from src.models import (
    Fill,
    MarketWindow,
    Phase,
    Position,
    Quote,
    SideStatus,
    SidePlan,
    TradeRecord,
)


@dataclass
class SideRuntime:
    plan: SidePlan
    window: MarketWindow
    status: SideStatus = SideStatus.WAITING
    position: Position | None = None
    last_entry_attempt: float = 0.0
    last_exit_attempt: float = 0.0
    entry_attempts: int = 0
    exit_attempts: int = 0
    closed_reason: str = ""
    last_reason: str = ""

    # ── timing ─────────────────────────────────────────────────────────
    def phase(self, now: float, general: GeneralConfig) -> Phase:
        t = self.window.seconds_since(now)
        if t < 0:
            return Phase.PRE
        if t <= general.entry_window_end_s:
            return Phase.ENTRY
        if t < general.force_exit_s:
            return Phase.TP
        if t <= general.force_exit_deadline_s:
            return Phase.FORCE_EXIT
        return Phase.ENDED

    # ── entry ──────────────────────────────────────────────────────────
    def entry_block_reason(
        self,
        now: float,
        quote: Quote | None,
        general: GeneralConfig,
        execution: ExecutionConfig,
        feed: FeedConfig,
    ) -> str | None:
        """None when an entry is due, otherwise a short skip reason."""
        if self.status is not SideStatus.WAITING:
            return "not-waiting"
        if self.phase(now, general) is not Phase.ENTRY:
            return "outside-entry-window"
        if now - self.last_entry_attempt < execution.entry_retry_s:
            return "retry-backoff"
        if quote is None:
            return "no-quote"
        if quote.age(now) > feed.stale_price_max_age_s:
            return "stale-price"
        if quote.mid > self.plan.entry_thresh:
            return "above-threshold"
        if (
            execution.max_spread_abs > 0
            and quote.spread > execution.max_spread_abs
        ):
            return "wide-spread"
        return None

    # ── take profit ────────────────────────────────────────────────────
    def tp_due(
        self,
        now: float,
        quote: Quote | None,
        general: GeneralConfig,
        execution: ExecutionConfig,
        feed: FeedConfig,
        mode: str = "observed",
    ) -> bool:
        if self.status is not SideStatus.HOLDING or self.position is None:
            return False
        t = self.window.seconds_since(now)
        if t < general.tp_window_start_s or t >= general.force_exit_s:
            return False
        if now - self.last_exit_attempt < execution.exit_retry_s:
            return False
        if quote is None or quote.age(now) > feed.exit_stale_price_max_age_s:
            return False
        if mode == "market":
            # Backtest-style observed signal: the mid/last series crosses
            # the target, then the executor crosses the book at market.
            trigger = quote.mid
        else:
            # Executable observed fill: only trigger when the best bid is
            # already at/above the target.  The actual fill is that bid
            # (which may be above the target).
            trigger = quote.bid if quote.bid > 0 else quote.mid
        return trigger >= self.position.tp_price

    # ── forced exit ────────────────────────────────────────────────────
    def force_exit_due(
        self, now: float, general: GeneralConfig, execution: ExecutionConfig
    ) -> bool:
        if self.status is not SideStatus.HOLDING or self.position is None:
            return False
        t = self.window.seconds_since(now)
        if t < general.force_exit_s:
            return False
        if t > general.force_exit_deadline_s:
            return False
        return now - self.last_exit_attempt >= execution.exit_retry_s

    def deadline_passed(self, now: float, general: GeneralConfig) -> bool:
        return (
            self.status is SideStatus.HOLDING
            and self.window.seconds_since(now) > general.force_exit_deadline_s
        )

    # ── bookings ───────────────────────────────────────────────────────
    def on_entry_fill(
        self, fill: Fill, now: float, execution: ExecutionConfig
    ) -> Position:
        if fill.price <= 0 or fill.price >= 1 or fill.shares <= 0:
            raise ValueError(f"invalid entry fill: {fill}")
        position = Position(
            position_id=f"{self.plan.asset}-{self.plan.side.value}-{self.window.start_ts}",
            asset=self.plan.asset,
            side=self.plan.side,
            window_start=self.window.start_ts,
            window_end=self.window.end_ts,
            slug=self.window.slug,
            token_id=self.window.token(self.plan.side),
            entry_ts=now,
            entry_price=float(fill.price),
            shares=float(fill.shares),
            cost_usd=float(fill.usdc),
            entry_fee=float(fill.fee),
            tp_price=float(fill.price) * (1.0 + self.plan.tp_pct),
            sl_price=0.0,
            stake_usd=float(self.plan.stake_usd),
            up_token=self.window.up_token,
            down_token=self.window.down_token,
            tick_size=self.window.tick_size,
            entry_order_id=fill.order_id,
        )
        self.position = position
        self.status = SideStatus.HOLDING
        self.entry_attempts += 1
        self.last_entry_attempt = now
        self.last_exit_attempt = 0.0
        return position

    def on_entry_missed(self, now: float) -> None:
        self.entry_attempts += 1
        self.last_entry_attempt = now
        if self.status is SideStatus.ENTERING:
            self.status = SideStatus.WAITING

    def on_exit_fill(
        self,
        fill: Fill,
        now: float,
        reason: str,
        execution: ExecutionConfig,
        mode: str,
    ) -> TradeRecord | None:
        """Book a (possibly partial) exit.  Returns a record only when flat."""
        position = self.position
        if position is None or fill.shares <= 0 or fill.usdc <= 0:
            return None
        remaining = position.remaining_shares
        sold = min(float(fill.shares), remaining)
        if sold <= 0:
            return None
        position.sold_shares += sold
        position.exit_proceeds += float(fill.usdc) * (sold / float(fill.shares))
        position.exit_fees += float(fill.fee) * (sold / float(fill.shares))
        self.exit_attempts += 1
        self.last_exit_attempt = now

        if not position.is_flat:
            self.status = SideStatus.HOLDING
            return None

        self.status = SideStatus.CLOSED
        self.closed_reason = reason
        exit_usdc = position.exit_proceeds
        exit_fee = position.exit_fees
        pnl_gross = exit_usdc - position.cost_usd
        pnl_net = pnl_gross - position.entry_fee - exit_fee
        observed = reason == "tp"
        return TradeRecord(
            position_id=position.position_id,
            asset=position.asset,
            side=position.side.value,
            window_start=position.window_start,
            slug=position.slug,
            entry_ts=position.entry_ts,
            entry_price=position.entry_price,
            shares=position.shares,
            cost_usd=position.cost_usd,
            entry_fee=position.entry_fee,
            tp_price=position.tp_price,
            exit_ts=now,
            exit_price=(
                exit_usdc / position.sold_shares
                if position.sold_shares > 0
                else 0.0
            ),
            exit_usdc=exit_usdc,
            exit_fee=exit_fee,
            reason=reason,
            pnl_gross=pnl_gross,
            pnl_net=pnl_net,
            stake_usd=position.stake_usd,
            mode=mode,
            sl_price=position.sl_price,
            tp_observed_price=position.tp_observed_price if observed else 0.0,
            tp_observed_bid=position.tp_observed_bid if observed else 0.0,
            tp_observed_ask=position.tp_observed_ask if observed else 0.0,
            tp_observed_source=(
                position.tp_observed_source if observed else ""
            ),
            tp_observed_ts=position.tp_observed_ts if observed else 0.0,
        )

    def on_exit_missed(self, now: float) -> None:
        self.exit_attempts += 1
        self.last_exit_attempt = now
        if self.status is SideStatus.EXITING:
            self.status = SideStatus.HOLDING

    def on_settled(
        self,
        payout_per_share: float,
        now: float,
        mode: str,
        note: str = "",
    ) -> TradeRecord | None:
        """Book a settlement for a position that could not be sold."""
        position = self.position
        if position is None or position.is_flat:
            return None
        payout = position.remaining_shares * float(payout_per_share)
        position.sold_shares += position.remaining_shares
        position.exit_proceeds += payout
        self.status = SideStatus.CLOSED
        self.closed_reason = "settle"
        pnl_gross = position.exit_proceeds - position.cost_usd
        pnl_net = pnl_gross - position.entry_fee - position.exit_fees
        return TradeRecord(
            position_id=position.position_id,
            asset=position.asset,
            side=position.side.value,
            window_start=position.window_start,
            slug=position.slug,
            entry_ts=position.entry_ts,
            entry_price=position.entry_price,
            shares=position.shares,
            cost_usd=position.cost_usd,
            entry_fee=position.entry_fee,
            tp_price=position.tp_price,
            exit_ts=now,
            exit_price=float(payout_per_share),
            exit_usdc=position.exit_proceeds,
            exit_fee=position.exit_fees,
            reason="settle",
            pnl_gross=pnl_gross,
            pnl_net=pnl_net,
            stake_usd=position.stake_usd,
            mode=mode,
            sl_price=position.sl_price,
            note=note,
        )

    # ── introspection ──────────────────────────────────────────────────
    def unrealized(self, quote: Quote | None) -> float:
        if self.position is None:
            return 0.0
        mid = quote.mid if quote is not None else self.position.entry_price
        return self.position.unrealized_pnl(mid)

    def describe(self, now: float, general: GeneralConfig, quote: Quote | None) -> str:
        t = self.window.seconds_since(now)
        phase = self.phase(now, general).value
        mid = f"{quote.mid:.3f}" if quote is not None else "---"
        if self.position is None:
            return (
                f"{self.plan.side.value}:{self.status.value} t={t:+.0f}s "
                f"phase={phase} mid={mid} entry<={self.plan.entry_thresh:.2f}"
            )
        pos = self.position
        return (
            f"{self.plan.side.value}:{self.status.value} t={t:+.0f}s "
            f"phase={phase} mid={mid} entry={pos.entry_price:.3f} "
            f"tp={pos.tp_price:.3f} sh={pos.remaining_shares:.2f} "
            f"upnl={self.unrealized(quote):+.2f}U"
        )
