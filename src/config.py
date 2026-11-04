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
    "DOGE": ("BOTH", 0.15, 0.25),
    "XRP": ("BOTH", 0.10, 0.25),
    "HYPE": ("BOTH", 0.30, 0.10),
}

# The doc's asset order, used when TRADE_ASSETS is not set.
DEFAULT_ASSET_ORDER = ("BTC", "ETH", "BNB", "SOL", "DOGE", "XRP", "HYPE")


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
class GeneralConfig:
    window_sec: int = 300
    entry_window_end_s: float = 60.0
    tp_window_start_s: float = 60.0
    force_exit_s: float = 240.0
    force_exit_deadline_s: float = 295.0
    tick_s: float = 0.25


@dataclass(frozen=True)
class FeedConfig:
    use_ws: bool = True
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
    taker_fee_rate: float = 0.07
    include_fees: bool = True
    # "observed": trigger on executable bid >= target; FAK sell at target
    #             floor, so the fill is the best bid (captures gaps above
    #             target, never below target).
    # "market":   trigger on observed mid >= target; marketable FAK below
    #             the bid (doc's option B).  Can fill below target.
    tp_execution_mode: str = "observed"
    # Price stop-loss after t=60s: exit when observed mid <= entry * (1-SL).
    # 0 disables it.  The doc's backtest found -20% improves both PnL and DD.
    stop_loss_pct: float = 0.20
    entry_retry_s: float = 1.0
    exit_retry_s: float = 1.0
    slippage_pct: float = 2.0
    max_spread_abs: float = 0.0
    max_daily_loss_usd: float = 0.0
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
    asset: str
    enabled: bool
    sides: tuple[Side, ...]
    entry_thresh: float
    tp_pct: float
    stake_usd: float | None = None
    stop_loss_pct: float = 0.20

    def plans(self, default_stake: float) -> list[SidePlan]:
        stake = float(self.stake_usd or default_stake)
        return [
            SidePlan(
                asset=self.asset,
                side=side,
                entry_thresh=float(self.entry_thresh),
                tp_pct=float(self.tp_pct),
                stake_usd=stake,
                stop_loss_pct=float(self.stop_loss_pct),
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
    assets: tuple[AssetConfig, ...]
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

    def asset(self, name: str) -> AssetConfig | None:
        name = name.upper()
        for asset in self.assets:
            if asset.asset == name:
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
        window_sec=int(_as_float(_get(general_raw, "WINDOW_SEC", 300), 300)),
        entry_window_end_s=_as_float(
            _get(general_raw, "ENTRY_WINDOW_END_S", 60.0), 60.0
        ),
        tp_window_start_s=_as_float(
            _get(general_raw, "TP_WINDOW_START_S", 60.0), 60.0
        ),
        force_exit_s=_as_float(_get(general_raw, "FORCE_EXIT_S", 240.0), 240.0),
        force_exit_deadline_s=_as_float(
            _get(general_raw, "FORCE_EXIT_DEADLINE_S", 295.0), 295.0
        ),
        tick_s=_as_float(_get(general_raw, "TICK_S", 0.25), 0.25),
    )

    feed = FeedConfig(
        use_ws=_as_bool(_get(feed_raw, "USE_WS", True), True),
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
            _as_float(_get(exec_raw, "TAKER_FEE_RATE", 0.07), 0.07),
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
            _as_float(_get(exec_raw, "STOP_LOSS_PCT", 0.20), 0.20),
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
            _as_float(_get(exec_raw, "MAX_DAILY_LOSS_USD", 0.0), 0.0),
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

    assets = _load_assets(raw, execution)
    credentials = _load_credentials()

    settings = Settings(
        general=general,
        feed=feed,
        execution=execution,
        storage=storage,
        assets=tuple(assets),
        credentials=credentials,
        web=web,
        log_level=log_level,
        config_path=str(path),
    )
    validate_settings(settings)
    return settings


def _load_assets(
    raw: dict[str, Any], execution: ExecutionConfig
) -> list[AssetConfig]:
    assets_raw = _section(raw, "ASSETS")
    names = [str(k).upper() for k in assets_raw.keys()]
    for name in ASSET_DEFAULTS:
        if name not in names:
            names.append(name)

    # TRADE_ASSETS selects and orders the universe.
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
        sides_raw = _env(
            f"{name}_SIDES", str(_get(table, "SIDES", default_sides))
        ).upper()
        sides = _parse_sides(sides_raw)
        # Safe default when config.toml is absent: only BTC is on.
        default_enabled = name == "BTC"
        enabled = _env_bool(
            f"{name}_ENABLED",
            _as_bool(_get(table, "ENABLED", default_enabled), default_enabled),
        )
        entry = _env_float(
            f"{name}_ENTRY_THRESH",
            _as_float(_get(table, "ENTRY_THRESH", default_entry), default_entry),
        )
        tp = _env_float(
            f"{name}_TP_PCT",
            _as_float(_get(table, "TP_PCT", default_tp), default_tp),
        )
        stake_raw = _env(f"{name}_STAKE_USD")
        stake = _get(table, "STAKE_USD", None)
        if stake_raw:
            try:
                stake = float(stake_raw)
            except ValueError as exc:
                raise ConfigError(
                    f"{name}_STAKE_USD must be a number, got {stake_raw!r}"
                ) from exc
        if stake is not None:
            stake = float(stake)
        sl = _env_float(
            f"{name}_STOP_LOSS_PCT",
            _as_float(
                _get(table, "STOP_LOSS_PCT", execution.stop_loss_pct),
                execution.stop_loss_pct,
            ),
        )
        out.append(
            AssetConfig(
                asset=name,
                enabled=enabled,
                sides=sides,
                entry_thresh=entry,
                tp_pct=tp,
                stake_usd=stake,
                stop_loss_pct=sl,
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

    if g.window_sec != 300:
        raise ConfigError(
            f"WINDOW_SEC must be 300 for the 5m strategy (got {g.window_sec})"
        )
    if not (0 < g.entry_window_end_s <= g.window_sec):
        raise ConfigError("ENTRY_WINDOW_END_S must be in (0, WINDOW_SEC]")
    if not (g.entry_window_end_s <= g.tp_window_start_s < g.window_sec):
        raise ConfigError("TP_WINDOW_START_S must be in [ENTRY_WINDOW_END_S, WINDOW_SEC)")
    if not (g.tp_window_start_s < g.force_exit_s <= g.force_exit_deadline_s < g.window_sec):
        raise ConfigError(
            "need TP_WINDOW_START_S < FORCE_EXIT_S <= FORCE_EXIT_DEADLINE_S < WINDOW_SEC"
        )
    if g.force_exit_deadline_s >= g.window_sec:
        raise ConfigError("FORCE_EXIT_DEADLINE_S must leave a settlement margin")
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
    if e.max_spread_abs < 0:
        raise ConfigError("MAX_SPREAD_ABS must be >= 0")

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
        if asset.asset in seen:
            raise ConfigError(f"duplicate asset {asset.asset}")
        seen.add(asset.asset)
        if not asset.sides:
            raise ConfigError(f"{asset.asset}: SIDES is empty")
        if not (0.0 < asset.entry_thresh < 1.0):
            raise ConfigError(
                f"{asset.asset}: ENTRY_THRESH must be in (0, 1), "
                f"got {asset.entry_thresh}"
            )
        if asset.tp_pct <= 0:
            raise ConfigError(f"{asset.asset}: TP_PCT must be > 0")
        if asset.entry_thresh * (1.0 + asset.tp_pct) >= 1.0:
            raise ConfigError(
                f"{asset.asset}: ENTRY_THRESH * (1 + TP_PCT) must be < 1"
            )
        if asset.stake_usd is not None and asset.stake_usd <= 0:
            raise ConfigError(f"{asset.asset}: STAKE_USD must be > 0")
        if not (0.0 <= asset.stop_loss_pct < 1.0):
            raise ConfigError(f"{asset.asset}: STOP_LOSS_PCT must be in [0, 1)")


def fee_negative_plans(settings: Settings) -> list[str]:
    """Return the keys of asset/side plans whose TP does not cover fees."""
    from src.economics import fee_line

    out: list[str] = []
    for plan in settings.all_plans():
        line = fee_line(
            plan,
            include_fees=settings.execution.include_fees,
            fee_rate=settings.execution.taker_fee_rate,
        )
        if line.net_per_trade <= 0:
            out.append(plan.key)
    return out


def describe(settings: Settings) -> str:
    """A compact multi-line summary used by --check and the startup log."""
    e = settings.execution
    lines = [
        f"mode={settings.mode}  assets={','.join(a.asset for a in settings.assets if a.enabled)}",
        f"window={settings.general.window_sec}s  entry<= {settings.general.entry_window_end_s:g}s  "
        f"tp=[{settings.general.tp_window_start_s:g},{settings.general.force_exit_s:g}]s  "
        f"force_exit>={settings.general.force_exit_s:g}s",
        f"stake={e.stake_usd:g}U  fee_rate={e.taker_fee_rate:g}  include_fees={e.include_fees}",
        (
            f"web=http://{settings.web.host}:{settings.web.port} "
            f"enabled={settings.web.enabled} "
            f"auth={'on' if settings.web.auth_enabled else 'off'}"
        ),
    ]
    for asset in settings.assets:
        sides = "+".join(s.value for s in asset.sides)
        stake = asset.stake_usd or e.stake_usd
        flag = "" if asset.enabled else "  [off]"
        lines.append(
            f"  {asset.asset:5} {sides:9} entry<={asset.entry_thresh:.2f}  "
            f"tp={asset.tp_pct*100:.0f}%  sl={asset.stop_loss_pct*100:.0f}%  "
            f"stake={stake:g}U{flag}"
        )
    return "\n".join(lines)
