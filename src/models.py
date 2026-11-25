"""Core data structures shared by the market, strategy and execution layers."""
from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Any


# CLOB market orders size in 0.01-share increments and require at least
# 5 shares and $1 of notional (a SELL is share-denominated, so it must
# satisfy both).  A remainder below SIZE_PRECISION is worth <1 cent and
# is written off; anything else that cannot meet both floors is left for
# Gamma settlement.
SIZE_PRECISION = 0.01
# The CLOB accepts a SELL when it is at least 5 shares OR at least $1
# notional.  Both floors are listed here; callers must use OR, not AND.
MIN_ORDER_SHARES = 5.0
MIN_ORDER_USD = 1.0
# The CLOB price range is [0.01, 0.99] regardless of tick size: a 0.001
# tick only refines the grid, it does not allow sub-cent limit prices.
# Orders below 0.01 are rejected with "invalid price".
MIN_LIMIT_PRICE = 0.01


class Side(str, Enum):
    UP = "UP"
    DOWN = "DOWN"

    @property
    def token_field(self) -> str:
        return "up_token" if self is Side.UP else "down_token"

    def opposite(self) -> "Side":
        return Side.DOWN if self is Side.UP else Side.UP


class Phase(str, Enum):
    PRE = "pre"                 # before the window opened
    ENTRY = "entry"             # first 60 s (5m) / 180 s (15m): buy on touch
    TP = "tp"                   # from the fill to the window end: TP/SL
    ENDED = "ended"             # window closed; wait for Gamma settlement


class SideStatus(str, Enum):
    WAITING = "waiting"
    ENTERING = "entering"
    HOLDING = "holding"
    EXITING = "exiting"
    CLOSED = "closed"
    SETTLE_PENDING = "settle_pending"


@dataclass(frozen=True)
class MarketWindow:
    """One Up/Down market window (5m or 15m)."""

    asset: str
    slug: str
    start_ts: int
    end_ts: int
    up_token: str
    down_token: str
    # Timeframe label ("5m" / "15m").  The slug carries it, and the
    # strategy uses it for position ids and settlement fallbacks.
    interval: str = "5m"
    # Newer ("v2") markets sign against Exchange V3 and are addressed by
    # position id; legacy markets (BTC/ETH) leave these empty and use the
    # clob token ids above.
    up_position_id: str = ""
    down_position_id: str = ""
    condition_id: str = ""
    tick_size: float = 0.01
    fee_rate_bps: int = 0

    def token(self, side: Side) -> str:
        return self.up_token if side is Side.UP else self.down_token

    def position(self, side: Side) -> str:
        """Exchange V3 position id for ``side`` ("" on legacy markets)."""
        return self.up_position_id if side is Side.UP else self.down_position_id

    def seconds_since(self, now: float) -> float:
        return float(now) - float(self.start_ts)

    @property
    def window_sec(self) -> int:
        return int(self.end_ts) - int(self.start_ts)

    @property
    def key(self) -> tuple[int, int]:
        """(window seconds, start ts): identifies one window across 5m/15m.

        The 12 markets run on two grids; a 5m and a 15m window can share
        the same start timestamp but are different windows for the
        per-window exposure caps.
        """
        return (self.window_sec, int(self.start_ts))

    def contains(self, now: float) -> bool:
        return self.start_ts <= now < self.end_ts


@dataclass(frozen=True)
class Quote:
    """A point-in-time view of one token.

    ``mid`` is the strategy reference price (WS BBO midpoint first, then the
    REST midpoint, then the last trade).  ``bid``/``ask`` are only populated
    when the WS top-of-book is fresh; execution uses the REST order book when
    they are absent.
    """

    token_id: str
    mid: float
    bid: float
    ask: float
    last: float
    updated_at: float
    source: str

    def age(self, now: float | None = None) -> float:
        return max(0.0, (now if now is not None else time.time()) - self.updated_at)

    @property
    def spread(self) -> float:
        if self.bid <= 0 or self.ask <= 0 or self.ask < self.bid:
            return 0.0
        return self.ask - self.bid


