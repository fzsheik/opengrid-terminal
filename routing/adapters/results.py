"""Explicit adapter results: the contract between adapters and the execution core.

Uncertainty is a first-class outcome. A provider call that may have created (or may not have
deleted) real infrastructure never reads as a clean success or failure.

    ProvisionResult.outcome
        accepted   the provider confirmed creation and returned an instance id
        rejected   the provider definitively created nothing (4xx capacity / validation / auth, no id)
        unknown    anything else after the request was sent: timeout, connection reset, 5xx,
                   unparseable 2xx. The instance may exist; reconcile via find_instance(name)
                   before ANY retry or failover.

    InstanceState.state
        pending | running | stopping | stopped | terminating | terminated | not_found | error | unknown
        error: the provider reports an error state on an instance that may still exist (and bill);
        unknown: the state could not be read (error_kind says why) or the word is unmapped.
        not_found from a single read is never proof of termination (see routing/reconcile.py).

    TerminateResult.outcome
        accepted       the provider took the delete; confirm with status/list before "terminated"
        already_gone   the provider says it does not exist (still confirm via list)
        failed         the provider refused; the instance is still there
        unknown        no usable answer; the instance may still be there
"""

from dataclasses import dataclass, field
from datetime import datetime

PROVISION_OUTCOMES = ("accepted", "rejected", "unknown")
INSTANCE_STATES = ("pending", "running", "stopping", "stopped", "terminating", "terminated", "not_found", "error",
                   "unknown")
TERMINATE_OUTCOMES = ("accepted", "already_gone", "failed", "unknown")


@dataclass
class ProvisionResult:
    outcome: str
    instance_id: str | None = None
    error_kind: str | None = None        # capacity | auth | quota | validation | rate_limit | timeout | server | network | parse
    message: str = ""
    raw_redacted: dict | list | None = None
    status_code: int | None = None


@dataclass
class InstanceState:
    state: str
    instance_id: str | None = None
    name: str | None = None
    provider_status: str | None = None
    region: str | None = None
    gpu: str | None = None
    gpu_count: int | None = None
    price_per_hour: float | None = None   # instance-hour price as the provider reports it
    created_at: datetime | None = None
    ip: str | None = None
    raw_redacted: dict | list | None = None
    labels: list = field(default_factory=list)   # tags/labels the provider returned (og-* name lives here too)
    error_kind: str | None = None        # set when state is 'unknown' because the read itself failed
    message: str = ""
    observed_at: datetime | None = None  # when OpenGrid read this (UTC)
    ended_at: datetime | None = None     # provider-reported termination time, when the provider gives one
    # Provider lifecycle timestamps (billing evidence; methodology/reconciliation.md "billable window").
    # created_at above = when the provider says the instance was created; running_at = when the provider
    # says it first ran / became healthy. time_fields names the provider field each came from, e.g.
    # {"running_at": "first_healthy"}; a timestamp the API does not expose stays None (never inferred).
    running_at: datetime | None = None
    time_fields: dict = field(default_factory=dict)
    ssh_key_names: list | None = None    # the keys the provider says can access the instance, where reported

    def matches(self, name: str) -> bool:
        """True when this instance carries OpenGrid's name `name` (as its name or a tag/label)."""
        return bool(name) and (self.name == name or name in (self.labels or []))

    @property
    def alive(self) -> bool:
        """May still exist and bill at the provider (anything but a confirmed end)."""
        return self.state not in ("terminated", "not_found")


@dataclass
class TerminateResult:
    outcome: str
    message: str = ""
    status_code: int | None = None
    error_kind: str | None = None        # e.g. still_creating (retry later), auth, network, server
    retryable: bool = False              # 'failed' that is expected to succeed later (Hyperstack CREATING)
    raw_redacted: dict | list | None = None


# stop() uses the same shape: accepted | failed | unknown (already_gone when the instance is gone).
ActionResult = TerminateResult


@dataclass
class ProviderKeyRef:
    """A per-deployment SSH key registered with the provider (register_ssh_key)."""
    key_id: str
    name: str
    fingerprint: str | None = None


@dataclass
class ProviderKey:
    """One SSH key listed on the provider account (list_ssh_keys). Never carries key material."""
    key_id: str
    name: str | None = None
    fingerprint: str | None = None
    created_at: datetime | None = None


