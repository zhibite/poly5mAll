"""L1 wallet authentication for Polymarket CLOB v2.

Copied from ``02.poly5m15m/src/execution/clob_l1_auth.py`` so a live run can
derive L2 API credentials from WALLET_PRIVATE_KEY when the operator did not
paste API_KEY / API_SECRET / API_PASSPHRASE into .env.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from py_clob_client_v2.signer import Signer
from py_clob_client_v2.signing.eip712 import sign_clob_auth_message


@dataclass
class ClobApiCreds:
    api_key: str
    api_secret: str
    api_passphrase: str

    def to_dict(self) -> dict:
        return {
            "api_key": self.api_key,
            "api_secret": self.api_secret,
            "api_passphrase": self.api_passphrase,
        }


class ClobL1Error(Exception):
    """Error during L1 auth / API key creation."""


def _build_l1_headers(signer: Signer, timestamp: int, nonce: int) -> dict:
    from py_clob_client_v2.headers.headers import (
        POLY_ADDRESS,
        POLY_NONCE,
        POLY_SIGNATURE,
        POLY_TIMESTAMP,
    )

    signature = sign_clob_auth_message(signer, timestamp, nonce)
    return {
        POLY_ADDRESS: signer.address(),
        POLY_SIGNATURE: signature,
        POLY_TIMESTAMP: str(timestamp),
        POLY_NONCE: str(nonce),
    }


def _create_api_key_with_headers(host: str, headers: dict) -> ClobApiCreds:
    from py_clob_client_v2.endpoints import CREATE_API_KEY
    from py_clob_client_v2.http_helpers.helpers import post

    resp = post(f"{host}{CREATE_API_KEY}", headers=headers)
    return ClobApiCreds(
        api_key=resp["apiKey"],
        api_secret=resp["secret"],
        api_passphrase=resp["passphrase"],
    )


def _derive_api_key_with_headers(host: str, headers: dict) -> ClobApiCreds:
    from py_clob_client_v2.endpoints import DERIVE_API_KEY
    from py_clob_client_v2.http_helpers.helpers import get

    resp = get(f"{host}{DERIVE_API_KEY}", headers=headers)
    return ClobApiCreds(
        api_key=resp["apiKey"],
        api_secret=resp["secret"],
        api_passphrase=resp["passphrase"],
    )


def derive_clob_api_creds(
    host: str, private_key: str, chain_id: int = 137
) -> ClobApiCreds:
    try:
        signer = Signer(private_key, chain_id)
        headers = _build_l1_headers(signer, int(time.time()), 0)
        return _derive_api_key_with_headers(host, headers)
    except Exception as exc:  # noqa: BLE001
        raise ClobL1Error(f"derive_clob_api_creds failed: {exc}") from exc


def create_clob_api_creds(
    host: str, private_key: str, chain_id: int = 137
) -> ClobApiCreds:
    try:
        signer = Signer(private_key, chain_id)
        headers = _build_l1_headers(signer, int(time.time()), 0)
        return _create_api_key_with_headers(host, headers)
    except Exception as exc:  # noqa: BLE001
        raise ClobL1Error(f"create_clob_api_creds failed: {exc}") from exc


def create_or_derive_clob_api_creds(
    host: str, private_key: str, chain_id: int = 137
) -> ClobApiCreds:
    """Create new credentials, falling back to deriving existing ones."""
    try:
        return create_clob_api_creds(host, private_key, chain_id)
    except ClobL1Error:
        pass
    try:
        return derive_clob_api_creds(host, private_key, chain_id)
    except ClobL1Error as exc:
        raise ClobL1Error(
            "Both create and derive failed. Ensure the wallet has pUSD "
            f"balance and has logged into Polymarket.com. Last error: {exc}"
        ) from exc
