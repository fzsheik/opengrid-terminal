"""The execution adapter interface: one class per provider API that can actually launch compute.

    check_availability(offer) -> Availability   live where the API permits, else says it could not
    quote(offer, availability) -> Quote         the price for THIS route (a quote, not an observation)
    provision(offer, availability, launch, name) -> Instance
    status(instance_id) -> Instance
    terminate(instance_id) -> Instance
    stop(instance_id) -> Instance               only where SUPPORTS_STOP

Everything returned is canonical (status words below, USD per GPU-hour). The
provider's own fields go into `.metadata`, which is stored for debugging in
deployments.provider_metadata and never returned by the public API.

LEVEL is what THIS adapter implements, and it is the only source of
`level_implemented` in routing/capabilities.py, so the registry cannot claim more
than the code does:
    1  availability check     2  provisioning     3  full lifecycle (status, terminate)

Adapters are synchronous (httpx.Client): they run from FastAPI's threadpool and
the tracker job's worker thread. Tests inject an httpx.MockTransport.

No adapter ever makes a provisioning call on its own initiative: only
routing.engine.route() calls provision(), and only behind its live-provisioning gate.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

# Canonical instance states an adapter maps the provider's words onto.
PROVISIONING, RUNNING, STOPPED, FAILED, TERMINATING, TERMINATED, UNKNOWN = (
    "provisioning", "running", "stopped", "failed", "terminating", "terminated", "unknown",
)

# AdapterError kinds. `timeout` and `unknown_state` during provision() mean the provider may have
# created an instance anyway: the engine never fails over after one of those.
AUTH, CAPACITY, TIMEOUT, RATE_LIMITED, NOT_FOUND, INVALID, PROVIDER_ERROR, CONFIG, UNKNOWN_STATE = (
    "auth", "capacity", "timeout", "rate_limited", "not_found", "invalid", "provider_error", "config",
    "unknown_state",
)


GENERIC_CAPACITY = ("insufficient capacity", "insufficient-capacity", "out of stock", "no capacity",
                    "not enough capacity")


class AdapterError(Exception):
    def __init__(self, kind: str, message: str, status_code: int | None = None, body: Any = None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.status_code = status_code
        self.body = body

    def as_dict(self) -> dict:
        return {"kind": self.kind, "message": self.message[:500], "status_code": self.status_code}


@dataclass
class Offer:
    """The listing a route chose, as the adapter needs it (from compute_listings)."""
    provider: str
    listing_id: str
    sku: str
    raw_gpu_name: str
    gpu: str                        # canonical
    gpu_count: int                  # GPUs per instance
    region: str | None
    price_per_gpu_hour: float | None        # observed market price
    price_per_instance_hour: float | None = None
    provider_tier: str | None = None
    observed_at: datetime | None = None
    want_region_group: str | None = None    # the route's region constraint, for picking a region


def pick_region(provider: str, regions: list[str], preferred: str | None, want_group: str | None) -> str | None:
    """A concrete region from the ones the provider says have capacity.

    The listing's own region first, then any region in the wanted group. With a group
    wanted and no region known to be in it, None: never launch outside the constraint.
    """
    regions = [r for r in regions if r]
    if want_group:
        try:
            from regions import region_group
        except ImportError:
            region_group = None
        if region_group is not None:
            regions = [r for r in regions if region_group(provider, r, None) == want_group]
        else:
            return None
    if preferred in regions:
        return preferred
    return regions[0] if regions else None


@dataclass
class Availability:
    available: bool | None          # None: the API gives no live answer
    live: bool                      # True when a provider API was called just now
    region: str | None = None       # a concrete region to launch in, when the API names one
    list_price_per_gpu_hour: float | None = None   # catalogue price read on this check
    note: str = ""
    checked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict = field(default_factory=dict)   # provider-specific (e.g. Vast ask id)


@dataclass
class Quote:
    price_per_gpu_hour: float
    price_per_instance_hour: float
    gpu_count: int
    basis: str                      # live_provider_api | observed_listing
    region: str | None = None
    currency: str = "USD"
    quoted_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class Instance:
    instance_id: str
    status: str                     # canonical, see above
    provider_status: str | None = None
    region: str | None = None
    price_per_gpu_hour: float | None = None   # execution price, when the provider reports one
    ip: str | None = None
    metadata: dict = field(default_factory=dict)


@dataclass
class LaunchSpec:
    """Canonical launch parameters. Adapters translate; unknown providers need nothing more.

    ssh_key   the NAME or ID of a key already registered with the provider account
    image     an OS image (VM providers) or container image (RunPod, Vast), provider's own name
    """
    name: str | None = None
    ssh_key: str | None = None
    image: str | None = None
    disk_gb: int | None = None
    env: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)   # operator-configured provider defaults only

    @classmethod
    def merged(cls, request: dict | None, defaults: dict | None) -> "LaunchSpec":
        """The request's fields over the operator's per-provider defaults."""
        d = dict(defaults or {})
        r = {k: v for k, v in (request or {}).items() if v not in (None, "", {}, [])}
        known = {"name", "ssh_key", "image", "disk_gb", "env"}
        extra = {k: v for k, v in d.items() if k not in known}
        base = {k: d.get(k) for k in known if k in d}
        base.update({k: v for k, v in r.items() if k in known})
        return cls(name=base.get("name"), ssh_key=base.get("ssh_key"), image=base.get("image"),
                   disk_gb=base.get("disk_gb"), env=dict(base.get("env") or {}), extra=extra)


class Adapter:
    """Base: credentials, an HTTP client, and error classification. Subclasses fill the verbs."""

    provider: str = ""
    LEVEL: int = 0
    SUPPORTS_STOP: bool = False
    BASE_URL: str = ""
    TIMEOUT_SECONDS: float = 30.0
    # Launch fields this provider cannot launch without (checked before any network call).
    REQUIRED_LAUNCH: tuple[str, ...] = ()
    # Credential fields needed (keys of the dict routing.credentials resolves).
    CREDENTIALS: tuple[str, ...] = ("api_key",)
    # False when the availability check reads a public endpoint (Shadeform, Vast).
    CHECK_NEEDS_CREDENTIALS: bool = True
    # Lower-case substrings in an error body that mean "no capacity", per provider.
    CAPACITY_SIGNALS: tuple[str, ...] = ()

    def __init__(self, credentials: dict | None, *, transport: httpx.BaseTransport | None = None,
                 provider: str | None = None):
        self.credentials = credentials or {}
        if provider:
            self.provider = provider
        self._transport = transport
        self._client: httpx.Client | None = None

    # -- HTTP ---------------------------------------------------------------

    def headers(self) -> dict:
        return {"accept": "application/json"}

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(base_url=self.BASE_URL, headers=self.headers(),
                                        timeout=self.TIMEOUT_SECONDS, transport=self._transport)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

    def request(self, method: str, path: str, **kw) -> Any:
        """One call; returns the decoded body or raises a classified AdapterError."""
        try:
            resp = self.client.request(method, path, **kw)
        except httpx.TimeoutException as exc:
            raise AdapterError(TIMEOUT, f"{self.provider}: {method} {path} timed out ({type(exc).__name__})")
        except httpx.HTTPError as exc:
            raise AdapterError(PROVIDER_ERROR, f"{self.provider}: {method} {path} failed: {type(exc).__name__}: {exc}")
        try:
            body = resp.json() if resp.content else None
        except ValueError:
            body = resp.text
        if resp.is_success:
            return body
        raise self.classify(resp.status_code, body, f"{method} {path}")

    def classify(self, status: int, body: Any, what: str) -> AdapterError:
        """Map an error response onto a kind. Subclasses add the provider's capacity signals."""
        msg = f"{self.provider}: {what} -> HTTP {status}: {_text(body)}"
        if self.is_capacity_error(status, body):
            return AdapterError(CAPACITY, msg, status, body)
        if status in (401, 403):
            return AdapterError(AUTH, msg, status, body)
        if status == 404:
            return AdapterError(NOT_FOUND, msg, status, body)
        if status == 429:
            return AdapterError(RATE_LIMITED, msg, status, body)
        if status in (400, 409, 422):
            return AdapterError(INVALID, msg, status, body)
        return AdapterError(PROVIDER_ERROR, msg, status, body)

    def is_capacity_error(self, status: int, body: Any) -> bool:
        t = _text(body).lower()
        return any(s in t for s in GENERIC_CAPACITY + self.CAPACITY_SIGNALS)

    # -- checks before any call ---------------------------------------------

    def missing_credentials(self) -> list[str]:
        return [c for c in self.CREDENTIALS if not self.credentials.get(c)]

    def missing_launch(self, launch: LaunchSpec, offer: Offer) -> list[str]:
        return [f for f in self.REQUIRED_LAUNCH if not getattr(launch, f, None)]

    # -- the verbs ----------------------------------------------------------

    def check_availability(self, offer: Offer) -> Availability:
        return Availability(available=None, live=False, note="this adapter has no live availability check")

    def quote(self, offer: Offer, availability: Availability) -> Quote:
        """The live catalogue price when the check read one, else the observed listing price."""
        if availability.list_price_per_gpu_hour is not None:
            p, basis = availability.list_price_per_gpu_hour, "live_provider_api"
        elif offer.price_per_gpu_hour is not None:
            p, basis = offer.price_per_gpu_hour, "observed_listing"
        else:
            raise AdapterError(INVALID, f"{self.provider}: no price to quote for {offer.listing_id}")
        return Quote(price_per_gpu_hour=round(p, 6), price_per_instance_hour=round(p * offer.gpu_count, 6),
                     gpu_count=offer.gpu_count, basis=basis, region=availability.region or offer.region)

    def provision(self, offer: Offer, availability: Availability, launch: LaunchSpec, name: str) -> Instance:
        raise AdapterError(CONFIG, f"{self.provider}: provisioning is not implemented")

    def status(self, instance_id: str) -> Instance:
        raise AdapterError(CONFIG, f"{self.provider}: status is not implemented")

    def terminate(self, instance_id: str) -> Instance:
        raise AdapterError(CONFIG, f"{self.provider}: terminate is not implemented")

    def stop(self, instance_id: str) -> Instance:
        raise AdapterError(CONFIG, f"{self.provider}: this provider's API has no stop; terminate instead")


def _text(body: Any) -> str:
    if body is None:
        return ""
    if isinstance(body, (dict, list)):
        import json
        return json.dumps(body)[:500]
    return str(body)[:500]


def timed(fn, *args, **kw):
    """(result_or_exception, latency_ms)."""
    t = time.perf_counter()
    try:
        out = fn(*args, **kw)
    except Exception as exc:  # noqa: BLE001 - the caller records every failure
        out = exc
    return out, int((time.perf_counter() - t) * 1000)
