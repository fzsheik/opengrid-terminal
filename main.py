import base64
import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.orm import Session

import mapping
import market
import normalize
import raw_store
from accounts import security
from config import settings
from db import get_db, init_db
from fetch import fetch_all
from fetch import save as save_raw
from poller import Ingest

import jobs
from api import accounts as accounts_api
from api import events as events_api
from api import families as families_api
from api import indices as indices_api
from api import news as news_api
from api import ops as ops_api
from api import pages as pages_api
from api import routing as routing_api
from api import structure as structure_api

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Deployed with no password would publish prices, raw provider responses and the fetch button.
    if security.deployed() and not settings.app_password:
        raise RuntimeError(f"APP_PASSWORD must be set when deployed ({'; '.join(security.deployment_signals())})")
    init_db()
    ingest = Ingest()
    if settings.poller_enabled:
        ingest.start()
    app.state.ingest = ingest
    if not os.environ.get("OPENGRID_NO_JOBS"):
        jobs.start()
    yield
    await jobs.stop()
    await ingest.stop()


# strict_content_type pinned (it is FastAPI's default today): JSON bodies sent as text/plain or a
# form are refused (422) instead of parsed, so a cross-site <form> cannot forge a JSON request.
app = FastAPI(title="opengrid-terminal", lifespan=lifespan, strict_content_type=True)

def password_ok(header: str) -> bool:
    """True when an Authorization header carries the site's user and password."""
    if not header.lower().startswith("basic "):
        return False
    try:
        user, _, password = base64.b64decode(header[6:]).decode().partition(":")
    except Exception:
        return False
    # Both are compared every time (& not `and`), in constant time, so timing reveals nothing.
    return secrets.compare_digest(user.encode(), settings.app_user.encode()) & secrets.compare_digest(
        password.encode(), (settings.app_password or "").encode()
    )


def _deny(status: int, detail: str, headers: dict | None = None) -> Response:
    return JSONResponse({"detail": detail}, status_code=status, headers=headers)


@app.middleware("http")
async def require_password(request, call_next):
    """Everything sits behind one password, except /health so the host can check the app is up.

    /v1 requests carrying an OpenGrid API key pass through: accounts.auth checks the key.
    With PUBLIC_PAGES set, the read-only public surface (pages, assets, /v1 data reads)
    is open too (rate limited per IP); anything that spends money or reads private data still
    needs a key. Deployed (accounts.security.deployed()) without APP_PASSWORD: closed (503).

    CSRF (methodology/security.md): the site login is AMBIENT (browsers re-send cached basic auth,
    and open dev needs none), so every state-changing request it authenticates must carry
    X-OpenGrid-Request: 1 and, when the browser states it, a same-origin Origin / Sec-Fetch-Site.
    Bearer-key requests are exempt: a cross-site page cannot make a browser attach a key.
    Failed basic-auth logins are throttled per client IP.
    """
    path = request.url.path
    if path == "/health":
        return await call_next(request)
    auth = request.headers.get("authorization", "")
    if path.startswith("/v1/") and auth.lower().startswith("bearer "):
        return await call_next(request)
    ip = security.client_ip(request)
    public = False
    if not settings.app_password:
        if security.deployed():
            return _deny(503, "APP_PASSWORD is not configured on a deployed server")
        if not security.open_dev_host_ok(request):  # DNS rebinding against an open dev server
            return _deny(403, "this development server (no APP_PASSWORD) only answers to localhost or an IP address")
    elif security.is_basic(auth):
        wait = security.login_locked(ip)
        if wait:
            return _deny(429, "too many failed logins from this address; try again later", {"Retry-After": str(wait)})
        if not password_ok(auth):
            security.login_failed(ip)
            if not (settings.public_pages and pages_api.is_public(request)):
                return Response("Authentication required", status_code=401,
                                headers={"WWW-Authenticate": 'Basic realm="OpenGrid"'})
            public = True
    elif settings.public_pages and pages_api.is_public(request):
        public = True
    else:
        return Response("Authentication required", status_code=401, headers={"WWW-Authenticate": 'Basic realm="OpenGrid"'})
    if public:
        if not path.startswith("/static/"):
            d = security.public_allowed(ip)
            if not d.allowed:
                return _deny(429, "rate limit exceeded for anonymous access; sign in or use an API key", d.headers())
        return await call_next(request)
    bad = security.csrf_violation(request)  # operator (site login or open dev) from here on
    if bad:
        return _deny(403, bad)
    return await call_next(request)


