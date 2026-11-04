from __future__ import annotations

import dataclasses
import time

import pytest
from fastapi.testclient import TestClient

from src.config import load_settings
from src.models import TradeRecord
from src.runtime_config import RuntimeConfig
from src.store import StateStore
from src.web.app import create_app


class FakeRunner:
    def __init__(self) -> None:
        self.paused = False
        self.pause_reason = ""
        self._positions: list = []

    def status_payload(self):
        return {
            "ts": time.time(),
            "iso": "",
            "mode": "paper",
            "uptime_s": 12.5,
            "config_version": 1,
            "paused": self.paused,
            "pause_reason": self.pause_reason,
            "entries_allowed": not self.paused,
            "entries_block_reason": "" if not self.paused else "paused",
            "daily": {},
            "assets": {},
            "market_service": {},
        }

    def set_pause(self, paused: bool, reason: str = "") -> None:
        self.paused = bool(paused)
        self.pause_reason = (reason or "paused") if self.paused else ""

    async def flatten_open_positions(self) -> int:
        return 0

    def all_positions(self):
        return list(self._positions)


def _trade(
    asset: str, side: str, net: float, gross: float, fee: float, ts: float,
    *, observed: float | None = None,
) -> TradeRecord:
    return TradeRecord(
        position_id=f"{asset}-{side}-{int(ts)}",
        asset=asset,
        side=side,
        window_start=int(ts) - 300,
        slug=f"{asset.lower()}-updown-5m-{int(ts) - 300}",
        entry_ts=ts - 60,
        entry_price=0.25,
        shares=40.0,
        cost_usd=10.0,
        entry_fee=fee / 2,
        tp_price=0.275,
        exit_ts=ts,
        exit_price=0.25 + (net + fee) / 40.0,
        exit_usdc=10.0 + gross,
        exit_fee=fee / 2,
        reason="tp",
        pnl_gross=gross,
        pnl_net=net,
        stake_usd=10.0,
        mode="paper",
        tp_observed_price=observed or 0.0,
        tp_observed_bid=observed or 0.0,
        tp_observed_ask=(observed + 0.01) if observed else 0.0,
        tp_observed_source="test" if observed else "",
        tp_observed_ts=ts if observed else 0.0,
    )


@pytest.fixture()
def client(tmp_path):
    settings = load_settings()
    runtime = RuntimeConfig(tmp_path / "runtime_config.json", settings.assets)
    store = StateStore(tmp_path)
    now = time.time()
    store.append_trade(
        _trade("BTC", "DOWN", 1.0, 1.5, 0.5, now, observed=0.29)
    )
    store.append_trade(_trade("ETH", "DOWN", -0.5, -0.3, 0.2, now - 10))
    runner = FakeRunner()
    app = create_app(settings, runner, store, runtime)
    with TestClient(app) as c:
        c.runner = runner  # type: ignore[attr-defined]
        c.runtime = runtime  # type: ignore[attr-defined]
        c.store = store  # type: ignore[attr-defined]
        yield c


def test_pages_and_health(client):
    assert client.get("/healthz").status_code == 200
    for path, needle in (
        ("/", "Poly5m All"),
        ("/trades", "交易记录"),
        ("/config", "策略配置"),
    ):
        r = client.get(path)
        assert r.status_code == 200
        assert needle in r.text
    assert "8033" in client.get("/config").text


