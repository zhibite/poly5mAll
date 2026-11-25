"""Strategy 1 (rebound) — per market/side state machine.

Two timeframes share the same logic; only the window length and the
entry window differ:

    5m:   t = 0 market opens; 0 <= t <= 60 s entry; TP/SL until t = 300 s
    15m:  t = 0 market opens; 0 <= t <= 180 s entry; TP/SL until t = 900 s

TP and SL are armed at the actual fill and run until the window closes.
There is no forced time exit: any remainder left at the close goes to
Gamma settlement (0/1 payout).

The class is pure: it never talks to the network.  ``runner.py`` supplies a
``Quote`` and a ``Fill``, and the state machine decides whether an action
is due and how to book the result.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.config import ExecutionConfig, FeedConfig
from src.models import (
    SIZE_PRECISION,
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
    last_tick_refresh: float = 0.0
    # After a partial SELL the CLOB keeps the matched shares locked for a
    # moment; retrying the full remainder immediately is rejected with
    # "not enough balance / allowance".  Hold the next sell until then.
    exit_cooldown_until: float = 0.0
    entry_attempts: int = 0
    exit_attempts: int = 0
    closed_reason: str = ""
    last_reason: str = ""

    # ── timing ─────────────────────────────────────────────────────────
    def phase(self, now: float) -> Phase:
        t = self.window.seconds_since(now)
        if t < 0:
            return Phase.PRE
        if t <= self.plan.entry_window_end_s:
            # TP/SL are armed at the fill, so a held position is already
            # in its management phase even inside the entry window.
            if (
                self.position is not None
                and self.status in (SideStatus.HOLDING, SideStatus.EXITING)
            ):
                return Phase.TP
            return Phase.ENTRY
        if t < self.window.window_sec:
            return Phase.TP
        return Phase.ENDED

    # ── entry ──────────────────────────────────────────────────────────
    def entry_block_reason(
        self,
        now: float,
        quote: Quote | None,
        execution: ExecutionConfig,
        feed: FeedConfig,
    ) -> str | None:
        """None when an entry is due, otherwise a short skip reason."""
        if self.status is not SideStatus.WAITING:
            return "not-waiting"
        if self.phase(now) is not Phase.ENTRY:
            return "outside-entry-window"
        if now - self.last_entry_attempt < execution.entry_retry_s:
            return "retry-backoff"
        if quote is None:
            return "no-quote"
        if quote.age(now) > feed.stale_price_max_age_s:
            return "stale-price"
        if quote.mid > self.plan.entry_thresh:
            return "above-threshold"
        if execution.max_spread_abs > 0:
            # A missing/invalid BBO is not a tight spread: with the filter
            # enabled we refuse to enter until both sides are quoted.
            if quote.bid <= 0 or quote.ask <= 0 or quote.ask < quote.bid:
                return "wide-spread"
            # 1e-9 tolerance so an exact one-tick spread (e.g. 0.245 -
            # 0.235 = 0.010000000000000009 in binary float) is allowed.
            if quote.spread > execution.max_spread_abs + 1e-9:
                return "wide-spread"
        return None

    # ── take profit ────────────────────────────────────────────────────
    def tp_due(
        self,
        now: float,
        quote: Quote | None,
        execution: ExecutionConfig,
        feed: FeedConfig,
        mode: str = "observed",
    ) -> bool:
        if self.status is not SideStatus.HOLDING or self.position is None:
            return False
        if now < self.position.entry_ts:
            return False
        if now < self.exit_cooldown_until:
            return False
        # The TP target is armed at the fill; the window close caps its
        # lifetime (any remainder then settles at the Gamma outcome).
        t = self.window.seconds_since(now)
        if t >= self.window.window_sec:
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

    # ── price stop ─────────────────────────────────────────────────────
    def sl_due(
        self,
        now: float,
        quote: Quote | None,
        execution: ExecutionConfig,
        feed: FeedConfig,
    ) -> bool:
        """Price stop armed at the fill and active until the window ends.

        Triggers on the executable bid (falling back to mid when no BBO is
        available).  A 0 ``sl_price`` means the stop is off for this side.
        """
        if self.status is not SideStatus.HOLDING or self.position is None:
            return False
        if self.position.sl_price <= 0:
            return False
        if now < self.position.entry_ts:
            return False
        if now < self.exit_cooldown_until:
            return False
        t = self.window.seconds_since(now)
        if t >= self.window.window_sec:
            return False
        if now - self.last_exit_attempt < execution.exit_retry_s:
            return False
        if quote is None or quote.age(now) > feed.exit_stale_price_max_age_s:
            return False
        trigger = quote.bid if quote.bid > 0 else quote.mid
        return trigger <= self.position.sl_price

    # ── window end ─────────────────────────────────────────────────────
    def window_ended(self, now: float) -> bool:
        """The market closed with this position still open."""
        return (
            self.position is not None
            and not self.position.is_flat
            and self.window.seconds_since(now) >= self.window.window_sec
        )

    # ── bookings ───────────────────────────────────────────────────────
    def on_entry_fill(
        self,
        fill: Fill,
        now: float,
        execution: ExecutionConfig,
        mode: str = "",
    ) -> Position:
        if fill.price <= 0 or fill.price >= 1 or fill.shares <= 0:
            raise ValueError(f"invalid entry fill: {fill}")
        interval = self.plan.interval or self.window.interval
        position = Position(
            position_id=(
                f"{self.plan.asset}-{interval.upper()}-"
                f"{self.plan.side.value}-{self.window.start_ts}"
            ),
            asset=self.plan.asset,
            side=self.plan.side,
            window_start=self.window.start_ts,
            window_end=self.window.end_ts,
            slug=self.window.slug,
            token_id=self.window.token(self.plan.side),
            exchange_position_id=self.window.position(self.plan.side),
            entry_ts=now,
            interval=interval,
            mode=str(mode or ""),
            entry_price=float(fill.price),
            shares=float(fill.shares),
            cost_usd=float(fill.usdc),
            entry_fee=float(fill.fee),
            tp_price=float(fill.price) * (1.0 + self.plan.tp_pct),
            sl_price=(
                float(fill.price) * (1.0 - self.plan.stop_loss_pct)
                if 0.0 < self.plan.stop_loss_pct < 1.0
                else 0.0
            ),
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

        dust = position.remaining_shares
        written_off = 0.0
        if not position.is_flat:
            # A remainder below one size lot is worth <1 cent and is
            # written off.  Any larger remainder stays HOLDING: the CLOB
            # accepts a sell at 5+ shares OR $1+ notional, so the next
            # attempt decides with the live quote whether it can post.
            if 0 < dust < SIZE_PRECISION - 1e-9:
                position.sold_shares = position.shares
                written_off = dust
            else:
                self.status = SideStatus.HOLDING
                return None

        self.status = SideStatus.CLOSED
        self.closed_reason = reason
        note = (
            f"dust {written_off:.6f} written off" if written_off > 0 else ""
        )
        return self._close_record(now, reason, mode, note)

    def on_dust(
        self, now: float, reason: str, mode: str
    ) -> TradeRecord | None:
        """Write off a remainder below one 0.01-share size lot."""
        position = self.position
        if position is None or position.is_flat:
            return None
        dust = position.remaining_shares
        if dust >= SIZE_PRECISION - 1e-9:
            return None
        position.sold_shares = position.shares
        self.status = SideStatus.CLOSED
        self.closed_reason = reason
        return self._close_record(
            now, reason, mode, note=f"dust {dust:.6f} written off"
        )

    def on_dust_pending(self, now: float, reason: str) -> None:
        """Stop selling a remainder the CLOB cannot accept."""
        position = self.position
        if position is None or position.is_flat:
            return
        self.status = SideStatus.SETTLE_PENDING
        self.closed_reason = "remainder_pending"
        self.last_exit_attempt = now

    def _close_record(
        self, now: float, reason: str, mode: str, note: str = ""
    ) -> TradeRecord:
        position = self.position
        assert position is not None
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
            interval=position.interval,
            sl_price=position.sl_price,
            tp_observed_price=position.tp_observed_price if observed else 0.0,
            tp_observed_bid=position.tp_observed_bid if observed else 0.0,
            tp_observed_ask=position.tp_observed_ask if observed else 0.0,
            tp_observed_source=(
                position.tp_observed_source if observed else ""
            ),
            tp_observed_ts=position.tp_observed_ts if observed else 0.0,
            entry_quote_mid=position.entry_quote_mid,
            entry_quote_bid=position.entry_quote_bid,
            entry_quote_ask=position.entry_quote_ask,
            entry_quote_spread=position.entry_quote_spread,
            entry_quote_source=position.entry_quote_source,
            entry_quote_ts=position.entry_quote_ts,
            note=note,
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
        had_partial = position.sold_shares > 0
        payout = position.remaining_shares * float(payout_per_share)
        position.sold_shares += position.remaining_shares
        position.exit_proceeds += payout
        self.status = SideStatus.CLOSED
        self.closed_reason = "settle"
        pnl_gross = position.exit_proceeds - position.cost_usd
        pnl_net = pnl_gross - position.entry_fee - position.exit_fees
        exit_usdc = position.exit_proceeds
        exit_price = (
            exit_usdc / position.sold_shares
            if position.sold_shares > 0
            else float(payout_per_share)
        )
        observed = position.tp_observed_ts > 0
        if had_partial:
            note = (
                (note + "; " if note else "")
                + "partial exit + remainder settlement"
            )
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
            exit_price=exit_price,
            exit_usdc=exit_usdc,
            exit_fee=position.exit_fees,
            reason="settle",
            pnl_gross=pnl_gross,
            pnl_net=pnl_net,
            stake_usd=position.stake_usd,
            mode=mode,
            interval=position.interval,
            sl_price=position.sl_price,
            tp_observed_price=position.tp_observed_price if observed else 0.0,
            tp_observed_bid=position.tp_observed_bid if observed else 0.0,
            tp_observed_ask=position.tp_observed_ask if observed else 0.0,
            tp_observed_source=(
                position.tp_observed_source if observed else ""
            ),
            tp_observed_ts=position.tp_observed_ts if observed else 0.0,
            entry_quote_mid=position.entry_quote_mid,
            entry_quote_bid=position.entry_quote_bid,
            entry_quote_ask=position.entry_quote_ask,
            entry_quote_spread=position.entry_quote_spread,
            entry_quote_source=position.entry_quote_source,
            entry_quote_ts=position.entry_quote_ts,
            note=note,
        )

    # ── introspection ──────────────────────────────────────────────────
    def unrealized(self, quote: Quote | None) -> float:
        if self.position is None:
            return 0.0
        mid = quote.mid if quote is not None else self.position.entry_price
        return self.position.unrealized_pnl(mid)

    def describe(self, now: float, quote: Quote | None) -> str:
        t = self.window.seconds_since(now)
        phase = self.phase(now).value
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
