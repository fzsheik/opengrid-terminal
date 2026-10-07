"""The execution adapter interface: one class per provider API that can actually launch compute.

    check_availability(offer) -> Availability       live where the API permits; raises AdapterError
    quote(offer, availability) -> Quote             the price for THIS route (a quote, not an observation)
    provision(offer, availability, launch, name) -> ProvisionResult     never raises
    status(instance_id) -> InstanceState            never raises; a failed read is state 'unknown'
    terminate(instance_id) -> TerminateResult       never raises
    stop(instance_id) -> TerminateResult            only where SUPPORTS_STOP (else outcome 'failed')
    list_instances() -> list[InstanceState]         ALL instances on the account; RAISES on any failure
                                                    (an empty list must only ever mean "there are none")
    find_instance(name) -> InstanceState | None     match by OpenGrid's og-* name/tag; RAISES on failure,
                                                    raises AdapterError(AMBIGUOUS) when >1 live match
    reported_cost(instance_id, start, end) -> CostReport   amount None + reason where the API has no billing

The result types live in routing/adapters/results.py. The rule they encode: a provider call that may
have created (or may not have deleted) real infrastructure never reads as a clean success or failure.

Launch outcome classification (provision only; see methodology/provider-capabilities.md):
    request never left (connect error, connect/pool timeout, pre-flight check)   -> rejected
    2xx with an instance id                                                       -> accepted
    2xx without an id, or a body that does not parse                              -> unknown
    4xx in REJECT_4XX (400/401/402/403/404/405/410/413/415/422/429)               -> rejected
    any other 4xx (408, 409 name conflict, ...)                                   -> unknown
    any 5xx                                                                       -> unknown, except a
        provider-DOCUMENTED capacity code (Verda 503 {"code":"service_unavailable"})
    read timeout, connection reset / protocol error after the request was written -> unknown
error_kind 'capacity' comes only from documented codes (is_capacity_error per adapter), never from a
broad substring of a 5xx body.

Every adapter declares CAPABILITIES (results.Capabilities, the founder's rows, with evidence URLs) and
VALIDATION_STATUS = 'SIMULATED': no adapter has made a real authenticated call. Only a recorded
validation cycle (routing/validation.py -> control.mark_validated) changes a provider's status, and
that lives in the DB (provider_execution_flags), never in this code.

Secrets (API keys, tokens, client secrets, SSH material, env values) never appear in raw_redacted,
messages, exceptions or logs: redact() strips secret-named keys and scrub() replaces any credential
value that a provider echoed back.

Adapters are synchronous (httpx.Client): they run from FastAPI's threadpool and job worker threads.
Tests inject an httpx.MockTransport. No adapter ever makes a provisioning call on its own initiative.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

from routing.adapters.results import (  # noqa: F401  (re-exported for the core)
    ActionResult, Capabilities, CostReport, InstanceState, ProvisionResult, TerminateResult, instance_name,
)

log = logging.getLogger("routing.adapters")

# Legacy canonical instance words (kept so older callers stay importable).
PROVISIONING, RUNNING, STOPPED, FAILED, TERMINATING, TERMINATED, UNKNOWN = (
    "provisioning", "running", "stopped", "failed", "terminating", "terminated", "unknown",
)

# AdapterError kinds.
AUTH, CAPACITY, TIMEOUT, RATE_LIMITED, NOT_FOUND, INVALID, PROVIDER_ERROR, CONFIG, UNKNOWN_STATE = (
    "auth", "capacity", "timeout", "rate_limited", "not_found", "invalid", "provider_error", "config",
    "unknown_state",
)
NETWORK, PARSE, QUOTA, AMBIGUOUS, SERVER = "network", "parse", "quota", "ambiguous", "server"

# 4xx codes that are a definitive "nothing was created" on a create call.
REJECT_4XX = {400, 401, 402, 403, 404, 405, 410, 413, 415, 422, 429}

SECRET_KEYS = re.compile(r"(api[_-]?key|token|secret|password|passwd|authorization|private|credential|"
                         r"public_key|ssh_key_material|^key$|^env$|user_data|script)", re.I)


class AdapterError(Exception):
    """A classified failure. `sent` says whether the request may have reached the provider:
    False (never left: connect error) | True (it was sent) | None (not an HTTP call)."""

    def __init__(self, kind: str, message: str, status_code: int | None = None, body: Any = None,
                 sent: bool | None = None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.status_code = status_code
        self.body = body
        self.sent = sent

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
    """Legacy result shape (pre-results.py). Kept importable; adapters no longer return it."""
    instance_id: str
    status: str
    provider_status: str | None = None
    region: str | None = None
    price_per_gpu_hour: float | None = None
    ip: str | None = None
    metadata: dict = field(default_factory=dict)


@dataclass
class LaunchSpec:
    """Canonical launch parameters. Adapters translate.

    ssh_key         the NAME or ID of a key ALREADY registered with the provider account. The core
                    decides when a reference is allowed (BYO credentials, or the operator key on a
                    validation deployment); customers never reference keys in OpenGrid's account.
    ssh_public_key  public key MATERIAL; where the provider can register keys (SSH_KEY_REGISTRATION)
                    the adapter registers it under the instance name (og-<deployment>) and uses it
    image           an OS image (VM providers) or container image (RunPod, Vast), provider's own name
    startup_script  cloud-init / onstart / start command, where the provider supports one
    """
    name: str | None = None
    ssh_key: str | None = None
    ssh_public_key: str | None = None
    image: str | None = None
    disk_gb: int | None = None
    startup_script: str | None = None
    env: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)   # operator-configured provider defaults only

    KNOWN = ("name", "ssh_key", "ssh_public_key", "image", "disk_gb", "startup_script", "env")

    @classmethod
    def merged(cls, request: dict | None, defaults: dict | None) -> "LaunchSpec":
        """The request's fields over the operator's per-provider defaults."""
        d = dict(defaults or {})
        r = {k: v for k, v in (request or {}).items() if v not in (None, "", {}, [])}
        known = set(cls.KNOWN)
        extra = {k: v for k, v in d.items() if k not in known}
        base = {k: d.get(k) for k in known if k in d}
        base.update({k: v for k, v in r.items() if k in known})
        return cls(name=base.get("name"), ssh_key=base.get("ssh_key"), ssh_public_key=base.get("ssh_public_key"),
                   image=base.get("image"), disk_gb=base.get("disk_gb"), startup_script=base.get("startup_script"),
                   env=dict(base.get("env") or {}), extra=extra)


def sanitize_name(name: str, max_len: int = 63) -> str:
    """Provider-safe instance name: lower-case [a-z0-9-], no leading/trailing '-'.

    Raises ValueError when the sanitised name would not fit: a truncated name could no longer be
    matched by find_instance, so it is refused instead of silently shortened.
    """
    n = re.sub(r"[^a-z0-9-]+", "-", (name or "").lower()).strip("-")
    n = re.sub(r"-{2,}", "-", n)
    if not n:
        raise ValueError("empty instance name")
    if len(n) > max_len:
        raise ValueError(f"instance name {n!r} is longer than {max_len} characters")
    return n


def redact(obj: Any, secrets: tuple = ()) -> Any:
    """A copy of a provider body/request safe to store: secret-named keys masked, echoed secrets scrubbed."""
    if isinstance(obj, dict):
        return {k: ("***" if SECRET_KEYS.search(str(k)) and v not in (None, "", [], {}) else redact(v, secrets))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v, secrets) for v in obj[:200]]
    if isinstance(obj, str):
        return scrub(obj, secrets)
    return obj


def scrub(text: str, secrets: tuple = ()) -> str:
    for sec in secrets:
        if sec and len(str(sec)) >= 6:
            text = text.replace(str(sec), "***")
    # Bearer tokens / obvious keys that may appear in echoed headers.
    return re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}", r"\1***", text)


def _text(body: Any) -> str:
    if body is None:
        return ""
    if isinstance(body, (dict, list)):
        return json.dumps(body, default=str)[:500]
    return str(body)[:500]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(v: Any) -> datetime | None:
    """ISO-8601 or epoch seconds -> aware UTC datetime; None when absent or unparseable."""
    if v in (None, ""):
        return None
    try:
        if isinstance(v, (int, float)):
            return datetime.fromtimestamp(float(v), tz=timezone.utc)
        s = str(v).strip().replace("Z", "+00:00")
        if " " in s and "T" not in s:
            s = s.replace(" ", "T", 1)
        t = datetime.fromisoformat(s)
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def _call_timeout() -> float:
    try:
        from config import settings
        return float(getattr(settings, "provider_call_timeout_seconds", 20) or 20)
    except Exception:  # noqa: BLE001
        return 20.0


class Adapter:
    """Base: credentials, an HTTP client, error classification and the result wrappers."""

    provider: str = ""
    LEVEL: int = 0
    SUPPORTS_STOP: bool = False
    BASE_URL: str = ""
    TIMEOUT_SECONDS: float | None = None      # None: settings.provider_call_timeout_seconds (default 20)
    REQUIRED_LAUNCH: tuple[str, ...] = ()
    CREDENTIALS: tuple[str, ...] = ("api_key",)
    # The provider name whose credentials this adapter authenticates with. Shadeform-routed clouds
    # (crusoe, denvr, latitude) set "shadeform": a native Crusoe key must never be sent to Shadeform.
    CREDENTIAL_PROVIDER: str | None = None
    CHECK_NEEDS_CREDENTIALS: bool = True
    NAME_MAX: int = 63
    SSH_KEY_REGISTRATION: bool = False        # can register ssh_public_key under the instance name
    FIND_RELIABLE: bool = True                # False: find_instance cannot match on a name/tag we set
    # Does the provider bill an instance it reports in an error state? (metering of "degraded" time)
    ERROR_STATE_BILLED: bool = True
    VALIDATION_STATUS: str = "SIMULATED"
    CAPABILITIES: Capabilities = Capabilities()

    def __init__(self, credentials: dict | None, *, transport: httpx.BaseTransport | None = None,
                 provider: str | None = None):
        self.credentials = credentials or {}
        if provider:
            self.provider = provider
        self._transport = transport
        self._client: httpx.Client | None = None
        self.log_context: dict = {}           # deployment_id / route_request_id for provider-call logs

    # -- secrets ------------------------------------------------------------

    def _secrets(self) -> tuple:
        return tuple(str(v) for v in self.credentials.values() if v)

    def redact(self, obj: Any) -> Any:
        return redact(obj, self._secrets())

    def scrub(self, text: str) -> str:
        return scrub(str(text), self._secrets())

    # -- HTTP ---------------------------------------------------------------

    def headers(self) -> dict:
        return {"accept": "application/json", "user-agent": "opengrid-terminal/0.1"}

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(base_url=self.BASE_URL, headers=self.headers(),
                                        timeout=self.TIMEOUT_SECONDS or _call_timeout(), transport=self._transport)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

    def request(self, method: str, path: str, *, allow_text: bool = False, **kw) -> Any:
        """One call; returns the decoded body or raises a classified AdapterError.

        A 2xx whose body is not JSON raises PARSE (sent=True) unless allow_text. Every call is logged
        (provider, method, path, status, latency; never headers or bodies).
        """
        t0 = time.perf_counter()
        status = None
        try:
            try:
                resp = self.client.request(method, path, **kw)
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.UnsupportedProtocol,
                    httpx.LocalProtocolError) as exc:
                # The request never reached the provider.
                kind = TIMEOUT if isinstance(exc, httpx.TimeoutException) else NETWORK
                raise AdapterError(kind, f"{self.provider}: {method} {path} not sent ({type(exc).__name__})",
                                   sent=False)
            except httpx.TimeoutException as exc:
                raise AdapterError(TIMEOUT, f"{self.provider}: {method} {path} timed out ({type(exc).__name__})",
                                   sent=True)
            except httpx.HTTPError as exc:
                raise AdapterError(NETWORK, f"{self.provider}: {method} {path} failed after send: "
                                            f"{type(exc).__name__}", sent=True)
            status = resp.status_code
            try:
                body = resp.json() if resp.content else None
            except ValueError:
                body = resp.text
                if resp.is_success and not allow_text:
                    raise AdapterError(PARSE, f"{self.provider}: {method} {path} -> HTTP {status} with a "
                                              f"non-JSON body: {self.scrub(_text(body))[:200]}", status, None, sent=True)
            if resp.is_success:
                return body
            raise self.classify(status, body, f"{method} {path}")
        finally:
            log.info("provider_call", extra={"provider": self.provider, "method": method, "path": path,
                                             "status": status, "latency_ms": int((time.perf_counter() - t0) * 1000),
                                             **self.log_context})

    def classify(self, status: int, body: Any, what: str) -> AdapterError:
        """Map an error response onto a kind. Bodies are scrubbed of credentials before they are kept."""
        safe = self.redact(body)
        msg = f"{self.provider}: {what} -> HTTP {status}: {_text(safe)}"
        if self.is_capacity_error(status, body):
            kind = CAPACITY
        elif status in (401, 403):
            kind = AUTH
        elif status == 402:
            kind = QUOTA
        elif status == 404:
            kind = NOT_FOUND
        elif status == 429:
            kind = RATE_LIMITED
        elif 400 <= status < 500:
            kind = INVALID
        else:
            kind = SERVER
        return AdapterError(kind, msg, status, safe, sent=True)

    def is_capacity_error(self, status: int, body: Any) -> bool:
        """Only provider-DOCUMENTED capacity codes. Subclasses override; the base knows none."""
        return False

    # -- checks before any call ---------------------------------------------

    def missing_credentials(self) -> list[str]:
        return [c for c in self.CREDENTIALS if not self.credentials.get(c)]

    def missing_launch(self, launch: LaunchSpec, offer: Offer) -> list[str]:
        missing = []
        for f in self.REQUIRED_LAUNCH:
            if f == "ssh_key":
                if not (launch.ssh_key or (self.SSH_KEY_REGISTRATION and launch.ssh_public_key)):
                    missing.append("ssh_key")
            elif not getattr(launch, f, None):
                missing.append(f)
        return missing

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

    def provision(self, offer: Offer, availability: Availability, launch: LaunchSpec, name: str) -> ProvisionResult:
        """Launch ONE instance named `name` (og-<deployment id>). Never raises."""
        try:
            safe_name = sanitize_name(name, self.NAME_MAX)
        except ValueError as exc:
            return ProvisionResult("rejected", error_kind="validation", message=f"{self.provider}: {exc}")
        missing = self.missing_credentials()
        if missing:
            return ProvisionResult("rejected", error_kind="auth", message=f"{self.provider}: missing credentials {missing}")
        missing = self.missing_launch(launch, offer)
        if missing:
            return ProvisionResult("rejected", error_kind="validation",
                                   message=f"{self.provider}: launch needs {', '.join(missing)}")
        try:
            r = self._provision(offer, availability, launch, safe_name)
        except AdapterError as exc:
            return self.provision_failure(exc)
        except Exception as exc:  # noqa: BLE001 - a bug in parsing after the call: existence unknown
            log.exception("%s: unexpected error in provision", self.provider)
            return ProvisionResult("unknown", error_kind="internal",
                                   message=self.scrub(f"{self.provider}: unexpected {type(exc).__name__}"))
        if r.outcome == "accepted" and not r.instance_id:
            return ProvisionResult("unknown", error_kind=PARSE, message=f"{self.provider}: accepted without an id",
                                   raw_redacted=r.raw_redacted, status_code=r.status_code)
        r.message = self.scrub(r.message)
        r.raw_redacted = self.redact(r.raw_redacted)
        return r

    def provision_failure(self, exc: AdapterError) -> ProvisionResult:
        """Classify a failed create call: rejected only when nothing can have been created."""
        msg = self.scrub(exc.message)
        raw = self.redact(exc.body) if exc.body is not None else None
        sc = exc.status_code
        if exc.sent is False or (exc.sent is None and sc is None):
            # Never reached the provider (or a pre-flight AdapterError raised before any call).
            return ProvisionResult("rejected", error_kind=_ekind(exc.kind), message=msg, raw_redacted=raw)
        if sc is None or 200 <= sc < 300:
            return ProvisionResult("unknown", error_kind=_ekind(exc.kind), message=msg, raw_redacted=raw,
                                   status_code=sc)
        if exc.kind == CAPACITY:
            # is_capacity_error only matches documented codes, including Verda's documented 503.
            return ProvisionResult("rejected", error_kind="capacity", message=msg, raw_redacted=raw, status_code=sc)
        if sc in REJECT_4XX:
            return ProvisionResult("rejected", error_kind=_ekind(exc.kind), message=msg, raw_redacted=raw,
                                   status_code=sc)
        return ProvisionResult("unknown", error_kind="server" if sc >= 500 else _ekind(exc.kind), message=msg,
                               raw_redacted=raw, status_code=sc)

    def _provision(self, offer, availability, launch, name) -> ProvisionResult:
        raise AdapterError(CONFIG, f"{self.provider}: provisioning is not implemented")

    def status(self, instance_id: str) -> InstanceState:
        """Never raises: a failed read is InstanceState('unknown', error_kind=...); 404 is 'not_found'."""
        try:
            st = self._status(instance_id)
        except AdapterError as exc:
            if exc.kind == NOT_FOUND:
                return InstanceState("not_found", instance_id=instance_id, observed_at=_now(),
                                     message=self.scrub(exc.message), raw_redacted=self.redact(exc.body))
            return InstanceState("unknown", instance_id=instance_id, error_kind=exc.kind, observed_at=_now(),
                                 message=self.scrub(exc.message))
        except Exception as exc:  # noqa: BLE001
            log.exception("%s: unexpected error in status", self.provider)
            return InstanceState("unknown", instance_id=instance_id, error_kind="internal", observed_at=_now(),
                                 message=f"{self.provider}: unexpected {type(exc).__name__}")
        st.observed_at = st.observed_at or _now()
        st.raw_redacted = self.redact(st.raw_redacted)
        return st

    def _status(self, instance_id: str) -> InstanceState:
        raise AdapterError(CONFIG, f"{self.provider}: status is not implemented")

    def terminate(self, instance_id: str) -> TerminateResult:
        """Never raises. 'accepted' is NOT 'terminated': confirm with status()/list_instances()."""
        try:
            r = self._terminate(instance_id)
        except AdapterError as exc:
            return self.action_failure(exc)
        except Exception as exc:  # noqa: BLE001
            log.exception("%s: unexpected error in terminate", self.provider)
            return TerminateResult("unknown", f"{self.provider}: unexpected {type(exc).__name__}", error_kind="internal")
        r.message = self.scrub(r.message)
        r.raw_redacted = self.redact(r.raw_redacted)
        return r

    def action_failure(self, exc: AdapterError) -> TerminateResult:
        msg = self.scrub(exc.message)
        sc = exc.status_code
        if exc.kind == NOT_FOUND:
            return TerminateResult("already_gone", msg, sc, error_kind=NOT_FOUND)
        if exc.sent is False:
            return TerminateResult("failed", msg, sc, error_kind=exc.kind, retryable=True)
        if sc is None or sc >= 500 or 200 <= sc < 300:
            return TerminateResult("unknown", msg, sc, error_kind=exc.kind)
        return TerminateResult("failed", msg, sc, error_kind=exc.kind, retryable=exc.kind in (RATE_LIMITED,))

    def _terminate(self, instance_id: str) -> TerminateResult:
        raise AdapterError(CONFIG, f"{self.provider}: terminate is not implemented")

    def stop(self, instance_id: str) -> TerminateResult:
        if not self.SUPPORTS_STOP:
            return TerminateResult("failed", f"{self.provider}: stop is not offered "
                                             f"({self.CAPABILITIES.stopped_billing[1] or 'terminate instead'})",
                                   error_kind="not_supported")
        try:
            r = self._stop(instance_id)
        except AdapterError as exc:
            return self.action_failure(exc)
        except Exception as exc:  # noqa: BLE001
            log.exception("%s: unexpected error in stop", self.provider)
            return TerminateResult("unknown", f"{self.provider}: unexpected {type(exc).__name__}", error_kind="internal")
        r.message = self.scrub(r.message)
        return r

    def _stop(self, instance_id: str) -> TerminateResult:
        raise AdapterError(CONFIG, f"{self.provider}: this provider's API has no stop; terminate instead")

    def list_instances(self) -> list[InstanceState]:
        """Every instance on the account (all pages). Raises AdapterError on any failure."""
        out = _unique(self._list())
        now = _now()
        for st in out:
            st.observed_at = st.observed_at or now
            st.raw_redacted = self.redact(st.raw_redacted)
        return out

    def _list(self) -> list[InstanceState]:
        raise AdapterError(CONFIG, f"{self.provider}: list_instances is not implemented")

    def find_instances(self, name: str) -> list[InstanceState]:
        """All instances carrying OpenGrid's name `name` (any state). Raises on failure."""
        n = sanitize_name(name, 255)
        found = [st for st in _unique(self._find(n)) if st.matches(n)]
        if not found and type(self)._find is not Adapter._find:
            # A server-side filter that finds nothing is re-checked against the full list, so
            # "absent" always means "absent from everything the account lists".
            found = [st for st in self.list_instances() if st.matches(n)]
        return found

    def _find(self, name: str) -> list[InstanceState]:
        """Candidates for `name`; subclasses use a server-side filter where the API has one."""
        return self.list_instances()

    def find_instance(self, name: str) -> InstanceState | None:
        """The one live instance named `name`, else the most recent ended one, else None.

        Raises AdapterError(AMBIGUOUS) when more than one live instance carries the name (a duplicate
        launch: reconciliation must surface it, never pick one silently)."""
        found = self.find_instances(name)
        live = [s for s in found if s.alive]
        if len(live) > 1:
            raise AdapterError(AMBIGUOUS, f"{self.provider}: {len(live)} live instances named {name}: "
                                          + ", ".join(str(s.instance_id) for s in live), body=[s.instance_id for s in live])
        if live:
            return live[0]
        return found[0] if found else None

    def reported_cost(self, instance_id: str, start: datetime | None, end: datetime | None) -> CostReport:
        """The provider's own cost for this instance, where its API exposes one."""
        return CostReport(amount_usd=None, reason=self.CAPABILITIES.reported_cost[1]
                          or f"{self.provider}'s API exposes no per-instance cost")

    # -- helpers for subclasses -----------------------------------------------

    def preflight(self, fn, *a, **kw):
        """Run a call that happens BEFORE the create request (token, ssh-key registration, lookups).

        Its failure cannot have created compute, so it is re-raised as not-sent: provision() then
        reports a clean 'rejected' instead of an ambiguous 'unknown'."""
        try:
            return fn(*a, **kw)
        except AdapterError as exc:
            exc.sent = False
            exc.message = f"before create: {exc.message}"
            raise

    def accepted(self, instance_id, body, *, status_code: int | None = None, message: str = "") -> ProvisionResult:
        return ProvisionResult("accepted", instance_id=str(instance_id), message=message,
                               raw_redacted=self.redact(body), status_code=status_code)

    def unknown(self, body, message: str, kind: str = PARSE) -> ProvisionResult:
        return ProvisionResult("unknown", error_kind=kind, message=self.scrub(message), raw_redacted=self.redact(body))


def _unique(states: list[InstanceState]) -> list[InstanceState]:
    """One entry per instance id. A provider that repeats an instance (overlapping pages, an eventually
    consistent list) must not look like a duplicate launch; two DIFFERENT ids with one name still do."""
    seen, out = set(), []
    for st in states:
        k = None if st.instance_id is None else str(st.instance_id)
        if k is not None and k in seen:
            continue
        if k is not None:
            seen.add(k)
        out.append(st)
    return out


def _ekind(kind: str) -> str:
    return {INVALID: "validation", CONFIG: "validation", RATE_LIMITED: "rate_limit", PROVIDER_ERROR: "server",
            UNKNOWN_STATE: "parse", NOT_FOUND: "validation"}.get(kind, kind)


def timed(fn, *args, **kw):
    """(result_or_exception, latency_ms)."""
    t = time.perf_counter()
    try:
        out = fn(*args, **kw)
    except Exception as exc:  # noqa: BLE001 - the caller records every failure
        out = exc
    return out, int((time.perf_counter() - t) * 1000)
