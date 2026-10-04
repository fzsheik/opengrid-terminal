import base64
import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.orm import Session

import mapping
import market
import normalize
import raw_store
from config import settings
from db import get_db, init_db
from fetch import fetch_all
from fetch import save as save_raw
from poller import Ingest

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Deployed with no password would publish prices, raw provider responses and the fetch button.
    if os.environ.get("RAILWAY_ENVIRONMENT") and not settings.app_password:
        raise RuntimeError("APP_PASSWORD must be set when deployed")
    init_db()
    ingest = Ingest()
    ingest.start()
    app.state.ingest = ingest
    yield
    await ingest.stop()


app = FastAPI(title="opengrid-terminal", lifespan=lifespan)

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


@app.middleware("http")
async def require_password(request, call_next):
    """Everything sits behind one password, except /health so the host can check the app is up."""
    if not settings.app_password or request.url.path == "/health":
        return await call_next(request)
    if password_ok(request.headers.get("authorization", "")):
        return await call_next(request)
    return Response("Authentication required", status_code=401, headers={"WWW-Authenticate": 'Basic realm="OpenGrid"'})


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


@app.get("/", include_in_schema=False)
def index():
    """The market web page. It reads the JSON endpoints below."""
    return FileResponse(WEB / "index.html")


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


@app.post("/normalize")
def do_normalize():
    """Re-run the normalizer over stored raw data."""
    return normalize.refresh()
