"""Crusoe: prices as listed by Shadeform (unchanged); capacity from Crusoe's own Cloud API when a key is set.

With CRUSOE_ACCESS_KEY_ID + CRUSOE_SECRET_KEY (an API access key made in console.crusoecloud.com), the poller makes
four signed, read-only GETs on https://api.cloud.crusoe.ai/v1:

    GET /capacities                              one row per (type, location): `quantity`, `num_slices`, `quota_type`
    GET /locations                               the location names (stored raw; capacity carries its own)
    GET /organizations/projects                  only to find the project id the types call needs; never stored
    GET /projects/{project_id}/compute/vms/types the VM type catalogue: GPU model text and count per product name

Nothing is ever created: no VM, reservation, disk or key. Launching Crusoe stays the Shadeform route
(routing/adapters/shadeform.py); there is no direct Crusoe launch adapter.

Signing (client-go auth/auth.go, docs.crusoe.ai/reference/api): every request carries

    X-Crusoe-Timestamp: <RFC3339 UTC>
    Authorization:      Bearer 1.0:<access key id>:<signature>

where signature = unpadded URL-safe base64 of HMAC-SHA256(key, message), the key is the secret decoded from unpadded
URL-safe base64, and message = "<path incl. /v1>\\n<canonical query>\\n<METHOD>\\n<timestamp>\\n". The canonical query
is Go's url.Values.Encode of the parsed query: keys sorted, each key's values in their original order, key and value
query-escaped (space as "+"). No body is signed. The keys are sent only to api.cloud.crusoe.ai, never stored, logged
or put in an error; the docs' printed example signature does not reproduce under any reading of its own inputs, so
tests pin vectors computed with the SDK's own Go code instead.

Capacity: `quantity` per (type, location). Seen 2026-11-01: only 0 or 1, for every type including 2-vCPU CPU VMs in
every location, with the project's quotas all at max 0. So it is read as a yes/no (quantity > 0), never a count:
`available_count` stays None and the quantity is kept in raw_value. The API repeats each (type, location) row (8
identical copies seen); one observation per key, in stock if any copy says so. CPU-only types are skipped. Skus are
Crusoe product names ("l40s-48gb.1x"), which are exactly the listing skus (Shadeform's `cloud_instance_type`), so
capacity joins to price by sku. Regions are Crusoe's location names ("us-east1-a"), not Shadeform's.

One capacity source per provider: Crusoe's API while its newest capacities snapshot is within API_FRESH of the
newest Shadeform catalogue (Shadeform is still fetched every poll, for prices), else Shadeform's yes/no as before.
So one failed API poll keeps the API state (it ages into stale), while a removed or long-broken key falls back;
capacity.prune drops the other source's current rows when the source switches.

Prices: the Crusoe API publishes no price list (the types catalogue has none; billing is account spend), so prices
stay Shadeform's.
"""

import base64
import hashlib
import hmac
import logging
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import httpx

import canonical
from models import CapacityObservation, RawResponse
from providers.base import find_endpoint, reshaped
from providers.shadeform import ENDPOINT as SHADEFORM_ENDPOINT
from providers.shadeform import ShadeformCloud, gpu_label, shadeform_capacity

if TYPE_CHECKING:
    from tables import RawSnapshot

log = logging.getLogger(__name__)

API_HOST = "https://api.cloud.crusoe.ai"
API_PATH = "/v1"
CAPACITIES = API_HOST + API_PATH + "/capacities"
LOCATIONS = API_HOST + API_PATH + "/locations"
PROJECTS = API_HOST + API_PATH + "/organizations/projects"
# Stored under this name, never with the account's project id in it.
TYPES = API_HOST + API_PATH + "/projects/{project_id}/compute/vms/types"
API_FRESH = timedelta(hours=6)
SIGNATURE_VERSION = "1.0"
# Location prefix -> ISO country; an unknown prefix leaves the country unset rather than guessed.
LOCATION_COUNTRY = (("us-", "US"), ("eu-iceland", "IS"))