def test_config_defaults_and_update(client):
    r = client.get("/api/config")
    assert r.status_code == 200
    data = r.json()
    assert data["version"] == 0  # empty runtime file
    by_name = {a["asset"]: a for a in data["assets"]}
    assert by_name["BTC"]["enabled"] is True
    assert by_name["ETH"]["enabled"] is False
    assert by_name["BTC"]["stop_loss_pct"] == pytest.approx(0.20)
    assert data["web"]["port"] == 8033
    assert any(f["asset"] == "BTC" and f["side"] == "DOWN" for f in data["fee_lines"])

    r = client.put(
        "/api/config",
        json={
            "assets": {
                "ETH": {
                    "enabled": True,
                    "sides": "DOWN",
                    "entry_thresh": 0.2,
                    "tp_pct": 0.3,
                    "stop_loss_pct": 0.15,
                    "stake_usd": 5,
                }
            }
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()
    by_name = {a["asset"]: a for a in data["assets"]}
    assert by_name["ETH"]["enabled"] is True
    assert by_name["ETH"]["entry_thresh"] == pytest.approx(0.2)
    assert by_name["ETH"]["stop_loss_pct"] == pytest.approx(0.15)
    assert by_name["ETH"]["stake_usd"] == pytest.approx(5.0)

    r = client.post("/api/config/reset")
    assert r.status_code == 200
    by_name = {a["asset"]: a for a in r.json()["assets"]}
    assert by_name["ETH"]["enabled"] is False


def test_trades_stats_and_filters(client):
    r = client.get("/api/trades?limit=10")
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 2
    assert len(data["trades"]) == 2

    r = client.get("/api/trades?asset=BTC")
    data = r.json()
    assert data["total"] == 1
    assert data["trades"][0]["asset"] == "BTC"

    r = client.get("/api/stats")
    assert r.status_code == 200
    stats = r.json()
    assert stats["total"]["trades"] == 2
    assert stats["total"]["wins"] == 1
    assert stats["total"]["losses"] == 1
    assert stats["total"]["net"] == pytest.approx(0.5)
    assert "BTC" in stats["by_asset"]
    assert stats["today"]["trades"] == 2
    tp = stats["tp_execution"]
    assert tp["tp_trades"] == 2
    assert tp["with_observation"] == 1
    assert tp["missing_observation"] == 1
    assert tp["observed_above_target"] == 1
    assert tp["actual_above_target"] == 1
    assert tp["avg_observed_edge"] == pytest.approx(0.015)


def test_pause_and_flatten(client):
    r = client.post("/api/pause", json={"paused": True, "reason": "test"})
    assert r.status_code == 200
    assert r.json()["paused"] is True
    assert client.get("/api/status").json()["paused"] is True

    r = client.post("/api/flatten", json={"confirm": "nope"})
    assert r.status_code == 400
    r = client.post("/api/flatten", json={"confirm": "FLATTEN"})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_login_page_when_auth_disabled(tmp_path):
    settings = load_settings()
    runtime = RuntimeConfig(tmp_path / "runtime_config.json", settings.assets)
    store = StateStore(tmp_path)
    app = create_app(settings, FakeRunner(), store, runtime)
    with TestClient(app) as c:
        r = c.get("/login", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/"
        # Open access: API works without a cookie.
        assert c.get("/api/status").status_code == 200


def test_login_session_auth(tmp_path):
    settings = load_settings()
    settings = dataclasses.replace(
        settings,
        web=dataclasses.replace(settings.web, password="secret"),
    )
    runtime = RuntimeConfig(tmp_path / "runtime_config.json", settings.assets)
    store = StateStore(tmp_path)
    app = create_app(settings, FakeRunner(), store, runtime)
    with TestClient(app) as c:
        # API returns JSON 401, not a browser Basic-auth challenge.
        r = c.get("/api/status")
        assert r.status_code == 401
        assert "www-authenticate" not in {k.lower() for k in r.headers}

        # HTML pages redirect to the login page.
        r = c.get("/", follow_redirects=False)
        assert r.status_code == 303
        assert "/login?next=%2F" in r.headers["location"]

        r = c.get("/login")
        assert r.status_code == 200
        assert "登录" in r.text

        assert c.post(
            "/api/auth/login",
            json={"username": "admin", "password": "bad"},
        ).status_code == 401

        r = c.post(
            "/api/auth/login",
            json={"username": "admin", "password": "secret"},
        )
        assert r.status_code == 200
        cookie = r.headers.get("set-cookie", "")
        assert "poly5m_session=" in cookie
        assert "HttpOnly" in cookie
        assert "SameSite=lax" in cookie or "samesite=lax" in cookie.lower()

        assert c.get("/api/status").status_code == 200
        me = c.get("/api/auth/me").json()
        assert me["authenticated"] is True
        assert me["auth_enabled"] is True

        assert c.post("/api/auth/logout").status_code == 200
        assert c.get("/api/status").status_code == 401