@dataclass
class Fill:
    """A (possibly partial) execution."""

    token_id: str
    side: str                  # "BUY" / "SELL"
    shares: float
    usdc: float                # share notional, excluding fee
    price: float
    fee: float
    ts: float
    order_id: str = ""
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.shares > 0 and self.usdc > 0


@dataclass
class Position:
    """An open or partially-closed position."""

    position_id: str
    asset: str
    side: Side
    window_start: int
    window_end: int
    slug: str
    token_id: str
    entry_ts: float
    entry_price: float
    shares: float
    cost_usd: float            # share notional spent at entry (excl. fee)
    entry_fee: float
    tp_price: float
    sl_price: float = 0.0      # 0 = price stop disabled
    stake_usd: float = 0.0
    # Market timeframe ("5m" / "15m").  State files written before 15m
    # support have no field and are treated as 5m.
    interval: str = "5m"
    # Execution venue this position was opened on ("paper" / "live").
    # Empty in state files written before per-market modes; the runner
    # fills it from the runtime config on restore.
    mode: str = ""
    up_token: str = ""
    down_token: str = ""
    # Exchange V3 position id used to sign live SELLs on v2 markets.
    # Persisted so a restart mid-window can still exit the position.
    exchange_position_id: str = ""
    tick_size: float = 0.01
    entry_order_id: str = ""
    sold_shares: float = 0.0
    exit_proceeds: float = 0.0
    exit_fees: float = 0.0
    # Observed quote at the (last) TP sell attempt: for comparing the
    # observed take-profit price with the actual fill.
    tp_observed_price: float = 0.0
    tp_observed_bid: float = 0.0
    tp_observed_ask: float = 0.0
    tp_observed_source: str = ""
    tp_observed_ts: float = 0.0
    # Quote snapshot used for the entry decision.  The fill price is a
    # taker ask; these fields let trades.jsonl show the spread that the
    # entry actually paid (and whether a valid BBO existed at all).
    entry_quote_mid: float = 0.0
    entry_quote_bid: float = 0.0
    entry_quote_ask: float = 0.0
    entry_quote_spread: float = 0.0
    entry_quote_source: str = ""
    entry_quote_ts: float = 0.0

    @property
    def market(self) -> str:
        """Market id (coin + timeframe), e.g. ``ETH-15M``."""
        return f"{self.asset}-{self.interval.upper()}"

    @property
    def window_sec(self) -> int:
        return max(0, int(self.window_end) - int(self.window_start))

    @property
    def window_key(self) -> tuple[int, int]:
        return (self.window_sec, int(self.window_start))

    @property
    def remaining_shares(self) -> float:
        return max(0.0, self.shares - self.sold_shares)

    @property
    def is_flat(self) -> bool:
        return self.remaining_shares <= 1e-9

    def unrealized_pnl(self, mid: float) -> float:
        return self.remaining_shares * mid - self.cost_usd + self.exit_proceeds

    def to_dict(self) -> dict[str, Any]:
        return {
            "position_id": self.position_id,
            "asset": self.asset,
            "side": self.side.value,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "slug": self.slug,
            "token_id": self.token_id,
            "interval": self.interval,
            "mode": self.mode,
            "entry_ts": round(self.entry_ts, 3),
            "entry_price": round(self.entry_price, 8),
            "shares": round(self.shares, 8),
            "cost_usd": round(self.cost_usd, 8),
            "entry_fee": round(self.entry_fee, 8),
            "tp_price": round(self.tp_price, 8),
            "sl_price": round(self.sl_price, 8),
            "stake_usd": round(self.stake_usd, 4),
            "up_token": self.up_token,
            "down_token": self.down_token,
            "exchange_position_id": self.exchange_position_id,
            "tick_size": self.tick_size,
            "entry_order_id": self.entry_order_id,
            "sold_shares": round(self.sold_shares, 8),
            "exit_proceeds": round(self.exit_proceeds, 8),
            "exit_fees": round(self.exit_fees, 8),
            "tp_observed_price": round(self.tp_observed_price, 8),
            "tp_observed_bid": round(self.tp_observed_bid, 8),
            "tp_observed_ask": round(self.tp_observed_ask, 8),
            "tp_observed_source": self.tp_observed_source,
            "tp_observed_ts": round(self.tp_observed_ts, 3),
            "entry_quote_mid": round(self.entry_quote_mid, 8),
            "entry_quote_bid": round(self.entry_quote_bid, 8),
            "entry_quote_ask": round(self.entry_quote_ask, 8),
            "entry_quote_spread": round(self.entry_quote_spread, 8),
            "entry_quote_source": self.entry_quote_source,
            "entry_quote_ts": round(self.entry_quote_ts, 3),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Position":
        interval = str(raw.get("interval") or "5m").strip().lower()
        default_span = 900 if interval == "15m" else 300
        return cls(
            position_id=str(raw["position_id"]),
            asset=str(raw["asset"]).upper(),
            side=Side(str(raw["side"]).upper()),
            window_start=int(raw["window_start"]),
            window_end=int(
                raw.get("window_end")
                or int(raw["window_start"]) + default_span
            ),
            slug=str(raw.get("slug", "")),
            token_id=str(raw["token_id"]),
            entry_ts=float(raw["entry_ts"]),
            entry_price=float(raw["entry_price"]),
            shares=float(raw["shares"]),
            cost_usd=float(raw["cost_usd"]),
            entry_fee=float(raw.get("entry_fee", 0.0)),
            tp_price=float(raw["tp_price"]),
            sl_price=float(raw.get("sl_price", 0.0) or 0.0),
            stake_usd=float(raw.get("stake_usd", raw.get("cost_usd", 0.0))),
            interval=interval,
            mode=str(raw.get("mode") or "").strip().lower(),
            up_token=str(raw.get("up_token", "")),
            down_token=str(raw.get("down_token", "")),
            exchange_position_id=str(raw.get("exchange_position_id", "") or ""),
            tick_size=float(raw.get("tick_size", 0.01)),
            entry_order_id=str(raw.get("entry_order_id", "")),
            sold_shares=float(raw.get("sold_shares", 0.0)),
            exit_proceeds=float(raw.get("exit_proceeds", 0.0)),
            exit_fees=float(raw.get("exit_fees", 0.0)),
            tp_observed_price=float(raw.get("tp_observed_price", 0.0) or 0.0),
            tp_observed_bid=float(raw.get("tp_observed_bid", 0.0) or 0.0),
            tp_observed_ask=float(raw.get("tp_observed_ask", 0.0) or 0.0),
            tp_observed_source=str(raw.get("tp_observed_source", "") or ""),
            tp_observed_ts=float(raw.get("tp_observed_ts", 0.0) or 0.0),
            entry_quote_mid=float(raw.get("entry_quote_mid", 0.0) or 0.0),
            entry_quote_bid=float(raw.get("entry_quote_bid", 0.0) or 0.0),
            entry_quote_ask=float(raw.get("entry_quote_ask", 0.0) or 0.0),
            entry_quote_spread=float(
                raw.get("entry_quote_spread", 0.0) or 0.0
            ),
            entry_quote_source=str(raw.get("entry_quote_source", "") or ""),
            entry_quote_ts=float(raw.get("entry_quote_ts", 0.0) or 0.0),
        )


@dataclass
class SidePlan:
    """Per-market (coin × timeframe), per-side strategy parameters."""

    asset: str
    side: Side
    entry_thresh: float
    tp_pct: float
    stake_usd: float
    # Market identity: "ETH-5M" / "ETH-15M".  ``asset`` stays the coin so
    # PnL can still be grouped per coin.
    market: str = ""
    interval: str = "5m"
    # The market's window and entry window: 300 s / 60 s for 5m,
    # 900 s / 180 s for 15m.  TP/SL are armed at the fill and run until
    # the window closes; there is no time-based forced exit.
    window_sec: int = 300
    entry_window_end_s: float = 60.0
    stop_loss_pct: float = 0.0
    # Official taker fee rate for this market:
    #   fee = shares * rate * p * (1 - p)
    # BTC markets are 0.05; other crypto markets are 0.01.
    taker_fee_rate: float = 0.01

    @property
    def key(self) -> str:
        return f"{self.market or self.asset}-{self.side.value}"


@dataclass
class TradeRecord:
    """One completed position, written to trades.jsonl."""

    position_id: str
    asset: str
    side: str
    window_start: int
    slug: str
    entry_ts: float
    entry_price: float
    shares: float
    cost_usd: float
    entry_fee: float
    tp_price: float
    exit_ts: float
    exit_price: float
    exit_usdc: float
    exit_fee: float
    reason: str                # tp / sl / settle / shutdown
    pnl_gross: float
    pnl_net: float
    stake_usd: float
    mode: str                  # paper / live
    # Market timeframe ("5m" / "15m").  Older trade rows default to 5m.
    interval: str = "5m"
    sl_price: float = 0.0      # 0 = no price stop
    tp_observed_price: float = 0.0
    tp_observed_bid: float = 0.0
    tp_observed_ask: float = 0.0
    tp_observed_source: str = ""
    tp_observed_ts: float = 0.0
    entry_quote_mid: float = 0.0
    entry_quote_bid: float = 0.0
    entry_quote_ask: float = 0.0
    entry_quote_spread: float = 0.0
    entry_quote_source: str = ""
    entry_quote_ts: float = 0.0
    note: str = ""

    @property
    def market(self) -> str:
        return f"{self.asset}-{self.interval.upper()}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "position_id": self.position_id,
            "asset": self.asset,
            "interval": self.interval,
            "market": self.market,
            "side": self.side,
            "window_start": self.window_start,
            "slug": self.slug,
            "entry_ts": round(self.entry_ts, 3),
            "entry_price": round(self.entry_price, 8),
            "shares": round(self.shares, 8),
            "cost_usd": round(self.cost_usd, 8),
            "entry_fee": round(self.entry_fee, 8),
            "tp_price": round(self.tp_price, 8),
            "exit_ts": round(self.exit_ts, 3),
            "exit_price": round(self.exit_price, 8),
            "exit_usdc": round(self.exit_usdc, 8),
            "exit_fee": round(self.exit_fee, 8),
            "reason": self.reason,
            "pnl_gross": round(self.pnl_gross, 8),
            "pnl_net": round(self.pnl_net, 8),
            "stake_usd": self.stake_usd,
            "mode": self.mode,
            "sl_price": round(self.sl_price, 8),
            "tp_observed_price": round(self.tp_observed_price, 8),
            "tp_observed_bid": round(self.tp_observed_bid, 8),
            "tp_observed_ask": round(self.tp_observed_ask, 8),
            "tp_observed_source": self.tp_observed_source,
            "tp_observed_ts": round(self.tp_observed_ts, 3),
            "entry_quote_mid": round(self.entry_quote_mid, 8),
            "entry_quote_bid": round(self.entry_quote_bid, 8),
            "entry_quote_ask": round(self.entry_quote_ask, 8),
            "entry_quote_spread": round(self.entry_quote_spread, 8),
            "entry_quote_source": self.entry_quote_source,
            "entry_quote_ts": round(self.entry_quote_ts, 3),
            "note": self.note,
        }


