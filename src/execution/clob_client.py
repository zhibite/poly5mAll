"""Minimal async wrapper around ``py-clob-client-v2`` for live FAK orders.

This is a trimmed version of the CLOB client used by ``02.poly5m15m``:
only the paths strategy 1 needs are kept.

    BUY   -> FAK market order sized in USDC
    SELL  -> FAK market order sized in shares
    read  -> conditional token balance (restart reconciliation)

Both directions are takers, so the official fee applies:
``shares * 0.07 * p * (1 - p)``.
"""
from __future__ import annotations

import asyncio
import time
from decimal import ROUND_DOWN, Decimal
from typing import Any, Callable, Optional

from src.economics import taker_fee
from src.infra.logging import get_logger
from src.models import Fill

log = get_logger("clob")

try:  # pragma: no cover - import failure is environment-specific
    from py_clob_client_v2 import (
        ClobClient,
        MarketOrderArgs,
        OrderType,
        Side as ClobSide,
    )
    from py_clob_client_v2.clob_types import ApiCreds

    _CLOB_IMPORT_ERROR = ""
except Exception as exc:  # noqa: BLE001
    ClobClient = None  # type: ignore[assignment]
    MarketOrderArgs = None  # type: ignore[assignment]
    OrderType = None  # type: ignore[assignment]
    ClobSide = None  # type: ignore[assignment]
    ApiCreds = None  # type: ignore[assignment]
    _CLOB_IMPORT_ERROR = str(exc)

_CLOB_BASE_UNITS = 1_000_000


def _round_down(value: float, tick: float) -> float:
    if tick <= 0:
        return float(value)
    tick_d = Decimal(str(tick))
    return float(
        Decimal(str(value)).quantize(tick_d, rounding=ROUND_DOWN)
    )


