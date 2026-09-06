"""Local web UI: FastAPI, one page, SSE progress, cart on explicit click.

Run with exactly one worker; the lifespan owns the browser profile:

    uvicorn shopping_hunter.web.app:app --host 127.0.0.1 --port 8765 --workers 1

Protections even though it is local-only: loopback bind, Host/Origin checks on every
state-changing request, a per-process CSRF token, single-use server-issued cart tokens.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from ..budgets import RunBudgets
from ..cart import CartService
from ..config import StartupError, check_environment, get_settings
from ..duties import RULES_BY_COUNTRY
from ..fx import FxRates
from ..logging import log_event, setup_logging
from ..models import MARKETPLACES, Depth, HuntRequest, Marketplace
from ..runners import make_runner
from ..runs import RunBusy, RunRegistry

STATIC = Path(__file__).parent / "static"


class AppState:
    settings = None
    session = None
    adapter_for = None
    registry: RunRegistry | None = None
    cart: CartService | None = None
    runner = None
    fx: FxRates | None = None
    cache = None
    csrf: str = ""
    status_cache: tuple[float, list[dict]] | None = None


S = AppState()


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    setup_logging(settings.log_level)
    try:
        check_environment(settings, require_api_key=not settings.fake_llm)
    except StartupError as e:
        raise SystemExit(f"startup error: {e}")
    S.settings = settings
    S.csrf = secrets.token_urlsafe(24)
    S.registry = RunRegistry(settings)
    if settings.fake_browser:
        from ..browser.fake import fake_adapter_factory
        S.adapter_for = fake_adapter_factory
        S.fx = FxRates(None)
    else:
        from ..browser.amazon import make_adapter_factory
        from ..browser.session import BrowserSession
        from ..cache import Cache
        S.session = BrowserSession(settings, on_needs_human=S.registry.note_needs_human)
        await S.session.start()
        S.cache = Cache(settings.data_dir / "cache.sqlite")
        await S.cache.open()
        S.adapter_for = make_adapter_factory(S.session, settings, S.cache)
        S.fx = FxRates(settings.data_dir, settings.fx_cache_ttl_s)
        await S.fx.load()
    S.runner = make_runner(settings, fake=settings.fake_llm)
    S.cart = CartService(S.registry, S.adapter_for, settings)
    log_event("web_started", host=settings.host, port=settings.port, fake_llm=settings.fake_llm, fake_browser=settings.fake_browser)
    try:
        yield
    finally:
        active = S.registry.active if S.registry else None
        if active:
            await S.registry.cancel(active.run_id)
        if S.cache:
            await S.cache.close()
        if S.session:
            await S.session.close()


app = FastAPI(title="Shopping Hunter", lifespan=lifespan, docs_url=None, redoc_url=None)


# --------------------------------------------------------------------------- protections

def _origin_ok(request: Request) -> bool:
    host = (request.headers.get("host") or "").split(":")[0]
    if host not in S.settings.allowed_hosts:
        return False
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin:
        return False
    from urllib.parse import urlparse
    o = urlparse(origin)
    return (o.hostname or "") in S.settings.allowed_hosts


@app.middleware("http")
async def guard(request: Request, call_next):
    host = (request.headers.get("host") or "").split(":")[0]
    if S.settings and host not in S.settings.allowed_hosts:
        return JSONResponse({"error": "bad host"}, status_code=400)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        if not _origin_ok(request):
            return JSONResponse({"error": "cross-origin request rejected"}, status_code=403)
        if request.headers.get("x-csrf-token") != S.csrf:
            return JSONResponse({"error": "missing or invalid CSRF token"}, status_code=403)
        if not (request.headers.get("content-type") or "").startswith("application/json"):
            return JSONResponse({"error": "JSON body required"}, status_code=415)
    return await call_next(request)


# --------------------------------------------------------------------------- pages

@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    html = (STATIC / "index.html").read_text("utf-8")
    cfg = {
        "csrf": S.csrf, "marketplaces": list(MARKETPLACES), "country": S.settings.country,
        "currency": S.settings.currency, "fake_llm": S.settings.fake_llm, "fake_browser": S.settings.fake_browser,
        "model": S.settings.model,
        "budgets": {d: RunBudgets.for_depth(d).__dict__ for d in ("ordinary", "thorough")},
    }
    return html.replace("__CONFIG__", json.dumps(cfg))


# --------------------------------------------------------------------------- hunts

class HuntIn(BaseModel):
    query: str = Field(min_length=2, max_length=200)
    marketplaces: list[Marketplace] = Field(default_factory=lambda: list(MARKETPLACES))
    min_rating: float = Field(default=4.0, ge=0, le=5)
    min_ratings_count: int = Field(default=50, ge=0, le=1_000_000)
    budget_jod: float | None = Field(default=None, ge=0)
    depth: Depth = "ordinary"
    include_other_sellers: bool = False
    allow_used: bool = False


@app.post("/hunts")
async def create_hunt(body: HuntIn) -> dict[str, Any]:
    from decimal import Decimal
    req = HuntRequest(
        query=body.query, marketplaces=body.marketplaces, min_rating=body.min_rating,
        min_ratings_count=body.min_ratings_count,
        budget_jod=Decimal(str(body.budget_jod)) if body.budget_jod is not None else None,
        depth=body.depth, include_other_sellers=body.include_other_sellers,
        allowed_conditions=["new", "renewed", "used"] if body.allow_used else ["new"],
    )
    try:
        rec = await S.registry.start(req, S.adapter_for, S.runner, S.fx, RULES_BY_COUNTRY[S.settings.country])
    except RunBusy as e:
        raise HTTPException(409, str(e))
    return {"run_id": rec.run_id}


@app.get("/hunts/{run_id}")
async def get_hunt(run_id: str) -> dict[str, Any]:
    rec = S.registry.get(run_id)
    if not rec:
        raise HTTPException(404, "unknown run")
    out = rec.public()
    out["result"] = json.loads(rec.result.model_dump_json()) if rec.result else None
    return out


@app.get("/hunts/{run_id}/events")
async def hunt_events(run_id: str, request: Request):
    rec = S.registry.get(run_id)
    if not rec:
        raise HTTPException(404, "unknown run")
    try:
        after = int(request.headers.get("last-event-id") or request.query_params.get("after") or 0)
    except ValueError:
        after = 0

    async def gen():
        yield "retry: 2000\n\n"
        async for ev in S.registry.stream(run_id, after):
            if await request.is_disconnected():
                return
            name = ev.get("event", "message")
            yield f"id: {ev.get('seq', 0)}\nevent: {name}\ndata: {json.dumps(ev, default=str)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/hunts/{run_id}/cancel")
async def cancel_hunt(run_id: str) -> dict[str, Any]:
    ok = await S.registry.cancel(run_id)
    return {"cancelled": ok}


# --------------------------------------------------------------------------- cart

class CartIn(BaseModel):
    offer_token: str = Field(min_length=8, max_length=64)
    idempotency_key: str = Field(min_length=8, max_length=64)
    confirm: bool = False


@app.post("/cart")
async def add_to_cart(body: CartIn) -> dict[str, Any]:
    return await S.cart.add(body.offer_token, body.idempotency_key, body.confirm)


# --------------------------------------------------------------------------- status / login

@app.get("/status")
async def status() -> dict[str, Any]:
    now = time.monotonic()
    if S.status_cache and now - S.status_cache[0] < 60:
        rows = S.status_cache[1]
    else:
        rows = []
        for m in MARKETPLACES:
            try:
                rows.append((await S.adapter_for(m).login_status()).model_dump())
            except Exception as e:  # noqa: BLE001 - a dead browser must not 500 the status card
                log_event("status_failed", marketplace=m, error=str(e)[:200])
                rows.append({"marketplace": m, "logged_in": None, "deliver_to_ok": None,
                             "detail": f"browser unavailable ({type(e).__name__})"})
        S.status_cache = (now, rows)
    active = S.registry.active
    return {"marketplaces": rows, "active_run": active.public() if active else None,
            "fake_llm": S.settings.fake_llm, "fake_browser": S.settings.fake_browser,
            "browser_channel": S.session.channel if S.session else None,
            "browser_relaunches": S.session.relaunches if S.session else 0}


@app.post("/login/{marketplace}")
async def open_login(marketplace: str) -> dict[str, Any]:
    if marketplace not in MARKETPLACES:
        raise HTTPException(404, "unknown marketplace")
    if S.session is None:
        return {"opened": False, "detail": "fixture browser mode; nothing to log into"}
    adapter = S.adapter_for(marketplace)
    try:
        await S.session.open_for_user(adapter.signin_url())
    except Exception as e:  # noqa: BLE001 - report, never 500
        log_event("login_open_failed", marketplace=marketplace, error=str(e)[:200])
        return {"opened": False, "detail": f"Could not open the sign-in page: {type(e).__name__}. "
                                           "Restart the app if the browser window was closed."}
    S.status_cache = None
    return {"opened": True, "detail": "Sign in inside the Shopping Hunter browser window, then set your delivery address."}


def main() -> None:
    import uvicorn
    settings = get_settings()
    uvicorn.run("shopping_hunter.web.app:app", host=settings.host, port=settings.port, workers=1, log_level="warning")


if __name__ == "__main__":
    main()
