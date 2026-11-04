"""Small JSON/JSONL persistence layer.

Files under ``<data_dir>``:

    state.json    open positions + daily realized PnL (restart recovery)
    trades.jsonl  one completed trade per line (append-only audit trail)
    status.json   snapshot written by the status loop for external monitors

Writes are atomic (tmp + os.replace) so a crash mid-write cannot leave a
truncated state file.
"""
from __future__ import annotations

import json
import os
import time
from collections import deque
from pathlib import Path
from typing import Any

from src.infra.logging import get_logger
from src.models import DailyStats, Position, TradeRecord

log = get_logger("store")


class StateStore:
    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.data_dir / "state.json"
        self.trades_path = self.data_dir / "trades.jsonl"
        self.status_path = self.data_dir / "status.json"

    # ── state ──────────────────────────────────────────────────────────
    def load(self) -> tuple[list[Position], dict[str, DailyStats]]:
        if not self.state_path.exists():
            return [], {}
        try:
            with self.state_path.open("r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("state.load_failed error={} — starting empty", exc)
            try:
                self.state_path.replace(
                    self.state_path.with_suffix(".json.corrupt")
                )
            except OSError:
                pass
            return [], {}

        positions: list[Position] = []
        for row in raw.get("positions") or []:
            try:
                positions.append(Position.from_dict(row))
            except (KeyError, TypeError, ValueError) as exc:
                log.warning("state.position_skipped error={}", exc)

        daily: dict[str, DailyStats] = {}
        for day, row in (raw.get("daily") or {}).items():
            try:
                daily[str(day)] = DailyStats.from_dict(row)
            except (TypeError, ValueError):
                continue
        return positions, daily

    def save(
        self,
        positions: list[Position],
        daily: dict[str, DailyStats],
        extra: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "version": 1,
            "updated_at": time.time(),
            "positions": [p.to_dict() for p in positions],
            "daily": {day: stats.to_dict() for day, stats in daily.items()},
        }
        if extra:
            payload.update(extra)
        self._atomic_write_json(self.state_path, payload)

    # ── audit trail ────────────────────────────────────────────────────
    def append_trade(self, record: TradeRecord) -> None:
        try:
            with self.trades_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")
        except OSError as exc:
            log.warning("trade.append_failed error={}", exc)

    def all_trades(self) -> list[dict[str, Any]]:
        """Read every well-formed trade record (file order)."""
        if not self.trades_path.exists():
            return []
        rows: list[dict[str, Any]] = []
        try:
            with self.trades_path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(row, dict):
                        rows.append(row)
        except OSError as exc:
            log.warning("trade.read_failed error={}", exc)
        return rows

    def query_trades(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        asset: str = "",
        reason: str = "",
        mode: str = "",
    ) -> tuple[list[dict[str, Any]], int]:
        """Filtered, newest-first trade page plus the unpaged total."""
        rows = self.all_trades()
        asset_u = asset.strip().upper()
        reason_l = reason.strip().lower()
        mode_l = mode.strip().lower()
        if asset_u:
            rows = [r for r in rows if str(r.get("asset", "")).upper() == asset_u]
        if reason_l:
            rows = [
                r for r in rows if str(r.get("reason", "")).lower() == reason_l
            ]
        if mode_l:
            rows = [r for r in rows if str(r.get("mode", "")).lower() == mode_l]
        rows.sort(key=lambda r: float(r.get("exit_ts", 0) or 0), reverse=True)
        total = len(rows)
        start = max(0, int(offset))
        end = start + max(1, min(int(limit), 500))
        return rows[start:end], total

    def tail_trades(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return the newest ``limit`` trade records (oldest first)."""
        if not self.trades_path.exists():
            return []
        n = max(1, min(int(limit), 1000))
        rows: deque[dict[str, Any]] = deque(maxlen=n)
        try:
            with self.trades_path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(row, dict):
                        rows.append(row)
        except OSError as exc:
            log.warning("trade.tail_failed error={}", exc)
        return list(rows)

    # ── status ─────────────────────────────────────────────────────────
    def write_status(self, payload: dict[str, Any]) -> None:
        self._atomic_write_json(self.status_path, payload)

    # ── helpers ────────────────────────────────────────────────────────
    @staticmethod
    def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except OSError as exc:
            log.warning("state.write_failed path={} error={}", path, exc)
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
