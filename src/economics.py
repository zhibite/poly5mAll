"""Fee arithmetic and the startup fee report.

Polymarket's crypto-category taker fee is

    fee = shares * fee_rate * p * (1 - p)

with ``fee_rate = 0.07`` (7%) and makers paying zero.  The formula is
maximised at p = 0.5, so a round trip in a 5m market is expensive relative
to the 10–25% TP targets used by strategy 1.  The doc's backtest excludes
fees; this module makes the cost visible before a single order is sent.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal

from src.models import SidePlan


def floor_tick(price: float, tick: float) -> float:
    """Round a price down to the exchange tick (BUY limit / SELL limit)."""
    if tick <= 0:
        return float(price)
    return float(
        Decimal(str(price)).quantize(Decimal(str(tick)), rounding=ROUND_DOWN)
    )


def ceil_tick(price: float, tick: float) -> float:
    if tick <= 0:
        return float(price)
    return float(
        Decimal(str(price)).quantize(Decimal(str(tick)), rounding=ROUND_UP)
    )


def taker_fee(shares: float, price: float, rate: float) -> float:
    """Fee in USDC for ``shares`` executed at ``price``."""
    s = max(0.0, float(shares))
    p = min(1.0, max(0.0, float(price)))
    return s * max(0.0, float(rate)) * p * (1.0 - p)


@dataclass(frozen=True)
class FeeLine:
    asset: str
    side: str
    entry: float
    tp_pct: float
    tp_price: float
    gross_per_share: float
    entry_fee_per_share: float
    exit_fee_per_share: float
    net_per_share: float
    shares: float
    gross_per_trade: float
    net_per_trade: float
    net_roi: float
    breakeven_tp: float | None

    @property
    def key(self) -> str:
        return f"{self.asset}-{self.side}"

    @property
    def fee_negative(self) -> bool:
        return self.net_per_trade <= 0


def fee_line(
    plan: SidePlan,
    *,
    include_fees: bool = True,
    fee_rate: float = 0.07,
    breakeven_scan_step: float = 0.001,
) -> FeeLine:
    """Expected TP outcome for one asset/side at its configured entry."""
    p = float(plan.entry_thresh)
    tp_pct = float(plan.tp_pct)
    tp_price = p * (1.0 + tp_pct)
    shares = plan.stake_usd / p if p > 0 else 0.0

    gross_per_share = p * tp_pct
    rate = float(fee_rate) if include_fees else 0.0
    entry_fee = taker_fee(1.0, p, rate)
    exit_fee = taker_fee(1.0, tp_price, rate)
    net_per_share = gross_per_share - entry_fee - exit_fee
    gross_per_trade = gross_per_share * shares
    net_per_trade = net_per_share * shares
    net_roi = net_per_trade / plan.stake_usd if plan.stake_usd > 0 else 0.0

    return FeeLine(
        asset=plan.asset,
        side=plan.side.value,
        entry=p,
        tp_pct=tp_pct,
        tp_price=tp_price,
        gross_per_share=gross_per_share,
        entry_fee_per_share=entry_fee,
        exit_fee_per_share=exit_fee,
        net_per_share=net_per_share,
        shares=shares,
        gross_per_trade=gross_per_trade,
        net_per_trade=net_per_trade,
        net_roi=net_roi,
        breakeven_tp=breakeven_tp(
            p, rate, step=breakeven_scan_step
        ),
    )


def breakeven_tp(
    entry: float, fee_rate: float, *, step: float = 0.001
) -> float | None:
    """Smallest TP fraction whose round-trip net profit is >= 0.

    Returns None when no TP below the market's $1 ceiling covers the fee.
    """
    p = float(entry)
    if p <= 0 or p >= 1:
        return None
    max_tp = (1.0 - 1e-9) / p - 1.0
    if max_tp <= 0:
        return None
    tp = 0.0
    while tp <= max_tp:
        exit_price = p * (1.0 + tp)
        net = (
            p * tp
            - taker_fee(1.0, p, fee_rate)
            - taker_fee(1.0, exit_price, fee_rate)
        )
        if net >= 0:
            return round(tp, 6)
        tp += step
    return None


def fee_table(plans: list[SidePlan], *, include_fees: bool = True,
              fee_rate: float = 0.07) -> str:
    """Human-readable table for --check and the startup log."""
    lines = [
        "Per-trade TP economics (1 share basis; doc params, taker fees):",
        "asset side  entry  tp%   tp$    gross   fees    net     net/10U  be_tp",
        "--------------------------------------------------------------------------------",
    ]
    for plan in plans:
        ln = fee_line(plan, include_fees=include_fees, fee_rate=fee_rate)
        be = "n/a" if ln.breakeven_tp is None else f"{ln.breakeven_tp*100:5.1f}%"
        flag = "  <-- FEE-NEGATIVE" if ln.fee_negative and include_fees else ""
        lines.append(
            f"{ln.asset:5} {ln.side:4} {ln.entry:6.3f} {ln.tp_pct*100:5.0f} "
            f"{ln.tp_price:6.3f} {ln.gross_per_share:7.4f} "
            f"{ln.entry_fee_per_share + ln.exit_fee_per_share:7.4f} "
            f"{ln.net_per_share:7.4f} {ln.net_per_trade:+8.3f} {be:>7}{flag}"
        )
    lines.append(
        "--------------------------------------------------------------------------------"
    )
    lines.append(
        "net/10U is the expected profit on a 10 U stake if the TP is hit. "
        "Fees alone can make a TP a loss."
    )
    return "\n".join(lines)
