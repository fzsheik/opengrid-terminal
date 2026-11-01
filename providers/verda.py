"""Verda (formerly DataCrunch): public prices; Cloud API credentials add capacity.

Spec: https://api.verda.com/v1/openapi.json

Prices (anonymous, always):
    GET /v1/instance-types               every instance type, on-demand and spot price, both per whole instance

Stored raw, not read (anonymous, always; the reserved data model is undecided):
    GET /v1/long-term/periods/instances  the commitment ladder (1 month 2% ... 2 years 25% off)
    GET /v1/cluster-types                the cluster catalogue (8-144 GPU clusters, price per hour)

Capacity (only with VERDA_CLIENT_ID + VERDA_CLIENT_SECRET):
    POST /v1/oauth2/token                client_credentials -> bearer token. Never stored: the call bypasses
                                         Provider.call, which records the request body (the secret).
    GET  /v1/instance-availability       [{location_code, availabilities: [instance_type]}], asked twice:
                                         is_spot=false (on-demand) and is_spot=true (spot); `request` says which
    GET  /v1/locations                   location code -> country_code
    GET  /v1/cluster-availability        stored raw, not read (clusters are not listings)

A 401 on a keyed GET refreshes the token once per poll and retries that call; a later 401 is stored as is.
The capacity half fails open: a token or availability failure is stored as a failed response, and nothing it does
can stop the price response from being returned.

Capacity is yes/no per instance type, location and market: every GPU type in the catalogue, at every location the
availability response names, is in stock exactly when the location lists it. The sku is the listing_id
(`<type>` on-demand, `<type>:spot` spot) because Verda reports the two markets separately.
"""

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import httpx

import canonical
from models import CapacityObservation, ComputeListing, RawResponse
from providers.base import PollingPolicy, Provider, find_endpoint, reshaped, to_decimal

if TYPE_CHECKING:
    from tables import RawSnapshot

log = logging.getLogger(__name__)

BASE_URL = "https://api.verda.com"
ENDPOINT = "/v1/instance-types"
TOKEN = "/v1/oauth2/token"
AVAILABILITY = "/v1/instance-availability"
LOCATIONS = "/v1/locations"
CLUSTER_AVAILABILITY = "/v1/cluster-availability"
LADDER = "/v1/long-term/periods/instances"
CLUSTER_TYPES = "/v1/cluster-types"

# market -> (price field that makes the listing exist, listing_id suffix)
MARKETS = {False: ("price_per_hour", ""), True: ("spot_price", ":spot")}


def _catalogue(by_endpoint: "dict[str, list[RawSnapshot]]") -> "RawSnapshot | None":
    # Exact match: "/v1/instance-types" is a fragment of nothing else fetched, but be explicit.
    rows = by_endpoint.get(ENDPOINT) or []
    return rows[0] if rows else None