@app.middleware("http")
async def security_headers(request, call_next):
    """nosniff / Referrer-Policy / X-Frame-Options on everything; on HTML a CSP that allows only our
    own scripts plus the page's own inline boot script (by hash), no framing (clickjacking)."""
    response = await call_next(request)
    for k, v in security.COMMON_HEADERS.items():
        if k not in response.headers:
            response.headers[k] = v
    if response.headers.get("content-type", "").startswith("text/html") and \
            "content-security-policy" not in response.headers:
        body = b"".join([chunk async for chunk in response.body_iterator])
        headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
        headers["Content-Security-Policy"] = security.csp(path=request.url.path, html=body.decode("utf-8", "replace"))
        return Response(body, status_code=response.status_code, headers=headers)
    return response


WEB = Path(__file__).parent / "web"
app.mount("/static", StaticFiles(directory=WEB), name="static")  # the page's css, js and provider logos


@app.get("/classic", include_in_schema=False)
def classic():
    """The earlier plain page: leaderboard, listings table, change arrows."""
    return FileResponse(WEB / "classic.html")


@app.get("/market", summary="Every canonical GPU: current price, change, sparkline")
def market_overview(hours: float = Query(24, ge=0, le=24 * 90)):
    """`hours=0` means all the history we have."""
    return market.overview(hours)


@app.get("/market/detail", summary="One canonical GPU: every provider's price over time")
def market_detail(gpu: str, hours: float = Query(24, ge=0, le=24 * 90)):
    return market.detail(gpu, hours)


@app.get("/health")
def health(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return {"status": "ok", "db": "up"}


@app.get("/listings")
def listings(q: str | None = None, available: bool = False, limit: int | None = None):
    """Normalized ComputeListing rows, cheapest first."""
    return normalize.listings(q=q, only_available=available, limit=limit)


@app.get("/changes")
def changes(hours: float = Query(24, gt=0, le=24 * 90)):
    """Each listing's per-GPU price now against its price `hours` ago."""
    return normalize.price_changes(hours)


@app.get("/history")
def history(q: str | None = None, limit: int = 200):
    """Recorded changes, newest first. A gap means nothing moved."""
    return normalize.history(q=q, limit=limit)


@app.get("/reference-prices")
def reference_prices(q: str | None = None, limit: int = 300):
    """Priced entries a provider publishes that are not purchasable listings."""
    return normalize.reference_prices(q=q, limit=limit)


@app.get("/polling")
def polling():
    """What is being polled and how often."""
    return {
        "running": app.state.ingest.running,
        "providers": [
            {
                "provider": p.name,
                "interval_seconds": p.polling.interval_seconds,
                "timeout_seconds": p.polling.timeout_seconds,
            }
            for p in app.state.ingest.providers
        ],
    }


@app.get("/mapping")
def provider_mapping():
    """How each provider's API fills in ComputeListing, plus its quirks."""
    return {"providers": mapping.as_dicts(), "coverage_problems": mapping.check_mapping_coverage()}


@app.get("/unmapped")
def unmapped():
    """Raw GPU names with no canonical name yet."""
    return normalize.unmapped_gpus()


@app.get("/raw")
def raw(q: str | None = None, limit: int = 200):
    return raw_store.snapshots(limit=limit, q=q)


@app.get("/raw/summary")
def raw_summary():
    return raw_store.endpoint_summary()


@app.get("/raw/payload")
def raw_payload(provider: str, endpoint: str):
    return raw_store.latest_payload(provider, endpoint)


@app.post("/fetch")
async def do_fetch():
    """Pull every provider, store raw, then normalize."""
    responses = await fetch_all()
    save_raw(responses)
    return {
        "responses": len(responses),
        "failed": sum(1 for r in responses if not r.ok),
        **normalize.refresh(),
    }


for _module in (indices_api, structure_api, families_api, events_api, ops_api, news_api, accounts_api, routing_api):
    app.include_router(_module.router)


@app.post("/normalize")
def do_normalize():
    """Re-run the normalizer over stored raw data."""
    return normalize.refresh()


from api import execution as execution_api  # noqa: E402
from api import partners as partners_api  # noqa: E402
from api import product as product_api  # noqa: E402
from api import reconcile as reconcile_api  # noqa: E402

for _module in (execution_api, partners_api, product_api, reconcile_api):
    app.include_router(_module.router)

# Last: the page router owns "/" and the catch-all page paths.
app.include_router(pages_api.router)

# API-key usage logging + X-RateLimit-* headers (outermost middleware); refuses to start deployed without API_KEY_PEPPER.
accounts_api.install(app)

# Request ids and structured logs (outermost, so every log line of a request carries its id).
import alerts.channels  # noqa: E402,F401  (registers the channel_feeds job: Discord/Slack feeds)
import observability  # noqa: E402

observability.install(app)
