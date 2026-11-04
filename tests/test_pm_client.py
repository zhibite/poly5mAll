from __future__ import annotations

import pytest

from src.market.pm_client import PolymarketClient


class _Response:
    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class _HTTP:
    is_closed = False

    def __init__(self) -> None:
        self.paths: list[str] = []

    async def get(self, url: str, **kwargs):
        self.paths.append(url)
        if url.endswith("/book"):
            return _Response(
                200,
                {
                    "bids": [{"price": "0.20", "size": "100"}],
                    "asks": [{"price": "0.25", "size": "100"}],
                },
            )
        return _Response(404)


@pytest.mark.asyncio
async def test_order_book_uses_book_endpoint():
    pm = PolymarketClient()
    http = _HTTP()
    pm._http = http  # type: ignore[attr-defined]
    book = await pm.get_order_book("tok")
    assert book is not None
    assert book["asks"][0]["price"] == "0.25"
    assert any(url.endswith("/book") for url in http.paths)
    # /orderbook is not tried when /book succeeds.
    assert not any(url.endswith("/orderbook") for url in http.paths)