@dataclass
class DailyStats:
    realized_pnl: float = 0.0
    trades: int = 0
    wins: int = 0
    losses: int = 0
    gross_pnl: float = 0.0
    fees: float = 0.0
    # Drawdown anchor: intraday high-water mark of realized PnL (starts
    # at 0).  The risk layer pauses new entries when peak_pnl -
    # realized_pnl reaches the configured intraday drawdown cap.
    peak_pnl: float = 0.0
    # Unix timestamp until which new entries stay paused after a
    # drawdown trigger; 0 = no pause scheduled.  When the cooldown
    # expires, peak_pnl is rebased to the current realized PnL so the
    # next pause needs a fresh drawdown of the same size.
    drawdown_pause_until: float = 0.0
    # Live-only risk series: the global risk caps apply to live markets
    # only, so the daily-loss / drawdown checks read these fields while
    # the aggregate fields above keep showing paper + live for the UI.
    live_realized_pnl: float = 0.0
    live_peak_pnl: float = 0.0
    live_drawdown_pause_until: float = 0.0

    def add(
        self, pnl_net: float, pnl_gross: float, fee: float, mode: str = ""
    ) -> None:
        self.realized_pnl += pnl_net
        self.gross_pnl += pnl_gross
        self.fees += fee
        self.trades += 1
        if pnl_net > 0:
            self.wins += 1
        elif pnl_net < 0:
            self.losses += 1
        self.peak_pnl = max(self.peak_pnl, self.realized_pnl)
        if str(mode or "").strip().lower() == "live":
            self.live_realized_pnl += pnl_net
            self.live_peak_pnl = max(
                self.live_peak_pnl, self.live_realized_pnl
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "realized_pnl": round(self.realized_pnl, 6),
            "trades": self.trades,
            "wins": self.wins,
            "losses": self.losses,
            "gross_pnl": round(self.gross_pnl, 6),
            "fees": round(self.fees, 6),
            "peak_pnl": round(self.peak_pnl, 6),
            "drawdown_pause_until": round(self.drawdown_pause_until, 3),
            "live_realized_pnl": round(self.live_realized_pnl, 6),
            "live_peak_pnl": round(self.live_peak_pnl, 6),
            "live_drawdown_pause_until": round(
                self.live_drawdown_pause_until, 3
            ),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "DailyStats":
        realized = float(raw.get("realized_pnl", 0.0))
        # state.json written before the drawdown pause has no peak_pnl.
        # The high-water mark is never below the current PnL, so treat a
        # positive realized PnL as having been the peak: a legacy file
        # must not look like a drawdown of its whole profit.
        peak = float(raw.get("peak_pnl", 0.0) or 0.0)
        # State files written before live-only risk have no live_* keys.
        # Reuse the aggregate series so an upgrade cannot hand back a
        # fresh daily-loss / drawdown budget mid-day.
        if "live_realized_pnl" in raw:
            live_realized = float(raw.get("live_realized_pnl", 0.0) or 0.0)
            live_peak = max(
                float(raw.get("live_peak_pnl", 0.0) or 0.0), live_realized
            )
            live_pause = float(
                raw.get("live_drawdown_pause_until", 0.0) or 0.0
            )
        else:
            live_realized = realized
            live_peak = max(peak, realized)
            live_pause = float(
                raw.get("drawdown_pause_until", 0.0) or 0.0
            )
        return cls(
            realized_pnl=realized,
            trades=int(raw.get("trades", 0)),
            wins=int(raw.get("wins", 0)),
            losses=int(raw.get("losses", 0)),
            gross_pnl=float(raw.get("gross_pnl", 0.0)),
            fees=float(raw.get("fees", 0.0)),
            peak_pnl=max(peak, realized),
            drawdown_pause_until=float(
                raw.get("drawdown_pause_until", 0.0) or 0.0
            ),
            live_realized_pnl=live_realized,
            live_peak_pnl=live_peak,
            live_drawdown_pause_until=live_pause,
        )
