"""Fetch the machine-readable cost sources and store them raw (cost_snapshots). Never parses on the way in.

One GET (or a few pages) per source, once every POLL_SECONDS. Each body is cut to the products cost/components.py
reads, the same way providers/oracle.py keeps only GPU parts: the AWS Data Transfer offer file is 1.4 MB and the
Oracle price list holds ~670 unrelated parts. Every fetch is stored, failures too, so a broken source is visible
(GET /v1/cost/sources) and a parser fix re-reads stored history.

Sources (no keys; all public price APIs or files):
    aws_data_transfer   AWS Price List offer file AWSDataTransfer, us-east-1: internet egress tiers, inter-AZ
    aws_s3              AWS Price List offer file AmazonS3, us-east-1: S3 Standard storage tiers, request prices
    azure_bandwidth     Azure Retail Prices API, serviceName 'Bandwidth', eastus: internet egress tiers, inter-AZ
    azure_storage       Azure Retail Prices API, serviceName 'Storage', eastus: Premium SSD v2, Premium Files, Blob
    oracle_price_list   Oracle cetools price list (the same JSON providers/oracle.py reads): egress, block, object
    verda_volume_types  Verda /v1/volume-types: NVMe, HDD and shared NVMe volume prices

Read from stored PROVIDER raw instead (no extra fetch): Vultr plan `bandwidth` and `deploy_ondemand`, Hyperstack's
pricebook (Cloud-SSD, ObjectStorage, PublicIP), Vast per-offer `storage_cost` / `inet_down_cost` / `inet_up_cost`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time as _time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

import httpx

log = logging.getLogger(__name__)

POLL_SECONDS = 24 * 3600  # these lists change monthly at most
TIMEOUT = 120
USER_AGENT = "opengrid-terminal/0.1 (+cost components)"
AZURE_MAX_PAGES = 5

AWS_REGION_NAME = "US East (N. Virginia)"
AWS_DT_URL = "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AWSDataTransfer/current/us-east-1/index.json"
AWS_S3_URL = "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonS3/current/us-east-1/index.json"
AZURE_URL = "https://prices.azure.com/api/retail/prices"
AZURE_REGION = "eastus"
AZURE_BANDWIDTH_FILTER = (f"serviceName eq 'Bandwidth' and armRegionName eq '{AZURE_REGION}' "
                          "and priceType eq 'Consumption'")
AZURE_STORAGE_PRODUCTS = ("Azure Premium SSD v2", "Premium Files", "Blob Storage")
AZURE_STORAGE_FILTER = (f"serviceName eq 'Storage' and armRegionName eq '{AZURE_REGION}' and priceType eq 'Consumption' "
                        "and (" + " or ".join(f"productName eq '{p}'" for p in AZURE_STORAGE_PRODUCTS) + ")")
ORACLE_URL = "https://apexapps.oracle.com/pls/apex/cetools/api/v1/products/?currencyCode=USD"
# Part numbers, from Oracle's price list (displayName in the comment).
ORACLE_PARTS = {
    "B88327": "Outbound Data Transfer - Originating in North America, Europe, and UK",
    "B91961": "Storage - Block Volume - Storage",
    "B91962": "Storage - Block Volume - Performance Units",
    "B91628": "Object Storage - Storage",
}
VERDA_URL = "https://api.verda.com/v1/volume-types"

_AZURE_FIELDS = ("productName", "skuName", "meterName", "serviceName", "armRegionName", "tierMinimumUnits",
                 "retailPrice", "unitPrice", "unitOfMeasure", "currencyCode", "effectiveStartDate", "type", "meterId")


# --------------------------------------------------------------------------- reshapers (pure)

def _aws_offer(body: dict, keep: Callable[[dict], bool]) -> dict:
    """An AWS offer file cut to the products `keep(attributes)` accepts, with their OnDemand terms."""
    if not isinstance(body, dict) or "products" not in body or "terms" not in body:
        raise ValueError("aws offer: no products / terms - reshaped")
    products = {sku: p for sku, p in body["products"].items() if keep(p.get("attributes") or {})}
    if not products:
        raise ValueError("aws offer: no product matched - reshaped")
    on_demand = body["terms"].get("OnDemand") or {}
    return {"offerCode": body.get("offerCode"), "version": body.get("version"),
            "publicationDate": body.get("publicationDate"), "products": products,
            "terms": {"OnDemand": {sku: on_demand[sku] for sku in products if sku in on_demand}}}


def reshape_aws_data_transfer(body: dict) -> dict:
    def keep(a: dict) -> bool:
        if a.get("fromLocation") != AWS_REGION_NAME:
            return False
        return (a.get("toLocation") == "External" and a.get("transferType") == "AWS Outbound") or \
            str(a.get("transferType", "")).startswith("IntraRegion")
    return _aws_offer(body, keep)


def reshape_aws_s3(body: dict) -> dict:
    def keep(a: dict) -> bool:
        if a.get("location") != AWS_REGION_NAME:
            return False
        if a.get("usagetype") == "TimedStorage-ByteHrs":
            return a.get("storageClass") == "General Purpose" and a.get("volumeType") == "Standard"
        return a.get("usagetype") in ("Requests-Tier1", "Requests-Tier2")
    return _aws_offer(body, keep)


def reshape_azure(items: list[dict]) -> dict:
    if not items:
        raise ValueError("azure retail prices: no items - reshaped or filter matched nothing")
    return {"region": AZURE_REGION, "items": [{k: i.get(k) for k in _AZURE_FIELDS} for i in items]}


def reshape_oracle(body: dict) -> dict:
    items = body.get("items") if isinstance(body, dict) else None
    if not isinstance(items, list):
        raise ValueError("oracle price list: no items - reshaped")
    kept = [i for i in items if i.get("partNumber") in ORACLE_PARTS]
    if not kept:
        raise ValueError("oracle price list: none of the cost parts present - reshaped")
    return {"lastUpdated": body.get("lastUpdated"), "items": kept}


def reshape_verda(body) -> list:
    if not isinstance(body, list) or not body:
        raise ValueError("verda volume-types: not a non-empty list - reshaped")
    return body


@dataclass(frozen=True)
class Source:
    key: str
    provider: str
    url: str
    kind: str                      # "aws_offer" | "azure" | "json"
    reshape: Callable
    params: dict | None = None
    note: str = ""


SOURCES: dict[str, Source] = {s.key: s for s in (
    Source("aws_data_transfer", "aws", AWS_DT_URL, "json", reshape_aws_data_transfer,
           note="AWS Price List offer file AWSDataTransfer (us-east-1)"),
    Source("aws_s3", "aws", AWS_S3_URL, "json", reshape_aws_s3, note="AWS Price List offer file AmazonS3 (us-east-1)"),
    Source("azure_bandwidth", "azure", AZURE_URL, "azure", reshape_azure, {"$filter": AZURE_BANDWIDTH_FILTER},
           note="Azure Retail Prices API, Bandwidth, eastus"),
    Source("azure_storage", "azure", AZURE_URL, "azure", reshape_azure, {"$filter": AZURE_STORAGE_FILTER},
           note="Azure Retail Prices API, Storage (Premium SSD v2, Premium Files, Blob), eastus"),
    Source("oracle_price_list", "oracle", ORACLE_URL, "json", reshape_oracle,
           note="Oracle cetools price list (USD), cost parts " + ", ".join(ORACLE_PARTS)),
    Source("verda_volume_types", "verda", VERDA_URL, "json", reshape_verda, note="Verda /v1/volume-types"),
)}


@dataclass
class Fetched:
    source: Source
    fetched_at: datetime
    status_code: int | None
    duration_ms: int | None
    ok: bool
    error: str | None
    payload: dict | list | None


async def fetch(src: Source, client: httpx.AsyncClient | None = None) -> Fetched:
    """One source, recorded whether or not it worked. A body that will not reshape is a failed fetch."""
    own = client is None
    client = client or httpx.AsyncClient(timeout=TIMEOUT, headers={"user-agent": USER_AGENT, "accept": "application/json"},
                                         follow_redirects=True)
    started_wall, started = datetime.now(timezone.utc), _time.perf_counter()
    status = None
    try:
        if src.kind == "azure":
            items, url, params = [], src.url, dict(src.params or {})
            for _ in range(AZURE_MAX_PAGES):
                resp = await client.get(url, params=params)
                status = resp.status_code
                resp.raise_for_status()
                body = resp.json()
                items += body.get("Items") or []
                url, params = body.get("NextPageLink"), None
                if not url:
                    break
            else:
                raise ValueError(f"azure retail prices: more than {AZURE_MAX_PAGES} pages")
            payload = src.reshape(items)
        else:
            resp = await client.get(src.url, params=src.params)
            status = resp.status_code
            resp.raise_for_status()
            payload = src.reshape(resp.json())
    except Exception as exc:  # noqa: BLE001 - a failed fetch is stored, never raised
        return Fetched(src, started_wall, status, int((_time.perf_counter() - started) * 1000), False,
                       f"{type(exc).__name__}: {exc}"[:500], None)
    finally:
        if own:
            await client.aclose()
    return Fetched(src, started_wall, status, int((_time.perf_counter() - started) * 1000), True, None, payload)


def save(f: Fetched) -> dict:
    import normalize
    from store.cost import CostSnapshot

    body = None if f.payload is None else json.dumps(f.payload, sort_keys=True, separators=(",", ":"))
    with normalize.SessionLocal.begin() as s:
        s.add(CostSnapshot(source=f.source.key, provider=f.source.provider, url=f.source.url, fetched_at=f.fetched_at,
                           status_code=f.status_code, duration_ms=f.duration_ms, ok=f.ok, error=f.error,
                           sha256=hashlib.sha256(body.encode()).hexdigest() if body else None, payload=f.payload))
    if not f.ok:
        log.warning("cost: %s fetch failed: %s", f.source.key, f.error)
    return {"source": f.source.key, "ok": f.ok, "status_code": f.status_code, "error": f.error}


async def refresh(client: httpx.AsyncClient | None = None) -> dict:
    """Fetch every source once and store it raw."""
    out = [save(await fetch(src, client)) for src in SOURCES.values()]
    return {"fetches": out, "failed": sum(1 for r in out if not r["ok"])}


def latest(session) -> dict[str, dict]:
    """source key -> {payload, fetched_at} of its newest OK snapshot, plus the newest attempt's state."""
    from sqlalchemy import select

    from store.cost import CostSnapshot

    out: dict[str, dict] = {}
    for key in SOURCES:
        ok = session.execute(select(CostSnapshot).where(CostSnapshot.source == key, CostSnapshot.ok.is_(True))
                             .order_by(CostSnapshot.fetched_at.desc(), CostSnapshot.id.desc()).limit(1)).scalar()
        last = session.execute(select(CostSnapshot).where(CostSnapshot.source == key)
                               .order_by(CostSnapshot.fetched_at.desc(), CostSnapshot.id.desc()).limit(1)).scalar()
        out[key] = {"payload": ok.payload if ok else None, "fetched_at": ok.fetched_at if ok else None,
                    "last_attempt_at": last.fetched_at if last else None, "last_ok": last.ok if last else None,
                    "last_error": last.error if last else None}
    return out


_registered = False


def register() -> None:
    """Idempotent: importing api.cost calls this once. Off when the poller is off."""
    global _registered
    from config import settings
    from jobs import job

    if _registered or not settings.poller_enabled:
        return
    _registered = True
    job("cost_components", every_seconds=POLL_SECONDS, initial_delay_seconds=120)(refresh)
