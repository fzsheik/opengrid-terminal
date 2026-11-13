"""Cost components: egress, storage and other charges beside the hourly price, in one normalised shape.

Computed on read, never stored parsed and never used by listings, indices, spreads, percentiles or routing (like
reserved.py). Three origins, one shape:

    api       parsed from a stored cost_snapshots body (cost/sources.py): AWS, Azure, Oracle, Verda
    provider  parsed from a provider's own stored raw (normalize._latest_raw): Hyperstack pricebook, Vultr plans
              (`bandwidth`, `deploy_ondemand`), Vast offers (`storage_cost`, `inet_down_cost`, `inet_up_cost`)
    manual    cost/manual.py, each value with its source page and verified-at date

A component:
    provider, component, product, region (None = provider-wide), unit, tiers [{start_gb, end_gb, price}],
    raw_unit, allowance_scope, billed_when_stopped, origin, source, status ("checked" | "unverified"),
    kind ("observed" | "derived" | "manual"), as_of (fetch time or verified-at date), notes

Units (canonical): usd_per_gb (transfer), usd_per_gb_month (storage), usd_per_1k_requests, usd_per_hour.
1 GB = 10^9 bytes. A per-GiB price is divided by 1.073741824 and a GiB tier bound multiplied by it; the published
unit is kept in raw_unit. A month is 730 hours. A storage price per hour is x 730.
Free is an explicit 0 with its source. Unpublished is ABSENT (the estimator then reports the component unknown).
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal

from cost import manual, sources

log = logging.getLogger(__name__)

GIB = Decimal("1.073741824")
HOURS_PER_MONTH = Decimal(730)

TRANSFER = ("egress_internet", "egress_inter_zone", "ingress_internet")  # ingress: used by the RL estimator (cost/rl.py)
STORAGE = ("block_storage", "shared_fs", "object_storage")
COMPONENTS = TRANSFER + STORAGE + ("object_requests_write", "object_requests_read", "public_ip")
UNIT = {**{c: "usd_per_gb" for c in TRANSFER}, **{c: "usd_per_gb_month" for c in STORAGE},
        "object_requests_write": "usd_per_1k_requests", "object_requests_read": "usd_per_1k_requests",
        "public_ip": "usd_per_hour"}


def _d(v) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        out = Decimal(str(v))
    except Exception:  # noqa: BLE001
        return None
    return out if out.is_finite() else None


def _tiers(tiers, raw_unit: str) -> list[dict]:
    """[(start, end|None, price)] in the published unit -> canonical per-GB tiers."""
    gib = raw_unit.startswith("GiB")
    hourly = raw_unit.endswith("-hour")
    out = []
    for start, end, price in tiers:
        p = Decimal(str(price))
        if hourly:
            p *= HOURS_PER_MONTH
        if gib:
            p /= GIB
        out.append({"start_gb": Decimal(str(start)) * (GIB if gib else 1),
                    "end_gb": None if end is None else Decimal(str(end)) * (GIB if gib else 1), "price": p})
    return out


def component(provider: str, comp: str, product: str, tiers, raw_unit: str, *, origin: str, source: str,
              status: str = "checked", kind: str = "observed", as_of=None, region: str | None = None,
              allowance_scope: str | None = None, billed_when_stopped: bool | None = None, notes: str | None = None,
              listing_id: str | None = None) -> dict:
    if comp not in COMPONENTS:
        raise ValueError(f"unknown component {comp!r}")
    return {"provider": provider, "component": comp, "product": product, "region": region, "unit": UNIT[comp],
            "tiers": _tiers(tiers, raw_unit), "raw_unit": raw_unit, "allowance_scope": allowance_scope,
            "billed_when_stopped": billed_when_stopped, "origin": origin, "source": source, "status": status,
            "kind": kind, "as_of": as_of, "notes": notes, "listing_id": listing_id}


# --------------------------------------------------------------------------- api parsers (pure)

def _aws_rows(payload: dict) -> list[tuple[dict, dict]]:
    """(attributes, priceDimension) for every OnDemand price dimension of the kept products."""
    out = []
    for sku, product in (payload.get("products") or {}).items():
        for term in ((payload.get("terms") or {}).get("OnDemand") or {}).get(sku, {}).values():
            for dim in (term.get("priceDimensions") or {}).values():
                out.append((product.get("attributes") or {}, dim))
    return out


def _aws_tiers(dims: list[dict]) -> list[tuple]:
    rows = []
    for d in dims:
        end = d.get("endRange")
        rows.append((Decimal(str(d.get("beginRange") or 0)), None if end in (None, "Inf") else Decimal(str(end)),
                     Decimal(str((d.get("pricePerUnit") or {})["USD"]))))
    rows.sort(key=lambda r: r[0])
    return rows


def parse_aws_data_transfer(payload: dict, as_of=None) -> list[dict]:
    src = sources.SOURCES["aws_data_transfer"]
    rows = _aws_rows(payload)
    out_dims = [d for a, d in rows if a.get("transferType") == "AWS Outbound" and a.get("toLocation") == "External"]
    if not out_dims:
        raise ValueError("aws_data_transfer: no AWS Outbound tiers")
    tiers = _aws_tiers(out_dims)
    # The offer file's first paid tier is "beyond the global free tier": 100 GB a month across all services.
    if tiers[0][0] == 0 and tiers[0][2] > 0:
        tiers = [(Decimal(0), Decimal(100), Decimal(0))] + [(max(s, Decimal(100)) if i == 0 else s, e, p)
                                                           for i, (s, e, p) in enumerate(tiers)]
    out = [component("aws", "egress_internet", "Data Transfer Out to the internet", tiers, "GB", origin="api",
                     source=src.url, as_of=as_of, region="us-east-1", allowance_scope="account_month",
                     notes="first 100 GB a month free across all AWS services (the global free tier)")]
    az = [d for a, d in rows if a.get("transferType") == "IntraRegion" and _d((d.get("pricePerUnit") or {}).get("USD"))]
    if az:
        out.append(component("aws", "egress_inter_zone", "Regional data transfer between Availability Zones",
                             [(0, None, Decimal(az[0]["pricePerUnit"]["USD"]))], "GB", origin="api", source=src.url,
                             as_of=as_of, region="us-east-1",
                             notes="charged in each direction; the file also lists $0 IntraRegion-xAZ rows"))
    return out


def parse_aws_s3(payload: dict, as_of=None) -> list[dict]:
    src = sources.SOURCES["aws_s3"]
    rows = _aws_rows(payload)
    out = []
    stor = [d for a, d in rows if a.get("usagetype") == "TimedStorage-ByteHrs"]
    if stor:
        out.append(component("aws", "object_storage", "S3 Standard", _aws_tiers(stor), "GB-month", origin="api",
                             source=src.url, as_of=as_of, region="us-east-1"))
    for usage, comp in (("Requests-Tier1", "object_requests_write"), ("Requests-Tier2", "object_requests_read")):
        dims = [d for a, d in rows if a.get("usagetype") == usage]
        if dims:  # priced per request; stored per 1k
            price = Decimal(str(dims[0]["pricePerUnit"]["USD"])) * 1000
            out.append(component("aws", comp, f"S3 Standard {usage}", [(0, None, price)], "1k requests",
                                 origin="api", source=src.url, as_of=as_of, region="us-east-1"))
    if not out:
        raise ValueError("aws_s3: no S3 Standard storage or request prices")
    return out


def _azure_tiers(items: list[dict]) -> list[tuple]:
    """Azure gives each tier's minimum only; a tier ends where the next begins. Duplicate minimums (the same
    meter listed twice) keep the first."""
    seen, rows = set(), []
    for i in sorted(items, key=lambda i: float(i.get("tierMinimumUnits") or 0)):
        start = Decimal(str(i.get("tierMinimumUnits") or 0))
        if start in seen:
            continue
        seen.add(start)
        rows.append([start, None, Decimal(str(i["retailPrice"]))])
    for a, b in zip(rows, rows[1:]):
        a[1] = b[0]
    return [tuple(r) for r in rows]


def parse_azure_bandwidth(payload: dict, as_of=None) -> list[dict]:
    src = sources.SOURCES["azure_bandwidth"]
    items = payload.get("items") or []
    # Default routing (Microsoft Global Network): productName "Rtn Preference: MGN".
    mgn = [i for i in items if i.get("productName") == "Rtn Preference: MGN" and i.get("skuName") == "Standard"]
    egress = [i for i in mgn if i.get("meterName") == "Standard Data Transfer Out"]
    if not egress:
        raise ValueError("azure_bandwidth: no Standard Data Transfer Out (MGN) tiers")
    out = [component("azure", "egress_internet", "Data Transfer Out, Microsoft Global Network routing",
                     _azure_tiers(egress), "GB", origin="api", source=src.url, as_of=as_of, region="eastus",
                     allowance_scope="account_month", notes="first 100 GB a month free")]
    az = [i for i in mgn if i.get("meterName") == "Standard Inter-Availability Zone Data Transfer Out"]
    if az:
        out.append(component("azure", "egress_inter_zone", "Inter-Availability Zone Data Transfer Out",
                             [(0, None, Decimal(str(az[0]["retailPrice"])))], "GB", origin="api", source=src.url,
                             as_of=as_of, region="eastus", notes="the pricing page does not confirm this charge"))
    return out


def parse_azure_storage(payload: dict, as_of=None) -> list[dict]:
    src = sources.SOURCES["azure_storage"]
    items = payload.get("items") or []
    out = []

    def pick(product, sku, meter):
        return [i for i in items if i.get("productName") == product and i.get("skuName") == sku
                and i.get("meterName") == meter]

    ssd = pick("Azure Premium SSD v2", "Premium LRS", "Premium LRS Provisioned Capacity")
    if ssd:
        unit = ssd[0].get("unitOfMeasure") or ""
        if "GiB/Hour" not in unit:
            raise ValueError(f"azure_storage: Premium SSD v2 capacity unit {unit!r} - reshaped")
        out.append(component("azure", "block_storage", "Premium SSD v2 (3,000 IOPS / 125 MBps included)",
                             [(0, None, Decimal(str(ssd[0]["retailPrice"])))], "GiB-hour", origin="api",
                             source=src.url, as_of=as_of, region="eastus"))
    files = pick("Premium Files", "Premium LRS", "Premium LRS Provisioned")
    if files:
        out.append(component("azure", "shared_fs", "Azure Files Premium LRS (provisioned)",
                             [(0, None, Decimal(str(files[0]["retailPrice"])))], "GB-month", origin="api",
                             source=src.url, as_of=as_of, region="eastus"))
    blob = pick("Blob Storage", "Hot LRS", "Hot LRS Data Stored")
    if blob:
        out.append(component("azure", "object_storage", "Blob Storage Hot LRS", _azure_tiers(blob), "GB-month",
                             origin="api", source=src.url, as_of=as_of, region="eastus"))
    if not out:
        raise ValueError("azure_storage: none of the expected storage meters")
    return out


def _oracle_prices(item: dict) -> list[dict]:
    for loc in item.get("currencyCodeLocalizations") or []:
        if loc.get("currencyCode") == "USD":
            return [p for p in loc.get("prices") or [] if p.get("model") == "PAY_AS_YOU_GO"]
    return []


def _oracle_tiers(prices: list[dict]) -> list[tuple]:
    rows = []
    for p in sorted(prices, key=lambda p: float(p.get("rangeMin") or 0)):
        end = p.get("rangeMax")
        rows.append((Decimal(str(p.get("rangeMin") or 0)),
                     None if end is None or float(end) >= 999_999_999 else Decimal(str(end)), Decimal(str(p["value"]))))
    return rows


def parse_oracle(payload: dict, as_of=None) -> list[dict]:
    src = sources.SOURCES["oracle_price_list"]
    by = {i.get("partNumber"): i for i in payload.get("items") or []}
    out = []
    if "B88327" in by:
        out.append(component("oracle", "egress_internet", "Outbound Data Transfer (North America, Europe, UK)",
                             _oracle_tiers(_oracle_prices(by["B88327"])), "GB", origin="api", source=src.url,
                             as_of=as_of, allowance_scope="account_month",
                             notes="first 10 TB a month free; APAC and other origins are priced higher"))
    if "B91961" in by and "B91962" in by:
        base = Decimal(str(_oracle_prices(by["B91961"])[0]["value"]))
        vpu = Decimal(str(_oracle_prices(by["B91962"])[0]["value"]))
        out.append(component("oracle", "block_storage", "Block Volume, Balanced (10 VPUs/GB)",
                             [(0, None, base + 10 * vpu)], "GB-month", origin="api", source=src.url, as_of=as_of,
                             kind="derived", notes=f"B91961 ${base} + 10 x B91962 ${vpu} per GB-month"))
    if "B91628" in by:
        out.append(component("oracle", "object_storage", "Object Storage (Standard)",
                             _oracle_tiers(_oracle_prices(by["B91628"])), "GB-month", origin="api", source=src.url,
                             as_of=as_of, allowance_scope="account_month", notes="first 10 GB a month free"))
    if not out:
        raise ValueError("oracle_price_list: none of the cost parts")
    return out


def parse_verda(payload: list, as_of=None) -> list[dict]:
    src = sources.SOURCES["verda_volume_types"]
    out = []
    for vt in payload:
        price = (vt.get("price") or {})
        if (price.get("currency") or "usd").lower() != "usd" or price.get("price_per_month_per_gb") is None:
            continue
        comp = "shared_fs" if vt.get("is_shared_fs") else "block_storage"
        out.append(component("verda", comp, f"{vt.get('type')} volume", [(0, None, price["price_per_month_per_gb"])],
                             "GB-month", origin="api", source=src.url, as_of=as_of))
    if not out:
        raise ValueError("verda_volume_types: no USD volume price")
    # Several block types (NVMe, HDD): the estimator takes the first per component, so NVMe (the default) first.
    out.sort(key=lambda c: (c["component"], "HDD" in c["product"]))
    return out


PARSERS = {"aws_data_transfer": parse_aws_data_transfer, "aws_s3": parse_aws_s3,
           "azure_bandwidth": parse_azure_bandwidth, "azure_storage": parse_azure_storage,
           "oracle_price_list": parse_oracle, "verda_volume_types": parse_verda}


# --------------------------------------------------------------------------- provider-raw readers (pure)

def from_hyperstack(pricebook: list, as_of=None) -> list[dict]:
    """Hyperstack's pricebook: Cloud-SSD and ObjectStorage per GB-hour, PublicIP per hour."""
    by = {e.get("name"): _d(e.get("value")) for e in pricebook or [] if isinstance(e, dict)}
    src = "https://infrahub-api.nexgencloud.com/v1/pricebook"
    out = []
    if by.get("Cloud-SSD") is not None:
        out.append(component("hyperstack", "block_storage", "Cloud-SSD volume", [(0, None, by["Cloud-SSD"])], "GB-hour",
                             origin="provider", source=src, as_of=as_of))
    if by.get("ObjectStorage") is not None:
        out.append(component("hyperstack", "object_storage", "Object Storage", [(0, None, by["ObjectStorage"])],
                             "GB-hour", origin="provider", source=src, as_of=as_of))
    if by.get("PublicIP") is not None:
        out.append(component("hyperstack", "public_ip", "Public IP", [(0, None, by["PublicIP"])], "hour",
                             origin="provider", source=src, as_of=as_of))
    return out


