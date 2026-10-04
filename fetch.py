"""Pull raw responses from every configured provider and store them."""

import asyncio
import hashlib
import json
import logging

from config import settings
from db import SessionLocal, init_db
from models import RawResponse
from providers import (
    AwsProvider,
    CrusoeProvider,
    DenvrProvider,
    DigitalOceanProvider,
    LatitudeProvider,
    VerdaProvider,
    HyperbolicProvider,
    HyperstackProvider,
    LambdaProvider,
    LiumProvider,
    MassedComputeProvider,
    NebiusProvider,
    Provider,
    RunPodProvider,
    SaladProvider,
    VastProvider,
    VoltageParkProvider,
)
from tables import RawSnapshot

log = logging.getLogger(__name__)


def build_fetchers() -> list[Provider]:
    """Every provider we have credentials for."""
    fetchers: list[Provider] = []
    if settings.salad_api_key and settings.salad_org:
        fetchers.append(SaladProvider(settings.salad_api_key, settings.salad_org))

    key = settings.hyperstack_api_key
    if key and key.startswith("MII"):
        # base64 DER always starts "MII": a private key body, never sent anywhere.
        log.warning("HYPERSTACK_API_KEY looks like a private key; skipping hyperstack")
    elif key:
        fetchers.append(HyperstackProvider(key))

    if settings.lambda_api_key:
        fetchers.append(LambdaProvider(settings.lambda_api_key))

    if settings.runpod_api_key:
        fetchers.append(RunPodProvider(settings.runpod_api_key))

    if settings.digitalocean_api_key:
        fetchers.append(DigitalOceanProvider(settings.digitalocean_api_key))

    # Public endpoints and pages: no credentials to be missing.
    fetchers.extend(
        cls()
        for cls in (
            HyperbolicProvider,
            VoltageParkProvider,
            LiumProvider,
            VastProvider,
            NebiusProvider,
            MassedComputeProvider,
            AwsProvider,
            VerdaProvider,
            CrusoeProvider,
            LatitudeProvider,
            DenvrProvider,
        )
    )
    return fetchers


def save(responses: list[RawResponse]) -> int:
    with SessionLocal.begin() as s:
        for r in responses:
            digest = None
            if r.payload is not None:
                body = json.dumps(r.payload, sort_keys=True, separators=(",", ":"), default=str)
                digest = hashlib.sha256(body.encode()).hexdigest()
            s.add(
                RawSnapshot(
                    provider=r.provider,
                    endpoint=r.endpoint,
                    method=r.method,
                    fetched_at=r.fetched_at,
                    status_code=r.status_code,
                    duration_ms=r.duration_ms,
                    ok=r.ok,
                    error=r.error,
                    sha256=digest,
                    request=r.request,
                    payload=r.payload,
                )
            )
    return len(responses)


async def fetch_all() -> list[RawResponse]:
    fetchers = build_fetchers()
    try:
        results = await asyncio.gather(
            *(f.fetch() for f in fetchers), return_exceptions=True
        )
    finally:
        for f in fetchers:
            await f.aclose()

    responses: list[RawResponse] = []
    for fetcher, result in zip(fetchers, results):
        if isinstance(result, BaseException):
            log.error("%s: fetch failed: %s", fetcher.name, result)
            continue
        responses.extend(result)
    return responses


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    init_db()
    responses = await fetch_all()
    save(responses)
    import normalize

    result = normalize.refresh()
    by_provider: dict[str, list[RawResponse]] = {}
    for r in responses:
        by_provider.setdefault(r.provider, []).append(r)
    for provider, rows in by_provider.items():
        failed = [r for r in rows if not r.ok]
        print(f"{provider}: {len(rows)} responses, {len(failed)} failed")
        for r in rows:
            if r.endpoint.count("/") <= 4 or not r.ok:
                print(f"   {r.status_code} {r.duration_ms:>5}ms  {r.endpoint}")
    print(
        f"normalized: {result['listings']} listings, "
        f"{result['observations']} changes recorded, "
        f"{result['reference_prices']} reference prices"
    )


if __name__ == "__main__":
    asyncio.run(main())
