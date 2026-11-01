"""AWS EC2: on-demand list prices for GPU instances (public), plus spot prices and capacity signals with a read-only key.

The public price file lists every EC2 instance type for one region and one
operating system. It carries vCPU, memory and price but NOT the GPU model or
count, so those come from GPU_INSTANCES below, hand-maintained like canonical.py
(cross-checked against DescribeInstanceTypes' gpuInfo on 2026-11-01).
An instance type missing from that table is not guessed: it is skipped and logged.

These are list prices. Large customers rarely pay them, which is why the
provider tier is "hyperscaler".

Spot Instance Advisor (raw only). The same poll stores AWS's public Spot Advisor
feed (SPOT_ADVISOR, the JSON behind aws.amazon.com/ec2/spot/instance-advisor/),
GPU-family instance types only, every region: per type, region and OS an
interruption band `r` (0-4, labelled by the feed's own `ranges`: <5%, 5-10%,
10-15%, 15-20%, >20% of instances interrupted over the trailing month) and `s`,
the average saving of spot over on-demand in percent. Its Last-Modified header
is kept, because the feed is updated rarely. normalize() never reads it, and it is
never shown on public pages or APIs (AWS site terms). Routing has no hook for it:
best execution ranks on-demand, non-interruptible listings only, and provider
reliability is measured only from OpenGrid's own launches (methodology/reliability.md).
spot_advice() reads it for later use. A failed or reshaped feed is stored as a
failed fetch and never touches prices.

With AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY (an IAM user allowed only read-only EC2 Describe/Get market calls;
launching is denied) the same poll adds three SigV4-signed EC2 Query API calls (providers/aws_sigv4.py, POST
form bodies to https://ec2.<region>.amazonaws.com/, API version 2016-11-15). Without the key nothing changes.

    DescribeSpotPriceHistory   every GPU_INSTANCES type, Linux/UNIX, StartTime = EndTime = now, in SPOT_REGIONS,
                               paginated, two regions at a time. AWS returns, per type and AZ, "the last price change
                               before the start time, which is the effective price as of the start time". Stored per
                               region: the GPU rows verbatim (XML -> JSON). Zero GPU rows is a failed fetch. Read by
                               normalize() as spot listings: one per (type, AZ), listing_id "<type>:<az>:spot",
                               region the AZ, market_type spot, interruptible, per-GPU price = spot price / GPUs.
    GetSpotPlacementScores     PLACEMENT_TYPES, one fixed request configuration each (one instance of that type,
                               regional scores over SPOT_REGIONS), refreshed every PLACEMENT_TTL, not every poll: new
                               accounts get a low target-capacity limit and AWS may throttle new configurations per
                               24 h, so the set is small and never changes between polls. Stored per type; read by
                               normalize_capacity() as a tier for the spot listings (sku "<type>:spot", region code;
                               methodology/capacity.md has the 1-10 mapping).
    DescribeCapacityBlockOfferings  CAPACITY_BLOCK_TYPES x 1 instance x 24 h, us-east-1, every CAPACITY_BLOCK_TTL
                               (its API bucket refills at 0.15 requests/s). Stored raw only. An account below the
                               Capacity Block limit gets CapacityBlockDescribeLimitExceeded: that answer is stored as
                               a successful snapshot with status "not_available_for_account" (it is the account's
                               state, not a feed failure, so it never counts against feed health), logged once.

Errors from AWS are stored as code + message with any 12-digit account id or ARN removed; keys never leave the
Authorization header.
"""

import asyncio
import logging
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import httpx

import canonical
from models import CapacityObservation, ComputeListing, RawResponse
from providers import aws_sigv4
from providers.base import PollingPolicy, Provider, find_endpoint, gather_limited, reshaped, to_decimal

if TYPE_CHECKING:
    from tables import RawSnapshot

log = logging.getLogger(__name__)