def vultr_plans(payloads: list) -> dict[str, dict]:
    """plan id -> {bandwidth_gb, deploy_ondemand} from Vultr's stored plan lists (VM and metal)."""
    out = {}
    for body in payloads:
        for key in ("plans", "plans_metal"):
            for p in (body or {}).get(key) or []:
                if p.get("id"):
                    out[p["id"]] = {"bandwidth_gb": _d(p.get("bandwidth")),
                                    "deploy_ondemand": p.get("deploy_ondemand")}
    return out


def vast_offer_components(offer: dict, listing_id: str, as_of=None) -> list[dict]:
    """One Vast offer's own storage and transfer prices. Absent fields stay absent (Vast's `_keep` must keep them)."""
    src = "https://console.vast.ai/api/v0/bundles/"
    out = []
    storage = _d(offer.get("storage_cost"))
    if storage is not None:
        out.append(component("vast", "block_storage", "Host disk (per offer)", [(0, None, storage)], "GB-month",
                             origin="provider", source=src, as_of=as_of, billed_when_stopped=True,
                             listing_id=listing_id, notes="billed while the instance exists, even stopped"))
    down = _d(offer.get("inet_down_cost"))
    up = _d(offer.get("inet_up_cost"))
    if down is not None:
        out.append(component("vast", "ingress_internet", "Download to the host (per offer)", [(0, None, down)], "GB",
                             origin="provider", source=src, as_of=as_of, listing_id=listing_id))
    if up is not None:
        out.append(component("vast", "egress_internet", "Upload from the host (per offer)", [(0, None, up)], "GB",
                             origin="provider", source=src, as_of=as_of, listing_id=listing_id,
                             notes=None if down is None else f"download (ingress) ${down}/GB"))
    return out


