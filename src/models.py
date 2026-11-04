"""Core data structures shared by the market, strategy and execution layers."""
from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Any


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
    ENTRY = "entry"             # 0–60 s: observe and buy on first touch
    TP = "tp"                   # 60–240 s: take profit only
    FORCE_EXIT = "force_exit"   # 240–295 s: sell at market
    ENDED = "ended"             # >295 s: give up on the market order


class SideStatus(str, Enum):
    WAITING = "waiting"
    ENTERING = "entering"
    HOLDING = "holding"
    EXITING = "exiting"
    CLOSED = "closed"
    SETTLE_PENDING = "settle_pending"


@dataclass(frozen=True)
class MarketWindow:
    """One 5m Up/Down market."""

    asset: str
    slug: str
    start_ts: int
    end_ts: int
    up_token: str
    down_token: str
    condition_id: str = ""
    tick_size: float = 0.01
    fee_rate_bps: int = 0

    def token(self, side: Side) -> str:
        return self.up_token if side is Side.UP else self.down_token

    def seconds_since(self, now: float) -> float:
        return float(now) - float(self.start_ts)

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
    up_token: str = ""
    down_token: str = ""
    tick_size: float = 0.01
    entry_order_id: str = ""
    sold_shares: float = 0.0
    exit_proceeds: float = 0.0
    exit_fees: float = 0.0

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
            "tick_size": self.tick_size,
            "entry_order_id": self.entry_order_id,
            "sold_shares": round(self.sold_shares, 8),
            "exit_proceeds": round(self.exit_proceeds, 8),
            "exit_fees": round(self.exit_fees, 8),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Position":
        return cls(
            position_id=str(raw["position_id"]),
            asset=str(raw["asset"]).upper(),
            side=Side(str(raw["side"]).upper()),
            window_start=int(raw["window_start"]),
            window_end=int(raw.get("window_end") or int(raw["window_start"]) + 300),
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
            up_token=str(raw.get("up_token", "")),
            down_token=str(raw.get("down_token", "")),
            tick_size=float(raw.get("tick_size", 0.01)),
            entry_order_id=str(raw.get("entry_order_id", "")),
            sold_shares=float(raw.get("sold_shares", 0.0)),
            exit_proceeds=float(raw.get("exit_proceeds", 0.0)),
            exit_fees=float(raw.get("exit_fees", 0.0)),
        )


@dataclass
class SidePlan:
    """Per-asset, per-side strategy parameters (from the doc)."""

    asset: str
    side: Side
    entry_thresh: float
    tp_pct: float
    stake_usd: float
    stop_loss_pct: float = 0.20

    @property
    def key(self) -> str:
        return f"{self.asset}-{self.side.value}"


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
    reason: str                # tp / time_exit / settle
    pnl_gross: float
    pnl_net: float
    stake_usd: float
    mode: str                  # paper / live
    sl_price: float = 0.0      # 0 = no price stop
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "position_id": self.position_id,
            "asset": self.asset,
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

    def add(self, pnl_net: float, pnl_gross: float, fee: float) -> None:
        self.realized_pnl += pnl_net
        self.gross_pnl += pnl_gross
        self.fees += fee
        self.trades += 1
        if pnl_net > 0:
            self.wins += 1
        elif pnl_net < 0:
            self.losses += 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "realized_pnl": round(self.realized_pnl, 6),
            "trades": self.trades,
            "wins": self.wins,
            "losses": self.losses,
            "gross_pnl": round(self.gross_pnl, 6),
            "fees": round(self.fees, 6),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "DailyStats":
        return cls(
            realized_pnl=float(raw.get("realized_pnl", 0.0)),
            trades=int(raw.get("trades", 0)),
            wins=int(raw.get("wins", 0)),
            losses=int(raw.get("losses", 0)),
            gross_pnl=float(raw.get("gross_pnl", 0.0)),
            fees=float(raw.get("fees", 0.0)),
        )