def canonical_query(query: str) -> str:
    """Go's url.ParseQuery + Values.Encode, as client-go canonicalizes before signing."""
    values: dict[str, list[str]] = {}
    for part in query.split("&"):
        if not part:
            continue
        if ";" in part:
            raise ValueError("crusoe: semicolon in query")
        key, _, value = part.partition("=")
        values.setdefault(urllib.parse.unquote_plus(key), []).append(urllib.parse.unquote_plus(value))
    return "&".join(f"{urllib.parse.quote_plus(k)}={urllib.parse.quote_plus(v)}"
                    for k in sorted(values) for v in values[k])


def signature(secret_key: str, method: str, path: str, query: str, timestamp: str) -> str:
    """The request signature (unpadded URL-safe base64), per client-go's signMessageV1_0."""
    key = base64.urlsafe_b64decode(secret_key + "=" * (-len(secret_key) % 4))
    message = f"{path}\n{canonical_query(query)}\n{method.upper()}\n{timestamp}\n".encode()
    return base64.urlsafe_b64encode(hmac.new(key, message, hashlib.sha256).digest()).rstrip(b"=").decode()


def auth_headers(access_key_id: str, secret_key: str, method: str, url: str,
                 now: datetime | None = None) -> dict[str, str]:
    """The two headers that authenticate one request to `url` (path and query are taken from it)."""
    ts = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    parts = urllib.parse.urlsplit(url)
    sig = signature(secret_key, method, parts.path, parts.query, ts)
    return {"X-Crusoe-Timestamp": ts, "Authorization": f"Bearer {SIGNATURE_VERSION}:{access_key_id}:{sig}"}


def _country(location: str) -> str | None:
    return next((c for prefix, c in LOCATION_COUNTRY if location.startswith(prefix)), None)


def gpu_types(types_payload) -> dict[str, dict]:
    """product_name -> type, for the catalogue's GPU types (num_gpu >= 1)."""
    items = types_payload.get("items") if isinstance(types_payload, dict) else None
    return {t["product_name"]: t for t in items or []
            if isinstance(t, dict) and t.get("product_name") and isinstance(t.get("num_gpu"), int) and t["num_gpu"] > 0}


def _failed(raw: RawResponse, why: str) -> RawResponse:
    """A response that arrived but cannot be read: a failed fetch, never an empty source."""
    return raw.model_copy(update={"ok": False, "error": why, "payload": None})


