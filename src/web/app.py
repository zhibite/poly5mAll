"""FastAPI backend for the 7-asset 5m bot.

Routes
------
GET  /                  dashboard (HTML)
GET  /trades            trade history + profit stats (HTML)
GET  /config            per-asset strategy config (HTML)
GET  /healthz           liveness (no auth)
GET  /api/status        full runner snapshot
GET  /api/stats         aggregate PnL/win-rate/asset/day stats
GET  /api/trades        filtered, paged trades from trades.jsonl
GET  /api/config        runtime per-asset config + fee report
PUT  /api/config        update per-asset config (persisted)
POST /api/config/reset  reset runtime overrides to config.toml
POST /api/pause         {"paused": true|false, "reason": "..."}
POST /api/flatten       {"confirm": "FLATTEN"} -> market-sell all positions

Set ``WEB_PASSWORD`` to enable HTTP Basic auth.  Empty means open access.
"""
from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import quote

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from src.config import Settings
from src.economics import fee_line
from src.infra.logging import get_logger
from src.runtime_config import RuntimeConfig
from src.store import StateStore
from src.web.auth import SESSION_COOKIE, SessionManager
from src.web.config_page import CONFIG_HTML
from src.web.dashboard import INDEX_HTML
from src.web.stats import compute_stats
from src.web.trades_page import TRADES_HTML
from src.web.ui import render_login_page

log = get_logger("web")


class PauseBody(BaseModel):
    paused: bool
    reason: str = ""


class FlattenBody(BaseModel):
    confirm: str = ""


class LoginBody(BaseModel):
    username: str = ""
    password: str = ""


def _is_https(request: Request) -> bool:
    proto = (
        request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    )
    return request.url.scheme == "https" or proto == "https"


