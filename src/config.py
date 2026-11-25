"""Configuration loading and validation.

Layering:

    code defaults (the doc's production parameters)
        < config.toml
        < .env / .env.docker
        < process environment

The strategy parameters live in ``[ASSETS.<NAME>]`` tables.  Everything
else is a small dataclass; no pydantic model is needed for a single-process
bot.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from src.models import Side, SidePlan

ROOT = Path(__file__).resolve().parent.parent

# Entry/TP values are from docs/all_5m_strategy1_summary.md §2.  The doc's
# direction study recommended DOWN (UP for XRP), but the operator default in
# this project is BOTH for every asset; change them per coin in /config.
# (sides, entry_thresh, tp_pct)
ASSET_DEFAULTS: dict[str, tuple[str, float, float]] = {
    "BTC": ("BOTH", 0.25, 0.10),
    "ETH": ("BOTH", 0.18, 0.15),
    "BNB": ("BOTH", 0.15, 0.10),
    "SOL": ("BOTH", 0.15, 0.20),
    "DOGE": ("BOTH", 0.12, 0.25),
    "XRP": ("BOTH", 0.10, 0.25),
    "HYPE": ("BOTH", 0.14, 0.25),
}

# The doc's asset order, used when TRADE_ASSETS is not set.
DEFAULT_ASSET_ORDER = ("BTC", "ETH", "BNB", "SOL", "DOGE", "XRP", "HYPE")

# Supported market timeframes: name -> (window seconds, entry window end).
# 5m markets enter in the first 60 s; 15m markets in the first 3 minutes.
INTERVAL_DEFAULTS: dict[str, tuple[int, float]] = {
    "5m": (300, 60.0),
    "15m": (900, 180.0),
}
DEFAULT_INTERVALS = ("5m", "15m")
# 15m is opt-in: its backtest was not good enough to run by default.  The
# market still exists in the config page and can be enabled per coin, or
# with TRADE_INTERVALS=15m (which makes the selected timeframe the default).
INTERVAL_DEFAULT_ENABLED: dict[str, bool] = {
    "5m": True,
    "15m": False,
}

# Official Polymarket taker fee rates for crypto markets
# (fee = shares * rate * p * (1 - p)): BTC is 5%, everything else is 1%.
# Used only when neither config.toml nor the environment sets a rate.
ASSET_DEFAULT_FEE_RATE: dict[str, float] = {"BTC": 0.05}

# Price stop defaults: 0 = off.  TP and the optional price stop are the
# only exits apart from Gamma settlement at the window close.
ASSET_DEFAULT_STOP_LOSS: dict[str, float] = {
    "BTC": 0.0,
    "ETH": 0.0,
    "BNB": 0.0,
    "SOL": 0.0,
    "DOGE": 0.0,
    "XRP": 0.0,
    "HYPE": 0.0,
}


class ConfigError(ValueError):
    """Invalid configuration."""


# ---------------------------------------------------------------------------
# Value helpers
# ---------------------------------------------------------------------------


def _env(name: str, default: str = "") -> str:
    return str(os.environ.get(name, default) or "").strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    """Case-insensitive section lookup (TOML keys are case-sensitive)."""
    for key, value in raw.items():
        if key.upper() == name.upper() and isinstance(value, dict):
            return value
    return {}


def _get(section: dict[str, Any], key: str, default: Any) -> Any:
    if key in section:
        return section[key]
    upper = key.upper()
    for k, v in section.items():
        if k.upper() == upper:
            return v
    return default


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IntervalConfig:
    """One market timeframe (5m / 15m)."""

    name: str
    window_sec: int
    entry_window_end_s: float

    @property
    def key(self) -> str:
        return self.name.upper()


@dataclass(frozen=True)
class GeneralConfig:
    tick_s: float = 0.25


@dataclass(frozen=True)
class FeedConfig:
    use_ws: bool = True
    # Split CLOB WS subscriptions across independent connections so one
    # slow-consumer reset cannot take down every asset.
    ws_shards: int = 7
    # REST /book fallback poll for current tokens (keeps bid/ask usable
    # when the WS is down).
    book_poll_s: float = 3.0
    rest_midpoint_poll_s: float = 2.0
    market_discovery_s: float = 2.0
    pre_discovery_lead_s: float = 60.0
    stale_price_max_age_s: float = 5.0
    exit_stale_price_max_age_s: float = 15.0
    request_timeout_s: float = 5.0


@dataclass(frozen=True)
class ExecutionConfig:
    paper_trading: bool = True
    live_enabled: bool = False
    stake_usd: float = 2.0
    # Default taker fee rate for markets without an override.  Polymarket
    # charges 0.05 on BTC markets and 0.01 on the other crypto markets;
    # see [ASSETS.<NAME>] TAKER_FEE_RATE and per-interval overrides.
    taker_fee_rate: float = 0.01
    include_fees: bool = True
    # "observed": trigger on executable bid >= target; FAK sell at target
    #             floor, so the fill is the best bid (captures gaps above
    #             target, never below target).
    # "market":   trigger on observed mid >= target; marketable FAK below
    #             the bid (doc's option B).  Can fill below target.
    tp_execution_mode: str = "observed"
    # Price stop-loss is retired.  Kept at 0 for config/state compatibility;
    # the 240s time-based forced exit remains.
    stop_loss_pct: float = 0.0
    # Liquidity gate before a BUY: require both sides of the book and at
    # least this much bid notional (USDC) so the position can be exited.
    require_two_sided_book: bool = True
    min_entry_bid_depth_usd: float = 5.0
    # Max concurrent open positions with the same side (UP or DOWN) across
    # all markets.  0 disables the cap.
    max_same_side_positions: int = 6
    # Global concurrent exposure across BOTH timeframes, both sides and all
    # markets.  Open positions plus in-flight entries count.  0 disables
    # the corresponding cap.  This is the aggregate guard on top of the
    # per-window caps below.
    max_open_positions: int = 6
    max_open_notional_usd: float = 10.0
    # Max same-side entries inside one market window, across all markets
    # on that timeframe.  Crypto markets are highly correlated, so several
    # UP entries in one window are effectively one leveraged bet.  0
    # disables the cap.
    max_same_side_per_window: int = 2
    entry_retry_s: float = 1.0
    exit_retry_s: float = 1.0
    slippage_pct: float = 2.0
    max_spread_abs: float = 0.0
    # Stop opening new positions once the UTC day's realized net PnL is at
    # or below -MAX_DAILY_LOSS_USD.  0 disables the cap.  Scaled for the
    # 12-market (5m + 15m) deployment.
    max_daily_loss_usd: float = 40.0
    # Pause new entries when the UTC day's realized PnL falls this far
    # below its intraday high-water mark.  0 disables the pause.
    max_intraday_drawdown_usd: float = 16.0
    # Cooldown after a drawdown pause before entries resume.  When it
    # expires the day's drawdown anchor is rebased to the current PnL, so
    # a further pause needs a fresh drawdown of the same size.
    # 0 disables the drawdown pause.
    intraday_drawdown_pause_min: float = 60.0
    # Total exposure per market window across BOTH sides and all markets
    # on that timeframe; 5m and 15m windows are counted separately.
    # 0 disables the corresponding cap.
    max_positions_per_window: int = 3
    max_window_notional_usd: float = 6.0
    # Per-asset rolling performance de-risking.  If an asset's realized
    # PnL over the lookback is <= ASSET_PERF_MIN_PNL_USD its stake is
    # multiplied by ASSET_PERF_STAKE_MULT; at or below
    # ASSET_PERF_DISABLE_PNL_USD (<0) new entries are skipped entirely.
    # ASSET_PERF_LOOKBACK_DAYS=0 disables the rule.
    asset_perf_lookback_days: float = 7.0
    asset_perf_min_pnl_usd: float = -3.0
    asset_perf_stake_mult: float = 0.5
    asset_perf_disable_pnl_usd: float = -6.0
    kill_switch_file: str = "data/STOP"
    pause_new_entries: bool = False
    flatten_on_exit: bool = False
    skip_fee_negative: bool = False

    @property
    def live(self) -> bool:
        return (not self.paper_trading) and self.live_enabled

    @property
    def mode(self) -> str:
        return "live" if self.live else "paper"


@dataclass(frozen=True)
class StorageConfig:
    data_dir: str = "data"


@dataclass(frozen=True)
class WebConfig:
    """In-process FastAPI backend (dashboard + JSON API)."""

    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 8033
    username: str = "admin"
    password: str = ""
    session_ttl_s: int = 7 * 24 * 3600
    log_level: str = "warning"
    cors_origins: tuple[str, ...] = ("*",)

    @property
    def auth_enabled(self) -> bool:
        return bool(self.password)


@dataclass(frozen=True)
class AssetConfig:
    """One tradable market: a coin on one timeframe (e.g. ETH-15M)."""

    asset: str
    enabled: bool
    sides: tuple[Side, ...]
    entry_thresh: float
    tp_pct: float
    stake_usd: float | None = None
    # Price stop as a fraction below the actual fill price (0 = off).
    # Armed at entry time, like the take-profit target.
    stop_loss_pct: float = 0.0
    # Market timeframe and its timing.  There is no forced time exit:
    # TP/SL run from the fill until the window closes, then any remainder
    # settles at the Gamma 0/1 outcome.
    interval: str = "5m"
    window_sec: int = 300
    entry_window_end_s: float = 60.0
    # Official taker fee rate for this market (BTC = 0.05, everything else
    # = 0.01).  Resolved from [ASSETS.<NAME>] TAKER_FEE_RATE or the
    # per-interval sub-table, falling back to [EXECUTION] TAKER_FEE_RATE.
    taker_fee_rate: float = 0.01

    @property
    def market(self) -> str:
        """Market id, e.g. ``ETH-5M`` / ``ETH-15M``."""
        return f"{self.asset}-{self.interval.upper()}"

    @property
    def label(self) -> str:
        return f"{self.asset} {self.interval}"

    def plans(self, default_stake: float) -> list[SidePlan]:
        stake = float(self.stake_usd or default_stake)
        return [
            SidePlan(
                asset=self.asset,
                side=side,
                entry_thresh=float(self.entry_thresh),
                tp_pct=float(self.tp_pct),
                stake_usd=stake,
                market=self.market,
                interval=self.interval,
                window_sec=int(self.window_sec),
                entry_window_end_s=float(self.entry_window_end_s),
                stop_loss_pct=float(self.stop_loss_pct),
                taker_fee_rate=float(self.taker_fee_rate),
            )
            for side in self.sides
        ]


@dataclass(frozen=True)
class Credentials:
    api_key: str = ""
    api_secret: str = ""
    api_passphrase: str = ""
    wallet_private_key: str = ""
    wallet_address: str = ""
    funder_address: str = ""
    signature_type: int = 0
    polymarket_host: str = "https://clob.polymarket.com"
    gamma_api: str = "https://gamma-api.polymarket.com"

    @property
    def has_l2(self) -> bool:
        return bool(self.api_key and self.api_secret and self.api_passphrase)


@dataclass(frozen=True)
class Settings:
    general: GeneralConfig
    feed: FeedConfig
    execution: ExecutionConfig
    storage: StorageConfig
    # One AssetConfig per market (coin × timeframe), 12 by default.
    assets: tuple[AssetConfig, ...]
    intervals: tuple[IntervalConfig, ...]
    credentials: Credentials
    web: WebConfig
    log_level: str = "INFO"
    config_path: str = ""

    # ── derived helpers ──────────────────────────────────────────────────
    @property
    def live(self) -> bool:
        return self.execution.live

    @property
    def mode(self) -> str:
        return self.execution.mode

    @property
    def data_dir(self) -> Path:
        p = Path(self.storage.data_dir)
        return p if p.is_absolute() else ROOT / p

    def resolve_path(self, raw: str) -> Path:
        p = Path(raw)
        return p if p.is_absolute() else ROOT / p

    def plans(self) -> list[SidePlan]:
        out: list[SidePlan] = []
        for asset in self.assets:
            if asset.enabled:
                out.extend(asset.plans(self.execution.stake_usd))
        return out

    def all_plans(self) -> list[SidePlan]:
        """Every configured asset/side, including disabled ones."""
        out: list[SidePlan] = []
        for asset in self.assets:
            out.extend(asset.plans(self.execution.stake_usd))
        return out

    def interval(self, name: str) -> IntervalConfig | None:
        name = str(name).strip().lower()
        for interval in self.intervals:
            if interval.name == name:
                return interval
        return None

    def asset(self, name: str) -> AssetConfig | None:
        """Look up a market by id ("ETH-15M") or a coin ("ETH" -> 5m)."""
        wanted = str(name).strip().upper()
        for asset in self.assets:
            if asset.market.upper() == wanted:
                return asset
        for asset in self.assets:
            if asset.asset.upper() == wanted and asset.interval == "5m":
                return asset
        return None

    def validate_live_credentials(self) -> None:
        if not self.live:
            return
        # L2 API credentials authenticate REST calls; every order still
        # has to be signed by the wallet, so the private key is mandatory.
        if not self.credentials.wallet_private_key:
            raise ConfigError(
                "live mode needs WALLET_PRIVATE_KEY in .env (order signing); "
                "API_KEY/SECRET/PASSPHRASE alone cannot sign orders"
            )


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def _load_env_files() -> None:
    """Load .env.docker, then .env, letting the real process env win.

    ``python-dotenv``'s ``override=False`` cannot express the three-level
    priority (process env > .env > .env.docker), so merge the files first
    and only fill keys that the process did not already define.
    """
    merged: dict[str, str] = {}
    for path in (ROOT / ".env.docker", ROOT / ".env"):
        if not path.exists():
            continue
        for key, value in dotenv_values(path).items():
            if value is not None:
                merged[str(key)] = str(value)
    for key, value in merged.items():
        os.environ.setdefault(key, value)


def load_settings(config_path: str | Path | None = None) -> Settings:
    """Load config.toml + .env + process environment and validate."""
    _load_env_files()

    path = Path(config_path) if config_path else (ROOT / "config.toml")
    raw: dict[str, Any] = {}
    if path.exists():
        with path.open("rb") as fh:
            raw = tomllib.load(fh)

    general_raw = _section(raw, "GENERAL")
    feed_raw = _section(raw, "FEED")
    exec_raw = _section(raw, "EXECUTION")
    storage_raw = _section(raw, "STORAGE")
    web_raw = _section(raw, "WEB")
    log_raw = _section(raw, "LOG")

    general = GeneralConfig(
        tick_s=_as_float(_get(general_raw, "TICK_S", 0.25), 0.25),
    )
    intervals = _load_intervals(raw)

    feed = FeedConfig(
        use_ws=_as_bool(_get(feed_raw, "USE_WS", True), True),
        ws_shards=_env_int(
            "WS_SHARDS",
            int(_as_float(_get(feed_raw, "WS_SHARDS", 7), 7)),
        ),
        book_poll_s=_env_float(
            "BOOK_POLL_S",
            _as_float(_get(feed_raw, "BOOK_POLL_S", 3.0), 3.0),
        ),
        rest_midpoint_poll_s=_as_float(
            _get(feed_raw, "REST_MIDPOINT_POLL_S", 2.0), 2.0
        ),
        market_discovery_s=_as_float(
            _get(feed_raw, "MARKET_DISCOVERY_S", 2.0), 2.0
        ),
        pre_discovery_lead_s=_as_float(
            _get(feed_raw, "PRE_DISCOVERY_LEAD_S", 60.0), 60.0
        ),
        stale_price_max_age_s=_as_float(
            _get(feed_raw, "STALE_PRICE_MAX_AGE_S", 5.0), 5.0
        ),
        exit_stale_price_max_age_s=_as_float(
            _get(feed_raw, "EXIT_STALE_PRICE_MAX_AGE_S", 15.0), 15.0
        ),
        request_timeout_s=_as_float(
            _get(feed_raw, "REQUEST_TIMEOUT_S", 5.0), 5.0
        ),
    )

    execution = ExecutionConfig(
        paper_trading=_env_bool(
            "PAPER_TRADING", _as_bool(_get(exec_raw, "PAPER_TRADING", True), True)
        ),
        live_enabled=_env_bool(
            "LIVE_ENABLED", _as_bool(_get(exec_raw, "LIVE_ENABLED", False), False)
        ),
        stake_usd=_env_float(
            "STAKE_USD", _as_float(_get(exec_raw, "STAKE_USD", 2.0), 2.0)
        ),
        taker_fee_rate=_env_float(
            "TAKER_FEE_RATE",
            _as_float(_get(exec_raw, "TAKER_FEE_RATE", 0.01), 0.01),
        ),
        include_fees=_env_bool(
            "INCLUDE_FEES", _as_bool(_get(exec_raw, "INCLUDE_FEES", True), True)
        ),
        tp_execution_mode=_env(
            "TP_EXECUTION_MODE",
            str(_get(exec_raw, "TP_EXECUTION_MODE", "observed")),
        ).lower()
        or "observed",
        stop_loss_pct=_env_float(
            "STOP_LOSS_PCT",
            _as_float(_get(exec_raw, "STOP_LOSS_PCT", 0.0), 0.0),
        ),
        require_two_sided_book=_env_bool(
            "REQUIRE_TWO_SIDED_BOOK",
            _as_bool(_get(exec_raw, "REQUIRE_TWO_SIDED_BOOK", True), True),
        ),
        min_entry_bid_depth_usd=_env_float(
            "MIN_ENTRY_BID_DEPTH_USD",
            _as_float(
                _get(exec_raw, "MIN_ENTRY_BID_DEPTH_USD", 5.0), 5.0
            ),
        ),
        max_same_side_positions=_env_int(
            "MAX_SAME_SIDE_POSITIONS",
            int(
                _as_float(
                    _get(exec_raw, "MAX_SAME_SIDE_POSITIONS", 6), 6
                )
            ),
        ),
        max_open_positions=_env_int(
            "MAX_OPEN_POSITIONS",
            int(
                _as_float(
                    _get(exec_raw, "MAX_OPEN_POSITIONS", 6), 6
                )
            ),
        ),
        max_open_notional_usd=_env_float(
            "MAX_OPEN_NOTIONAL_USD",
            _as_float(
                _get(exec_raw, "MAX_OPEN_NOTIONAL_USD", 10.0), 10.0
            ),
        ),
        max_same_side_per_window=_env_int(
            "MAX_SAME_SIDE_PER_WINDOW",
            int(
                _as_float(
                    _get(exec_raw, "MAX_SAME_SIDE_PER_WINDOW", 2), 2
                )
            ),
        ),
        entry_retry_s=_as_float(_get(exec_raw, "ENTRY_RETRY_S", 1.0), 1.0),
        exit_retry_s=_as_float(_get(exec_raw, "EXIT_RETRY_S", 1.0), 1.0),
        slippage_pct=_as_float(_get(exec_raw, "SLIPPAGE_PCT", 2.0), 2.0),
        max_spread_abs=_env_float(
            "MAX_SPREAD_ABS",
            _as_float(_get(exec_raw, "MAX_SPREAD_ABS", 0.0), 0.0),
        ),
        max_daily_loss_usd=_env_float(
            "MAX_DAILY_LOSS_USD",
            _as_float(_get(exec_raw, "MAX_DAILY_LOSS_USD", 40.0), 40.0),
        ),
        max_intraday_drawdown_usd=_env_float(
            "MAX_INTRADAY_DRAWDOWN_USD",
            _as_float(
                _get(exec_raw, "MAX_INTRADAY_DRAWDOWN_USD", 16.0), 16.0
            ),
        ),
        intraday_drawdown_pause_min=_env_float(
            "INTRADAY_DRAWDOWN_PAUSE_MIN",
            _as_float(
                _get(exec_raw, "INTRADAY_DRAWDOWN_PAUSE_MIN", 60.0), 60.0
            ),
        ),
        max_positions_per_window=_env_int(
            "MAX_POSITIONS_PER_WINDOW",
            int(
                _as_float(
                    _get(exec_raw, "MAX_POSITIONS_PER_WINDOW", 3), 3
                )
            ),
        ),
        max_window_notional_usd=_env_float(
            "MAX_WINDOW_NOTIONAL_USD",
            _as_float(
                _get(exec_raw, "MAX_WINDOW_NOTIONAL_USD", 6.0), 6.0
            ),
        ),
        asset_perf_lookback_days=_env_float(
            "ASSET_PERF_LOOKBACK_DAYS",
            _as_float(
                _get(exec_raw, "ASSET_PERF_LOOKBACK_DAYS", 7.0), 7.0
            ),
        ),
        asset_perf_min_pnl_usd=_env_float(
            "ASSET_PERF_MIN_PNL_USD",
            _as_float(
                _get(exec_raw, "ASSET_PERF_MIN_PNL_USD", -3.0), -3.0
            ),
        ),
        asset_perf_stake_mult=_env_float(
            "ASSET_PERF_STAKE_MULT",
            _as_float(
                _get(exec_raw, "ASSET_PERF_STAKE_MULT", 0.5), 0.5
            ),
        ),
        asset_perf_disable_pnl_usd=_env_float(
            "ASSET_PERF_DISABLE_PNL_USD",
            _as_float(
                _get(exec_raw, "ASSET_PERF_DISABLE_PNL_USD", -6.0), -6.0
            ),
        ),
        kill_switch_file=_env(
            "KILL_SWITCH_FILE",
            str(_get(exec_raw, "KILL_SWITCH_FILE", "data/STOP")),
        ),
        pause_new_entries=_env_bool(
            "PAUSE_NEW_ENTRIES",
            _as_bool(_get(exec_raw, "PAUSE_NEW_ENTRIES", False), False),
        ),
        flatten_on_exit=_env_bool(
            "FLATTEN_ON_EXIT",
            _as_bool(_get(exec_raw, "FLATTEN_ON_EXIT", False), False),
        ),
        skip_fee_negative=_env_bool(
            "SKIP_FEE_NEGATIVE",
            _as_bool(_get(exec_raw, "SKIP_FEE_NEGATIVE", False), False),
        ),
    )

    storage = StorageConfig(
        data_dir=_env("DATA_DIR", str(_get(storage_raw, "DATA_DIR", "data")))
    )

    web = _load_web(web_raw)

    log_level = _env(
        "LOG_LEVEL", str(_get(log_raw, "LEVEL", "INFO"))
    ).upper()

    assets = _load_assets(raw, execution, intervals)
    credentials = _load_credentials()

    settings = Settings(
        general=general,
        feed=feed,
        execution=execution,
        storage=storage,
        assets=tuple(assets),
        intervals=intervals,
        credentials=credentials,
        web=web,
        log_level=log_level,
        config_path=str(path),
    )
    validate_settings(settings)
    return settings


def _load_intervals(raw: dict[str, Any]) -> tuple[IntervalConfig, ...]:
    """Load the market timeframes (5m, 15m) and their windows."""
    intervals_raw = _section(raw, "INTERVALS")
    names = [str(k).strip().lower() for k in intervals_raw.keys()]
    if not names:
        names = list(DEFAULT_INTERVALS)
    else:
        for name in DEFAULT_INTERVALS:
            if name not in names:
                names.append(name)

    # TRADE_INTERVALS selects a subset/order, e.g. TRADE_INTERVALS=15m.
    env = _env("TRADE_INTERVALS")
    if env:
        wanted = [x.strip().lower() for x in env.split(",") if x.strip()]
        if not wanted:
            raise ConfigError("TRADE_INTERVALS must not be empty")
        known = set(names) | set(INTERVAL_DEFAULTS)
        unknown = [n for n in wanted if n not in known]
        if unknown:
            raise ConfigError(
                "TRADE_INTERVALS contains unknown interval(s): "
                + ",".join(unknown)
            )
        names = wanted

    out: list[IntervalConfig] = []
    for name in names:
        default_window, default_entry = INTERVAL_DEFAULTS.get(
            name, (300, 60.0)
        )
        table = _section(intervals_raw, name)
        window = int(
            _as_float(
                _get(table, "WINDOW_SEC", default_window), default_window
            )
        )
        entry = _as_float(
            _get(table, "ENTRY_WINDOW_END_S", default_entry), default_entry
        )
        out.append(
            IntervalConfig(
                name=name,
                window_sec=window,
                entry_window_end_s=entry,
            )
        )
    return tuple(out)


def _stake_value(
    table: dict[str, Any], name: str, *, market: str = ""
) -> float | None:
    """STAKE_USD from the market/coin table with env overrides."""
    env_name = f"{name}_{market}_STAKE_USD" if market else f"{name}_STAKE_USD"
    raw_env = _env(env_name)
    if raw_env:
        try:
            return float(raw_env)
        except ValueError as exc:
            raise ConfigError(
                f"{env_name} must be a number, got {raw_env!r}"
            ) from exc
    raw = _get(table, "STAKE_USD", None)
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            f"{name}: STAKE_USD must be a number, got {raw!r}"
        ) from exc


def _load_assets(
    raw: dict[str, Any],
    execution: ExecutionConfig,
    intervals: tuple[IntervalConfig, ...],
) -> list[AssetConfig]:
    """Expand the coin-level [ASSETS.X] tables into one config per market.

    Coin-level values (entry/TP/SL/sides/stake/fee) are defaults for both
    timeframes.  ``[ASSETS.ETH.15M]`` may override any of them for that
    market; the web config page persists the same per-market values.
    """
    assets_raw = _section(raw, "ASSETS")
    names = [str(k).upper() for k in assets_raw.keys()]
    for name in ASSET_DEFAULTS:
        if name not in names:
            names.append(name)

    # TRADE_ASSETS selects and orders the coin universe.
    trade_env = _env("TRADE_ASSETS")
    if trade_env:
        wanted = [x.strip().upper() for x in trade_env.split(",") if x.strip()]
        unknown = [n for n in wanted if n not in names]
        if unknown:
            raise ConfigError(
                f"TRADE_ASSETS contains unknown asset(s): {','.join(unknown)}"
            )
        names = [n for n in wanted if n in names]

    out: list[AssetConfig] = []
    for name in names:
        table = _section(assets_raw, name)
        default_sides, default_entry, default_tp = ASSET_DEFAULTS.get(
            name, ("BOTH", 0.20, 0.10)
        )
        # Fallback when config.toml is absent: the six non-BTC assets are
        # on, BTC stays off (its 5% taker fee is not covered by the
        # 0.25/10% TP over the 6-month recheck).
        default_enabled = name != "BTC"
        sl_default = ASSET_DEFAULT_STOP_LOSS.get(
            name, execution.stop_loss_pct
        )
        fee_default = ASSET_DEFAULT_FEE_RATE.get(
            name, execution.taker_fee_rate
        )

        coin_enabled = _env_bool(
            f"{name}_ENABLED",
            _as_bool(_get(table, "ENABLED", default_enabled), default_enabled),
        )
        coin_sides_raw = _env(
            f"{name}_SIDES", str(_get(table, "SIDES", default_sides))
        )
        coin_entry = _env_float(
            f"{name}_ENTRY_THRESH",
            _as_float(_get(table, "ENTRY_THRESH", default_entry), default_entry),
        )
        coin_tp = _env_float(
            f"{name}_TP_PCT",
            _as_float(_get(table, "TP_PCT", default_tp), default_tp),
        )
        coin_sl = _env_float(
            f"{name}_STOP_LOSS_PCT",
            _as_float(_get(table, "STOP_LOSS_PCT", sl_default), sl_default),
        )
        coin_fee = _env_float(
            f"{name}_TAKER_FEE_RATE",
            _as_float(_get(table, "TAKER_FEE_RATE", fee_default), fee_default),
        )
        coin_stake = _stake_value(table, name)

        # When TRADE_INTERVALS selects a single timeframe, that timeframe is
        # the default for the selected coins; otherwise 5m is on and 15m
        # is opt-in (see INTERVAL_DEFAULT_ENABLED).
        only_interval = len(intervals) == 1
        for interval in intervals:
            sub = _section(table, interval.name)
            mk = interval.key  # "5M" / "15M"
            market = f"{name}-{mk}"
            interval_default = bool(
                coin_enabled
                and (
                    only_interval
                    or INTERVAL_DEFAULT_ENABLED.get(interval.name, False)
                )
            )

            enabled = _env_bool(
                f"{name}_{mk}_ENABLED",
                _as_bool(
                    _get(sub, "ENABLED", interval_default),
                    interval_default,
                ),
            )
            sides_raw = _env(
                f"{name}_{mk}_SIDES",
                str(_get(sub, "SIDES", coin_sides_raw)),
            ).upper()
            sides = _parse_sides(sides_raw)
            entry = _env_float(
                f"{name}_{mk}_ENTRY_THRESH",
                _as_float(_get(sub, "ENTRY_THRESH", coin_entry), coin_entry),
            )
            tp = _env_float(
                f"{name}_{mk}_TP_PCT",
                _as_float(_get(sub, "TP_PCT", coin_tp), coin_tp),
            )
            sl = _env_float(
                f"{name}_{mk}_STOP_LOSS_PCT",
                _as_float(_get(sub, "STOP_LOSS_PCT", coin_sl), coin_sl),
            )
            fee = _env_float(
                f"{name}_{mk}_TAKER_FEE_RATE",
                _as_float(
                    _get(sub, "TAKER_FEE_RATE", coin_fee), coin_fee
                ),
            )
            stake = coin_stake
            if "STAKE_USD" in sub:
                stake = _stake_value(sub, name)
            market_stake = _env(f"{name}_{mk}_STAKE_USD")
            if market_stake:
                try:
                    stake = float(market_stake)
                except ValueError as exc:
                    raise ConfigError(
                        f"{name}_{mk}_STAKE_USD must be a number, "
                        f"got {market_stake!r}"
                    ) from exc

            out.append(
                AssetConfig(
                    asset=name,
                    enabled=enabled,
                    sides=sides,
                    entry_thresh=entry,
                    tp_pct=tp,
                    stake_usd=stake,
                    stop_loss_pct=sl,
                    interval=interval.name,
                    window_sec=int(interval.window_sec),
                    entry_window_end_s=float(interval.entry_window_end_s),
                    taker_fee_rate=fee,
                )
            )

    # Display and task order: every 5m market first, then the other
    # timeframes (15m), preserving the coin order inside each group.
    interval_rank: dict[str, int] = {"5m": 0}
    for idx, interval in enumerate(intervals):
        interval_rank.setdefault(interval.name, idx + 1)
    coin_rank = {name: idx for idx, name in enumerate(names)}
    out.sort(
        key=lambda a: (
            interval_rank.get(a.interval, 99),
            coin_rank.get(a.asset, 99),
        )
    )
    return out


def _parse_sides(raw: str) -> tuple[Side, ...]:
    token = str(raw or "").strip().upper()
    if token in ("BOTH", "ALL", "*"):
        return (Side.UP, Side.DOWN)
    if token in ("UP", "U"):
        return (Side.UP,)
    if token in ("DOWN", "D"):
        return (Side.DOWN,)
    # Allow "UP,DOWN" / "DOWN,UP".
    sides: list[Side] = []
    for part in token.split(","):
        part = part.strip().upper()
        if part in ("UP", "U") and Side.UP not in sides:
            sides.append(Side.UP)
        elif part in ("DOWN", "D") and Side.DOWN not in sides:
            sides.append(Side.DOWN)
    if not sides:
        raise ConfigError(f"invalid SIDES value: {raw!r}")
    return tuple(sides)


def _load_web(web_raw: dict[str, Any]) -> WebConfig:
    port = _env_int(
        "WEB_PORT", int(_as_float(_get(web_raw, "PORT", 8033), 8033))
    )
    cors_raw = _env(
        "WEB_CORS_ORIGINS", str(_get(web_raw, "CORS_ORIGINS", "*"))
    )
    cors = tuple(x.strip() for x in cors_raw.split(",") if x.strip()) or ("*",)
    return WebConfig(
        enabled=_env_bool(
            "WEB_ENABLED", _as_bool(_get(web_raw, "ENABLED", True), True)
        ),
        host=_env("WEB_HOST", str(_get(web_raw, "HOST", "0.0.0.0")))
        or "0.0.0.0",
        port=port,
        username=_env(
            "WEB_USERNAME", str(_get(web_raw, "USERNAME", "admin"))
        )
        or "admin",
        password=_env(
            "WEB_PASSWORD", str(_get(web_raw, "PASSWORD", ""))
        ),
        session_ttl_s=_env_int(
            "WEB_SESSION_TTL_S",
            int(
                _as_float(
                    _get(web_raw, "SESSION_TTL_S", 7 * 24 * 3600),
                    7 * 24 * 3600,
                )
            ),
        ),
        log_level=_env(
            "WEB_LOG_LEVEL", str(_get(web_raw, "LOG_LEVEL", "warning"))
        ).lower()
        or "warning",
        cors_origins=cors,
    )


def _load_credentials() -> Credentials:
    return Credentials(
        api_key=_env("API_KEY"),
        api_secret=_env("API_SECRET"),
        api_passphrase=_env("API_PASSPHRASE"),
        wallet_private_key=_env("WALLET_PRIVATE_KEY"),
        wallet_address=_env("WALLET_ADDRESS"),
        funder_address=_env("FUNDER_ADDRESS"),
        signature_type=_env_int("SIGNATURE_TYPE", 0),
        polymarket_host=_env("POLYMARKET_HOST", "https://clob.polymarket.com")
        or "https://clob.polymarket.com",
        gamma_api=_env("GAMMA_API", "https://gamma-api.polymarket.com")
        or "https://gamma-api.polymarket.com",
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_settings(settings: Settings) -> None:
    g = settings.general
    e = settings.execution

    if not settings.intervals:
        raise ConfigError("no market intervals configured")
    seen_intervals: set[str] = set()
    for interval in settings.intervals:
        if not interval.name:
            raise ConfigError("interval name must not be empty")
        if interval.name in seen_intervals:
            raise ConfigError(f"duplicate interval {interval.name}")
        seen_intervals.add(interval.name)
        if interval.window_sec <= 0:
            raise ConfigError(
                f"{interval.name}: WINDOW_SEC must be > 0"
            )
        if not (0 < interval.entry_window_end_s <= interval.window_sec):
            raise ConfigError(
                f"{interval.name}: ENTRY_WINDOW_END_S must be in "
                "(0, WINDOW_SEC]"
            )
    if not (0.01 <= g.tick_s <= 2.0):
        raise ConfigError("TICK_S must be in [0.01, 2.0]")

    if e.stake_usd <= 0:
        raise ConfigError("STAKE_USD must be > 0")
    if e.taker_fee_rate < 0:
        raise ConfigError("TAKER_FEE_RATE must be >= 0")
    if e.entry_retry_s <= 0 or e.exit_retry_s <= 0:
        raise ConfigError("retry intervals must be > 0")
    if e.slippage_pct < 0:
        raise ConfigError("SLIPPAGE_PCT must be >= 0")
    if e.tp_execution_mode not in ("observed", "market"):
        raise ConfigError(
            "TP_EXECUTION_MODE must be 'observed' or 'market', got "
            f"{e.tp_execution_mode!r}"
        )
    if not (0.0 <= e.stop_loss_pct < 1.0):
        raise ConfigError("STOP_LOSS_PCT must be in [0, 1)")
    if e.min_entry_bid_depth_usd < 0:
        raise ConfigError("MIN_ENTRY_BID_DEPTH_USD must be >= 0")
    if e.max_same_side_positions < 0:
        raise ConfigError("MAX_SAME_SIDE_POSITIONS must be >= 0")
    if e.max_open_positions < 0:
        raise ConfigError("MAX_OPEN_POSITIONS must be >= 0")
    if e.max_open_notional_usd < 0:
        raise ConfigError("MAX_OPEN_NOTIONAL_USD must be >= 0")
    if e.max_same_side_per_window < 0:
        raise ConfigError("MAX_SAME_SIDE_PER_WINDOW must be >= 0")
    if settings.feed.ws_shards < 1:
        raise ConfigError("WS_SHARDS must be >= 1")
    if settings.feed.book_poll_s < 0.5:
        raise ConfigError("BOOK_POLL_S must be >= 0.5")
    if e.max_spread_abs < 0:
        raise ConfigError("MAX_SPREAD_ABS must be >= 0")
    if e.max_daily_loss_usd < 0:
        raise ConfigError("MAX_DAILY_LOSS_USD must be >= 0")
    if e.max_intraday_drawdown_usd < 0:
        raise ConfigError("MAX_INTRADAY_DRAWDOWN_USD must be >= 0")
    if e.intraday_drawdown_pause_min < 0:
        raise ConfigError("INTRADAY_DRAWDOWN_PAUSE_MIN must be >= 0")
    if e.max_positions_per_window < 0:
        raise ConfigError("MAX_POSITIONS_PER_WINDOW must be >= 0")
    if e.max_window_notional_usd < 0:
        raise ConfigError("MAX_WINDOW_NOTIONAL_USD must be >= 0")
    if e.asset_perf_lookback_days < 0:
        raise ConfigError("ASSET_PERF_LOOKBACK_DAYS must be >= 0")
    if not (0.0 <= e.asset_perf_stake_mult <= 1.0):
        raise ConfigError("ASSET_PERF_STAKE_MULT must be in [0, 1]")

    w = settings.web
    if w.enabled:
        if not (1 <= int(w.port) <= 65535):
            raise ConfigError(f"WEB_PORT must be in 1..65535, got {w.port}")
        if not w.host:
            raise ConfigError("WEB_HOST must not be empty")
        if w.session_ttl_s < 60:
            raise ConfigError("WEB_SESSION_TTL_S must be >= 60")

    if not settings.assets:
        raise ConfigError("no assets configured")
    if not any(asset.enabled for asset in settings.assets):
        raise ConfigError("all assets are disabled")
    seen: set[str] = set()
    for asset in settings.assets:
        name = asset.market
        if name in seen:
            raise ConfigError(f"duplicate market {name}")
        seen.add(name)
        if not asset.sides:
            raise ConfigError(f"{name}: SIDES is empty")
        if not (0.0 < asset.entry_thresh < 1.0):
            raise ConfigError(
                f"{name}: ENTRY_THRESH must be in (0, 1), "
                f"got {asset.entry_thresh}"
            )
        if asset.tp_pct <= 0:
            raise ConfigError(f"{name}: TP_PCT must be > 0")
        if asset.entry_thresh * (1.0 + asset.tp_pct) >= 1.0:
            raise ConfigError(
                f"{name}: ENTRY_THRESH * (1 + TP_PCT) must be < 1"
            )
        if asset.stake_usd is not None and asset.stake_usd <= 0:
            raise ConfigError(f"{name}: STAKE_USD must be > 0")
        if not (0.0 <= asset.stop_loss_pct < 1.0):
            raise ConfigError(f"{name}: STOP_LOSS_PCT must be in [0, 1)")
        if not (0.0 <= asset.taker_fee_rate < 1.0):
            raise ConfigError(
                f"{name}: TAKER_FEE_RATE must be in [0, 1)"
            )
        if asset.interval not in seen_intervals:
            raise ConfigError(
                f"{name}: interval {asset.interval!r} is not configured"
            )
        if asset.window_sec <= 0:
            raise ConfigError(f"{name}: WINDOW_SEC must be > 0")
        if not (0 < asset.entry_window_end_s <= asset.window_sec):
            raise ConfigError(
                f"{name}: ENTRY_WINDOW_END_S must be in (0, WINDOW_SEC]"
            )


def fee_negative_plans(settings: Settings) -> list[str]:
    """Return the keys of asset/side plans whose TP does not cover fees."""
    from src.economics import fee_line

    out: list[str] = []
    for plan in settings.all_plans():
        line = fee_line(
            plan,
            include_fees=settings.execution.include_fees,
        )
        if line.net_per_trade <= 0:
            out.append(plan.key)
    return out


def describe(settings: Settings) -> str:
    """A compact multi-line summary used by --check and the startup log."""
    e = settings.execution
    lines = [
        f"mode={settings.mode}  markets={','.join(a.market for a in settings.assets if a.enabled)}",
        "intervals: "
        + "  ".join(
            f"{i.name}={i.window_sec}s entry<={i.entry_window_end_s:g}s"
            for i in settings.intervals
        ),
        f"stake={e.stake_usd:g}U  fee_rate_default={e.taker_fee_rate:g}  "
        f"include_fees={e.include_fees}",
        (
            f"risk: daily_loss<={e.max_daily_loss_usd:g}U  "
            f"drawdown<={e.max_intraday_drawdown_usd:g}U/"
            f"{e.intraday_drawdown_pause_min:g}min  "
            f"open<={e.max_open_positions}pos/"
            f"{e.max_open_notional_usd:g}U (all markets)  "
            f"window<={e.max_positions_per_window}pos/"
            f"{e.max_window_notional_usd:g}U (per market window)  "
            f"asset_perf={e.asset_perf_lookback_days:g}d"
        ),
        (
            f"web=http://{settings.web.host}:{settings.web.port} "
            f"enabled={settings.web.enabled} "
            f"auth={'on' if settings.web.auth_enabled else 'off'}"
        ),
        "exits: TP/SL armed at the fill; no forced time exit; "
        "the remainder settles at the Gamma outcome",
    ]
    for asset in settings.assets:
        sides = "+".join(s.value for s in asset.sides)
        stake = asset.stake_usd or e.stake_usd
        flag = "" if asset.enabled else "  [off]"
        lines.append(
            f"  {asset.market:8} {sides:9} entry<={asset.entry_thresh:.2f}  "
            f"tp={asset.tp_pct*100:.0f}%  "
            f"sl={asset.stop_loss_pct*100:.0f}%  "
            f"fee={asset.taker_fee_rate:g}  "
            f"stake={stake:g}U  window={asset.window_sec}s"
            f" entry<={asset.entry_window_end_s:g}s{flag}"
        )
    return "\n".join(lines)
