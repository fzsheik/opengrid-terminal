"""Alibaba Cloud ECS: GPU spot prices per instance type and zone, plus the pay-as-you-go price each one discounts.

Needs an AccessKey (ALIBABA_ACCESS_KEY_ID + ALIBABA_ACCESS_KEY_SECRET) of an INTERNATIONAL-site (alibabacloud.com)
account, so prices come back in USD; a China-site key answers in CNY and those rows are skipped, never converted.
The RAM user needs only ecs:DescribeSpotPriceHistory, ecs:DescribeInstanceTypes and ecs:DescribeAvailableResource
(read-only; nothing here can create or change a resource). Without the key the provider is not built
(fetch.build_fetchers). Researched 2026-11-13: Alibaba publishes no spot price without a key (its price pages and
calculator carry pay-as-you-go and subscription prices only).

ECS RPC API, version 2014-05-26, signature v1 (HMAC-SHA1 over the sorted query; sign() below), GET
https://ecs.<region>.aliyuncs.com/. Every call is stored under "https://ecs.<region>.aliyuncs.com/?Action=<action>"
with its business parameters as `request`: never the key, signature or nonce.

    DescribeInstanceTypes      once per CATALOG_TTL (us-east-1 endpoint; the catalogue is global): every type with a
                               GPU (MinimumGPUAmount=1), paged by NextToken. Read: GPUSpec, GPUAmount,
                               GPUMemorySize, CpuCoreCount, MemorySize. vGPU / sliced types (GPUSpec like "A10*1/3",
                               families sgn*/vgn*) are dropped: a fraction of a card is not a GPU-hour.
    DescribeAvailableResource  per region, once per CATALOG_TTL: the instance types sold as spot (SpotAsPriceGo,
                               PostPaid) in each zone, so prices are asked only for what is for sale there.
    DescribeSpotPriceHistory   per (region, GPU type) on every poll, all zones, Linux, vpc, SpotDuration 1 (the
                               one-hour protected spot the market quotes), over the last WINDOW (Alibaba's own
                               default): Alibaba prices spot hourly, each row stamped with its hour (`Timestamp`), so
                               the window always holds the price in force and the changes before it. Whether a
                               narrower change-log window would return the price in force at its start is not
                               documented, so it is not relied on. Paged by Offset/NextOffset. Stored per (region,
                               type): endpoint "...?Action=DescribeSpotPriceHistory&InstanceType=<type>".

Listings, from the newest price per (type, zone): "<type>:<zone>:spot" (market spot, interruptible, region the zone,
like AWS), and "<type>:<region>" on demand from the same rows' OriginPrice (the pay-as-you-go price Alibaba states
the spot price against; a region whose zones disagree on it is skipped). Earlier prices in the window become history
rows with their own Timestamp (models.PriceChange).
"""

import base64
import hashlib
import hmac
import logging
import re
import time
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx

import canonical
import changed_at
from models import ComputeListing, PriceChange, RawResponse
from providers.base import PollingPolicy, Provider, gather_limited, to_decimal

if TYPE_CHECKING:
    from tables import RawSnapshot

log = logging.getLogger(__name__)

VERSION = "2014-05-26"
# International regions with GPU instance families, -> ISO country.
REGIONS = {"ap-northeast-1": "JP", "ap-southeast-1": "SG", "ap-southeast-3": "MY", "ap-southeast-5": "ID",
           "cn-hongkong": "HK", "us-west-1": "US", "us-east-1": "US", "eu-central-1": "DE", "eu-west-1": "GB",
           "me-east-1": "AE"}
CATALOG_REGION = "us-east-1"
CATALOG_TTL = timedelta(hours=24)
WINDOW = timedelta(hours=3)           # Alibaba's own default window
CONCURRENCY = 2                       # no published QPS; ECS throttles per API per region (Throttling)
MAX_PAGES = 10
_SLICED = re.compile(r"\*\s*\d+\s*/\s*\d+|^ecs\.(?:sgn|vgn)")