class CrusoeProvider(ShadeformCloud):
    name = "crusoe"
    cloud = "crusoe"

    def __init__(self, client: httpx.AsyncClient | None = None, access_key_id: str | None = None,
                 secret_key: str | None = None):
        super().__init__(client)
        # Both or neither: half a key pair cannot sign anything.
        self._keys = (access_key_id, secret_key) if access_key_id and secret_key else None

    async def _signed_get(self, url: str) -> RawResponse:
        kid, secret = self._keys
        return await self.get(url, headers={**auth_headers(kid, secret, "GET", url), "accept": "application/json"},
                              follow_redirects=False)

    async def fetch(self) -> list[RawResponse]:
        out = await super().fetch()  # prices (and the no-key capacity), from Shadeform
        if self._keys:
            out += await self._api()
        return out

    async def _api(self) -> list[RawResponse]:
        """Crusoe's own capacity, locations and type catalogue. Every failure is a recorded failed response."""
        types = await self._types()
        caps = await self._signed_get(CAPACITIES)
        items = caps.payload.get("items") if caps.ok and isinstance(caps.payload, dict) else None
        if caps.ok and not isinstance(items, list):
            caps = _failed(caps, "crusoe: capacities has no items list - response reshaped")
        elif caps.ok:
            gpus = gpu_types(types.payload) if types.ok else {}
            # GPU rows only, verbatim, when the catalogue says which types are GPUs; otherwise every row is kept.
            kept = [i for i in items if isinstance(i, dict) and (not gpus or i.get("type") in gpus)]
            caps = reshaped(caps, {"items": kept}) if kept else _failed(caps, "crusoe: capacities has no GPU rows")
        locs = await self._signed_get(LOCATIONS)
        if locs.ok and not (isinstance(locs.payload, dict) and isinstance(locs.payload.get("items"), list)):
            locs = _failed(locs, "crusoe: locations has no items list - response reshaped")
        return [caps, locs, types]

    async def _types(self) -> RawResponse:
        projects = await self._signed_get(PROJECTS)
        items = projects.payload.get("items") if projects.ok and isinstance(projects.payload, dict) else None
        pid = next((p.get("id") for p in items or [] if isinstance(p, dict) and p.get("id")), None)
        if not pid:
            why = projects.error if not projects.ok else "crusoe: no project to read the type catalogue from"
            return projects.model_copy(update={"endpoint": TYPES, "ok": False, "payload": None, "error": why})
        raw = await self._signed_get(TYPES.format(project_id=urllib.parse.quote(str(pid), safe="")))
        raw = raw.model_copy(update={"endpoint": TYPES})
        if raw.ok and not gpu_types(raw.payload):
            raw = _failed(raw, "crusoe: type catalogue has no GPU types - response reshaped")
        return raw

    @classmethod
    def normalize_capacity(cls, by_endpoint: "dict[str, list[RawSnapshot]]") -> list[CapacityObservation]:
        """Crusoe's own yes/no per (type, location) while its API is the fresh source, else Shadeform's."""
        rows, shade = find_endpoint(by_endpoint, CAPACITIES), find_endpoint(by_endpoint, SHADEFORM_ENDPOINT)
        if not rows or (shade and rows[0].fetched_at < shade[0].fetched_at - API_FRESH):
            return shadeform_capacity(cls.name, cls.cloud, by_endpoint)
        snap = rows[0]
        items = snap.payload.get("items") if isinstance(snap.payload, dict) else None
        if not isinstance(items, list):
            raise ValueError("crusoe: capacities has no items list - response reshaped")
        types_rows = find_endpoint(by_endpoint, TYPES)
        catalogue = gpu_types(types_rows[0].payload) if types_rows else {}
        # Shadeform's label for the same type, where it lists it: what the price listing maps.
        shade_types = shade[0].payload.get("instance_types") if shade and isinstance(shade[0].payload, dict) else None
        labels = {t.get("cloud_instance_type"): t for t in shade_types or []
                  if isinstance(t, dict) and isinstance(t.get("num_gpus"), int) and t["num_gpus"] > 0}

        merged: dict[tuple[str, str], list] = {}
        for i in items:
            if isinstance(i, dict) and i.get("type") and i.get("location"):
                merged.setdefault((i["type"], i["location"]), []).append(i.get("quantity"))
        out = []
        for (sku, location), quantities in merged.items():
            crusoe, listed = catalogue.get(sku), labels.get(sku)
            if crusoe:
                raw_name, count = str(crusoe.get("gpu_type") or ""), crusoe["num_gpu"]
            elif listed:
                raw_name, count = gpu_label(listed), listed["num_gpus"]
            else:
                continue  # a CPU type, or a type neither catalogue names
            known = [q for q in quantities if isinstance(q, int) and not isinstance(q, bool) and q >= 0]
            in_stock = any(q > 0 for q in known) if known else None
            out.append(CapacityObservation(
                provider=cls.name, sku=sku, region=location, country=_country(location),
                raw_gpu_name=raw_name, canonical_gpu_name=canonical.canonical_gpu_name(raw_name), gpu_count=count,
                in_stock=in_stock, available_count=None, count_unit=None,
                raw_value=f"quantity={','.join(sorted({str(q) for q in quantities}))}", source=CAPACITIES,
                observed_at=snap.fetched_at))
        return out
