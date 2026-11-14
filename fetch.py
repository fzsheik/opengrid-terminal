"""Pull raw responses from every configured provider and store them."""

import asyncio
import logging

from sqlalchemy.dialects.postgresql import insert

from config import settings
from db import SessionLocal, init_db
from models import RawResponse
from providers import gcp_auth
from providers import (
    AkashProvider,
    CivoProvider,
    CloreProvider,
    AtlanticNetProvider,
    DeepInfraProvider,
    E2EProvider,
    SimplePodProvider,
    AwsProvider,
    AzureProvider,
    BoostRunProvider,
    CoreWeaveProvider,
    GpuAiProvider,
    HetznerProvider,
    IonosProvider,
    SakuraProvider,
    YottaProvider,
    FireworksProvider,
    GcoreProvider,
    HotAisleProvider,
    HuggingFaceProvider,
    HydraHostProvider,
    JarvislabsProvider,
    TogetherProvider,
    VultrProvider,
    ExoscaleProvider,
    GcpProvider,
    HorizonProvider,
    PhyntecProvider,
    PaperspaceProvider,
    LinodeProvider,
    OvhCloudProvider,
    ScalewayProvider,
    KoyebProvider,
    OracleProvider,
    IbmProvider,
    ArkaneProvider,
    VesslProvider,
    MithrilProvider,
    ModalProvider,
    ReplicateProvider,
    BasetenProvider,
    BeamProvider,
    CrusoeProvider,
    CudoProvider,
    DenvrProvider,
    DigitalOceanProvider,
    ThunderComputeProvider,
    LatitudeProvider,
    VerdaProvider,
    HyperbolicProvider,
    HyperstackProvider,
    LambdaProvider,
    LiumProvider,
    MassedComputeProvider,
    NebiusProvider,
    NosanaProvider,
    Provider,
    RunPodProvider,
    SaladProvider,
    VastProvider,
    VoltageParkProvider,
)
from tables import RawPayload, RawSnapshot
from storage import content_hash, lock_storage

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

    # Pods from REST v2's catalog with a key (it needs one); without a key, or when REST fails, from GraphQL (no key,
    # retired early 2027). Serverless pool prices are GraphQL only (providers/runpod.py).
    fetchers.append(RunPodProvider(api_key=settings.runpod_api_key))

    if settings.digitalocean_api_key:
        fetchers.append(DigitalOceanProvider(settings.digitalocean_api_key))

    if settings.jarvis_api_key:
        fetchers.append(JarvislabsProvider(settings.jarvis_api_key))

    if settings.gcp_api_key:
        # Prices from the Billing Catalog key; a read-only service account adds Compute Engine's zone lists (regions
        # beyond Iowa) and calendar-mode advice (providers/gcp.py). A key that cannot be read is skipped, unnamed.
        sa = None
        if settings.gcp_service_account_json:
            try:
                sa = gcp_auth.load(settings.gcp_service_account_json.get_secret_value())
            except gcp_auth.GcpAuthError as exc:
                log.warning("%s; GCP regions stay Iowa only", exc)
        fetchers.append(GcpProvider(settings.gcp_api_key, service_account=sa, project_id=settings.gcp_project_id))

    # The official inventory API with a key (prices + capacity counts); without one, the public page + Shadeform.
    fetchers.append(MassedComputeProvider(api_key=settings.massedcompute_api_key))

    # Public prices; a key adds capacity (Vast's free/total census, Latitude's own stock, Verda's per-location
    # yes/no). The poller only makes Verda's token call and GETs (providers/verda.py); launching is
    # routing/adapters/verda.py, behind the execution gates (routing/control.py).
    fetchers.append(VastProvider(api_key=settings.vast_api_key))
    fetchers.append(LatitudeProvider(api_key=settings.latitude_api_key))
    fetchers.append(VerdaProvider(client_id=settings.verda_client_id, client_secret=settings.verda_client_secret))
    # Prices via Shadeform; a key pair adds Crusoe's own per-location capacity (signed GETs only, providers/crusoe.py).
    fetchers.append(CrusoeProvider(access_key_id=settings.crusoe_access_key_id, secret_key=settings.crusoe_secret_key))
    # Public prices (its page); a key adds stock from its container API.
    fetchers.append(DeepInfraProvider(api_key=settings.deepinfra_api_key))
    # Public prices (its page); a key adds VM stock counts from its own API (read-only GETs).
    fetchers.append(HotAisleProvider(api_key=settings.hotaisle_api_key, team=settings.hotaisle_team))

    if settings.thunder_api_key:
        fetchers.append(ThunderComputeProvider(settings.thunder_api_key))

    if settings.clore_api_key:
        fetchers.append(CloreProvider(settings.clore_api_key))

    # Alibaba Cloud spot: only with an AccessKey (no public spot price exists).
    if settings.alibaba_access_key_id and settings.alibaba_access_key_secret:
        from providers.alibaba import AlibabaProvider
        fetchers.append(AlibabaProvider(settings.alibaba_access_key_id, settings.alibaba_access_key_secret))

    # Public price file + Spot Advisor feed; an IAM key pair (read-only EC2 market calls) adds spot listings,
    # spot placement scores (capacity tiers) and Capacity Block offerings (providers/aws.py).
    fetchers.append(AwsProvider(access_key_id=settings.aws_access_key_id,
                                secret_access_key=settings.aws_secret_access_key))

    # Public endpoints and pages: no credentials to be missing.
    fetchers.extend(
        cls()
        for cls in (
            CivoProvider,
            AtlanticNetProvider,
            E2EProvider,
            SimplePodProvider,
            HyperbolicProvider,
            VoltageParkProvider,
            LiumProvider,
            NebiusProvider,
            DenvrProvider,
            BoostRunProvider,
            CoreWeaveProvider,
            TogetherProvider,
            VultrProvider,
            AzureProvider,
            HorizonProvider,
            PhyntecProvider,
            PaperspaceProvider,
            LinodeProvider,
            OvhCloudProvider,
            ScalewayProvider,
            KoyebProvider,
            OracleProvider,
            IbmProvider,
            ArkaneProvider,
            VesslProvider,
            MithrilProvider,
            ModalProvider,
            ReplicateProvider,
            BasetenProvider,
            BeamProvider,
            NosanaProvider,
            AkashProvider,
            HydraHostProvider,
            CudoProvider,
            GpuAiProvider,
            ExoscaleProvider,
            HuggingFaceProvider,
            GcoreProvider,
            FireworksProvider,
            IonosProvider,
            HetznerProvider,
            YottaProvider,
            SakuraProvider,
        )
    )
    return fetchers


def save(responses: list[RawResponse]) -> int:
    bodies = {content_hash(r.payload): r.payload for r in responses if r.payload is not None}
    with SessionLocal.begin() as s:
        lock_storage(s)
        if bodies:
            s.execute(insert(RawPayload).values([
                {"sha256": digest, "payload": bodies[digest]} for digest in sorted(bodies)
            ]).on_conflict_do_nothing(index_elements=["sha256"]))
        for r in responses:
            digest = content_hash(r.payload) if r.payload is not None else None
            s.add(
                RawSnapshot(
                    provider=r.provider,
                    endpoint=r.endpoint,
                    method=r.method,
                    fetched_at=r.fetched_at,
                    status_code=r.status_code,
                    duration_ms=r.duration_ms,
                    ok=r.ok,
                    throttled=r.throttled or None,
                    error=r.error,
                    sha256=digest,
                    request=r.request,
                    payload_hash=digest,
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
    import fx
    import normalize

    await fx.refresh()  # converted listings (Scaleway's EUR catalogue) need the day's ECB rate

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