def endpoint(region: str, action: str, **extra: str) -> str:
    return f"https://ecs.{region}.aliyuncs.com/?Action={action}" + "".join(f"&{k}={v}" for k, v in extra.items())


INSTANCE_TYPES = endpoint(CATALOG_REGION, "DescribeInstanceTypes")
AVAILABLE = "?Action=DescribeAvailableResource"
SPOT_PRICES = "?Action=DescribeSpotPriceHistory"


def _q(s) -> str:
    return urllib.parse.quote(str(s), safe="-_.~")


def sign(params: dict, secret: str, method: str = "GET") -> str:
    """Signature v1: base64(HMAC-SHA1(secret + "&", METHOD&%2F&percent(sorted canonical query)))."""
    canon = "&".join(f"{_q(k)}={_q(v)}" for k, v in sorted(params.items()))
    to_sign = f"{method}&%2F&{_q(canon)}"
    return base64.b64encode(hmac.new((secret + "&").encode(), to_sign.encode(), hashlib.sha1).digest()).decode()


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def gpu_types(by_endpoint: "dict[str, list[RawSnapshot]]") -> dict[str, dict]:
    """instance type -> {gpus, raw_gpu_name, vcpu, ram_gb} from the newest stored catalogue; sliced types left out."""
    snaps = by_endpoint.get(INSTANCE_TYPES) or []
    body = snaps[0].payload if snaps and isinstance(snaps[0].payload, dict) else {}
    out = {}
    for t in body.get("types") or []:
        itype, spec, n = t.get("InstanceTypeId"), (t.get("GPUSpec") or "").strip(), t.get("GPUAmount")
        if not itype or not spec or not isinstance(n, int) or n < 1 or _SLICED.search(spec) or _SLICED.search(itype):
            continue
        mem = t.get("GPUMemorySize")
        name = f"{spec} {mem:g}GB" if isinstance(mem, (int, float)) and mem > 0 else spec
        out[itype] = {"gpus": n, "raw_gpu_name": name, "vcpu": t.get("CpuCoreCount"), "ram_gb": t.get("MemorySize")}
    return out