BASE_URL = "https://b0.p.awsstatic.com"
# us-east-1, Linux, shared tenancy, no extra software.
ENDPOINT = (
    "/pricing/2.0/meteredUnitMaps/ec2/USD/current/ec2-ondemand-without-sec-sel/"
    "US%20East%20%28N.%20Virginia%29/Linux/index.json"
)
REGION_NAME = "US East (N. Virginia)"
REGION = "us-east-1"

# Sizes of the small GPU families: GPUs per size.
_SMALL = {"xlarge": 1, "2xlarge": 1, "4xlarge": 1, "8xlarge": 1, "16xlarge": 1, "12xlarge": 4, "24xlarge": 4, "48xlarge": 8}


def _family(name: str, gpu: str, sizes: dict[str, int]) -> dict[str, tuple[int, str]]:
    return {f"{name}.{size}": (count, gpu) for size, count in sizes.items()}


# instance type -> (GPUs, GPU name as canonical.py knows it)
GPU_INSTANCES: dict[str, tuple[int, str]] = {
    # Training and inference flagships, 8 GPUs per full instance.
    "p3.2xlarge": (1, "Tesla V100-SXM2-16GB"),
    "p3.8xlarge": (4, "Tesla V100-SXM2-16GB"),
    "p3.16xlarge": (8, "Tesla V100-SXM2-16GB"),
    "p4d.24xlarge": (8, "NVIDIA A100-SXM4-40GB"),
    "p4de.24xlarge": (8, "NVIDIA A100-SXM4-80GB"),
    "p5.4xlarge": (1, "NVIDIA H100 80GB HBM3"),
    "p5.48xlarge": (8, "NVIDIA H100 80GB HBM3"),
    "p5e.48xlarge": (8, "NVIDIA H200"),
    "p5en.48xlarge": (8, "NVIDIA H200"),
    "p6-b200.48xlarge": (8, "NVIDIA B200"),
    "p6-b300.48xlarge": (8, "B300-SXM"),
    # Graphics-optimised L4 with more memory per vCPU (DescribeInstanceTypes: 1 x L4).
    "gr6.4xlarge": (1, "NVIDIA L4"),
    "gr6.8xlarge": (1, "NVIDIA L4"),
    # Smaller GPUs; g4dn.metal is the only odd size.
    **_family("g4dn", "NVIDIA T4", {**_SMALL, "24xlarge": 0, "48xlarge": 0}),
    "g4dn.metal": (8, "NVIDIA T4"),
    **_family("g5", "NVIDIA A10G", _SMALL),
    **_family("g6", "NVIDIA L4", _SMALL),
    **_family("g6e", "NVIDIA L40S", _SMALL),
}
GPU_INSTANCES = {k: v for k, v in GPU_INSTANCES.items() if v[0] > 0}

_GIB = re.compile(r"^([\d.]+) GiB$")
_GPU_FAMILY = re.compile(r"^(p\d|g\d|dl\d|trn|inf)")

SPOT_ADVISOR = "https://spot-bid-advisor.s3.amazonaws.com/spot-advisor-data.json"

# --- read-only EC2 market calls (need the key; see the module docstring) ---
EC2_VERSION = "2016-11-15"
# Regions polled for spot prices and scored for placement -> ISO country. Chosen 2026-11-01 from a live
# DescribeSpotPriceHistory sweep of every enabled region: the six with spot prices for H100, H200 and/or B200.
SPOT_REGIONS = {"us-east-1": "US", "us-east-2": "US", "us-west-2": "US", "eu-west-2": "GB", "eu-north-1": "SE",
                "ap-northeast-1": "JP"}