class VerdaProvider(Provider):
    name = "verda"
    # 3 anonymous requests per poll; 5 more with credentials (token, 2 availability, locations, clusters).
    polling = PollingPolicy(interval_seconds=900, timeout_seconds=60)

    def __init__(self, client: httpx.AsyncClient | None = None, client_id: str | None = None,
                 client_secret: str | None = None):
        super().__init__(
            client
            or httpx.AsyncClient(
                base_url=BASE_URL,
                headers={"accept": "application/json", "user-agent": "opengrid-terminal/0.1"},
                timeout=30,
            )
        )
        self._creds = (client_id, client_secret) if client_id and client_secret else None
        self._token: str | None = None
        self._refreshed = False

    async def fetch(self) -> list[RawResponse]:
        out = [await self.get(ENDPOINT), await self.get(LADDER), await self.get(CLUSTER_TYPES)]
        if self._creds:
            try:
                out += await self._capacity()
            except Exception as exc:  # fail open: prices above are already in hand
                log.warning("verda: capacity fetch failed (%s); prices unaffected", type(exc).__name__)
        return out

    # -- capacity (keyed) --

    async def _capacity(self) -> list[RawResponse]:
        self._refreshed = False
        failed = await self._refresh_token()
        if failed is not None:
            return [failed]
        out = []
        for spot in (False, True):
            raw = await self._keyed_get(AVAILABILITY, params={"is_spot": "true" if spot else "false"})
            out.append(reshaped(raw, raw.payload, request={"is_spot": spot}))
        out.append(await self._keyed_get(LOCATIONS))
        out.append(await self._keyed_get(CLUSTER_AVAILABILITY))
        return out

    async def _refresh_token(self) -> RawResponse | None:
        """Get a bearer token. Returns None on success, or a failed response (no body, no secret) to store."""
        self._token = None
        started = datetime.now(timezone.utc)
        client_id, client_secret = self._creds
        status, why = None, None
        try:
            resp = await self._client.post(TOKEN, json={"grant_type": "client_credentials", "client_id": client_id,
                                                        "client_secret": client_secret})
            status = resp.status_code
            body = resp.json() if resp.is_success else None
            token = body.get("access_token") if isinstance(body, dict) else None
            if isinstance(token, str) and token:
                self._token = token
                return None
            why = f"HTTP {status}" if not resp.is_success else "no access_token in the response"
        except Exception as exc:
            why = type(exc).__name__
        # Only the status and the reason: the token endpoint's body is never stored.
        return RawResponse(provider=self.name, endpoint=TOKEN, method="POST", fetched_at=started, status_code=status,
                           ok=False, error=f"verda: token request failed: {why}")

    async def _keyed_get(self, path: str, **kw) -> RawResponse:
        raw = await self.get(path, headers={"Authorization": f"Bearer {self._token}"}, **kw)
        if raw.status_code == 401 and not self._refreshed:  # expired token: refresh once per poll, then store
            self._refreshed = True                             # whatever comes back
            if await self._refresh_token() is None:
                raw = await self.get(path, headers={"Authorization": f"Bearer {self._token}"}, **kw)
        return raw

    @staticmethod
    def normalize_capacity(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[CapacityObservation]:
        """One yes/no row per GPU instance type, location and market (on-demand, and spot where it is priced)."""
        catalogue = _catalogue(by_endpoint)
        polls = {(s.request or {}).get("is_spot"): s for s in find_endpoint(by_endpoint, AVAILABILITY)}
        polls = {k: v for k, v in polls.items() if isinstance(k, bool)}
        if catalogue is None or not polls:
            return []
        if not isinstance(catalogue.payload, list):
            raise ValueError("verda: instance-types is not a list - reshaped")
        locs = find_endpoint(by_endpoint, LOCATIONS)
        country = {l.get("code"): l.get("country_code") for l in (locs[0].payload if locs else None) or []
                   if isinstance(l, dict)}
        out = []
        for spot, snap in polls.items():
            if not isinstance(snap.payload, list):
                raise ValueError("verda: instance-availability is not a list - reshaped")
            listed = {}
            for loc in snap.payload:
                code, types = (loc or {}).get("location_code"), (loc or {}).get("availabilities")
                if not code or not isinstance(types, list):
                    raise ValueError("verda: availability entry without location_code / availabilities - reshaped")
                listed[code] = set(types)
            price_field, suffix = MARKETS[spot]
            market = "spot" if spot else "on-demand"
            for it in catalogue.payload:
                count = (it.get("gpu") or {}).get("number_of_gpus") or 0
                price = to_decimal(it.get(price_field))
                if not count or price is None or price <= 0:
                    continue  # CPU-only, or no listing in this market to join to
                itype, raw_name = it["instance_type"], it.get("name") or it["instance_type"]
                for code, types in listed.items():
                    stocked = itype in types
                    out.append(CapacityObservation(
                        provider="verda", sku=itype + suffix, region=code, country=country.get(code),
                        raw_gpu_name=raw_name, canonical_gpu_name=canonical.canonical_gpu_name(raw_name),
                        gpu_count=count, in_stock=stocked,
                        raw_value=f"{'listed' if stocked else 'not listed'} at {code} ({market})",
                        source=AVAILABILITY + ("?is_spot=true" if spot else ""), observed_at=snap.fetched_at))
        return out

    @staticmethod
    def normalize(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ComputeListing]:
        """One instance type becomes an on-demand listing and a spot listing."""
        snapshot = _catalogue(by_endpoint)
        if snapshot is None:
            return []
        if not isinstance(snapshot.payload, list):
            log.warning("verda: unexpected payload shape %s", type(snapshot.payload).__name__)
            return []

        listings = []
        for it in snapshot.payload:
            count = (it.get("gpu") or {}).get("number_of_gpus") or 0
            if not count:
                continue  # CPU-only instance type
            if (it.get("currency") or "").lower() != "usd":
                log.warning("verda: %s is priced in %r, not USD; skipped", it.get("instance_type"), it.get("currency"))
                continue
            raw_name = it.get("name")
            itype = it["instance_type"]
            storage = it.get("storage") or {}
            for market, price_key, suffix in (("on_demand", "price_per_hour", ""), ("spot", "spot_price", ":spot")):
                price = to_decimal(it.get(price_key))
                if price is None or price <= 0:
                    continue
                listings.append(
                    ComputeListing(
                        provider="verda",
                        sku=itype,
                        listing_id=itype + suffix,
                        raw_gpu_name=raw_name,
                        canonical_gpu_name=canonical.canonical_gpu_name(raw_name),
                        gpu_count=count,
                        region=None,
                        country=None,
                        # Both prices are for the whole instance.
                        price_per_gpu_hour=price / count,
                        price_per_instance_hour=price,
                        currency="USD",
                        market_type=market,
                        # ".CC" types run in confidential-computing mode.
                        provider_tier="confidential" if itype.endswith(".CC") else None,
                        interruptible=market == "spot",
                        # Listings are provider-wide; per-location stock is in the capacity tables
                        # (normalize_capacity), so the price path never depends on the keyed calls.
                        available=None,
                        capacity=None,
                        capacity_unit=None,
                        vcpu=(it.get("cpu") or {}).get("number_of_cores"),
                        ram_gb=(it.get("memory") or {}).get("size_in_gigabytes"),
                        storage_gb=storage.get("size_in_gigabytes") if isinstance(storage, dict) else None,
                        observed_at=snapshot.fetched_at,
                    )
                )
        return listings