class KeyRegistration(str):
    """SSH_KEY_REGISTRATION value: 'per_deployment' | 'account_only' | 'none'.

    A str (compare with ==), whose truth value is False for 'none' so older truthiness checks
    (`if cls.SSH_KEY_REGISTRATION:`) keep their meaning."""
    VALUES = ("per_deployment", "account_only", "none")

    def __new__(cls, v):
        if v is True:
            v = "per_deployment"
        elif v in (False, None):
            v = "none"
        if v not in cls.VALUES:
            raise ValueError(f"SSH_KEY_REGISTRATION must be one of {cls.VALUES}")
        return super().__new__(cls, v)

    def __bool__(self):
        return self != "none"


PER_DEPLOYMENT, ACCOUNT_ONLY, NO_REGISTRATION = (KeyRegistration("per_deployment"), KeyRegistration("account_only"),
                                                 KeyRegistration("none"))


def key_registration(cls_or_adapter) -> KeyRegistration:
    """The normalised SSH_KEY_REGISTRATION of an adapter class/instance (legacy bools accepted)."""
    return KeyRegistration(getattr(cls_or_adapter, "SSH_KEY_REGISTRATION", "none"))


@dataclass
class CostReport:
    amount_usd: float | None
    period_start: datetime | None = None
    period_end: datetime | None = None
    basis: str = ""                      # e.g. "provider billing API", "invoice line"
    raw_redacted: dict | list | None = None
    reason: str | None = None            # when amount is None: why the provider cannot tell us


def instance_name(deployment_id: str) -> str:
    """The name/tag OpenGrid gives every instance it launches: og-<deployment id>, [a-z0-9-] only."""
    import re
    return "og-" + re.sub(r"[^a-z0-9-]+", "-", deployment_id.lower()).strip("-")


@dataclass
class Capabilities:
    """Per-provider capability matrix (founder's rows). Each value: (answer, evidence)."""
    quote: tuple = ("NO", "")
    live_availability: tuple = ("NO", "")
    launch: tuple = ("NO", "")
    ssh_key_injection: tuple = ("NO", "")
    startup_script: tuple = ("NO", "")
    status: tuple = ("NO", "")
    stop: tuple = ("NO", "")
    terminate: tuple = ("NO", "")
    region_selection: tuple = ("NO", "")
    gpu_count_selection: tuple = ("NO", "")
    price_known_before_launch: tuple = ("NO", "")
    billing_unit: tuple = ("UNKNOWN", "")
    minimum_commitment: tuple = ("UNKNOWN", "")
    interruptible: tuple = ("UNKNOWN", "")
    name_tag_at_launch: tuple = ("NO", "")
    list_instances: tuple = ("NO", "")
    idempotency_token: tuple = ("NO", "")
    stopped_billing: tuple = ("UNKNOWN", "")   # full | storage_only | none | n/a (no stop)
    # Added rows (not in the founder's list, needed by reconciliation and cost reconciliation):
    find_by_name: tuple = ("NO", "")           # can OpenGrid find its instance by og-* name/tag? (reliability)
    reported_cost: tuple = ("NO", "")          # does the API expose a per-instance cost?
    error_semantics: tuple = ("UNKNOWN", "")   # how definitive the launch errors are
    # Does the provider ALWAYS install an account-level/default SSH key on a launch, whatever keys the launch
    # request names? YES | NO | UNKNOWN. The core refuses customer launches unless NO (or an admin override is
    # recorded in deployments.operator_access). Adapters never add account keys to a customer launch.
    forces_account_ssh_key: tuple = ("UNKNOWN", "")
    # When does provider billing start: running (first healthy) | created | UNKNOWN (billable window fallback)
    billing_starts: tuple = ("UNKNOWN", "")
    risks: list = field(default_factory=list)

    ROWS = ("quote", "live_availability", "launch", "ssh_key_injection", "startup_script", "status", "stop",
            "terminate", "region_selection", "gpu_count_selection", "price_known_before_launch", "billing_unit",
            "minimum_commitment", "interruptible", "name_tag_at_launch", "list_instances", "idempotency_token",
            "stopped_billing", "find_by_name", "reported_cost", "error_semantics", "forces_account_ssh_key",
            "billing_starts")

    def as_dict(self) -> dict:
        out = {r: {"value": getattr(self, r)[0], "evidence": getattr(self, r)[1]} for r in self.ROWS}
        out["risks"] = list(self.risks)
        return out