SPOT_PRODUCT = "Linux/UNIX"
SPOT_CONCURRENCY = 2            # regions fetched at once; each region has its own EC2 request bucket anyway
SPOT_MAX_PAGES = 10
# Placement scores: one fixed request configuration per type (1 instance, regional), H100 / H200 / B200 / B300 / A100.
PLACEMENT_TYPES = ("p5.48xlarge", "p5en.48xlarge", "p6-b200.48xlarge", "p6-b300.48xlarge", "p4d.24xlarge")
PLACEMENT_TARGET = 1
PLACEMENT_TTL = timedelta(hours=2)   # < capacity's stale mark (3 polls of 1 h), so rows never age into stale
# AWS: 10 = "highly likely - but not guaranteed - to succeed", 1 = "not likely to succeed at all".
PLACEMENT_TIERS = ((1, 3, "low"), (4, 6, "medium"), (7, 10, "high"))
CAPACITY_BLOCK_TYPES = ("p5.48xlarge", "p5en.48xlarge", "p6-b200.48xlarge")
CAPACITY_BLOCK_REGION = "us-east-1"
CAPACITY_BLOCK_HOURS = 24
CAPACITY_BLOCK_TTL = timedelta(hours=24)
CAPACITY_BLOCK_UNAVAILABLE = "CapacityBlockDescribeLimitExceeded"
RETRY_DELAY = 1.5               # seconds before the one retry of a throttled (RequestLimitExceeded) or 5xx call


def ec2_endpoint(region: str, action: str, **extra: str) -> str:
    """The name a call is stored under: host, action and what tells snapshots of one action apart. Never a key."""
    return f"https://ec2.{region}.amazonaws.com/?Action={action}" + "".join(f"&{k}={v}" for k, v in extra.items())


SPOT_PRICES = "?Action=DescribeSpotPriceHistory"
PLACEMENT_SCORES = "?Action=GetSpotPlacementScores"
CAPACITY_BLOCKS = ec2_endpoint(CAPACITY_BLOCK_REGION, "DescribeCapacityBlockOfferings")

_ACCOUNT = re.compile(r"arn:aws[\w-]*:[^\s,\]\)\"']*|(?<!\d)\d{12}(?!\d)")


def redact(text: str) -> str:
    """An AWS error message with account ids and ARNs removed (authorization errors name the IAM user)."""
    return _ACCOUNT.sub("<redacted>", text or "")


def _tag(el: ET.Element) -> str:
    return el.tag.rsplit("}", 1)[-1]


def xml_to_json(el: ET.Element):
    """EC2 Query XML -> JSON, names verbatim: <xSet><item/>...</xSet> and <item> runs become lists, leaves text."""
    kids = list(el)
    if not kids:
        return el.text if el.text is None else el.text.strip()
    if all(_tag(k) == "item" for k in kids):
        return [xml_to_json(k) for k in kids]
    return {_tag(k): xml_to_json(k) for k in kids}


def parse_ec2(text: str) -> tuple[dict | None, str | None, str | None]:
    """(body, error code, error message) of one EC2 Query response. A set with no items reads as []."""
    root = ET.fromstring(text)
    if _tag(root) == "Response":
        err = root.find("./Errors/Error")
        code = err.findtext("Code") if err is not None else None
        return None, code or "UnknownError", redact(err.findtext("Message") or "") if err is not None else ""
    body = {}
    for k in root:
        name = _tag(k)
        body[name] = [] if name.endswith("Set") and not list(k) else xml_to_json(k)
    return body, None, None


def _keep(row: dict) -> dict:
    return {
        k: row.get(k)
        for k in ("Instance Type", "price", "vCPU", "Memory", "Operating System", "Pre Installed S/W", "License Model")
    }


