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


def _tp_execution(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Observed-vs-actual take-profit analysis.

    ``tp_observed_price`` is the quote mid captured on the TP sell attempt;
    ``exit_price`` is the actual fill.  Legacy trades without the field are
    counted as missing observation, not as zero-edge trades.
    """
    tp_rows = [
        r for r in rows if str(r.get("reason", "")).lower() == "tp"
    ]
    observed = [
        r for r in tp_rows if _num(r, "tp_observed_price") > 0
    ]
    n = len(observed)

    def _avg(values: list[float]) -> float:
        return round(sum(values) / len(values), 8) if values else 0.0

    observed_edges = [
        _num(r, "tp_observed_price") - _num(r, "tp_price")
        for r in observed
    ]
    actual_edges = [
        _num(r, "exit_price") - _num(r, "tp_price") for r in observed
    ]
    fill_vs_observed = [
        _num(r, "exit_price") - _num(r, "tp_observed_price")
        for r in observed
    ]
    observed_bps = [
        (_num(r, "tp_observed_price") / _num(r, "tp_price") - 1.0)
        * 10000.0
        for r in observed
        if _num(r, "tp_price") > 0
    ]
    actual_bps = [
        (_num(r, "exit_price") / _num(r, "tp_price") - 1.0)
        * 10000.0
        for r in observed
        if _num(r, "tp_price") > 0
    ]
    fill_vs_observed_bps = [
        (_num(r, "exit_price") / _num(r, "tp_observed_price") - 1.0)
        * 10000.0
        for r in observed
        if _num(r, "tp_observed_price") > 0
    ]
    observed_above = sum(
        1
        for r in observed
        if _num(r, "tp_observed_price") > _num(r, "tp_price") + 1e-9
    )
    actual_above = sum(
        1
        for r in observed
        if _num(r, "exit_price") > _num(r, "tp_price") + 1e-9
    )

    return {
        "tp_trades": len(tp_rows),
        "with_observation": n,
        "missing_observation": len(tp_rows) - n,
        "observed_above_target": observed_above,
        "observed_above_rate": (observed_above / n) if n else None,
        "actual_above_target": actual_above,
        "actual_above_rate": (actual_above / n) if n else None,
        "avg_observed_edge": _avg(observed_edges),
        "avg_actual_edge": _avg(actual_edges),
        "avg_fill_vs_observed": _avg(fill_vs_observed),
        "avg_observed_edge_bps": _avg(observed_bps),
        "avg_actual_edge_bps": _avg(actual_bps),
        "avg_fill_vs_observed_bps": _avg(fill_vs_observed_bps),
    }


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
        "tp_execution": _tp_execution(rows),
    }


def _by_day(
    rows: list[dict[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        day = day_key(_num(row, "exit_ts"))
        out.setdefault(day, []).append(row)
    return out