class AlibabaProvider(Provider):
    name = "alibaba"
    # Spot is priced hourly; a poll every 15 min reads a new hour's price within 15 min of it being set.
    polling = PollingPolicy(interval_seconds=900, timeout_seconds=600, tier="medium", rate_limit=(5, 1.0))

    def __init__(self, access_key_id: str, access_key_secret: str, client: httpx.AsyncClient | None = None):
        super().__init__(client or httpx.AsyncClient(timeout=30, headers={"user-agent": "opengrid-terminal/0.1"}))
        self._kid, self._secret = access_key_id, access_key_secret
        self._catalog: dict[str, dict] = {}            # instance type -> spec, from the last good catalogue
        self._spot_types: dict[str, set[str]] = {}     # region -> GPU types sold as spot there
        self._catalog_at: datetime | None = None

    async def _rpc(self, region: str, action: str, params: dict, stored_as: str | None = None) -> RawResponse:
        """One signed GET. Stored under `stored_as` (default endpoint(region, action)) with `params` (never the key)
        as request."""
        base = dict(provider=self.name, endpoint=stored_as or endpoint(region, action), method="GET",
                    fetched_at=datetime.now(timezone.utc), request=params)
        q = {"Format": "JSON", "Version": VERSION, "AccessKeyId": self._kid, "SignatureMethod": "HMAC-SHA1",
             "SignatureVersion": "1.0", "SignatureNonce": uuid.uuid4().hex, "Timestamp": _iso(datetime.now(timezone.utc)),
             "Action": action, "RegionId": region, **params}
        q["Signature"] = sign(q, self._secret)
        started = time.perf_counter()
        skipped = await self._before_call(base["endpoint"])
        if skipped is not None:
            return skipped
        try:
            resp = await self._client.get(f"https://ecs.{region}.aliyuncs.com/", params=q, follow_redirects=False)
            body = resp.json()
        except Exception as exc:  # network error or not JSON: never the URL (it carries the key id)
            return RawResponse(**base, ok=False, duration_ms=int((time.perf_counter() - started) * 1000),
                               error=f"{type(exc).__name__}"[:200])
        ms = int((time.perf_counter() - started) * 1000)
        if not resp.is_success or not isinstance(body, dict) or body.get("Code"):
            code = body.get("Code") if isinstance(body, dict) else resp.status_code
            msg = (body.get("Message") if isinstance(body, dict) else "") or ""
            return RawResponse(**base, ok=False, status_code=resp.status_code, duration_ms=ms,
                               throttled=str(code).startswith("Throttling") or resp.status_code == 429,
                               error=f"{code}: {msg}"[:300].replace(self._kid, "<key>"))
        body.pop("RequestId", None)
        return RawResponse(**base, ok=True, status_code=resp.status_code, duration_ms=ms, payload=body)

    async def fetch(self) -> list[RawResponse]:
        now = datetime.now(timezone.utc)
        out: list[RawResponse] = []
        if self._catalog_at is None or now - self._catalog_at >= CATALOG_TTL:
            out += await self._refresh_catalog()
        jobs = [(r, t) for r, types in sorted(self._spot_types.items()) for t in sorted(types) if t in self._catalog]
        out += await gather_limited([lambda j=j: self._spot(*j, now) for j in jobs], limit=CONCURRENCY)
        return out

    async def _refresh_catalog(self) -> list[RawResponse]:
        types, first, token = [], None, None
        for _ in range(MAX_PAGES):
            raw = await self._rpc(CATALOG_REGION, "DescribeInstanceTypes",
                                  {"MinimumGPUAmount": "1", "MaxResults": "100", **({"NextToken": token} if token else {})})
            first = first or raw
            if not raw.ok:
                return [raw]
            types += ((raw.payload.get("InstanceTypes") or {}).get("InstanceType") or [])
            token = raw.payload.get("NextToken")
            if not token:
                break
        keep = ("InstanceTypeId", "InstanceTypeFamily", "GPUSpec", "GPUAmount", "GPUMemorySize", "CpuCoreCount", "MemorySize")
        catalog = first.model_copy(update={"payload": {"types": sorted(({k: t.get(k) for k in keep} for t in types),
                                                                          key=lambda t: str(t.get("InstanceTypeId")))},
                                           "request": {"MinimumGPUAmount": "1"}})
        self._catalog = gpu_types({INSTANCE_TYPES: [catalog]})
        out = [catalog]
        for region in REGIONS:
            raw = await self._rpc(region, "DescribeAvailableResource",
                                  {"DestinationResource": "InstanceType", "InstanceChargeType": "PostPaid",
                                   "SpotStrategy": "SpotAsPriceGo", "IoOptimized": "optimized"})
            out.append(raw)
            if raw.ok:
                zones = ((raw.payload.get("AvailableZones") or {}).get("AvailableZone") or [])
                sold = set()
                for z in zones:
                    for res in ((z.get("AvailableResources") or {}).get("AvailableResource") or []):
                        for v in ((res.get("SupportedResources") or {}).get("SupportedResource") or []):
                            if v.get("Status") == "Available" and v.get("Value") in self._catalog:
                                sold.add(v["Value"])
                self._spot_types[region] = sold
        self._catalog_at = datetime.now(timezone.utc) if self._catalog else None
        return out

    async def _spot(self, region: str, itype: str, now: datetime) -> RawResponse:
        # The window is in `request`; the stored body is the rows, so an unchanged market is stored once.
        params = {"NetworkType": "vpc", "InstanceType": itype, "OSType": "linux", "SpotDuration": "1",
                  "StartTime": _iso(now - WINDOW), "EndTime": _iso(now)}
        stored_as = endpoint(region, "DescribeSpotPriceHistory", InstanceType=itype)
        rows, first, offset = [], None, None
        for _ in range(MAX_PAGES):
            raw = await self._rpc(region, "DescribeSpotPriceHistory",
                                  {**params, **({"Offset": str(offset)} if offset else {})}, stored_as)
            first = first or raw
            if not raw.ok:
                return raw.model_copy(update={"request": params})
            rows += ((raw.payload.get("SpotPrices") or {}).get("SpotPriceType") or [])
            offset = raw.payload.get("NextOffset")
            if not offset:
                break
        return first.model_copy(update={"request": params, "payload": {
            "region": region, "instance_type": itype, "currency": first.payload.get("Currency"),
            "rows": sorted(rows, key=lambda r: (str(r.get("ZoneId")), str(r.get("Timestamp"))))}})

    @staticmethod
    def normalize(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ComputeListing]:
        return spot_listings(by_endpoint)