class AwsProvider(Provider):
    name = "aws"
    # List prices change rarely and the file is large, so hourly is plenty.
    polling = PollingPolicy(interval_seconds=3600, timeout_seconds=60)

    def __init__(self, client: httpx.AsyncClient | None = None, access_key_id: str | None = None,
                 secret_access_key: str | None = None):
        super().__init__(
            client
            or httpx.AsyncClient(
                base_url=BASE_URL,
                headers={"accept": "application/json", "user-agent": "opengrid-terminal/0.1"},
                timeout=60,
            )
        )
        # Both or neither: half a key pair cannot sign anything.
        self._keys = (access_key_id, secret_access_key) if access_key_id and secret_access_key else None
        # When each slow-moving call last ran in this process (placement scores, Capacity Blocks).
        self._last: dict[str, datetime] = {}

    async def fetch(self) -> list[RawResponse]:
        out = [*await self._prices(), await self._spot_advisor()]
        if self._keys:
            out += await self._ec2_market()
        return out

    # ------------------------------------------------------------------ EC2 (keyed, read-only)

    async def _ec2(self, region: str, action: str, params: dict, endpoint: str) -> tuple[RawResponse, str | None]:
        """One signed EC2 Query call, stored under `endpoint` with its parameters (never the key) as `request`.
        Returns the response (payload = the body as JSON) and AWS's error code, if any. One retry on throttling."""
        kid, secret = self._keys
        url = f"https://ec2.{region}.amazonaws.com/"
        form = {"Action": action, "Version": EC2_VERSION, **params}
        body = urllib.parse.urlencode(form).encode()
        started_wall, started = datetime.now(timezone.utc), time.perf_counter()
        base = dict(provider=self.name, endpoint=endpoint, method="POST", fetched_at=started_wall, request=params)
        code = None
        for attempt in range(2):
            headers = {"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"}
            headers.update(aws_sigv4.sign("POST", url, region, "ec2", kid, secret, body=body, headers=headers))
            try:
                resp = await self._client.post(url, content=body, headers={**headers, "accept": "text/xml"},
                                               follow_redirects=False)
                parsed, code, message = parse_ec2(resp.text)
            except Exception as exc:  # network error or a body that is not EC2 XML
                return RawResponse(**base, duration_ms=int((time.perf_counter() - started) * 1000), ok=False,
                                   error=redact(f"{type(exc).__name__}: {exc}")[:500]), None
            if attempt == 0 and (code == "RequestLimitExceeded" or resp.status_code >= 500):
                await asyncio.sleep(RETRY_DELAY)
                continue
            break
        ms = int((time.perf_counter() - started) * 1000)
        if code or not resp.is_success:
            return RawResponse(**base, status_code=resp.status_code, duration_ms=ms, ok=False,
                               error=f"{code or resp.status_code}: {message or ''}"[:500]), code
        return RawResponse(**base, status_code=resp.status_code, duration_ms=ms, ok=True, payload=parsed), None

    async def _ec2_market(self) -> list[RawResponse]:
        now = datetime.now(timezone.utc)
        out = list(await gather_limited([lambda r=r: self._spot_prices(r, now) for r in SPOT_REGIONS],
                                        limit=SPOT_CONCURRENCY))
        if self._due("placement", PLACEMENT_TTL, now):
            out += [await self._placement(t) for t in PLACEMENT_TYPES]
        if self._due("capacity_blocks", CAPACITY_BLOCK_TTL, now):
            out.append(await self._capacity_blocks(now))
        return out

    def _due(self, what: str, ttl: timedelta, now: datetime) -> bool:
        if what in self._last and now - self._last[what] < ttl:
            return False
        self._last[what] = now
        return True

    async def _spot_prices(self, region: str, now: datetime) -> RawResponse:
        """The effective Linux spot price now, per GPU type and AZ, in one region (all pages). GPU rows verbatim."""
        ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        types = sorted(GPU_INSTANCES)
        params = {**{f"InstanceType.{i}": t for i, t in enumerate(types, 1)}, "ProductDescription.1": SPOT_PRODUCT,
                  "StartTime": ts, "EndTime": ts, "MaxResults": "1000"}
        endpoint = ec2_endpoint(region, "DescribeSpotPriceHistory")
        items: list = []
        first = None
        token = None
        for _ in range(SPOT_MAX_PAGES):
            raw, _code = await self._ec2(region, "DescribeSpotPriceHistory",
                                         {**params, **({"NextToken": token} if token else {})}, endpoint)
            first = first or raw
            if not raw.ok:
                return raw.model_copy(update={"request": params})
            page = raw.payload.get("spotPriceHistorySet")
            if not isinstance(page, list):
                return _failed(raw, "aws spot: no spotPriceHistorySet - response reshaped", params)
            items += page
            token = raw.payload.get("nextToken")
            if not token:
                break
        else:
            return _failed(first, f"aws spot: more than {SPOT_MAX_PAGES} pages", params)
        rows = [i for i in items if isinstance(i, dict) and i.get("instanceType") in GPU_INSTANCES]
        if not rows:
            return _failed(first, f"aws spot: no GPU rows in {region}", params)
        return reshaped(first, {"region": region, "product_description": SPOT_PRODUCT, "as_of": ts,
                                "spotPriceHistorySet": rows}, request=params)

    async def _placement(self, itype: str) -> RawResponse:
        """One fixed configuration: PLACEMENT_TARGET instance(s) of `itype`, regional scores over SPOT_REGIONS."""
        params = {"InstanceType.1": itype, "TargetCapacity": str(PLACEMENT_TARGET), "TargetCapacityUnitType": "units",
                  "SingleAvailabilityZone": "false",
                  **{f"RegionName.{i}": r for i, r in enumerate(SPOT_REGIONS, 1)}}
        endpoint = ec2_endpoint(CAPACITY_BLOCK_REGION, "GetSpotPlacementScores", InstanceType=itype)
        raw, _code = await self._ec2(CAPACITY_BLOCK_REGION, "GetSpotPlacementScores", params, endpoint)
        if not raw.ok:
            return raw
        scores = raw.payload.get("spotPlacementScoreSet")
        if not isinstance(scores, list):
            return _failed(raw, "aws placement: no spotPlacementScoreSet - response reshaped", params)
        return reshaped(raw, {"instance_type": itype, "target_capacity": PLACEMENT_TARGET,
                              "target_capacity_unit": "units", "single_availability_zone": False,
                              "spotPlacementScoreSet": scores})

    async def _capacity_blocks(self, now: datetime) -> RawResponse:
        """Capacity Block offerings for 1 instance x 24 h; the account-limit answer is stored once, quietly."""
        global _CAPACITY_BLOCK_LOGGED
        queries, offerings, first = [], [], None
        for itype in CAPACITY_BLOCK_TYPES:
            params = {"InstanceType": itype, "InstanceCount": "1", "CapacityDurationHours": str(CAPACITY_BLOCK_HOURS),
                      "MaxResults": "100"}
            raw, code = await self._ec2(CAPACITY_BLOCK_REGION, "DescribeCapacityBlockOfferings", params,
                                        CAPACITY_BLOCKS)
            first = first or raw
            if code == CAPACITY_BLOCK_UNAVAILABLE:
                # The account's state, not a broken feed: one successful snapshot that says so, one log line.
                if not _CAPACITY_BLOCK_LOGGED:
                    _CAPACITY_BLOCK_LOGGED = True
                    log.warning("aws: Capacity Block offerings are not available for this account (%s); "
                                "stored, retried every 24 h", code)
                return raw.model_copy(update={"ok": True, "error": None, "request": {"queries": [params]}, "payload": {
                    "status": "not_available_for_account", "region": CAPACITY_BLOCK_REGION,
                    "error_code": code, "error_message": raw.error.split(": ", 1)[-1]}})
            if not raw.ok:
                return raw.model_copy(update={"request": {"queries": [*queries, params]}})
            page = raw.payload.get("capacityBlockOfferingSet")
            if not isinstance(page, list):
                return _failed(raw, "aws capacity blocks: no capacityBlockOfferingSet - response reshaped", params)
            queries.append(params)
            offerings += page
        return reshaped(first, {"status": "ok", "region": CAPACITY_BLOCK_REGION, "capacityBlockOfferingSet": offerings},
                        request={"queries": queries})

    async def _spot_advisor(self) -> RawResponse:
        """The Spot Advisor feed, GPU-family types only, verbatim, plus its Last-Modified header. Fails open."""
        started_wall, started = datetime.now(timezone.utc), time.perf_counter()
        base = dict(provider=self.name, endpoint=SPOT_ADVISOR, fetched_at=started_wall)
        try:
            resp = await self._client.get(SPOT_ADVISOR)
            resp.raise_for_status()
            body = resp.json()
            payload = spot_advisor_payload(body, resp.headers.get("last-modified"))
        except Exception as exc:
            return RawResponse(**base, duration_ms=int((time.perf_counter() - started) * 1000), ok=False,
                               error=f"{type(exc).__name__}: {exc}"[:500])
        return RawResponse(**base, status_code=resp.status_code, ok=True, payload=payload,
                           duration_ms=int((time.perf_counter() - started) * 1000))

    async def _prices(self) -> list[RawResponse]:
        raw = await self.get(ENDPOINT)
        regions = (raw.payload or {}).get("regions") if isinstance(raw.payload, dict) else None
        if not isinstance(regions, dict) or REGION_NAME not in regions:
            return [raw] if not raw.ok else [raw.model_copy(update={"ok": False, "error": "aws: region missing from price file", "payload": None})]
        # Keep the GPU instances only: the rest is 1,300 unrelated rows.
        rows = [_keep(r) for r in regions[REGION_NAME].values() if r.get("Instance Type") in GPU_INSTANCES]
        # An unlisted GPU family is a new product to add to the table, not to guess.
        for r in regions[REGION_NAME].values():
            t = r.get("Instance Type", "")
            if re.match(r"^(p\d|g\d|dl\d|trn|inf)", t) and t not in GPU_INSTANCES and t.split(".")[0] not in _SEEN_UNLISTED:
                _SEEN_UNLISTED.add(t.split(".")[0])
                log.info("aws: instance family %s is not in GPU_INSTANCES; skipped", t.split(".")[0])
        return [reshaped(raw, {"region": REGION_NAME, "rows": rows})]

    @staticmethod
    def normalize(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ComputeListing]:
        """One GPU instance type becomes one on-demand listing; with the key, one spot listing per type and AZ."""
        on_demand = _on_demand(by_endpoint)
        # vCPU and memory are the instance type's, the same in every region: taken from the price file.
        specs = {l.sku: (l.vcpu, l.ram_gb) for l in on_demand}
        return [*on_demand, *spot_listings(by_endpoint, specs)]

    @staticmethod
    def normalize_capacity(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[CapacityObservation]:
        """Spot placement scores as a tier per (type, region) for the spot listings. Empty without the key."""
        out = []
        for snap in find_endpoint(by_endpoint, PLACEMENT_SCORES):
            body = snap.payload if isinstance(snap.payload, dict) else {}
            itype = body.get("instance_type")
            spec = GPU_INSTANCES.get(itype)
            if spec is None:
                continue
            count, raw_name = spec
            for row in body.get("spotPlacementScoreSet") or []:
                region = (row or {}).get("region") if isinstance(row, dict) else None
                score = _int((row or {}).get("score")) if isinstance(row, dict) else None
                if not region:
                    continue
                out.append(CapacityObservation(
                    provider="aws", sku=f"{itype}:spot", region=region, country=SPOT_REGIONS.get(region),
                    raw_gpu_name=raw_name, canonical_gpu_name=canonical.canonical_gpu_name(raw_name), gpu_count=count,
                    # A score is AWS's likelihood that a spot request succeeds, not a stock report: in_stock stays null.
                    in_stock=None, tier=placement_tier(score),
                    raw_value=f"placement score {row.get('score')}/10 (target {body.get('target_capacity')} "
                              f"instance, regional)",
                    source=getattr(snap, "endpoint", PLACEMENT_SCORES),
                    observed_at=snap.fetched_at))
        return out


def _int(v) -> int | None:
    try:
        return int(str(v))
    except (TypeError, ValueError):
        return None


def placement_tier(score: int | None) -> str | None:
    """AWS's 1-10 spot placement score -> low / medium / high (PLACEMENT_TIERS). Anything else is never guessed."""
    return next((t for lo, hi, t in PLACEMENT_TIERS if score is not None and lo <= score <= hi), None)


def _failed(raw: RawResponse, why: str, request=None) -> RawResponse:
    """A response that arrived but cannot be read: a failed fetch, never an empty source."""
    update = {"ok": False, "error": why, "payload": None}
    if request is not None:
        update["request"] = request
    return raw.model_copy(update=update)


_CAPACITY_BLOCK_LOGGED = False


def _on_demand(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ComputeListing]:
    rows = find_endpoint(by_endpoint, ENDPOINT)
    if not rows:
        return []
    snapshot = rows[0]
    listings = []
    for row in (snapshot.payload or {}).get("rows") or []:
        itype = row.get("Instance Type")
        spec = GPU_INSTANCES.get(itype)
        price = to_decimal(row.get("price"))
        if spec is None or price is None or price <= 0:
            continue
        count, raw_name = spec
        mem = _GIB.match(row.get("Memory") or "")
        listings.append(
            ComputeListing(
                provider="aws",
                sku=itype,
                listing_id=f"{itype}:{REGION}",
                raw_gpu_name=raw_name,
                canonical_gpu_name=canonical.canonical_gpu_name(raw_name),
                gpu_count=count,
                region=REGION,
                country="US",
                price_per_gpu_hour=price / count,
                price_per_instance_hour=price,
                currency="USD",
                market_type="on_demand",
                provider_tier="hyperscaler",
                interruptible=False,
                # The price file says nothing about capacity.
                available=None,
                capacity=None,
                capacity_unit=None,
                vcpu=int(row["vCPU"]) if (row.get("vCPU") or "").isdigit() else None,
                ram_gb=float(mem.group(1)) if mem else None,
                storage_gb=None,
                observed_at=snapshot.fetched_at,
            )
        )
    return listings


def spot_listings(by_endpoint: "dict[str, list[RawSnapshot]]",
                  specs: dict[str, tuple] | None = None) -> list[ComputeListing]:
    """The newest effective Linux spot price per (GPU type, AZ), from each region's newest stored snapshot."""
    out = []
    for snap in find_endpoint(by_endpoint, SPOT_PRICES):
        body = snap.payload if isinstance(snap.payload, dict) else {}
        region = body.get("region")
        latest: dict[tuple[str, str], dict] = {}
        for item in body.get("spotPriceHistorySet") or []:
            if not isinstance(item, dict) or item.get("productDescription") != SPOT_PRODUCT:
                continue
            key = (item.get("instanceType"), item.get("availabilityZone"))
            if key[0] in GPU_INSTANCES and key[1] and (key not in latest or
                                                       str(item.get("timestamp")) > str(latest[key].get("timestamp"))):
                latest[key] = item
        for (itype, az), item in sorted(latest.items()):
            price = to_decimal(item.get("spotPrice"))
            if price is None or price <= 0:
                continue
            count, raw_name = GPU_INSTANCES[itype]
            vcpu, ram = (specs or {}).get(itype, (None, None))
            out.append(ComputeListing(
                provider="aws", sku=itype, listing_id=f"{itype}:{az}:spot", raw_gpu_name=raw_name,
                canonical_gpu_name=canonical.canonical_gpu_name(raw_name), gpu_count=count, region=az,
                country=SPOT_REGIONS.get(region or az[:-1]), price_per_gpu_hour=price / count,
                price_per_instance_hour=price, currency="USD", market_type="spot", provider_tier="hyperscaler",
                interruptible=True,
                # A spot price says nothing about stock; placement scores are capacity rows of their own.
                available=None, capacity=None, capacity_unit=None, vcpu=vcpu, ram_gb=ram, storage_gb=None,
                observed_at=snap.fetched_at))
    return out


_SEEN_UNLISTED: set[str] = set()


def spot_advisor_payload(body, last_modified: str | None) -> dict:
    """What is stored from the feed: its band table, and the GPU-family rows of every region and OS, verbatim.
    Raises (so the fetch is stored as failed) when the feed is reshaped or carries no GPU row at all."""
    if not isinstance(body, dict) or not isinstance(body.get("spot_advisor"), dict) or not isinstance(body.get("ranges"), list):
        raise ValueError("aws spot advisor: no spot_advisor map / ranges list - feed reshaped")
    bands = {r.get("index") for r in body["ranges"] if isinstance(r, dict)}
    if bands != {0, 1, 2, 3, 4}:
        raise ValueError(f"aws spot advisor: interruption bands are no longer 0-4: {sorted(bands, key=str)}")
    advisor = {
        region: {os_: {t: v for t, v in rows.items() if _GPU_FAMILY.match(t)}
                 for os_, rows in by_os.items() if isinstance(rows, dict)}
        for region, by_os in body["spot_advisor"].items() if isinstance(by_os, dict)
    }
    gpu_types = {t for by_os in advisor.values() for rows in by_os.values() for t in rows}
    if not gpu_types:
        raise ValueError("aws spot advisor: no GPU instance type in any region - feed reshaped")
    types = body.get("instance_types") if isinstance(body.get("instance_types"), dict) else {}
    return {"last_modified": last_modified, "ranges": body["ranges"], "global_rate": body.get("global_rate"),
            "instance_types": {t: types[t] for t in sorted(gpu_types) if t in types}, "spot_advisor": advisor}


@dataclass(frozen=True)
class SpotAdvice:
    """One (instance type, region) row of AWS's Spot Advisor, Linux. Never a listing, capacity row or score."""

    instance_type: str
    region: str
    gpu_count: int
    raw_gpu_name: str
    canonical_gpu_name: str | None
    interruption_band: int      # the feed's r: 0 (<5%) .. 4 (>20%)
    interruption_label: str     # the feed's own label for that band, e.g. "<5%"
    savings_pct: int            # the feed's s: average spot saving over on-demand, percent
    last_modified: str | None   # when AWS last rewrote the feed


def spot_advice(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[SpotAdvice]:
    """Stored Spot Advisor rows for the GPU types this module prices (GPU_INSTANCES), Linux, every region. Pure.
    A row whose band or saving is not an integer in range is skipped, never guessed."""
    rows = find_endpoint(by_endpoint, SPOT_ADVISOR)
    body = rows[0].payload if rows and isinstance(rows[0].payload, dict) else {}
    labels = {r["index"]: str(r.get("label")) for r in body.get("ranges") or [] if isinstance(r, dict) and "index" in r}
    out = []
    for region, by_os in sorted((body.get("spot_advisor") or {}).items()):
        for itype, v in sorted(((by_os or {}).get("Linux") or {}).items()):
            spec, r, s = GPU_INSTANCES.get(itype), (v or {}).get("r"), (v or {}).get("s")
            if spec is None or not isinstance(r, int) or r not in labels or not isinstance(s, int) or not 0 <= s <= 100:
                continue
            count, raw_name = spec
            out.append(SpotAdvice(instance_type=itype, region=region, gpu_count=count, raw_gpu_name=raw_name,
                                  canonical_gpu_name=canonical.canonical_gpu_name(raw_name), interruption_band=r,
                                  interruption_label=labels[r], savings_pct=s, last_modified=body.get("last_modified")))
    return out