class CLOBClient:
    def __init__(
        self,
        host: str = "https://clob.polymarket.com",
        private_key: str = "",
        api_key: str = "",
        api_secret: str = "",
        api_passphrase: str = "",
        signature_type: int = 0,
        funder_address: str = "",
        fee_rate: float = 0.07,
        include_fees: bool = True,
        fill_callback: Optional[Callable[[Any], None]] = None,
    ) -> None:
        self.host = host.rstrip("/")
        self.private_key = private_key
        self.api_key = api_key
        self.api_secret = api_secret
        self.api_passphrase = api_passphrase
        self.signature_type = int(signature_type)
        self.funder_address = funder_address
        self.fee_rate = float(fee_rate)
        self.include_fees = bool(include_fees)
        self.fill_callback = fill_callback
        self._client: Any = None
        self._resolved_funder = ""
        self._init_error = ""

        if _CLOB_IMPORT_ERROR:
            self._init_error = f"py-clob-client-v2 import failed: {_CLOB_IMPORT_ERROR}"
            log.error("clob.import_failed error={}", _CLOB_IMPORT_ERROR)
            return
        self._init_client()

    # ------------------------------------------------------------------
    # Init / status
    # ------------------------------------------------------------------
    def _init_client(self) -> None:
        if not (self.api_key and self.api_secret and self.api_passphrase):
            if not self.private_key:
                self._init_error = (
                    "missing credentials: set API_KEY/SECRET/PASSPHRASE or "
                    "WALLET_PRIVATE_KEY"
                )
                log.error("clob.no_credentials")
                return
            from src.execution.clob_l1_auth import (
                ClobL1Error,
                create_or_derive_clob_api_creds,
            )

            try:
                creds = create_or_derive_clob_api_creds(
                    self.host, self.private_key
                )
                self.api_key = creds.api_key
                self.api_secret = creds.api_secret
                self.api_passphrase = creds.api_passphrase
                log.info("clob.l2_creds_derived")
            except ClobL1Error as exc:
                self._init_error = str(exc)
                log.error("clob.l1_auth_failed error={}", exc)
                return

        try:
            eoa = ""
            if self.private_key:
                try:
                    from eth_account import Account

                    eoa = Account.from_key(self.private_key).address
                except Exception:  # noqa: BLE001
                    eoa = ""
            funder = self.funder_address or eoa
            self._resolved_funder = funder
            creds = ApiCreds(
                api_key=self.api_key,
                api_secret=self.api_secret,
                api_passphrase=self.api_passphrase,
            )
            self._client = ClobClient(
                host=self.host,
                chain_id=137,
                key=self.private_key,
                creds=creds,
                signature_type=self.signature_type,
                funder=funder,
            )
            log.info(
                "clob.ready host={} sig_type={} funder={}",
                self.host,
                self.signature_type,
                funder or "(empty)",
            )
        except Exception as exc:  # noqa: BLE001
            self._init_error = str(exc)
            log.error("clob.init_failed error={}", exc)
            self._client = None

    @property
    def is_configured(self) -> bool:
        return self._client is not None

    @property
    def init_error(self) -> str:
        return self._init_error

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------
    async def place_fak_buy(
        self,
        token_id: str,
        amount_usdc: float,
        limit_price: float,
        tick_size: float = 0.01,
    ):
        """FAK BUY sized in USDC, walking the ask side up to ``limit_price``."""
        if not self._client:
            return None
        price = _round_down(limit_price, tick_size)
        amount = _round_down(amount_usdc, 0.01)
        if price <= 0 or amount <= 0:
            return None
        try:
            args = MarketOrderArgs(
                token_id=token_id,
                amount=float(amount),
                side=ClobSide.BUY,
                price=float(price),
                order_type=OrderType.FAK,
            )
            resp = await self._run(
                self._client.create_and_post_market_order,
                args,
                order_type=OrderType.FAK,
            )
        except Exception as exc:  # noqa: BLE001
            self._log_post_error("buy", token_id, exc)
            return None
        return self._parse_fill(
            token_id, "BUY", resp, price, tick_size
        )

    async def place_fak_sell(
        self,
        token_id: str,
        shares: float,
        limit_price: float,
        tick_size: float = 0.01,
    ):
        """FAK SELL sized in shares, crossing the bid side down to ``limit_price``."""
        if not self._client:
            return None
        price = _round_down(limit_price, tick_size)
        size = _round_down(shares, 0.0001)
        if price <= 0 or size <= 0:
            return None
        try:
            args = MarketOrderArgs(
                token_id=token_id,
                amount=float(size),
                side=ClobSide.SELL,
                price=float(price),
                order_type=OrderType.FAK,
            )
            resp = await self._run(
                self._client.create_and_post_market_order,
                args,
                order_type=OrderType.FAK,
            )
        except Exception as exc:  # noqa: BLE001
            self._log_post_error("sell", token_id, exc)
            return None
        return self._parse_fill(
            token_id, "SELL", resp, price, tick_size
        )

    def _parse_fill(
        self,
        token_id: str,
        side: str,
        resp: Any,
        limit_price: float,
        tick_size: float,
    ):
        if not isinstance(resp, dict):
            return None
        try:
            taking = float(resp.get("takingAmount") or 0)
            making = float(resp.get("makingAmount") or 0)
        except (TypeError, ValueError):
            taking = making = 0.0

        if taking > 0 and making > 0:
            if side == "BUY":
                shares, usdc = taking, making
            else:
                shares, usdc = making, taking
        else:
            shares = usdc = 0.0
            for raw in resp.get("fills") or []:
                try:
                    size = float(raw.get("size", 0))
                    px = float(raw.get("price", limit_price))
                except (TypeError, ValueError):
                    continue
                if size > 0 and px > 0:
                    shares += size
                    usdc += size * px

        if shares > _CLOB_BASE_UNITS:
            shares /= _CLOB_BASE_UNITS
        if usdc > _CLOB_BASE_UNITS:
            usdc /= _CLOB_BASE_UNITS
        if shares <= 0 or usdc <= 0:
            log.warning(
                "clob.{}_no_fill token={} limit={}",
                side.lower(),
                token_id[:12],
                limit_price,
            )
            return None

        avg = usdc / shares
        fee = (
            taker_fee(shares, avg, self.fee_rate)
            if self.include_fees
            else 0.0
        )
        fill = Fill(
            token_id=token_id,
            side=side,
            shares=shares,
            usdc=usdc,
            price=avg,
            fee=fee,
            ts=time.time(),
            order_id=str(
                resp.get("orderID")
                or resp.get("order_id")
                or resp.get("orderId")
                or ""
            ),
            note="live",
        )
        log.info(
            "clob.{}_filled token={} shares={:.4f} usdc={:.4f} avg={:.4f} fee={:.4f}",
            side.lower(),
            token_id[:12],
            shares,
            usdc,
            avg,
            fee,
        )
        if self.fill_callback:
            try:
                self.fill_callback(fill)
            except Exception:  # noqa: BLE001
                pass
        return fill

    @staticmethod
    def _log_post_error(side: str, token_id: str, exc: Exception) -> None:
        msg = str(exc)
        if "no orders found to match with fak order" in msg.lower():
            log.warning(
                "clob.{}_no_match token={}", side, token_id[:12]
            )
        else:
            log.warning(
                "clob.{}_post_error token={} error={}",
                side,
                token_id[:12],
                msg,
            )

    # ------------------------------------------------------------------
    # Balances
    # ------------------------------------------------------------------
    async def get_token_balance(self, token_id: str) -> float | None:
        """Conditional token balance in shares, or None on query failure."""
        if not self._client:
            return None
        try:
            from py_clob_client_v2.clob_types import (
                AssetType,
                BalanceAllowanceParams,
            )

            resp = await self._run(
                self._client.get_balance_allowance,
                BalanceAllowanceParams(
                    asset_type=AssetType.CONDITIONAL, token_id=token_id
                ),
            )
            if not isinstance(resp, dict) or resp.get("balance") is None:
                return None
            return float(resp["balance"]) / _CLOB_BASE_UNITS
        except Exception as exc:  # noqa: BLE001
            log.debug("clob.token_balance_error token={} error={}", token_id[:12], exc)
            return None

    async def get_collateral(self) -> float | None:
        """USDC (pUSD) collateral balance, or None on query failure."""
        if not self._client:
            return None
        try:
            from py_clob_client_v2.clob_types import (
                AssetType,
                BalanceAllowanceParams,
            )

            resp = await self._run(
                self._client.get_balance_allowance,
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL),
            )
            if not isinstance(resp, dict) or resp.get("balance") is None:
                return None
            return float(resp["balance"]) / _CLOB_BASE_UNITS
        except Exception as exc:  # noqa: BLE001
            log.debug("clob.collateral_error error={}", exc)
            return None

    # ------------------------------------------------------------------
    @staticmethod
    async def _run(fn, *args, **kwargs):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))
