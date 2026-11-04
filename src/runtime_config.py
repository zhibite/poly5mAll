"""Runtime per-asset overrides edited from the Web config page.

The immutable ``config.toml`` remains the baseline.  This file stores the
effective per-asset values in ``<data_dir>/runtime_config.json`` so a web
edit survives a restart.  The runner reads the current asset config at the
start of every 5m window; enable/disable is checked on every entry attempt,
so closing a coin takes effect immediately while entry/TP/side/stake edits
wait for the next window.

Only strategy fields are stored here.  Credentials, fee rate, port and the
timing constants stay in .env / config.toml.
"""
from __future__ import annotations

import dataclasses
import json
import os
import time
from pathlib import Path
from typing import Any

from src.config import AssetConfig
from src.infra.logging import get_logger
from src.models import Side

log = get_logger("runtime_config")


def _parse_sides(raw: Any) -> tuple[Side, ...]:
    if isinstance(raw, (list, tuple)):
        tokens = [str(x).strip().upper() for x in raw]
    else:
        text = str(raw or "").replace("+", ",")
        tokens = [x.strip().upper() for x in text.split(",")]
    if len(tokens) == 1 and tokens[0] in ("BOTH", "ALL", "*"):
        return (Side.UP, Side.DOWN)
    sides: list[Side] = []
    for token in tokens:
        if token in ("UP", "U") and Side.UP not in sides:
            sides.append(Side.UP)
        elif token in ("DOWN", "D") and Side.DOWN not in sides:
            sides.append(Side.DOWN)
    if not sides:
        raise ValueError("sides must contain UP and/or DOWN")
    return tuple(sides)


def _validate_values(
    name: str, raw: dict[str, Any], base: AssetConfig
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"{name}: asset override must be an object")
    out: dict[str, Any] = {}
    if "enabled" in raw:
        out["enabled"] = bool(raw["enabled"])
    if "sides" in raw:
        out["sides"] = _parse_sides(raw["sides"])

    try:
        entry = float(raw.get("entry_thresh", base.entry_thresh))
        tp = float(raw.get("tp_pct", base.tp_pct))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name}: entry_thresh/tp_pct must be numbers") from exc
    if not (0.0 < entry < 1.0):
        raise ValueError(f"{name}: entry_thresh must be in (0, 1)")
    if tp <= 0:
        raise ValueError(f"{name}: tp_pct must be > 0")
    if entry * (1.0 + tp) >= 1.0:
        raise ValueError(
            f"{name}: entry_thresh * (1 + tp_pct) must be < 1"
        )
    out["entry_thresh"] = entry
    out["tp_pct"] = tp

    if "stake_usd" in raw and raw["stake_usd"] is not None:
        try:
            stake = float(raw["stake_usd"])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name}: stake_usd must be a number") from exc
        if stake <= 0:
            raise ValueError(f"{name}: stake_usd must be > 0")
        out["stake_usd"] = stake
    elif "stake_usd" in raw:
        out["stake_usd"] = None

    return out


class RuntimeConfig:
    def __init__(
        self,
        path: str | Path | None,
        base_assets: tuple[AssetConfig, ...],
    ) -> None:
        self.path = Path(path) if path else None
        self.base: dict[str, AssetConfig] = {
            a.asset: a for a in base_assets
        }
        self.order: list[str] = [a.asset for a in base_assets]
        self.overrides: dict[str, dict[str, Any]] = {}
        self.version = 0
        self.updated_at = 0.0
        self.last_error = ""
        self.load()

    # ── reads ──────────────────────────────────────────────────────────
    def asset(self, name: str) -> AssetConfig:
        name = name.upper()
        base = self.base[name]
        ov = self.overrides.get(name, {})
        return dataclasses.replace(
            base,
            enabled=bool(ov.get("enabled", base.enabled)),
            sides=tuple(ov.get("sides", base.sides)),
            entry_thresh=float(ov.get("entry_thresh", base.entry_thresh)),
            tp_pct=float(ov.get("tp_pct", base.tp_pct)),
            stake_usd=ov.get("stake_usd", base.stake_usd),
        )

    def assets(self) -> tuple[AssetConfig, ...]:
        return tuple(self.asset(name) for name in self.order)

    def snapshot(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for name in self.order:
            a = self.asset(name)
            out.append(
                {
                    "asset": a.asset,
                    "enabled": a.enabled,
                    "sides": [s.value for s in a.sides],
                    "entry_thresh": a.entry_thresh,
                    "tp_pct": a.tp_pct,
                    "stake_usd": a.stake_usd,
                }
            )
        return out

    # ── writes ─────────────────────────────────────────────────────────
    def update(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Apply ``{"assets": {NAME: {...}}}`` and persist."""
        assets_raw = payload.get("assets", payload)
        if not isinstance(assets_raw, dict):
            raise ValueError("assets must be an object")
        unknown = [k for k in assets_raw if str(k).upper() not in self.base]
        if unknown:
            raise ValueError(f"unknown asset(s): {', '.join(unknown)}")

        for name, raw in assets_raw.items():
            name_u = str(name).upper()
            base = self.base[name_u]
            # Validate the combination of the existing override and the
            # patch, so a partial update cannot create entry*(1+tp) >= 1
            # using the baseline value for the missing field.
            merged = dict(self.overrides.get(name_u, {}))
            merged.update(raw)
            self.overrides[name_u] = _validate_values(
                name_u, merged, base
            )

        self.version += 1
        self.updated_at = time.time()
        self._save()
        log.info(
            "runtime_config.updated version={} assets={}",
            self.version,
            ",".join(str(k).upper() for k in assets_raw),
        )
        return self.snapshot()

    def reset(self) -> list[dict[str, Any]]:
        self.overrides = {}
        self.version += 1
        self.updated_at = time.time()
        if self.path is not None:
            try:
                self.path.unlink(missing_ok=True)
            except OSError as exc:
                log.warning("runtime_config.reset_failed error={}", exc)
        log.info("runtime_config.reset version={}", self.version)
        return self.snapshot()

    # ── persistence ────────────────────────────────────────────────────
    def load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            self.last_error = str(exc)
            log.warning("runtime_config.load_failed error={}", exc)
            return

        self.version = int(raw.get("version", 0) or 0)
        self.updated_at = float(raw.get("updated_at", 0.0) or 0.0)
        assets_raw = raw.get("assets") or {}
        if not isinstance(assets_raw, dict):
            return
        for name, values in assets_raw.items():
            name_u = str(name).upper()
            base = self.base.get(name_u)
            if base is None or not isinstance(values, dict):
                continue
            try:
                # The saved file contains effective values, so treat every
                # field as an override (same values as base when untouched).
                validated = _validate_values(name_u, values, base)
            except ValueError as exc:
                self.last_error = str(exc)
                log.warning(
                    "runtime_config.asset_skipped asset={} error={}",
                    name_u,
                    exc,
                )
                continue
            self.overrides[name_u] = validated
        log.info(
            "runtime_config.loaded version={} overrides={}",
            self.version,
            len(self.overrides),
        )

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": max(1, self.version),
            "updated_at": self.updated_at or time.time(),
            "assets": {
                row["asset"]: {
                    "enabled": row["enabled"],
                    "sides": "+".join(row["sides"]),
                    "entry_thresh": row["entry_thresh"],
                    "tp_pct": row["tp_pct"],
                    "stake_usd": row["stake_usd"],
                }
                for row in self.snapshot()
            },
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("runtime_config.save_failed error={}", exc)
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            raise