# --------------------------------------------------------------------------- manual

def manual_components() -> list[dict]:
    out = []
    for m in manual.COMPONENTS:
        out.append(component(m["provider"], m["component"], m["product"], m["tiers"], m["raw_unit"], origin="manual",
                             source=m["source"], status=m.get("status", "checked"), kind="manual",
                             as_of=date.fromisoformat(m.get("verified_at", manual.V)), region=m.get("region"),
                             allowance_scope=m.get("allowance_scope"),
                             billed_when_stopped=m.get("billed_when_stopped"), notes=m.get("notes")))
    return out


# --------------------------------------------------------------------------- assembly

def build(snapshots: dict[str, dict], provider_raw: dict[str, dict] | None = None) -> tuple[list[dict], list[str]]:
    """snapshots: sources.latest() shape. provider_raw: provider -> {endpoint: [snapshot-like]} (only hyperstack is
    read here; Vultr and Vast are per listing, see cost/estimate.py). Returns (components, notes)."""
    out, notes = [], []
    for key, parser in PARSERS.items():
        snap = snapshots.get(key) or {}
        if snap.get("payload") is None:
            notes.append(f"{key}: no stored fetch yet" if snap.get("last_attempt_at") is None
                         else f"{key}: no successful fetch ({snap.get('last_error') or 'failed'})")
            continue
        try:
            out += parser(snap["payload"], snap.get("fetched_at"))
        except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
            log.warning("cost: %s could not be parsed: %s", key, exc)
            notes.append(f"{key}: stored body could not be parsed ({type(exc).__name__})")
    hs = (provider_raw or {}).get("hyperstack") or {}
    rows = [r for ep, rs in hs.items() if "/pricebook" in ep for r in rs]
    if rows:
        out += from_hyperstack(rows[0].payload, rows[0].fetched_at)
    out += manual_components()
    return out, notes


def lookup(components: list[dict], provider: str, comp: str, listing_id: str | None = None,
           usable_only: bool = True) -> dict | None:
    """The component a job is priced with: a per-listing one first (Vast), then API/provider, then manual.
    usable_only: unverified values are not used (they read as unknown)."""
    cands = [c for c in components if c["provider"] == provider and c["component"] == comp
             and (c["listing_id"] is None or c["listing_id"] == listing_id)
             and (not usable_only or c["status"] == "checked")]
    if not cands:
        return None
    rank = {"provider": 0, "api": 1, "manual": 2}
    return min(cands, key=lambda c: (c["listing_id"] is None, rank.get(c["origin"], 3)))


def to_json(c: dict) -> dict:
    out = dict(c)
    out["tiers"] = [{"start_gb": float(t["start_gb"]), "end_gb": None if t["end_gb"] is None else float(t["end_gb"]),
                     "price": float(t["price"])} for t in c["tiers"]]
    a = c.get("as_of")
    out["as_of"] = a.isoformat() if isinstance(a, (date, datetime)) else a
    return out