def spot_listings(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ComputeListing]:
    """Spot per (type, zone) and on-demand per (type, region) from the stored change logs (module docstring)."""
    specs = gpu_types(by_endpoint)
    out = []
    for ep, snaps in sorted(by_endpoint.items()):
        if SPOT_PRICES not in ep:
            continue
        for snap in snaps:
            body = snap.payload if isinstance(snap.payload, dict) else {}
            region, itype = body.get("region"), body.get("instance_type")
            spec = specs.get(itype)
            if spec is None or region not in REGIONS:
                continue
            if (body.get("currency") or "USD") != "USD":
                log.warning("alibaba: %s priced in %s; skipped (no conversion)", region, body.get("currency"))
                continue
            by_zone: dict[str, list[dict]] = {}
            for r in body.get("rows") or []:
                if isinstance(r, dict) and r.get("ZoneId") and r.get("InstanceType") == itype:
                    by_zone.setdefault(r["ZoneId"], []).append(r)
            origin: set[Decimal] = set()
            n = spec["gpus"]
            for zone, rows in sorted(by_zone.items()):
                rows.sort(key=lambda r: str(r.get("Timestamp")))
                last = rows[-1]
                price = to_decimal(last.get("SpotPrice"))
                if price is None or price <= 0:
                    continue
                o = to_decimal(last.get("OriginPrice"))
                if o and o > 0:
                    origin.add(o)
                earlier = [PriceChange(provider_changed_at=t, price_per_gpu_hour=p / n, price_per_instance_hour=p)
                           for r in rows[:-1]
                           for p, t in [(to_decimal(r.get("SpotPrice")), changed_at.parse_time(r.get("Timestamp")))]
                           if p and p > 0 and t]
                since = changed_at.parse_time(last.get("Timestamp"))
                out.append(_listing(itype, spec, f"{itype}:{zone}:spot", zone, region, price, snap.fetched_at,
                                    spot=True, provider_changed_at=since,
                                    changed_at_basis="exact" if since else None, superseded=tuple(earlier)))
            if len(origin) == 1:
                out.append(_listing(itype, spec, f"{itype}:{region}", region, region, origin.pop(), snap.fetched_at))
            elif len(origin) > 1:
                log.warning("alibaba: %s pay-as-you-go priced differently across %s zones; skipped", itype, region)
    return out


def _listing(itype, spec, listing_id, where, region, price, observed_at, spot=False, **changed) -> ComputeListing:
    n = spec["gpus"]
    return ComputeListing(
        **changed, provider="alibaba", sku=itype, listing_id=listing_id, raw_gpu_name=spec["raw_gpu_name"],
        canonical_gpu_name=canonical.canonical_gpu_name(spec["raw_gpu_name"]), gpu_count=n, region=where,
        country=REGIONS.get(region), price_per_gpu_hour=price / n, price_per_instance_hour=price, currency="USD",
        market_type="spot" if spot else "on_demand", provider_tier="hyperscaler", interruptible=spot,
        available=None, capacity=None, capacity_unit=None, vcpu=spec.get("vcpu"),
        ram_gb=float(spec["ram_gb"]) if isinstance(spec.get("ram_gb"), (int, float)) else None, storage_gb=None,
        observed_at=observed_at)