def create_app(
    settings: Settings,
    runner: Any,
    store: StateStore,
    runtime: RuntimeConfig,
) -> FastAPI:
    app = FastAPI(
        title="Poly5m All",
        version="0.1.0",
        description="7-asset 5m rebound strategy backend",
        docs_url="/docs",
        redoc_url=None,
    )
    app.state.settings = settings
    app.state.runner = runner
    app.state.store = store
    app.state.runtime = runtime
    sessions = SessionManager(
        settings.web.username,
        settings.web.password,
        settings.web.session_ttl_s,
    )
    app.state.sessions = sessions

    origins = list(settings.web.cors_origins) or ["*"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "OPTIONS"],
        allow_headers=["*"],
    )

    def require_auth(request: Request):
        session = sessions.validate(request.cookies.get(SESSION_COOKIE))
        if session is not None:
            return session
        if request.url.path.startswith("/api/"):
            raise HTTPException(status_code=401, detail="authentication required")
        next_path = request.url.path
        if request.url.query:
            next_path += "?" + request.url.query
        raise HTTPException(
            status_code=303,
            headers={
                "Location": f"/login?next={quote(next_path, safe='')}"
            },
        )

    # ── auth ───────────────────────────────────────────────────────────
    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        if not sessions.enabled or sessions.validate(
            request.cookies.get(SESSION_COOKIE)
        ):
            return RedirectResponse("/", status_code=303)
        return HTMLResponse(render_login_page())

    @app.get("/api/auth/me")
    async def api_auth_me(request: Request) -> dict[str, Any]:
        session = sessions.validate(request.cookies.get(SESSION_COOKIE))
        return {
            "authenticated": (not sessions.enabled) or session is not None,
            "username": session.username if session else "",
            "auth_enabled": sessions.enabled,
        }

    @app.post("/api/auth/login")
    async def api_auth_login(
        request: Request, body: LoginBody
    ) -> dict[str, Any]:
        if not sessions.enabled:
            return {"ok": True, "auth_enabled": False, "username": ""}
        token = sessions.login(body.username, body.password)
        if token is None:
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        response = JSONResponse(
            {
                "ok": True,
                "auth_enabled": True,
                "username": sessions.username,
            }
        )
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=sessions.ttl_s,
            httponly=True,
            samesite="lax",
            secure=_is_https(request),
            path="/",
        )
        return response

    @app.post("/api/auth/logout")
    async def api_auth_logout(request: Request) -> dict[str, Any]:
        sessions.logout(request.cookies.get(SESSION_COOKIE))
        response = JSONResponse({"ok": True})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    # ── pages ──────────────────────────────────────────────────────────
    @app.get("/", response_class=HTMLResponse)
    async def index(_: None = Depends(require_auth)) -> HTMLResponse:
        return HTMLResponse(INDEX_HTML)

    @app.get("/trades", response_class=HTMLResponse)
    async def trades_page(_: None = Depends(require_auth)) -> HTMLResponse:
        return HTMLResponse(TRADES_HTML)

    @app.get("/config", response_class=HTMLResponse)
    async def config_page(_: None = Depends(require_auth)) -> HTMLResponse:
        return HTMLResponse(CONFIG_HTML)

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        payload = runner.status_payload()
        return {
            "ok": True,
            "mode": settings.mode,
            "port": settings.web.port,
            "uptime_s": payload.get("uptime_s", 0.0),
        }

    # ── status / stats ────────────────────────────────────────────────
    @app.get("/api/status")
    async def api_status(_: None = Depends(require_auth)) -> dict[str, Any]:
        payload = runner.status_payload()
        payload["server"] = {
            "port": settings.web.port,
            "auth": settings.web.auth_enabled,
        }
        return payload

    @app.get("/api/stats")
    async def api_stats(_: None = Depends(require_auth)) -> dict[str, Any]:
        rows = await asyncio.to_thread(store.all_trades)
        stats = await asyncio.to_thread(compute_stats, rows)
        stats["open_positions"] = len(runner.all_positions())
        return stats

    @app.get("/api/trades")
    async def api_trades(
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
        asset: str = Query(""),
        reason: str = Query(""),
        mode: str = Query(""),
        _: None = Depends(require_auth),
    ) -> dict[str, Any]:
        rows, total = await asyncio.to_thread(
            store.query_trades,
            limit=limit,
            offset=offset,
            asset=asset,
            reason=reason,
            mode=mode,
        )
        return {
            "trades": rows,
            "total": total,
            "offset": offset,
            "limit": limit,
        }

    # ── runtime config ────────────────────────────────────────────────
    def _fee_rows() -> list[dict[str, Any]]:
        e = settings.execution
        rows: list[dict[str, Any]] = []
        for asset in runtime.assets():
            for plan in asset.plans(e.stake_usd):
                line = fee_line(
                    plan,
                    include_fees=e.include_fees,
                    fee_rate=e.taker_fee_rate,
                )
                rows.append(
                    {
                        "asset": plan.asset,
                        "side": plan.side.value,
                        "entry_thresh": plan.entry_thresh,
                        "tp_pct": plan.tp_pct,
                        "stake_usd": plan.stake_usd,
                        "net_per_share": round(line.net_per_share, 8),
                        "net_per_trade": round(line.net_per_trade, 6),
                        "entry_fee": round(line.entry_fee_per_share, 8),
                        "exit_fee": round(line.exit_fee_per_share, 8),
                        "breakeven_tp": line.breakeven_tp,
                        "fee_negative": line.fee_negative,
                    }
                )
        return rows

    def _config_payload() -> dict[str, Any]:
        e = settings.execution
        fee_rows = _fee_rows()
        fees_by_asset: dict[str, list[dict[str, Any]]] = {}
        for row in fee_rows:
            fees_by_asset.setdefault(row["asset"], []).append(row)

        assets: list[dict[str, Any]] = []
        for row in runtime.snapshot():
            name = row["asset"]
            fees = fees_by_asset.get(name, [])
            assets.append(
                {
                    **row,
                    "effective_stake_usd": row["stake_usd"] or e.stake_usd,
                    "fees": fees,
                    "fee_negative": any(f["fee_negative"] for f in fees),
                    "net_per_trade": min(
                        (f["net_per_trade"] for f in fees), default=None
                    ),
                }
            )
        return {
            "mode": settings.mode,
            "paper_trading": e.paper_trading,
            "live_enabled": e.live_enabled,
            "stake_usd": e.stake_usd,
            "taker_fee_rate": e.taker_fee_rate,
            "include_fees": e.include_fees,
            "skip_fee_negative": e.skip_fee_negative,
            "max_daily_loss_usd": e.max_daily_loss_usd,
            "timing": {
                "window_sec": settings.general.window_sec,
                "entry_window_end_s": settings.general.entry_window_end_s,
                "tp_window_start_s": settings.general.tp_window_start_s,
                "force_exit_s": settings.general.force_exit_s,
                "force_exit_deadline_s": settings.general.force_exit_deadline_s,
            },
            "assets": assets,
            "fee_lines": fee_rows,
            "version": runtime.version,
            "runtime_path": str(runtime.path) if runtime.path else "",
            "paused": runner.paused,
            "paused_reason": getattr(runner, "pause_reason", ""),
            "web": {
                "enabled": settings.web.enabled,
                "host": settings.web.host,
                "port": settings.web.port,
                "auth": settings.web.auth_enabled,
            },
        }

    @app.get("/api/config")
    async def api_config(_: None = Depends(require_auth)) -> dict[str, Any]:
        return _config_payload()

    @app.put("/api/config")
    async def api_update_config(
        payload: dict[str, Any] = Body(...),
        _: None = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            runtime.update(payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(
                status_code=500, detail=f"could not save runtime config: {exc}"
            ) from exc
        return {"ok": True, **_config_payload()}

    @app.post("/api/config/reset")
    async def api_reset_config(
        _: None = Depends(require_auth),
    ) -> dict[str, Any]:
        runtime.reset()
        return {"ok": True, **_config_payload()}

    # ── controls ──────────────────────────────────────────────────────
    @app.post("/api/pause")
    async def api_pause(
        body: PauseBody,
        _: None = Depends(require_auth),
    ) -> dict[str, Any]:
        runner.set_pause(body.paused, body.reason)
        return {
            "ok": True,
            "paused": runner.paused,
            "reason": getattr(runner, "pause_reason", ""),
        }

    @app.post("/api/flatten")
    async def api_flatten(
        body: FlattenBody,
        _: None = Depends(require_auth),
    ) -> dict[str, Any]:
        if body.confirm != "FLATTEN":
            raise HTTPException(
                status_code=400,
                detail='set confirm="FLATTEN" to market-sell all positions',
            )
        sold = await runner.flatten_open_positions()
        return {
            "ok": True,
            "sold": sold,
            "positions": len(runner.all_positions()),
        }

    return app
