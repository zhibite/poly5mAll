"""Entry point: ``python -m src.main``.

Examples
--------
Paper run (safe default)::

    python -m src.main

Validate config and print the fee economics without connecting::

    python -m src.main --check

Probe the live 5m markets for all seven assets::

    python -m src.main --probe

Run only BTC/ETH for 10 minutes::

    python -m src.main --assets BTC,ETH --duration 600

Live trading requires BOTH ``PAPER_TRADING=false`` and ``LIVE_ENABLED=true``
in .env (or ``--live``) plus valid CLOB credentials.
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import signal
import sys
import time

from src.config import (
    ConfigError,
    Settings,
    describe,
    fee_negative_plans,
    load_settings,
)
from src.economics import fee_table
from src.execution.broker import LiveBroker, PaperBroker
from src.execution.clob_client import CLOBClient
from src.infra.logging import configure_logging, get_logger
from src.infra.timeutil import utc_hms
from src.market.market_ws import MarketWS, PriceCache
from src.market.pm_client import PolymarketClient
from src.market.service import MarketService
from src.runner import MultiAssetRunner
from src.runtime_config import RuntimeConfig
from src.store import StateStore
from src.web.server import WebServer

log = get_logger("main")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Polymarket 7-asset 5m rebound strategy"
    )
    parser.add_argument("--config", default=None, help="path to config.toml")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--paper", action="store_true", help="force paper mode")
    mode.add_argument("--live", action="store_true", help="force live mode")
    parser.add_argument(
        "--assets", default="", help="comma-separated subset, e.g. BTC,ETH"
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=0.0,
        help="stop after this many seconds (0 = run until SIGINT)",
    )
    parser.add_argument(
        "--check", action="store_true", help="validate config and exit"
    )
    parser.add_argument(
        "--probe", action="store_true", help="print live 5m markets and exit"
    )
    parser.add_argument(
        "--flatten", action="store_true", help="sell open positions on shutdown"
    )
    parser.add_argument("--log-level", default="", help="override LOG_LEVEL")
    return parser.parse_args(argv)


def _apply_cli_overrides(
    settings: Settings, args: argparse.Namespace
) -> Settings:
    execution = settings.execution
    if args.live:
        execution = dataclasses.replace(
            execution, paper_trading=False, live_enabled=True
        )
    elif args.paper:
        execution = dataclasses.replace(
            execution, paper_trading=True, live_enabled=False
        )
    if args.flatten:
        execution = dataclasses.replace(execution, flatten_on_exit=True)

    assets = settings.assets
    if args.assets.strip():
        wanted = {
            x.strip().upper() for x in args.assets.split(",") if x.strip()
        }
        known = {a.asset for a in assets}
        missing = wanted - known
        if missing:
            raise ConfigError(
                f"unknown asset(s): {','.join(sorted(missing))}; "
                f"known: {','.join(sorted(known))}"
            )
        assets = tuple(a for a in assets if a.asset in wanted)

    log_level = (args.log_level or settings.log_level or "INFO").upper()
    return dataclasses.replace(
        settings,
        execution=execution,
        assets=assets,
        log_level=log_level,
    )


def _print_config(settings: Settings) -> None:
    print(describe(settings))
    print()
    print(
        fee_table(
            settings.all_plans(),
            include_fees=settings.execution.include_fees,
            fee_rate=settings.execution.taker_fee_rate,
        )
    )
    negative = fee_negative_plans(settings)
    if negative:
        print()
        print(
            "WARNING: these asset/side TP targets do not cover the round-trip "
            f"taker fee: {', '.join(negative)}"
        )
        print(
            "         The doc's backtest excludes fees.  Paper PnL includes "
            "them; set TAKER_FEE_RATE=0 only for doc-parity research."
        )
        if settings.execution.skip_fee_negative:
            print("         SKIP_FEE_NEGATIVE=true — those sides will not trade.")
    print()


async def _probe(settings: Settings) -> int:
    pm = PolymarketClient(
        gamma_api=settings.credentials.gamma_api,
        polymarket_host=settings.credentials.polymarket_host,
        timeout_s=settings.feed.request_timeout_s,
    )
    try:
        # Probe every configured asset, including disabled ones: the Web
        # config page may enable them later, so discovery should be verified.
        names = [a.asset for a in settings.assets]
        print(f"Probing {len(names)} asset(s) at {utc_hms(time.time())} UTC\n")
        for asset in names:
            markets = await pm.get_asset_updown_markets(
                asset, slug_interval="5m", window_sec=300, limit=4
            )
            if not markets:
                print(f"{asset:5} no 5m market returned by Gamma")
                continue
            for market in markets:
                start = int(market.get("_window_start_ts") or 0)
                tokens = market.get("tokens") or []
                up = str(tokens[0].get("token_id", "")) if tokens else ""
                down = str(tokens[1].get("token_id", "")) if tokens else ""
                up_mid = await pm.get_midpoint(up) if up else 0.0
                down_mid = await pm.get_midpoint(down) if down else 0.0
                print(
                    f"{asset:5} {market.get('_event_slug', ''):32} "
                    f"start={utc_hms(start)} up={up_mid:.3f} down={down_mid:.3f} "
                    f"tok={up[:10]}../{down[:10]}.."
                )
    finally:
        await pm.close()
    return 0


async def _build_broker(settings: Settings, pm, cache: PriceCache):
    e = settings.execution
    if settings.live:
        c = settings.credentials
        clob = CLOBClient(
            host=c.polymarket_host,
            private_key=c.wallet_private_key,
            api_key=c.api_key,
            api_secret=c.api_secret,
            api_passphrase=c.api_passphrase,
            signature_type=c.signature_type,
            funder_address=c.funder_address,
            fee_rate=e.taker_fee_rate,
            include_fees=e.include_fees,
        )
        return LiveBroker(
            clob,
            pm,
            require_two_sided_book=e.require_two_sided_book,
            min_entry_bid_depth_usd=e.min_entry_bid_depth_usd,
        )
    return PaperBroker(
        pm,
        cache,
        fee_rate=e.taker_fee_rate,
        include_fees=e.include_fees,
        require_two_sided_book=e.require_two_sided_book,
        min_entry_bid_depth_usd=e.min_entry_bid_depth_usd,
    )


async def _run(settings: Settings, duration_s: float = 0.0) -> int:
    pm = PolymarketClient(
        gamma_api=settings.credentials.gamma_api,
        polymarket_host=settings.credentials.polymarket_host,
        timeout_s=settings.feed.request_timeout_s,
    )
    cache = PriceCache()
    market_service = MarketService(
        pm,
        window_sec=settings.general.window_sec,
        pre_discovery_lead_s=settings.feed.pre_discovery_lead_s,
    )
    broker = await _build_broker(settings, pm, cache)
    if isinstance(broker, LiveBroker):
        try:
            balance = await broker.collateral()
        except Exception:  # noqa: BLE001
            balance = None
        if balance is None:
            log.warning("could not query collateral balance")
        else:
            log.info("collateral balance={:.2f} USDC", balance)
            if balance < settings.execution.stake_usd:
                log.warning(
                    "collateral {:.2f} USDC is below one stake ({:.2f} U)",
                    balance,
                    settings.execution.stake_usd,
                )
    store = StateStore(settings.data_dir)
    ws = (
        MarketWS(cache)
        if settings.feed.use_ws
        else None
    )
    runtime = RuntimeConfig(
        settings.data_dir / "runtime_config.json", settings.assets
    )
    runner = MultiAssetRunner(
        settings,
        pm,
        cache,
        market_service,
        broker,
        store,
        market_ws=ws,
        runtime=runtime,
    )
    web = (
        WebServer(settings, runner, store, runtime)
        if settings.web.enabled
        else None
    )

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _request_stop(*_: object) -> None:
        loop.call_soon_threadsafe(stop_event.set)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except (NotImplementedError, RuntimeError):
            try:
                signal.signal(sig, _request_stop)
            except (ValueError, OSError):
                pass

    if duration_s > 0:
        loop.call_later(duration_s, stop_event.set)

    try:
        await runner.start()
        if web is not None:
            await web.start()
        log.warning(
            "mode={} live={} assets={} web={} — press Ctrl+C to stop",
            settings.mode,
            settings.live,
            ",".join(runner.assets),
            web.url if web is not None else "off",
        )
        await stop_event.wait()
    finally:
        if web is not None:
            try:
                await web.stop()
            except Exception as exc:  # noqa: BLE001
                log.warning("web shutdown error: {}", exc)
        try:
            await runner.stop()
        finally:
            await pm.close()
        status = runner.status_payload()
        today = status.get("daily", {})
        log.info("runner.summary daily={}", today)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        settings = load_settings(args.config)
        settings = _apply_cli_overrides(settings, args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    _print_config(settings)

    if args.check:
        return 0

    if not args.probe:
        try:
            settings.validate_live_credentials()
        except ConfigError as exc:
            print(f"live mode refused: {exc}", file=sys.stderr)
            return 2

    configure_logging(settings.log_level, str(settings.data_dir))

    if args.probe:
        # Read-only public endpoints: no credentials needed.
        return asyncio.run(_probe(settings))

    if settings.live:
        log.warning(
            "LIVE MODE: real CLOB orders, stake={}U per trade, fee_rate={}",
            settings.execution.stake_usd,
            settings.execution.taker_fee_rate,
        )
        if (
            settings.credentials.signature_type != 0
            and not settings.credentials.funder_address
        ):
            log.warning(
                "SIGNATURE_TYPE={} without FUNDER_ADDRESS; the CLOB may "
                "reject the maker address. Set it to the deposit wallet.",
                settings.credentials.signature_type,
            )
    else:
        log.info(
            "PAPER MODE: simulated fills against the live CLOB book"
        )

    try:
        return asyncio.run(_run(settings, args.duration))
    except KeyboardInterrupt:
        return 0
    except RuntimeError as exc:
        print(f"startup failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
