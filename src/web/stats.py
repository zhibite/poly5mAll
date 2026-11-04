"""Trade-history statistics for the Web backend.

Input is the list of dicts from ``trades.jsonl``.  Everything is computed
from net PnL (fees included); gross and fees are reported alongside so the
operator can see what the 0.07 taker rate is costing.
"""
from __future__ import annotations

import time
from typing import Any, Iterable

from src.infra.timeutil import day_key


def _num(row: dict[str, Any], key: str) -> float:
    try:
        return float(row.get(key) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _bucket(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    net = sum(_num(r, "pnl_net") for r in rows)
    gross = sum(_num(r, "pnl_gross") for r in rows)
    fees = sum(
        _num(r, "entry_fee") + _num(r, "exit_fee") for r in rows
    )
    pnls = [_num(r, "pnl_net") for r in rows]
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x < 0]
    hold = [
        max(0.0, _num(r, "exit_ts") - _num(r, "entry_ts")) for r in rows
    ]
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for pnl in pnls:
        cum += pnl
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / n) if n else None,
        "net": round(net, 6),
        "gross": round(gross, 6),
        "fees": round(fees, 6),
        "avg_net": round(net / n, 6) if n else 0.0,
        "avg_win": round(sum(wins) / len(wins), 6) if wins else 0.0,
        "avg_loss": round(sum(losses) / len(losses), 6) if losses else 0.0,
        "profit_factor": (
            round(sum(wins) / abs(sum(losses)), 6) if losses else None
        ),
        "max_drawdown": round(max_dd, 6),
        "avg_hold_s": round(sum(hold) / n, 2) if n else 0.0,
    }


def _group(
    rows: Iterable[dict[str, Any]], key: str
) -> dict[str, dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        name = str(row.get(key) or "-")
        buckets.setdefault(name, []).append(row)
    return {name: _bucket(items) for name, items in sorted(buckets.items())}


def compute_stats(trades: list[dict[str, Any]]) -> dict[str, Any]:
    rows = sorted(
        trades, key=lambda r: _num(r, "exit_ts")
    )
    today = day_key(time.time())
    today_rows = [
        r for r in rows if day_key(_num(r, "exit_ts")) == today
    ]
    best = max(rows, key=lambda r: _num(r, "pnl_net"), default=None)
    worst = min(rows, key=lambda r: _num(r, "pnl_net"), default=None)

    return {
        "generated_at": time.time(),
        "today_date": today,
        "total": _bucket(rows),
        "today": _bucket(today_rows),
        "by_asset": _group(rows, "asset"),
        "by_reason": _group(rows, "reason"),
        "by_side": _group(rows, "side"),
        "by_day": {
            day: _bucket(items)
            for day, items in sorted(_by_day(rows).items())
        },
        "best_trade": best,
        "worst_trade": worst,
    }


def _by_day(
    rows: list[dict[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        day = day_key(_num(row, "exit_ts"))
        out.setdefault(day, []).append(row)
    return out
