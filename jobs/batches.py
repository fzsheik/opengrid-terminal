"""Batch fan-out: many independent jobs (a hyperparameter sweep, eval shards, batch work) spread across the cheapest
suitable GPU clouds at once, as ONE batch with one budget, one approval and one status (methodology/batches.md).

A batch adds no way to provision. Every shard is an ORDINARY job submitted through jobs.orchestrator.submit: the same
validate / select / quote / engine.route / approval / cost guards / single provision call per deployment / no failover
on an ambiguous outcome; its relaunches are jobs/relaunch.py's. What a batch adds:

    preview(body, who)      validate + expand the shards (explicit list or sweep grid, <= settings.batch_max_shards)
                            + the PLACEMENT PLAN: one workload-aware selection (workloads.select, every candidate) ->
                            shards spread over providers in best-execution rank order (price, availability now,
                            listed capacity, reliability where data exists) under the DIVERSIFICATION CAP (at most
                            max_shards_per_provider, default floor(max_share_per_provider x N) with share 50%, so one
                            cloud's outage cannot take the whole batch) and the provider / region allow + deny lists;
                            per-shard provider, price, estimate and reservation; the total vs "all on one cloud".
                            Never writes, never calls a provider.
    submit(body, who)       the same plan; refused unless every shard is placed and the reservations fit the budget;
                            batch + shard rows, then the first wave (<= concurrency.max_running) through the submit path
    advance(batch_id)       the batch_advance pass: follow each shard's job, apply the failure policy, apply the batch
                            approval to shards submitted later, submit queued shards into free slots
    approve(batch_id, who)  admin, ONE approval for the batch: records the terms it covers per shard (the exact quote of
                            a waiting shard, the planned price of a queued one) and approves every waiting shard through
                            the EXISTING engine.approve, which re-validates each one individually; a quote that moved
                            past quote_price_tolerance is re-quoted by the engine and that shard needs re-approval
    cancel(batch_id, who)   queued shards cancelled; submitted ones through the existing jobs cancel path

Money: max_total_usd covers ALL shards and ALL relaunch attempts
    * every submission and every relaunch first RESERVES its ceiling in batch_attempts, in a transaction holding
      pg_advisory_xact_lock(LOCK_BATCH, hashtext(batch_id)) (routing/guards.py's pattern: lock, count committed state,
      write, commit, only THEN act). committed = per reservation: reserved_usd while it has no job or its deployment
      is not reconciled (the max with the metered cost), the reconciled actual cost afterwards, 0 when it provably
      created nothing (released; no deployment and finished; rejected / provision_failed with no instance)
    * the reservation IS the attempt's limits.max_total_cost_usd, so the existing max-price cap (orchestrator._route,
      engine.approve over_max_price) binds the launch: price x billed runtime can never exceed it.
      reservation = min(per-shard limit, est_max_total x (1 + quote_price_tolerance)), rounded up to the cent
    * the account cost guards still apply to each shard, unchanged, on top
Double submission is impossible: UNIQUE(batch_id, shard_index); a queued shard is claimed (queued -> submitting) under
the batch lock; the job is attached to its reservation AND becomes the shard's current job by compare-and-set inside
the job-creating transaction (orchestrator._create_job -> attach), so the database refuses a second live job for one
shard (batch_shard_conflict) and every job of a batch is covered by a reservation.

Approval (SUPERVISED). A shard submitted after the approval (the queue) or re-quoted on expiry is approved by advance()
under the SAME approval only when its quote is on the approved provider at <= approved price x (1 + tolerance); else
it waits in needs_reapproval for the next batch approval (a relaunch on another provider always does). The approver
is re-verified each time (the operator, or a key that still exists, is not revoked / expired and is admin +
platform_admin). LIVE providers launch without approval, exactly as a plain job. Sandbox: every shard is a SIMULATED
job (no provider, no spend, no approval).
Failure policy: continue | stop_on_first_failure (queued shards skipped, submitted ones cancelled). Interrupted / lost
shards relaunch through jobs/relaunch.py (the shard's retry policy) with the batch budget as the outer ceiling
(relaunch_terms / reserve_relaunch); training failures are reported, never retried (an out_of_memory shard opted in to
retry.on_oom escalate is escalated once, jobs/continuation.py). A shard's resume, OOM escalation and move reserve
their ceiling in this ledger exactly like a relaunch (kinds resume | oom_escalation | move), before the submission;
a shard being moved stays `relaunching` until its successor is attached.
"""

from __future__ import annotations

import copy
import itertools
import logging
import math
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select, text, update

import normalize
from config import settings
from jobs import job as _register_job
from jobs import states as S
from store.batches import Batch, BatchAttempt, BatchEvent, BatchShard

log = logging.getLogger(__name__)

METHODOLOGY = "batches"
LOCK_BATCH = 0x4F470004            # pg_advisory_xact_lock(int4 namespace, hashtext(batch_id)); guards.py uses ..01-03
SHARD_STATES = ("queued", "submitting", "pending_approval", "needs_reapproval", "running", "relaunching",
                "succeeded", "failed", "cancelled", "skipped", "refused")
TERMINAL = ("succeeded", "failed", "cancelled", "skipped", "refused")
BATCH_STATES = ("active", "stopping", "cancelling", "finished")
# A refused submission of a queued shard is tried again on the next pass (the market moves), at most MAX_TRIES times.
RETRYABLE = ("price_changed", "gpu_unavailable", "quote_expired", "no_eligible_provider", "budget_exceeded",
             "placement_unavailable")
MAX_TRIES = 3
STALE_SUBMIT = timedelta(minutes=15)
NAME_MAX = 59                      # + "-s000" = the 64-character job name limit
MAX_GRID_VALUES = 64
MAX_TOTAL_USD = 10000.0
METRIC_RE = r"^[a-z][a-z0-9_]{0,63}$"


class BatchRefused(Exception):
    """A refusal the API returns as HTTP `status` with {code, message, ...}."""

    def __init__(self, code: str, message: str, status: int = 422, **extra):
        super().__init__(message)
        self.code, self.message, self.status, self.extra = code, message, status, extra

    def detail(self) -> dict:
        return {"code": self.code, "message": self.message, **self.extra}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _f(v):
    return None if v is None else float(v)


def _dec(v):
    return None if v is None else Decimal(str(round(float(v), 4)))


def _ceil_cents(x: float) -> float:
    return math.ceil(round(float(x) * 100, 6)) / 100


def _floor_cents(x: float) -> float:
    return math.floor(round(float(x) * 100, 6)) / 100


def new_batch_id() -> str:
    return "bat-" + secrets.token_hex(8)


def _actor(who) -> tuple[str, str]:
    aid = f"key:{who.key_id}" if who.key_id is not None else (who.kind or "operator")
    return ("admin" if who.has("admin") else "user"), aid


# --------------------------------------------------------------------------
# The request
# --------------------------------------------------------------------------

class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ShardIn(_M):
    config: dict[str, Any] = Field(default_factory=dict)


class Sweep(_M):
    grid: dict[str, list[Any]]
    max_shards: int | None = None


class Placement(_M):
    max_share_per_provider: float = 0.5
    max_shards_per_provider: int | None = None
    include_providers: list[str] | None = None
    exclude_providers: list[str] | None = None
    include_regions: list[str] | None = None
    exclude_regions: list[str] | None = None
    allow_interruptible: bool = False


class Budget(_M):
    max_total_usd: float


class Concurrency(_M):
    max_running: int = 4


class Policy(_M):
    on_failure: Literal["continue", "stop_on_first_failure"] = "continue"
    max_attempts: int = 1


class Metric(_M):
    name: str = "test_accuracy"
    goal: Literal["max", "min"] = "max"


class BatchLimits(_M):
    max_price_per_gpu_hour_usd: float
    max_runtime_minutes: int
    max_total_cost_usd: float | None = None      # per shard attempt; default: the template maximum, within the budget


class BatchRouting(_M):
    strategy: str = "BALANCED"
    weights: dict[str, float] | None = None


class BatchSpec(_M):
    name: str | None = None
    job_type: str = "training"
    template: str
    config: dict[str, Any] = Field(default_factory=dict)
    shards: list[ShardIn] | None = None
    sweep: Sweep | None = None
    resources: dict[str, Any] | None = None
    routing: BatchRouting = Field(default_factory=BatchRouting)
    limits: BatchLimits
    budget: Budget
    placement: Placement = Field(default_factory=Placement)
    concurrency: Concurrency = Field(default_factory=Concurrency)
    policy: Policy = Field(default_factory=Policy)
    metric: Metric = Field(default_factory=Metric)
    ssh_public_key: str | None = None
    sandbox: bool = False


def _loc(e: dict) -> str:
    return ".".join(str(p) for p in e.get("loc") or ()) or "body"


def _parse(body) -> BatchSpec:
    if not isinstance(body, dict):
        raise BatchRefused("invalid_request", "the batch request must be a JSON object")
    try:
        return BatchSpec.model_validate(body)
    except ValidationError as exc:
        errs = exc.errors()
        kind = errs[0].get("type")
        code = {"missing": "missing_field", "extra_forbidden": "unknown_field"}.get(kind, "invalid_request")
        raise BatchRefused(code, "; ".join(f"{_loc(e)}: {e.get('msg')}" for e in errs[:5]),
                           field=_loc(errs[0])) from None


def _providers(v, field: str) -> list[str] | None:
    from workloads.spec import MAX_EXCLUDED_PROVIDERS, PROVIDER_RE
    if v is None:
        return None
    out = sorted({str(p).strip().lower() for p in v})
    if not out or len(out) > MAX_EXCLUDED_PROVIDERS or any(not PROVIDER_RE.match(p) for p in out):
        raise BatchRefused("invalid_placement", f"{field} must be 1 to {MAX_EXCLUDED_PROVIDERS} provider ids "
                                                "[a-z0-9_-]", field=field)
    return out


def _regions(v, field: str) -> list[str] | None:
    if v is None:
        return None
    out = sorted({str(r).strip() for r in v if str(r).strip()})
    if not out or len(out) > 32 or any(len(r) > 48 for r in out):
        raise BatchRefused("invalid_placement", f"{field} must be 1 to 32 region names or region groups",
                           field=field)
    return out


def expand_grid(grid: dict, cap: int) -> list[dict]:
    """The cartesian product of `grid` in a stable order (keys sorted, values as given); refused above `cap`."""
    if not grid:
        raise BatchRefused("invalid_sweep", "sweep.grid needs at least one config field", field="sweep.grid")
    keys = sorted(grid)
    n = 1
    for k in keys:
        vals = grid[k]
        if not isinstance(vals, list) or not vals or len(vals) > MAX_GRID_VALUES:
            raise BatchRefused("invalid_sweep", f"sweep.grid.{k} must be a list of 1 to {MAX_GRID_VALUES} values",
                               field=f"sweep.grid.{k}")
        if len({repr(x) for x in vals}) != len(vals):
            raise BatchRefused("invalid_sweep", f"sweep.grid.{k} repeats a value", field=f"sweep.grid.{k}")
        n *= len(vals)
    if n > cap:   # checked BEFORE expanding: a huge grid is never materialised
        raise BatchRefused("too_many_shards", f"the sweep expands to {n} shards; at most {cap} per batch",
                           field="sweep", shards=n, max_shards=cap)
    return [dict(zip(keys, combo)) for combo in itertools.product(*(grid[k] for k in keys))]


def validate_request(body) -> dict:
    """The validated batch: {request, params[], base, per_shard_limit, ssh_public_key, fingerprint}. Every shard's
    job body is validated exactly like a job (workloads.spec.validate). Raises BatchRefused."""
    from workloads import templates as T
    from workloads.spec import NAME_RE, WorkloadError, validate

    s = _parse(body)
    hard = max(1, int(settings.batch_max_shards))
    tmpl = T.get_template(s.template)
    if tmpl is None:
        raise BatchRefused("unknown_template", f"template {s.template!r} is not allowlisted; available: "
                                                f"{', '.join(t['id'] for t in T.list_templates())}", field="template")
    name = s.name if s.name is not None else tmpl["preset"]["name"] + "-batch"
    if not NAME_RE.match(name or "") or len(name) > NAME_MAX:
        raise BatchRefused("invalid_name", f"name must be 1-{NAME_MAX} characters [a-z0-9._-], starting with a letter "
                                           "or digit", field="name")
    if s.shards is not None and s.sweep is not None:
        raise BatchRefused("shards_and_sweep", "give either shards (an explicit list) or sweep (a grid), not both",
                           field="shards")
    if s.shards is None and s.sweep is None:
        raise BatchRefused("no_shards", "give shards (a list of {config}) or sweep {grid}", field="shards")
    if s.shards is not None:
        if not s.shards:
            raise BatchRefused("no_shards", "shards is empty", field="shards")
        if len(s.shards) > hard:
            raise BatchRefused("too_many_shards", f"{len(s.shards)} shards; at most {hard} per batch",
                               field="shards", shards=len(s.shards), max_shards=hard)
        params = [dict(x.config) for x in s.shards]
    else:
        cap = hard
        if s.sweep.max_shards is not None:
            if s.sweep.max_shards < 1:
                raise BatchRefused("invalid_sweep", "sweep.max_shards must be >= 1", field="sweep.max_shards")
            cap = min(cap, int(s.sweep.max_shards))
        params = expand_grid(s.sweep.grid, cap)
    pl = s.placement
    if not (0 < pl.max_share_per_provider <= 1) or not math.isfinite(pl.max_share_per_provider):
        raise BatchRefused("invalid_placement", "placement.max_share_per_provider must be in (0, 1]",
                           field="placement.max_share_per_provider")
    if pl.max_shards_per_provider is not None and pl.max_shards_per_provider < 1:
        raise BatchRefused("invalid_placement", "placement.max_shards_per_provider must be >= 1",
                           field="placement.max_shards_per_provider")
    inc = _providers(pl.include_providers, "placement.include_providers")
    exc = _providers(pl.exclude_providers, "placement.exclude_providers")
    if inc and exc and set(inc) & set(exc):
        raise BatchRefused("invalid_placement", "a provider is both included and excluded", field="placement")
    budget = float(s.budget.max_total_usd)
    if not math.isfinite(budget) or budget <= 0 or budget > MAX_TOTAL_USD:
        raise BatchRefused("invalid_budget", f"budget.max_total_usd must be in (0, {MAX_TOTAL_USD:g}]",
                           field="budget.max_total_usd")
    if not 1 <= int(s.concurrency.max_running) <= hard:
        raise BatchRefused("invalid_concurrency", f"concurrency.max_running must be 1..{hard}",
                           field="concurrency.max_running")
    from workloads.spec import MAX_ATTEMPTS
    if not 1 <= int(s.policy.max_attempts) <= MAX_ATTEMPTS:
        raise BatchRefused("invalid_policy", f"policy.max_attempts must be 1..{MAX_ATTEMPTS} (1 = no relaunch)",
                           field="policy.max_attempts")
    import re
    if not re.match(METRIC_RE, s.metric.name):
        raise BatchRefused("invalid_metric", "metric.name must be a metric name such as test_accuracy",
                           field="metric.name")
    tmax = float(tmpl["limits_bounds"]["max_total_cost_usd"]["max"])
    per_shard = s.limits.max_total_cost_usd
    if per_shard is None:
        per_shard = min(tmax, budget)
    elif not math.isfinite(per_shard) or per_shard <= 0:
        raise BatchRefused("limit_out_of_bounds", "limits.max_total_cost_usd must be a positive number",
                           field="limits.max_total_cost_usd")
    per_shard = round(min(float(per_shard), budget), 2)
    resources = dict(s.resources or {})
    if pl.allow_interruptible:
        resources["allow_spot"] = True        # the template decides (pytorch-mnist: spot_not_supported)
    elif resources.get("allow_spot"):
        raise BatchRefused("invalid_placement", "interruptible capacity is placement.allow_interruptible",
                           field="resources.allow_spot")
    routing = {"strategy": s.routing.strategy, "weights": s.routing.weights}
    if inc:
        routing["include_providers"] = inc
    if exc:
        routing["exclude_providers"] = exc
    base = {"name": name, "job_type": s.job_type, "template": tmpl["id"], "config": dict(s.config),
            "resources": resources, "routing": routing,
            "limits": {"max_price_per_gpu_hour_usd": s.limits.max_price_per_gpu_hour_usd,
                       "max_total_cost_usd": per_shard, "max_runtime_minutes": s.limits.max_runtime_minutes},
            "ssh_public_key": s.ssh_public_key, "sandbox": bool(s.sandbox)}
    m = int(s.policy.max_attempts)
    retry = None if m <= 1 else {"max_attempts": m, "max_total_usd": round(min(per_shard * m, tmax * MAX_ATTEMPTS), 2)}
    fp, pk = None, None
    for i, p in enumerate(params):
        if not isinstance(p, dict):
            raise BatchRefused("invalid_shard", f"shards[{i}].config must be an object", field=f"shards[{i}]")
        try:
            v = validate(_body_of(base, p, i, retry=retry))
        except WorkloadError as exc:
            field = f"shards[{i}].{exc.field}" if exc.field else f"shards[{i}]"
            raise BatchRefused(exc.code, f"shard {i}: {exc.message}", exc.status, field=field, shard_index=i) from None
        params[i] = {k: v.request["config"][k] for k in p}      # normalised values (ints as ints)
        if i == 0:
            fp, pk = v.ssh_key_fingerprint, v.request["ssh_public_key"]
            base["name"], base["template"] = name, v.request["template"]
    request = {"name": name, "job_type": s.job_type, "template": tmpl["id"], "config": dict(s.config),
               "shards_total": len(params), "resources": resources, "routing": routing,
               "limits": {**base["limits"]}, "budget": {"max_total_usd": round(budget, 4)},
               "placement": {"max_share_per_provider": pl.max_share_per_provider,
                             "max_shards_per_provider": pl.max_shards_per_provider, "include_providers": inc,
                             "exclude_providers": exc, "include_regions": _regions(pl.include_regions,
                                                                                   "placement.include_regions"),
                             "exclude_regions": _regions(pl.exclude_regions, "placement.exclude_regions"),
                             "allow_interruptible": pl.allow_interruptible},
               "concurrency": {"max_running": int(s.concurrency.max_running)},
               "policy": {"on_failure": s.policy.on_failure, "max_attempts": m},
               "retry": retry, "metric": {"name": s.metric.name, "goal": s.metric.goal},
               "sandbox": bool(s.sandbox), "ssh_key_fingerprint": fp,
               "sweep": None if s.sweep is None else {"grid": s.sweep.grid, "max_shards": s.sweep.max_shards}}
    return {"request": request, "params": params, "base": base, "per_shard_limit": per_shard, "retry": retry,
            "ssh_public_key": pk, "fingerprint": fp, "template": tmpl}


def _body_of(base: dict, params: dict, idx: int, *, retry=None, provider: str | None = None,
             limit: float | None = None) -> dict:
    b = copy.deepcopy(base)
    b["config"] = {**b["config"], **params}
    b["name"] = f"{base['name']}-s{idx:03d}"
    if provider:
        b["routing"]["include_providers"] = [provider]
    if limit is not None:
        b["limits"]["max_total_cost_usd"] = round(float(limit), 2)
    if retry:
        b["retry"] = dict(retry)
    return b


def shard_body(spec: dict, params: dict, idx: int, *, provider: str | None = None,
               limit: float | None = None) -> dict:
    """The ordinary job request of shard `idx` from a stored batch spec."""
    base = {"name": spec["name"], "job_type": spec["job_type"], "template": spec["template"],
            "config": spec["config"], "resources": spec["resources"], "routing": spec["routing"],
            "limits": spec["limits"], "ssh_public_key": spec.get("ssh_public_key"), "sandbox": spec["sandbox"]}
    return _body_of(base, params, idx, retry=spec.get("retry"), provider=provider, limit=limit)


# --------------------------------------------------------------------------
# Placement
# --------------------------------------------------------------------------

def provider_cap(n: int, placement: dict) -> int:
    """Max shards per provider: max_shards_per_provider if given, else floor(share x N); at least 1."""
    by_share = max(1, math.floor(float(placement.get("max_share_per_provider") or 0.5) * n + 1e-9))
    explicit = placement.get("max_shards_per_provider")
    return max(1, min(by_share, int(explicit)) if explicit else by_share)


def _region_ok(c: dict, placement: dict) -> bool:
    inc, exc = placement.get("include_regions"), placement.get("exclude_regions")
    if not inc and not exc:
        return True
    try:
        import regions
        groups = {g.lower() for g in regions.region_groups(c.get("provider"), c.get("region"), c.get("country"))}
    except Exception:  # noqa: BLE001
        groups = set()
    names = {str(c.get("region") or "").lower()} | groups
    if inc and not names & {r.lower() for r in inc}:
        return False
    return not (exc and names & {r.lower() for r in exc})


def _capacity(cands: list[dict], gpu_count: int) -> dict:
    """(provider, listing_id) -> shards the listing's reported capacity can hold (None = not reported)."""
    keys = list({(c["provider"], c["listing_id"]) for c in cands})
    if not keys:
        return {}
    out = {}
    with normalize.SessionLocal() as s:
        for prov, lid, cap, unit in s.execute(text(
                "SELECT provider, listing_id, capacity, capacity_unit FROM compute_listings "
                "WHERE (provider, listing_id) IN (SELECT * FROM unnest(CAST(:p AS text[]), CAST(:l AS text[])))"),
                {"p": [k[0] for k in keys], "l": [k[1] for k in keys]}):
            if cap is None or int(cap) <= 0:
                continue
            out[(prov, lid)] = int(cap) // max(1, gpu_count) if (unit or "").lower() == "gpu" else int(cap)
    return out


def _factor(c: dict, name: str) -> dict:
    f = (c.get("factors") or {}).get(name) or {}
    return {"value": f.get("value"), "note": f.get("note")}


def _reservation(est_max: float, per_shard_limit: float) -> float:
    return round(min(float(per_shard_limit), _ceil_cents(float(est_max) * (1 + settings.quote_price_tolerance))), 2)


def _pool(sel: dict, sandbox: bool, placement: dict) -> list[dict]:
    cands = sel.get("candidates") or []
    if not sandbox:
        cands = [c for c in cands if c.get("executable")]
    return [c for c in cands if _region_ok(c, placement)]


def plan(V: dict, who, *, sel: dict | None = None) -> dict:
    """The placement plan of a validated batch (validate_request). DB reads only, never a provider call."""
    from workloads import select as W
    from workloads import templates as T

    req, params = V["request"], V["params"]
    n = len(params)
    sandbox = bool(req["sandbox"])
    pl = req["placement"]
    if sel is None:
        sel = W.select(_body_of(V["base"], params[0], 0, retry=V["retry"]), who, all_candidates=True)
    count = int(sel["request"]["resources"]["gpu_count"])
    runtime = int((sel.get("runtime") or {}).get("effective_max_runtime_minutes") or req["limits"]["max_runtime_minutes"])
    pool = _pool(sel, sandbox, pl)
    cap = provider_cap(n, pl)
    capacity = _capacity(pool, count)
    # per provider: its candidates in rank order (the provider's rank = its best candidate's)
    order: list[str] = []
    by_prov: dict[str, list[dict]] = {}
    for c in pool:
        if c["provider"] not in by_prov:
            order.append(c["provider"])
        by_prov.setdefault(c["provider"], []).append(c)
    # how many shards each provider takes: greedy in rank order, up to the cap and the listed capacity
    alloc: dict[str, list[dict]] = {}
    left = n
    for p in order:
        if left <= 0:
            break
        room, slots = min(cap, left), []
        for c in by_prov[p]:
            k = capacity.get((p, c["listing_id"]))
            take = room - len(slots) if k is None else min(k, room - len(slots))
            slots += [c] * max(0, take)
            if len(slots) >= room:
                break
        if slots:
            alloc[p] = slots
            left -= len(slots)
    # interleave providers over the shard order, so the first wave (and the queue) is spread too
    seq: list[dict] = []
    queues = {p: list(v) for p, v in alloc.items()}
    while any(queues.values()):
        for p in [p for p in order if queues.get(p)]:
            seq.append(queues[p].pop(0))
    tol, limit = settings.quote_price_tolerance, float(V["per_shard_limit"])
    billing: dict[str, dict] = {}
    shards, unplaced = [], []
    for i, p in enumerate(params):
        cfg = {**req["config"], **p}
        if i >= len(seq):
            why = ("no candidate after the provider / region lists" if not pool else
                   f"diversification cap: at most {cap} shard(s) per provider and only {len(order)} eligible "
                   "provider(s)" + (" (listed capacity counted)" if capacity else ""))
            unplaced.append({"shard_index": i, "config": p, "reason": why})
            shards.append({"shard_index": i, "config": p, "provider": None, "placed": False, "reason": why})
            continue
        c = seq[i]
        k = c.get("cost") or {}
        b = billing.setdefault(c["provider"], W.billing(c["provider"]))
        typ = W.cost(c["price_per_gpu_hour"], count, runtime, T.expected_minutes(V["template"], cfg), b)
        shards.append({"shard_index": i, "config": p, "placed": True, "provider": c["provider"],
                       "listing_id": c["listing_id"], "gpu": c["gpu"], "region": c.get("region"),
                       "price_per_gpu_hour": c["price_per_gpu_hour"], "price_kind": c.get("price_kind"),
                       "est_max_total_usd": k.get("est_max_total"), "est_typical_total_usd": typ["est_typical_total"],
                       "reservation_usd": _reservation(k.get("est_max_total") or 0, limit),
                       "approval_mode": (c.get("readiness") or {}).get("approval_mode"),
                       "executable": bool(c.get("executable")), "score": c.get("score")})
    placed = [s for s in shards if s["placed"]]
    tot = lambda key: round(sum(float(s.get(key) or 0) for s in placed), 4)  # noqa: E731
    providers = []
    for p in order:
        best = by_prov[p][0]
        k = best.get("cost") or {}
        mine = [s for s in placed if s["provider"] == p]
        providers.append({
            "provider": p, "shards": len(mine), "rank": best.get("rank"), "score": best.get("score"),
            "price_per_gpu_hour": best["price_per_gpu_hour"], "est_max_total_usd_per_shard": k.get("est_max_total"),
            "est_max_total_usd": round(sum(float(s["est_max_total_usd"] or 0) for s in mine), 4),
            "listings": len(by_prov[p]), "executable": bool(best.get("executable")),
            "approval_mode": (best.get("readiness") or {}).get("approval_mode"),
            "availability_now": _factor(best, "availability_now"), "reliability": _factor(best, "reliability"),
            "listed_capacity_shards": (sum(capacity.get((p, c["listing_id"])) or 0 for c in by_prov[p])
                                       if all((p, c["listing_id"]) in capacity for c in by_prov[p]) else None)})
    singles = []
    for p in order:
        k = by_prov[p][0].get("cost") or {}
        if k.get("est_max_total") is None:
            continue
        cap_p = providers[order.index(p)]["listed_capacity_shards"]
        singles.append({"provider": p, "est_max_total_usd": round(float(k["est_max_total"]) * n, 4),
                        "est_typical_total_usd": round(float(k.get("est_typical_total") or 0) * n, 4),
                        "fits_listed_capacity": None if cap_p is None else cap_p >= n})
    plan_max, plan_typ = tot("est_max_total_usd"), tot("est_typical_total_usd")
    one = singles[0] if singles else None
    savings = None if one is None or unplaced else round(one["est_max_total_usd"] - plan_max, 4)
    reserve = tot("reservation_usd")
    budget = float(req["budget"]["max_total_usd"])
    modes = {s["approval_mode"] for s in placed if s.get("approval_mode")}
    warnings = []
    if unplaced:
        warnings.append(f"{len(unplaced)} of {n} shard(s) cannot be placed under the diversification cap / lists: "
                        "raise placement.max_share_per_provider (1.0 allows all on one provider), add providers, "
                        "or relax the region lists")
    if reserve > budget + 1e-9:
        warnings.append(f"the shards' reservations total ${reserve:.2f}, over the ${budget:.2f} budget")
    if sandbox:
        warnings.append("sandbox: placement shown for information; every shard runs SIMULATED (no provider, no spend)")
    try:
        from routing import guards
        lim, use = guards.limits_for(who.account_id), guards.usage(who.account_id)
        mad = lim.get("max_active_deployments")
        run = int(req["concurrency"]["max_running"])
        if not sandbox and mad is not None and run > int(mad) - int(use.get("active_deployments") or 0):
            warnings.append(f"concurrency.max_running {run} is above what the account's max_active_deployments "
                            f"({mad}, {use.get('active_deployments')} active) allows: the cost guards will hold "
                            "the extra shards at approval")
    except Exception:  # noqa: BLE001 - informational
        pass
    return {
        "shards_total": n, "placed": len(placed), "unplaced": unplaced, "shards": shards, "providers": providers,
        "cap": {"max_shards_per_provider": cap, "max_share_per_provider": pl.get("max_share_per_provider"),
                "explicit_max_shards_per_provider": pl.get("max_shards_per_provider"),
                "basis": "max_shards_per_provider if given, else floor(max_share_per_provider x shards), at least 1: "
                         "one provider's outage cannot take out more than this many shards"},
        "totals": {"est_max_total_usd": plan_max, "est_typical_total_usd": plan_typ, "reservation_total_usd": reserve,
                   "budget_usd": budget, "fits_budget": reserve <= budget + 1e-9,
                   "relaunch_headroom_usd": round(max(0.0, budget - reserve), 4),
                   "per_shard_limit_usd": limit, "kind": "estimated"},
        "alternatives": {
            "all_on_one": one, "single_provider": singles,
            "savings_vs_all_on_one_usd": savings,
            "diversification_premium_usd": None if savings is None else round(max(0.0, -savings), 4),
            "note": ("all_on_one = every shard on the rank-1 provider (what N separate jobs would each pick), ignoring "
                     "its capacity; a positive saving means the spread plan is cheaper, a negative one is the price "
                     "of not depending on one cloud")},
        "would": ("simulated" if sandbox else "pending_approval" if "SUPERVISED" in modes else
                  "launch" if modes else "refused"),
        "strategy": sel.get("strategy"), "execution_mode": sel.get("execution_mode"), "as_of": sel.get("as_of"),
        "market_data_freshness": sel.get("market_data_freshness"), "runtime": sel.get("runtime"),
        "reservation_rule": (f"reservation = min(per-shard limit ${limit:.2f}, est_max_total x (1 + {tol:g} quote "
                             "tolerance)) rounded up to the cent; it becomes the shard's limits.max_total_cost_usd, "
                             "so the launch price cap binds it"),
        "warnings": warnings, "kind": "estimated",
    }


def _refusal(V: dict, pl: dict, sel: dict) -> BatchRefused | None:
    """Why a REAL batch cannot be submitted now (None = it can)."""
    if V["request"]["sandbox"]:
        return None
    if not pl["placed"]:
        from jobs import orchestrator as O
        r = O.refusal_for(sel) if not sel.get("execution_ready") else None
        if r is not None:
            return BatchRefused(r.code, r.message, 422, blockers=r.extra.get("blockers"),
                                hint="sandbox: true runs the whole batch SIMULATED (no provider, no spend)")
        return BatchRefused("placement_infeasible", "no shard can be placed", 422, unplaced=pl["unplaced"])
    if pl["unplaced"]:
        return BatchRefused("placement_infeasible", pl["warnings"][0], 422, unplaced=pl["unplaced"],
                            cap=pl["cap"]["max_shards_per_provider"])
    if not pl["totals"]["fits_budget"]:
        t = pl["totals"]
        return BatchRefused("batch_budget_exceeded", f"the {pl['shards_total']} shards reserve ${t['reservation_total_usd']:.2f}"
                                                     f" at the planned prices, over budget.max_total_usd "
                                                     f"${t['budget_usd']:.2f}", 422, totals=t)
    return None


def preview(body, who) -> dict:
    from workloads import select as W
    from workloads.spec import WorkloadError

    V = validate_request(body)
    try:
        sel = W.select(_body_of(V["base"], V["params"][0], 0, retry=V["retry"]), who, all_candidates=True)
    except WorkloadError as exc:
        raise BatchRefused(exc.code, exc.message, exc.status, field=exc.field) from None
    pl = plan(V, who, sel=sel)
    r = _refusal(V, pl, sel)
    sub = ({"can_submit": True, "would": pl["would"]} if r is None else
           {"can_submit": False, "would": "refused", "refusal": r.detail()})
    if not V["request"]["sandbox"] and r is None and pl["would"] == "pending_approval":
        sub["note"] = ("POST /v1/batches submits the first wave (pending_approval); ONE batch approval "
                       "(POST /v1/batches/{id}/approve, admin) covers every shard's exact quote; each shard is still "
                       "re-validated individually at launch")
    elif V["request"]["sandbox"]:
        sub["note"] = "SIMULATED: every shard is a sandbox job: no provider is called and nothing is billed"
    from jobs import relaunch
    sub["retry"] = relaunch.policy_preview({"retry": V["retry"], "sandbox": V["request"]["sandbox"]})
    return {"request": V["request"], "shards": [{"shard_index": i, "config": p} for i, p in enumerate(V["params"])],
            "plan": pl, "submission": sub, "template": sel.get("template"), "methodology": "/methodology/batches"}


# --------------------------------------------------------------------------
# Submit
# --------------------------------------------------------------------------

def _event(s, batch_id: str, kind: str, reason: str, *, shard_index: int | None = None, actor: str = "system",
           actor_id: str | None = None, evidence: dict | None = None) -> None:
    s.add(BatchEvent(batch_id=batch_id, at=_now(), kind=kind[:32], shard_index=shard_index, actor=actor,
                     actor_id=actor_id, reason=(reason or "")[:4000] or None,
                     evidence=S._jsonable(evidence) if evidence else None))


def submit(body, who) -> tuple[int, dict]:
    from routing import engine
    from workloads import select as W
    from workloads.spec import WorkloadError

    V = validate_request(body)
    try:
        sel = W.select(_body_of(V["base"], V["params"][0], 0, retry=V["retry"]), who, all_candidates=True)
    except WorkloadError as exc:
        raise BatchRefused(exc.code, exc.message, exc.status, field=exc.field) from None
    pl = plan(V, who, sel=sel)
    r = _refusal(V, pl, sel)
    if r is not None:
        raise r
    sandbox = bool(V["request"]["sandbox"])
    if not sandbox:
        try:
            engine._check_account_active(who)
        except Exception as exc:  # noqa: BLE001 - RouteRefused (HTTPException) with a code
            d = getattr(exc, "detail", None) or {}
            raise BatchRefused(d.get("code", "account_suspended"), d.get("message", str(exc)),
                               getattr(exc, "status_code", 403)) from None
    now = _now()
    batch_id = new_batch_id()
    actor, actor_id = _actor(who)
    spec = {**V["request"], "ssh_public_key": V["ssh_public_key"]}   # the public key only; never shown (public())
    with normalize.SessionLocal.begin() as s:
        s.add(Batch(batch_id=batch_id, account_id=who.account_id, key_id=who.key_id, principal_kind=who.kind,
                    name=V["request"]["name"], template=V["request"]["template"], sandbox=sandbox, spec=S._jsonable(spec),
                    plan=S._jsonable(pl), status="active", shards_total=len(V["params"]),
                    max_total_usd=_dec(V["request"]["budget"]["max_total_usd"]),
                    max_running=int(V["request"]["concurrency"]["max_running"]),
                    on_failure=V["request"]["policy"]["on_failure"],
                    approval_status="not_required" if sandbox else "none", created_at=now, updated_at=now))
        s.flush()
        for row in pl["shards"]:
            planned = None if not row.get("provider") else {k: row.get(k) for k in (
                "provider", "listing_id", "gpu", "region", "price_per_gpu_hour", "est_max_total_usd",
                "est_typical_total_usd", "reservation_usd")}
            s.add(BatchShard(batch_id=batch_id, shard_index=row["shard_index"], params=S._jsonable(row["config"]),
                             status="queued", planned=planned, attempts=0, tries=0, created_at=now, updated_at=now))
        _event(s, batch_id, "submitted", f"batch of {len(V['params'])} shard(s) submitted"
               + (" (SIMULATED sandbox)" if sandbox else ""), actor=actor, actor_id=actor_id,
               evidence={"shards": len(V["params"]), "budget_usd": V["request"]["budget"]["max_total_usd"],
                         "reservation_total_usd": pl["totals"]["reservation_total_usd"],
                         "providers": {p["provider"]: p["shards"] for p in pl["providers"] if p["shards"]}})
    advance(batch_id, who=who)
    return (201 if sandbox else 202), get(batch_id, who)


# --------------------------------------------------------------------------
# The ledger (budget) and reservations
# --------------------------------------------------------------------------

def _lock(s, batch_id: str) -> None:
    s.execute(text("SELECT pg_advisory_xact_lock(:ns, hashtext(:b))"), {"ns": LOCK_BATCH, "b": batch_id})


def _metered(dep) -> float:
    from routing import transactions as T
    tot = T.metered_totals(dep.deployment_id)
    return max(float(tot["usage_cost_usd"]), float(tot["slice_cost_usd"]))


def _attempt_usd(s, a: BatchAttempt) -> dict:
    """{committed, spent, basis, provider} of one reservation (module docstring, Money)."""
    from jobs import relaunch
    from store.jobs import Job
    from store.routing import Deployment

    reserved = float(a.reserved_usd or 0)
    if a.status == "released":
        return {"committed": 0.0, "spent": 0.0, "basis": "released: no job was created", "provider": None}
    if a.job_id is None:
        return {"committed": reserved, "spent": 0.0, "basis": "reserved: submission in progress", "provider": None}
    job = s.get(Job, a.job_id)
    if job is None:
        return {"committed": reserved, "spent": 0.0, "basis": "reserved: job unreadable", "provider": None}
    prov = (job.selection or {}).get("provider")
    if job.sandbox:
        return {"committed": 0.0, "spent": 0.0, "basis": "simulated: nothing billed", "provider": prov}
    dep = s.get(Deployment, job.deployment_id) if job.deployment_id else None
    if dep is None:
        if S.is_finished(job):
            return {"committed": 0.0, "spent": 0.0, "basis": "refused before any deployment", "provider": prov}
        return {"committed": reserved, "spent": 0.0, "basis": "reserved: submission in progress", "provider": prov}
    prov = dep.provider or prov
    sp = relaunch.attempt_spend(job, dep)
    if dep.status == "terminated" and dep.reconciled_at is not None:
        return {"committed": sp["usd"], "spent": sp["usd"], "basis": "reconciled actual cost", "provider": prov}
    if sp["usd"] == 0 and "nothing was created" in sp["basis"]:
        return {"committed": 0.0, "spent": 0.0, "basis": sp["basis"], "provider": prov}
    metered = _metered(dep)
    return {"committed": round(max(sp["usd"], reserved, metered), 4), "spent": round(metered, 4),
            "basis": "reserved until the deployment is reconciled", "provider": prov}


def ledger(s, batch_id: str) -> dict:
    rows = list(s.scalars(select(BatchAttempt).where(BatchAttempt.batch_id == batch_id).order_by(BatchAttempt.id)))
    items = []
    for a in rows:
        u = _attempt_usd(s, a)
        items.append({"attempt_id": a.id, "shard_index": a.shard_index, "kind": a.kind, "job_id": a.job_id,
                      "status": a.status, "reserved_usd": float(a.reserved_usd or 0), **u})
    return {"committed_usd": round(sum(i["committed"] for i in items), 4),
            "spent_usd": round(sum(i["spent"] for i in items), 4), "attempts": items}


def _busy(s, batch_id: str) -> int:
    """Shards occupying a concurrency slot: being submitted, waiting for a relaunch, or with an unfinished job."""
    return int(s.execute(text(
        "SELECT count(*) FROM batch_shards bs LEFT JOIN jobs j ON j.job_id = bs.job_id WHERE bs.batch_id = :b AND "
        "(bs.status IN ('submitting', 'relaunching') OR (j.job_id IS NOT NULL AND j.finished_at IS NULL))"),
        {"b": batch_id}).scalar() or 0)


def reserve(batch_id: str, shard_index: int, *, amount: float, kind: str, ref: str | None = None,
            prev_job_id: str | None = None, claim_shard: bool = False, check_slots: bool = False) -> dict:
    """Reserve `amount` against the batch budget, atomically: under the batch's advisory lock, count what is already
    committed (ledger) and refuse if committed + amount > max_total_usd. claim_shard: also move the shard queued ->
    submitting (only one claimer wins) and number the reservation by its try; check_slots: refuse when max_running
    shards are busy. Returns {attempt_id, reserved_usd, ref, replayed}. Raises BatchRefused."""
    now = _now()
    amount = round(max(0.0, float(amount)), 4)
    with normalize.SessionLocal.begin() as s:
        _lock(s, batch_id)
        b = s.get(Batch, batch_id)
        if b is None:
            raise BatchRefused("batch_not_found", "batch not found", 404)
        if ref is not None:
            old = s.scalars(select(BatchAttempt).where(BatchAttempt.batch_id == batch_id,
                                                       BatchAttempt.ref == ref)).first()
            if old is not None:     # a crashed earlier pass reserved it: reuse, never a second reservation
                if old.status == "reserved" and old.job_id is None:
                    return {"attempt_id": old.id, "reserved_usd": float(old.reserved_usd), "ref": ref, "replayed": True}
                raise BatchRefused("batch_attempt_used", f"reservation {ref} is already used", 409)
        if b.cancel_requested_at is not None or b.status != "active":
            raise BatchRefused("batch_not_active", f"batch {batch_id} is {b.status}", 409)
        if check_slots:
            busy = _busy(s, batch_id)
            if busy >= int(b.max_running):
                raise BatchRefused("no_free_slot", f"{busy} of {b.max_running} shards are running", 409)
        led = ledger(s, batch_id)
        cap = float(b.max_total_usd)
        if led["committed_usd"] + amount > cap + 1e-9:
            raise BatchRefused("batch_budget_exhausted",
                               f"${led['committed_usd']:.2f} committed + ${amount:.2f} for shard {shard_index} would "
                               f"pass the batch budget ${cap:.2f}", 422, committed_usd=led["committed_usd"],
                               amount_usd=amount, max_total_usd=cap)
        if claim_shard:
            got = s.execute(update(BatchShard).where(BatchShard.batch_id == batch_id,
                                                     BatchShard.shard_index == shard_index,
                                                     BatchShard.status == "queued")
                            .values(status="submitting", tries=BatchShard.tries + 1, updated_at=now)
                            .returning(BatchShard.tries, BatchShard.job_id)).first()
            if got is None:
                raise BatchRefused("shard_not_queued", f"shard {shard_index} is not queued", 409)
            ref = f"s{shard_index}:submit:{got.tries}"
            prev_job_id = got.job_id
        a = BatchAttempt(batch_id=batch_id, shard_index=shard_index, ref=ref, kind=kind, prev_job_id=prev_job_id,
                         reserved_usd=_dec(amount), status="reserved", created_at=now, updated_at=now)
        s.add(a)
        s.flush()
        return {"attempt_id": a.id, "reserved_usd": amount, "ref": ref, "replayed": False}


def release(attempt_id: int) -> None:
    """A reservation no job will use (refused before a job existed): it stops counting. Never releases an attached
    one (that job's own spend counts)."""
    with normalize.SessionLocal.begin() as s:
        s.execute(update(BatchAttempt).where(BatchAttempt.id == attempt_id, BatchAttempt.job_id.is_(None),
                                             BatchAttempt.status == "reserved")
                  .values(status="released", updated_at=_now()))


def attach(s, attempt_id: int, job_id: str) -> None:
    """In the job-creating transaction (orchestrator._create_job): the job takes its reservation and becomes the
    shard's current job by compare-and-set on the job it replaces. Raises JobRefused batch_shard_conflict (the job
    is rolled back) when the reservation is used or the shard moved on."""
    from jobs.orchestrator import JobRefused

    now = _now()
    a = s.execute(update(BatchAttempt).where(BatchAttempt.id == attempt_id, BatchAttempt.job_id.is_(None),
                                             BatchAttempt.status == "reserved")
                  .values(job_id=job_id, status="attached", updated_at=now)
                  .returning(BatchAttempt.batch_id, BatchAttempt.shard_index, BatchAttempt.prev_job_id)).first()
    if a is None:
        raise JobRefused("batch_shard_conflict", "this shard reservation is already used or released", 409)
    cond = BatchShard.job_id.is_(None) if a.prev_job_id is None else BatchShard.job_id == a.prev_job_id
    got = s.execute(update(BatchShard).where(BatchShard.batch_id == a.batch_id, BatchShard.shard_index == a.shard_index,
                                             cond)
                    .values(job_id=job_id, root_job_id=func.coalesce(BatchShard.root_job_id, job_id),
                            attempts=BatchShard.attempts + 1, updated_at=now)
                    .returning(BatchShard.id)).scalar()
    if got is None:
        raise JobRefused("batch_shard_conflict", f"shard {a.shard_index} of {a.batch_id} already has another job", 409)


# --------------------------------------------------------------------------
# Relaunch (jobs/relaunch.py): the batch is the outer ceiling
# --------------------------------------------------------------------------

def _attempt_of_job(s, job_id: str) -> BatchAttempt | None:
    return s.scalars(select(BatchAttempt).where(BatchAttempt.job_id == job_id)).first()


def relaunch_terms(job) -> dict | None:
    """None for an ordinary job. For a shard: the batch's remaining budget and lists; raises relaunch.Stop when the
    batch is cancelled / stopping / finished (no relaunch then)."""
    from jobs.relaunch import Stop
    with normalize.SessionLocal() as s:
        a = _attempt_of_job(s, job.job_id)
        if a is None:
            return None
        b = s.get(Batch, a.batch_id)
        if b is None:
            return None
        if b.cancel_requested_at is not None or b.status != "active":
            raise Stop("batch_stopped", f"batch {b.batch_id} is {b.status}: its shards are not relaunched")
        led = ledger(s, b.batch_id)
    cap = float(b.max_total_usd)
    pl = (b.spec or {}).get("placement") or {}
    return {"batch_id": b.batch_id, "shard_index": a.shard_index, "max_total_usd": cap,
            "per_shard_limit_usd": float(((b.spec or {}).get("limits") or {}).get("max_total_cost_usd") or cap),
            "committed_usd": led["committed_usd"], "remaining_usd": max(0.0, _floor_cents(cap - led["committed_usd"])),
            "include_providers": pl.get("include_providers"), "exclude_providers": pl.get("exclude_providers") or []}


def shape_relaunch_body(bt: dict, body: dict, *, remaining: float) -> None:
    """The successor follows the batch's provider lists, not the first attempt's placement pin, and its cost limit
    is the batch's per-shard limit (not the first attempt's reservation), within what remains."""
    body["limits"]["max_total_cost_usd"] = round(min(float(bt["per_shard_limit_usd"]), float(remaining)), 2)
    r = body.setdefault("routing", {})
    r.pop("include_providers", None)
    if bt.get("include_providers"):
        r["include_providers"] = list(bt["include_providers"])
    ex = sorted(set(r.get("exclude_providers") or ()) | set(bt.get("exclude_providers") or ()))
    if ex:
        r["exclude_providers"] = ex


def reserve_relaunch(bt: dict, failed_job_id: str, *, est: float, body: dict) -> dict:
    """The successor's reservation (it becomes its limits.max_total_cost_usd). Raises relaunch.Stop."""
    from jobs.relaunch import Stop
    limit = float(body["limits"]["max_total_cost_usd"])
    amount = round(min(limit, _ceil_cents(est * (1 + settings.quote_price_tolerance))), 2)
    try:
        r = reserve(bt["batch_id"], bt["shard_index"], amount=amount, kind="relaunch",
                    ref=f"s{bt['shard_index']}:relaunch:{failed_job_id}", prev_job_id=failed_job_id)
    except BatchRefused as e:
        raise Stop("batch_budget_exhausted" if e.code == "batch_budget_exhausted" else "batch_stopped",
                   f"batch {bt['batch_id']}: {e.message}") from None
    body["limits"]["max_total_cost_usd"] = round(min(limit, float(r["reserved_usd"])), 2)
    with normalize.SessionLocal.begin() as s:
        _event(s, bt["batch_id"], "relaunch_reserved", f"${r['reserved_usd']:.2f} reserved to relaunch shard "
               f"{bt['shard_index']} after {failed_job_id}", shard_index=bt["shard_index"],
               evidence={"failed_job_id": failed_job_id, "reserved_usd": r["reserved_usd"]})
    return {"attempt_id": r["attempt_id"], "reserved_usd": r["reserved_usd"], "batch_id": bt["batch_id"],
            "shard_index": bt["shard_index"]}


# --------------------------------------------------------------------------
# Shard state (DB only)
# --------------------------------------------------------------------------

def _load_batch(batch_id: str, who, *, admin_ok: bool = False) -> Batch:
    with normalize.SessionLocal() as s:
        b = s.get(Batch, batch_id)
    if b is None or (who is not None and who.account_id is not None and b.account_id != who.account_id
                     and not (admin_ok and who.has("admin"))):
        raise BatchRefused("batch_not_found", "batch not found", 404)
    return b


def _shards(s, batch_id: str) -> list[BatchShard]:
    return list(s.scalars(select(BatchShard).where(BatchShard.batch_id == batch_id).order_by(BatchShard.shard_index)))


def _jobs_deps(s, shards) -> tuple[dict, dict]:
    from store.jobs import Job
    from store.routing import Deployment
    ids = [sh.job_id for sh in shards if sh.job_id]
    jobs = {j.job_id: j for j in s.scalars(select(Job).where(Job.job_id.in_(ids)))} if ids else {}
    dids = [j.deployment_id for j in jobs.values() if j.deployment_id]
    deps = {d.deployment_id: d for d in s.scalars(select(Deployment).where(Deployment.deployment_id.in_(dids)))} \
        if dids else {}
    return jobs, deps


def _derive(b: Batch, sh: BatchShard, job, chain: dict | None) -> tuple[str, str | None, str | None]:
    """(status, reason_code, reason) of a shard from its current job."""
    stopping = b.cancel_requested_at is not None or b.status in ("stopping", "cancelling")
    if job is None:
        return sh.status, sh.reason_code, sh.reason
    if (chain or {}).get("continuation_pending") and not stopping:
        # a move is under way (jobs/continuation.py): the successor is coming, the shard is not over
        return "relaunching", "moving", "the shard is moving to another GPU"
    if S.is_finished(job) and job.deployment_id is None and sh.status in ("queued", "refused", "cancelled", "skipped"):
        return sh.status, sh.reason_code, sh.reason       # a refused submission: the shard's own state decides
    if job.outcome == "cancelled":
        return "cancelled", "cancelled", job.failure_reason or "cancelled"
    if job.outcome in ("succeeded", "simulated"):
        return "succeeded", None, None
    if job.outcome == "failed":
        rl = (chain or {}).get("relaunch") or {}
        st = rl.get("status")
        if st in ("pending", "claimed", "blocked", "relaunched") and not stopping:
            return "relaunching", job.failure_code, rl.get("reason") or "an automatic relaunch is due"
        if st == "stopped" and rl.get("code") == "batch_stopped" or (st in ("pending", "claimed") and stopping):
            return ("cancelled" if b.cancel_requested_at else "skipped"), "batch_stopped", \
                "not relaunched: the batch was " + ("cancelled" if b.cancel_requested_at else "stopped")
        if st == "stopped":
            return "failed", rl.get("code") or job.failure_code, rl.get("reason") or job.failure_reason
        return "failed", job.failure_code, job.failure_reason
    if job.status in ("pending_approval", "quote_expired"):
        return ("needs_reapproval" if sh.status == "needs_reapproval" else "pending_approval"), sh.reason_code, \
            sh.reason if sh.status == "needs_reapproval" else None
    if job.status in ("created", "validating", "quoted"):
        return "submitting", None, None
    return "running", None, None


def sync_batch(batch_id: str) -> Batch | None:
    """Recompute every shard's status from its job (DB only, never a provider call); recover a submission that
    crashed before creating a job; finish the batch when every shard is terminal and settled."""
    from jobs import relaunch
    now = _now()
    with normalize.SessionLocal() as s:
        b = s.get(Batch, batch_id)
        if b is None:
            return None
        shards = _shards(s, batch_id)
        jobs, _ = _jobs_deps(s, shards)
    chains = relaunch.chain_infos(list(jobs.values()))
    from jobs import continuation
    for jid in continuation.pending_succession(list(jobs)):
        chains[jid] = {**(chains.get(jid) or {}), "continuation_pending": True}
    with normalize.SessionLocal.begin() as s:
        b = s.get(Batch, batch_id, with_for_update=True)
        for sh0 in shards:
            sh = s.get(BatchShard, sh0.id, with_for_update=True)
            if sh.job_id != sh0.job_id:      # moved on concurrently: the next pass follows the new job
                continue
            job = jobs.get(sh.job_id) if sh.job_id else None
            if sh.status == "submitting" and job is None and now - sh.updated_at > STALE_SUBMIT:
                # crashed before any job existed (attach is atomic with the job): nothing was created
                for a in s.scalars(select(BatchAttempt).where(BatchAttempt.batch_id == batch_id,
                                                              BatchAttempt.shard_index == sh.shard_index,
                                                              BatchAttempt.job_id.is_(None),
                                                              BatchAttempt.status == "reserved")):
                    a.status, a.updated_at = "released", now
                sh.status, sh.updated_at = "queued", now
                _event(s, batch_id, "shard_recovered", "a submission that never created a job was put back in the "
                                                       "queue", shard_index=sh.shard_index)
                continue
            st, code, why = _derive(b, sh, job, chains.get(sh.job_id) if sh.job_id else None)
            if st != sh.status or code != sh.reason_code:
                if st != sh.status:
                    _event(s, batch_id, "shard_" + st, f"shard {sh.shard_index}: {sh.status} -> {st}"
                           + (f" ({code})" if code else ""), shard_index=sh.shard_index,
                           evidence={"job_id": sh.job_id, "job_status": job.status if job else None})
                sh.status, sh.reason_code, sh.reason, sh.updated_at = st, code, why, now
            settled = job is None or S.is_finished(job)
            if sh.status in TERMINAL and settled and sh.finished_at is None:
                sh.finished_at = now
        if b.finished_at is None:
            rows = _shards(s, batch_id)
            if rows and all(r.status in TERMINAL and r.finished_at is not None for r in rows):
                st = [r.status for r in rows]
                b.outcome = ("simulated" if b.sandbox else "cancelled" if b.cancel_requested_at else
                             "stopped" if b.status == "stopping" else
                             "succeeded" if all(x == "succeeded" for x in st) else
                             "partial" if "succeeded" in st else "failed")
                b.status, b.finished_at, b.updated_at = "finished", now, now
                _event(s, batch_id, "finished", f"batch finished: {b.outcome} ({st.count('succeeded')} of {len(st)} "
                                                "shard(s) succeeded)",
                       evidence={"counts": {k: st.count(k) for k in set(st)}})
    with normalize.SessionLocal() as s:
        return s.get(Batch, batch_id)


# --------------------------------------------------------------------------
# advance: the queue, the failure policy, the batch approval for later shards
# --------------------------------------------------------------------------

def _system_who(b: Batch):
    """The principal of OpenGrid's own spend-reducing actions on a batch (cancel / skip): the batch's account, only
    route:execute. Never used to submit or approve."""
    from accounts.auth import Principal
    return Principal(kind="batch", account_id=b.account_id, key_id=None, scopes=frozenset({"route:execute"}))


def _wind_down(b: Batch, who=None) -> dict:
    """Queued shards -> cancelled (cancel) / skipped (stop); every shard job that has no outcome yet -> the EXISTING
    jobs cancel path (before launch: reject; after: terminate). Idempotent."""
    from jobs import orchestrator as O
    from store.jobs import Job

    cancel = b.cancel_requested_at is not None
    to = "cancelled" if cancel else "skipped"
    now = _now()
    out = {"queued": 0, "jobs": []}
    with normalize.SessionLocal.begin() as s:
        _lock(s, b.batch_id)          # no reservation / claim races this: a claimed shard is no longer queued
        for sh in s.scalars(select(BatchShard).where(BatchShard.batch_id == b.batch_id,
                                                     BatchShard.status == "queued")):
            sh.status, sh.reason_code, sh.updated_at = to, "batch_cancelled" if cancel else "batch_stopped", now
            sh.reason = "the batch was cancelled" if cancel else f"the batch stopped: {b.stop_reason or 'policy'}"
            out["queued"] += 1
        if out["queued"]:
            _event(s, b.batch_id, "queue_" + to, f"{out['queued']} queued shard(s) {to}")
        ids = [sh.job_id for sh in _shards(s, b.batch_id) if sh.job_id]
        live = list(s.scalars(select(Job.job_id).where(Job.job_id.in_(ids), Job.outcome.is_(None),
                                                       Job.finished_at.is_(None),
                                                       Job.cancel_requested_at.is_(None)))) if ids else []
    actor = who or _system_who(b)
    for jid in live:
        try:
            code, res = O.cancel(jid, actor)
            out["jobs"].append({"job_id": jid, "status": res.get("status"), "cancel": res.get("cancel")})
        except O.JobRefused as r:     # e.g. submission_in_progress: the next pass retries
            out["jobs"].append({"job_id": jid, "error": r.code})
        except Exception as exc:  # noqa: BLE001 - one job never blocks the others
            log.exception("batch %s: cancelling %s failed", b.batch_id, jid)
            out["jobs"].append({"job_id": jid, "error": type(exc).__name__})
    if out["jobs"]:
        with normalize.SessionLocal.begin() as s:
            _event(s, b.batch_id, "cancel_jobs", f"{len(out['jobs'])} shard job(s) cancelled through the jobs cancel "
                                                 "path", evidence={"jobs": out["jobs"]})
    return out


def _approver(b: Batch):
    """The batch approver, re-verified now. Raises BatchRefused."""
    from accounts.auth import ALL, OPERATOR, Principal
    if b.approver_kind == "operator" and b.approver_key_id is None:
        return OPERATOR
    if b.approver_kind != "api_key" or b.approver_key_id is None:
        raise BatchRefused("approver_unavailable", "the batch approver cannot be re-verified")
    from accounts import keys
    from store.accounts import ApiKey
    from store.security import ApiKeyLineage
    with normalize.SessionLocal() as s:
        k = s.get(ApiKey, b.approver_key_id)
        lin = s.get(ApiKeyLineage, b.approver_key_id)
        revoked = k is not None and s.execute(keys._ANCESTOR_REVOKED, {"k": k.id}).first() is not None
    if k is None or k.revoked_at is not None or revoked or (k.expires_at is not None and k.expires_at <= _now()):
        raise BatchRefused("approver_key_revoked", "the key that approved the batch is revoked or expired")
    scopes = frozenset(x for x in (k.scopes or []) if x != ALL)
    if not (lin is not None and lin.platform_admin and "admin" in scopes):
        raise BatchRefused("approver_not_admin", "the key that approved the batch is no longer an admin key")
    return Principal(kind="api_key", account_id=k.account_id, key_id=k.id, scopes=scopes, platform_admin=True)


def _approve_shard(b: Batch, sh_index: int, job, dep, who, *, reason: str) -> dict:
    """The EXISTING engine.approve for one shard (re-validated individually). A re-quote beyond the tolerance (or any
    refusal) leaves the shard in needs_reapproval with the engine's reason; nothing is launched silently."""
    from fastapi import HTTPException

    from jobs import sync
    from routing import engine

    out = {"shard_index": sh_index, "job_id": job.job_id, "quote_id": dep.quote_id, "provider": dep.provider}
    try:
        code, res = engine.approve(job.route_request_id, who, quote_id=dep.quote_id, reason=reason)
        launch = res.get("launch") or {}
        out.update(result="already_approved" if res.get("already_approved") else
                   "launched" if launch.get("launched") else "approved", status=res.get("status"), http_code=code)
    except HTTPException as exc:
        d = exc.detail if isinstance(exc.detail, dict) else {"code": "approve_refused", "message": str(exc.detail)}
        newq = d.get("new_quote")
        expired = d.get("code") == "quote_expired"
        out.update(result="requoted" if newq and expired else "needs_reapproval", code=d.get("code"),
                   message=d.get("message"), new_quote=None if not newq else {
                       k: newq.get(k) for k in ("quote_id", "quote_price_per_gpu_hour", "est_total_cost", "expires_at")})
        if not (newq and expired):        # a price move / guard / anything else: a person decides again
            with normalize.SessionLocal.begin() as s:
                row = s.scalars(select(BatchShard).where(BatchShard.batch_id == b.batch_id,
                                                         BatchShard.shard_index == sh_index)).first()
                row.status, row.reason_code, row.updated_at = "needs_reapproval", d.get("code"), _now()
                row.reason = f"{d.get('message')}"[:2000]
                _event(s, b.batch_id, "needs_reapproval", f"shard {sh_index}: {d.get('code')}: {d.get('message')}",
                       shard_index=sh_index, evidence={"new_quote": out.get("new_quote"), "job_id": job.job_id})
    try:
        sync.sync_job(job.job_id, enforce=False)
    except Exception:  # noqa: BLE001
        log.exception("batch %s: sync of %s after approval failed", b.batch_id, job.job_id)
    return out


def _waiting(s, b: Batch) -> list[tuple]:
    """(shard, job, dep, quote) of every shard whose deployment waits for an approval."""
    from routing import quotes
    shards = _shards(s, b.batch_id)
    jobs, deps = _jobs_deps(s, shards)
    out = []
    for sh in shards:
        job = jobs.get(sh.job_id) if sh.job_id else None
        dep = deps.get(job.deployment_id) if job is not None and job.deployment_id else None
        if dep is None or dep.status not in ("pending_approval", "quote_expired") or not job.route_request_id:
            continue
        out.append((sh, job, dep, quotes.get(dep.quote_id) or {}))
    return out


def _within(terms: dict | None, dep, q: dict) -> bool:
    if not terms or terms.get("provider") != dep.provider or terms.get("price_per_gpu_hour") is None:
        return False
    price = q.get("quote_price_per_gpu_hour")
    return price is not None and float(price) <= float(terms["price_per_gpu_hour"]) * (
        1 + settings.quote_price_tolerance) + 1e-9


def _apply_batch_approval(b: Batch) -> list[dict]:
    """advance(): a shard submitted (or re-quoted) after the batch approval launches under it ONLY within the
    approved terms; otherwise it waits in needs_reapproval for the next batch approval."""
    with normalize.SessionLocal() as s:
        waiting = [w for w in _waiting(s, b) if w[0].status == "pending_approval"]
    if not waiting:
        return []
    try:
        who = _approver(b)
    except BatchRefused as e:
        who, why = None, e
    out = []
    for sh, job, dep, q in waiting:
        if who is None or not _within(sh.approved, dep, q):
            reason = (f"the batch approver cannot be re-verified ({why.code})" if who is None else
                      f"quote ${float(q.get('quote_price_per_gpu_hour') or 0):.4f}/GPU-h on {dep.provider} is outside "
                      f"the approved terms ({(sh.approved or {}).get('provider') or 'none'} at "
                      f"${float((sh.approved or {}).get('price_per_gpu_hour') or 0):.4f} + "
                      f"{settings.quote_price_tolerance:.0%})")
            with normalize.SessionLocal.begin() as s:
                row = s.get(BatchShard, sh.id, with_for_update=True)
                if row.status == "pending_approval":
                    row.status, row.reason_code, row.reason, row.updated_at = "needs_reapproval", \
                        "outside_approved_terms" if who is not None else why.code, reason, _now()
                    _event(s, b.batch_id, "needs_reapproval", f"shard {sh.shard_index}: {reason}",
                           shard_index=sh.shard_index, evidence={"job_id": job.job_id, "quote_id": dep.quote_id})
            out.append({"shard_index": sh.shard_index, "result": "needs_reapproval", "reason": reason})
            continue
        out.append(_approve_shard(b, sh.shard_index, job, dep, who,
                                  reason=f"batch {b.batch_id} approval by {b.approved_by} (within the approved terms)"))
    return out


def _place_now(b: Batch, sh: BatchShard, who) -> dict | None:
    """Where shard `sh` goes NOW: the batch's lists + the diversification cap over the other unfinished shards; the
    planned provider while it is still within the tolerance of its planned price, else the best candidate."""
    from workloads import select as W
    from workloads.spec import WorkloadError

    spec = b.spec or {}
    with normalize.SessionLocal() as s:
        shards = _shards(s, b.batch_id)
        jobs, deps = _jobs_deps(s, shards)
    load: dict[str, int] = {}
    for o in shards:
        if o.shard_index == sh.shard_index or o.status in TERMINAL:
            continue
        j = jobs.get(o.job_id) if o.job_id else None
        d = deps.get(j.deployment_id) if j is not None and j.deployment_id else None
        p = (d.provider if d is not None else None) or ((j.selection or {}).get("provider") if j is not None else None) \
            or (o.planned or {}).get("provider")
        if p:
            load[p] = load.get(p, 0) + 1
    cap = int(((b.plan or {}).get("cap") or {}).get("max_shards_per_provider") or provider_cap(b.shards_total,
                                                                                                spec["placement"]))
    body = shard_body(spec, sh.params or {}, sh.shard_index)
    full = sorted(p for p, n in load.items() if n >= cap)
    if full:
        body["routing"]["exclude_providers"] = sorted(set(body["routing"].get("exclude_providers") or ()) | set(full))
    try:
        sel = W.select(body, who, all_candidates=True)
    except WorkloadError as exc:
        raise BatchRefused(exc.code, exc.message, exc.status) from None
    pool = _pool(sel, bool(b.sandbox), spec["placement"])
    if not pool:
        if b.sandbox:
            return None
        from jobs import orchestrator as O
        r = O.refusal_for(sel)
        code = "placement_unavailable" if r.code == "no_eligible_provider" else r.code
        raise BatchRefused(code, (f"no candidate under the diversification cap ({', '.join(full)} full); " if full else
                                  "") + r.message)
    pp = sh.planned or {}
    for c in pool:
        if c["provider"] == pp.get("provider") and pp.get("price_per_gpu_hour") is not None and \
                c["price_per_gpu_hour"] <= float(pp["price_per_gpu_hour"]) * (1 + settings.quote_price_tolerance) + 1e-9:
            return c
    return pool[0]


def _set_shard(batch_id: str, idx: int, status: str, code: str | None, reason: str | None, *, kind: str,
               evidence: dict | None = None) -> None:
    with normalize.SessionLocal.begin() as s:
        row = s.scalars(select(BatchShard).where(BatchShard.batch_id == batch_id,
                                                 BatchShard.shard_index == idx)).first()
        row.status, row.reason_code, row.reason, row.updated_at = status, code, (reason or "")[:2000] or None, _now()
        _event(s, batch_id, kind, f"shard {idx}: {code}: {reason}" if code else f"shard {idx}: {reason}",
               shard_index=idx, evidence=evidence)


def _refused_shard(b: Batch, sh: BatchShard, code: str, message: str, tries: int) -> None:
    """A refused submission: back to the queue (the market moves) while retryable and under MAX_TRIES, else final."""
    final = code not in RETRYABLE or tries >= MAX_TRIES
    _set_shard(b.batch_id, sh.shard_index, "refused" if final else "queued", code,
               message + ("" if final else f" (try {tries} of {MAX_TRIES}; tried again on the next pass)"),
               kind="shard_refused" if final else "shard_retry")


def _submit_shard(b: Batch, sh: BatchShard, who) -> str:
    """One queued shard -> placement -> reservation (lock: budget + slot + claim) -> the ordinary job submit."""
    from jobs import orchestrator as O

    try:
        cand = _place_now(b, sh, who)
    except BatchRefused as e:
        _refused_shard(b, sh, e.code, e.message, int(sh.tries or 0) + 1)
        with normalize.SessionLocal.begin() as s:
            s.execute(update(BatchShard).where(BatchShard.id == sh.id).values(tries=BatchShard.tries + 1))
        return "refused"
    limit = float(b.spec["limits"]["max_total_cost_usd"])
    amount = 0.0 if b.sandbox else _reservation(((cand or {}).get("cost") or {}).get("est_max_total") or limit, limit)
    try:
        att = reserve(b.batch_id, sh.shard_index, amount=amount, kind="submit", claim_shard=True, check_slots=True)
    except BatchRefused as e:
        if e.code == "batch_budget_exhausted":
            with normalize.SessionLocal() as s:
                pending = [a for a in ledger(s, b.batch_id)["attempts"] if a["basis"].startswith("reserved")]
            if pending:
                return "budget_wait"      # reserved attempts may settle below their reservation
            _set_shard(b.batch_id, sh.shard_index, "refused", e.code, e.message, kind="shard_refused")
            return "refused"
        return e.code
    body = shard_body(b.spec, sh.params or {}, sh.shard_index, provider=(cand or {}).get("provider"),
                      limit=amount if not b.sandbox else None)
    try:
        code, out = O.submit(body, who, batch={"attempt_id": att["attempt_id"], "batch_id": b.batch_id,
                                               "shard_index": sh.shard_index})
    except O.JobRefused as r:
        release(att["attempt_id"])
        with normalize.SessionLocal() as s:
            row = s.get(BatchShard, sh.id)
        if r.extra.get("job_id") and row.job_id == r.extra["job_id"]:
            from store.jobs import Job
            with normalize.SessionLocal() as s:
                j = s.get(Job, row.job_id)
            if j is not None and j.deployment_id is not None and not S.is_finished(j):
                return "submitted"        # a deployment exists: the job (job_sync) owns it from here
        _refused_shard(b, row, r.code, r.message, int(row.tries or 0))
        return "refused"
    except Exception:
        release(att["attempt_id"])
        with normalize.SessionLocal.begin() as s:
            s.execute(update(BatchShard).where(BatchShard.id == sh.id, BatchShard.status == "submitting")
                      .values(status="queued", updated_at=_now()))
        raise
    with normalize.SessionLocal.begin() as s:
        _event(s, b.batch_id, "shard_submitted", f"shard {sh.shard_index} submitted as {out['job_id']}"
               + (f" on {cand['provider']}" if cand else "") + (f", ${amount:.2f} reserved" if amount else ""),
               shard_index=sh.shard_index, evidence={"job_id": out["job_id"], "status": out.get("status"),
                                                     "provider": (cand or {}).get("provider"),
                                                     "reserved_usd": amount, "http_code": code})
    return "submitted"


def _fill(b: Batch, who=None) -> dict:
    from jobs import relaunch
    out = {"submitted": 0, "refused": 0, "stopped_on": None}
    if who is None:
        try:
            who = relaunch.owner(b)       # the batch's original principal, re-verified (never escalated)
        except relaunch.Stop as e:
            with normalize.SessionLocal() as s:
                queued = [sh for sh in _shards(s, b.batch_id) if sh.status == "queued"]
            for sh in queued:
                _set_shard(b.batch_id, sh.shard_index, "refused", e.code, e.message, kind="shard_refused")
            out["refused"] = len(queued)
            return out
    with normalize.SessionLocal() as s:     # each queued shard is tried at most once per pass
        queue = sorted((sh for sh in _shards(s, b.batch_id) if sh.status == "queued"),
                       key=lambda sh: (sh.tries, sh.shard_index))
    for nxt in queue:
        r = _submit_shard(b, nxt, who)
        if r == "submitted":
            out["submitted"] += 1
            if b.sandbox:
                sync_batch(b.batch_id)    # a simulation finishes at once: its slot is free again
            continue
        if r == "refused":
            out["refused"] += 1
            continue
        out["stopped_on"] = r            # no_free_slot | budget_wait | batch_not_active | shard_not_queued
        break
    return out


def advance(batch_id: str, *, who=None) -> dict:
    """One pass over a batch. `who`: the submitting principal (the first wave); None = the batch's owner, re-verified."""
    b = sync_batch(batch_id)
    if b is None or b.finished_at is not None:
        return {"batch_id": batch_id, "finished": True}
    out: dict = {"batch_id": batch_id}
    if b.status == "active" and b.on_failure == "stop_on_first_failure" and b.cancel_requested_at is None:
        with normalize.SessionLocal() as s:
            failed = [sh for sh in _shards(s, batch_id) if sh.status == "failed"]
        if failed:
            with normalize.SessionLocal.begin() as s:
                row = s.get(Batch, batch_id, with_for_update=True)
                if row.status == "active":
                    f0 = failed[0]
                    row.status, row.updated_at = "stopping", _now()
                    row.stop_reason = f"shard {f0.shard_index} failed ({f0.reason_code or 'failed'}): " \
                                      "stop_on_first_failure"
                    _event(s, batch_id, "stopping", row.stop_reason, shard_index=f0.shard_index)
            b = sync_batch(batch_id)
    if b.cancel_requested_at is not None or b.status in ("stopping", "cancelling"):
        out["wind_down"] = _wind_down(b)
    else:
        if settings.batch_queue_enabled:
            out["queue"] = _fill(b, who)
        else:
            out["queue"] = {"disabled": "batch_queue_enabled is off: queued shards wait"}
        if b.approval_status == "approved" and not b.sandbox:    # after the fill: it covers the shards just queued
            sync_batch(batch_id)
            out["approvals"] = _apply_batch_approval(b)
    sync_batch(batch_id)
    return out


def open_batch_ids(limit: int = 500) -> list[str]:
    with normalize.SessionLocal() as s:
        return list(s.scalars(select(Batch.batch_id).where(Batch.finished_at.is_(None))
                              .order_by(Batch.created_at).limit(limit)))


def run_all() -> dict:
    out = {"batches": 0, "errors": 0}
    for bid in open_batch_ids():
        out["batches"] += 1
        try:
            advance(bid)
        except Exception:  # noqa: BLE001 - one batch never blocks the others
            out["errors"] += 1
            log.exception("batch %s: advance failed", bid)
    return out


@_register_job("batch_advance", every_seconds=settings.batch_advance_interval_seconds, initial_delay_seconds=50)
def _batch_advance():
    return run_all()


# --------------------------------------------------------------------------
# approve / cancel
# --------------------------------------------------------------------------

def approve(batch_id: str, who, *, quotes: dict | None = None, reason: str | None = None) -> tuple[int, dict]:
    """ONE approval for the batch (admin). quotes {shard_index: quote_id}: when given, only the listed waiting shards
    are approved and each must still be on that exact quote (else quote_mismatch for that shard)."""
    b = _load_batch(batch_id, who, admin_ok=True)
    if b.sandbox:
        raise BatchRefused("approval_not_required", "a sandbox batch is SIMULATED: nothing to approve", 409)
    b = sync_batch(batch_id)
    if b.finished_at is not None or b.status != "active" or b.cancel_requested_at is not None:
        raise BatchRefused("batch_not_active", f"batch {batch_id} is {b.status}: nothing to approve", 409)
    quotes = {str(k): str(v) for k, v in (quotes or {}).items()} if quotes is not None else None
    actor, actor_id = _actor(who)
    with normalize.SessionLocal() as s:
        waiting = _waiting(s, b)
        shards = _shards(s, batch_id)
    terms, skipped = {}, []
    for sh, job, dep, q in waiting:
        k = str(sh.shard_index)
        if quotes is not None and k not in quotes:
            skipped.append({"shard_index": sh.shard_index, "result": "not_in_request", "quote_id": dep.quote_id})
            continue
        if quotes is not None and quotes[k] != dep.quote_id:
            skipped.append({"shard_index": sh.shard_index, "result": "quote_mismatch", "quote_id": dep.quote_id,
                            "requested_quote_id": quotes[k]})
            continue
        terms[sh.shard_index] = {"provider": dep.provider, "price_per_gpu_hour": q.get("quote_price_per_gpu_hour"),
                                 "quote_id": dep.quote_id, "est_total_cost": q.get("est_total_cost"), "basis": "quote"}
    for sh in shards:
        if sh.status == "queued" and (sh.planned or {}).get("provider"):
            terms[sh.shard_index] = {"provider": sh.planned["provider"],
                                     "price_per_gpu_hour": sh.planned.get("price_per_gpu_hour"),
                                     "est_total_cost": sh.planned.get("reservation_usd"), "basis": "plan"}
    now = _now()
    with normalize.SessionLocal.begin() as s:
        row = s.get(Batch, batch_id, with_for_update=True)
        row.approval_status, row.approved_by, row.approved_at = "approved", actor_id, now
        row.approver_kind, row.approver_key_id, row.updated_at = who.kind, who.key_id, now
        for sh in _shards(s, batch_id):
            if sh.shard_index in terms:
                sh.approved = S._jsonable({**terms[sh.shard_index], "approved_at": now.isoformat(), "by": actor_id})
                if sh.status == "needs_reapproval":
                    sh.status, sh.reason_code, sh.reason, sh.updated_at = "pending_approval", None, None, now
        _event(s, batch_id, "approved", f"batch approved by {actor_id}: {len(terms)} shard(s) covered"
               + (f"; {reason}" if reason else ""), actor=actor, actor_id=actor_id,
               evidence={"terms": {str(k): v for k, v in terms.items()}, "skipped": skipped})
    results = []
    b = _load_batch(batch_id, None)
    for sh, job, dep, q in waiting:
        if sh.shard_index not in terms or terms[sh.shard_index]["basis"] != "quote":
            continue
        results.append(_approve_shard(b, sh.shard_index, job, dep, who,
                                      reason=f"batch {batch_id} approval by {actor_id}" + (f": {reason}" if reason
                                                                                          else "")))
    adv = advance(batch_id)
    out = get(batch_id, who, admin=True)
    out["approval_result"] = {"approved_shards": sorted(terms), "results": results + skipped,
                              "advance": {k: v for k, v in adv.items() if k != "batch_id"}}
    return 200, out


def cancel(batch_id: str, who) -> tuple[int, dict]:
    b = _load_batch(batch_id, who)
    if b.finished_at is not None:
        return 200, {**get(batch_id, who), "cancel": {"requested": False, "note": f"batch is {b.outcome}: nothing to "
                                                                                    "cancel"}}
    actor, actor_id = _actor(who)
    now = _now()
    with normalize.SessionLocal.begin() as s:
        _lock(s, batch_id)               # serialises with every reservation: nothing is claimed after this commits
        row = s.get(Batch, batch_id, with_for_update=True)
        first = row.cancel_requested_at is None
        if first:
            row.cancel_requested_at, row.updated_at = now, now
            if row.status != "finished":
                row.status = "cancelling"
            _event(s, batch_id, "cancel_requested", "cancel requested: queued shards cancelled, submitted ones "
                                                    "cancelled through the jobs cancel path", actor=actor,
                   actor_id=actor_id)
    with normalize.SessionLocal() as s:
        b = s.get(Batch, batch_id)
    wd = _wind_down(b, who)
    sync_batch(batch_id)
    out = get(batch_id, who)
    out["cancel"] = {"requested": True, "first": first, **wd,
                     "note": "a submitted shard is cancelled once its job is: before launch nothing was created; after "
                             "launch the GPU is terminated and the batch finishes when the provider confirms it"}
    return 202, out


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------

def link_for_job(job_id: str) -> dict | None:
    with normalize.SessionLocal() as s:
        a = _attempt_of_job(s, job_id)
        if a is None:
            return None
        b = s.get(Batch, a.batch_id)
    return {"batch_id": a.batch_id, "shard_index": a.shard_index, "name": b.name if b else None,
            "shards_total": b.shards_total if b else None, "link": f"/v1/batches/{a.batch_id}",
            "page": f"/batch/{a.batch_id}"}


def _metric_values(job_ids: list[str], name: str) -> dict:
    if not job_ids:
        return {}
    from store.jobs import JobMetric
    with normalize.SessionLocal() as s:
        rows = s.execute(select(JobMetric.job_id, JobMetric.value, JobMetric.source).where(
            JobMetric.job_id.in_(job_ids), JobMetric.name == name).order_by(JobMetric.id)).all()
    return {r.job_id: {"value": r.value, "source": r.source} for r in rows}      # the last one wins


def _iso(t):
    return None if t is None else t.isoformat()


def public(b: Batch, *, detail: bool = False) -> dict:
    from routing import quotes

    with normalize.SessionLocal() as s:
        shards = _shards(s, b.batch_id)
        jobs, deps = _jobs_deps(s, shards)
        led = ledger(s, b.batch_id)
        evs = [] if not detail else [
            {"at": e.at.isoformat(), "kind": e.kind, "shard_index": e.shard_index, "actor": e.actor,
             "actor_id": e.actor_id, "reason": e.reason, "evidence": e.evidence}
            for e in s.scalars(select(BatchEvent).where(BatchEvent.batch_id == b.batch_id)
                               .order_by(BatchEvent.at, BatchEvent.id).limit(1000))]
    spec = dict(b.spec or {})
    spec.pop("ssh_public_key", None)
    metric = spec.get("metric") or {"name": "test_accuracy", "goal": "max"}
    mv = _metric_values([sh.job_id for sh in shards if sh.job_id], metric["name"])
    counts = {k: 0 for k in SHARD_STATES}
    by_prov: dict[str, dict] = {}
    rows, pending = [], []
    for sh in shards:
        counts[sh.status] = counts.get(sh.status, 0) + 1
        job = jobs.get(sh.job_id) if sh.job_id else None
        dep = deps.get(job.deployment_id) if job is not None and job.deployment_id else None
        prov = (dep.provider if dep is not None else None) or ((job.selection or {}).get("provider") if job else None)
        planned = not prov
        prov = prov or (sh.planned or {}).get("provider")
        m = mv.get(sh.job_id) if sh.job_id else None
        r = {"shard_index": sh.shard_index, "config": sh.params, "status": sh.status, "provider": prov,
             "provider_planned": planned, "job_id": sh.job_id, "root_job_id": sh.root_job_id,
             "attempts": sh.attempts, "job_status": job.status if job else None,
             "outcome": job.outcome if job else None,
             "deployment_status": dep.status if dep is not None else None,
             "price_per_gpu_hour": (float(dep.quoted_price_per_gpu_hour) if dep is not None and
                                    dep.quoted_price_per_gpu_hour is not None else (sh.planned or {}).get(
                                        "price_per_gpu_hour")),
             "planned": sh.planned, "approved": sh.approved, "reason_code": sh.reason_code, "reason": sh.reason,
             "metric": None if m is None else {"name": metric["name"], "value": m["value"],
                                               "simulated": m["source"] == "simulated"},
             "finished": sh.finished_at is not None,
             "links": {"job": f"/v1/jobs/{sh.job_id}" if sh.job_id else None,
                       "page": f"/jobs/{sh.job_id}" if sh.job_id else None}}
        if dep is not None and dep.status in ("pending_approval", "quote_expired"):
            q = quotes.get(dep.quote_id) or {}
            w = {"shard_index": sh.shard_index, "job_id": sh.job_id, "route_request_id": job.route_request_id,
                 "quote_id": dep.quote_id, "provider": dep.provider,
                 "quote_price_per_gpu_hour": q.get("quote_price_per_gpu_hour"), "est_total_cost": q.get(
                     "est_total_cost"), "quote_expires_at": q.get("expires_at"),
                 "needs_reapproval": sh.status == "needs_reapproval", "reason": sh.reason}
            pending.append(w)
            r["approval"] = w
        rows.append(r)
        if prov:
            pv = by_prov.setdefault(prov, {"provider": prov, "shards": 0, "by_state": {}, "committed_usd": 0.0,
                                           "spent_usd": 0.0})
            pv["shards"] += 1
            pv["by_state"][sh.status] = pv["by_state"].get(sh.status, 0) + 1
    for a in led["attempts"]:
        if a.get("provider") and a["provider"] in by_prov:
            by_prov[a["provider"]]["committed_usd"] = round(by_prov[a["provider"]]["committed_usd"] + a["committed"], 4)
            by_prov[a["provider"]]["spent_usd"] = round(by_prov[a["provider"]]["spent_usd"] + a["spent"], 4)
    scored = [r for r in rows if r["metric"] is not None and r["status"] == "succeeded"]
    best = None
    if scored:
        pick = (max if metric["goal"] == "max" else min)(scored, key=lambda r: r["metric"]["value"])
        best = {"metric": metric["name"], "goal": metric["goal"], "shard_index": pick["shard_index"],
                "job_id": pick["job_id"], "value": pick["metric"]["value"], "config": pick["config"],
                "simulated": pick["metric"]["simulated"],
                "note": "SIMULATED placeholder value (sandbox)" if pick["metric"]["simulated"] else
                        "the runner's last reported value for the shard's current job"}
    cap = float(b.max_total_usd)
    with normalize.SessionLocal() as s:
        busy = _busy(s, b.batch_id)
    out = {
        "batch_id": b.batch_id, "name": b.name, "template": b.template, "status": b.status, "outcome": b.outcome,
        "finished": b.finished_at is not None, "sandbox": bool(b.sandbox), "simulated": bool(b.sandbox),
        "simulated_note": ("SIMULATED: every shard is a sandbox job; no provider was called, nothing was billed"
                           if b.sandbox else None),
        "shards_total": b.shards_total, "counts": {k: v for k, v in counts.items() if v},
        "budget": {"max_total_usd": cap, "committed_usd": led["committed_usd"], "spent_usd": led["spent_usd"],
                   "remaining_usd": round(max(0.0, cap - led["committed_usd"]), 4),
                   "note": "committed = reservations of unfinished attempts + reconciled actual costs (an upper bound "
                           "until every deployment is reconciled); spent = metered / reconciled cost so far"},
        "concurrency": {"max_running": b.max_running, "busy": busy,
                        "queue_enabled": bool(settings.batch_queue_enabled)},
        "policy": {"on_failure": b.on_failure, "max_attempts": (spec.get("policy") or {}).get("max_attempts", 1),
                   "stop_reason": b.stop_reason},
        "approval": {"status": b.approval_status, "required": bool(pending) and not b.sandbox,
                     "approved_by": b.approved_by, "approved_at": _iso(b.approved_at),
                     "waiting": pending, "approve": f"POST /v1/batches/{b.batch_id}/approve",
                     "body": {"quotes": {str(w["shard_index"]): w["quote_id"] for w in pending}},
                     "rule": "one approval covers every shard's exact quote (and the planned price of a queued shard); "
                             "a later quote on the same provider within quote_price_tolerance launches under it, "
                             "anything else waits here for re-approval"},
        "providers": sorted(by_prov.values(), key=lambda p: -p["shards"]),
        "best": best, "metric": metric, "shards": rows,
        "request": spec, "plan_summary": {k: (b.plan or {}).get(k) for k in ("totals", "cap", "alternatives",
                                                                             "warnings", "would")},
        "created_at": _iso(b.created_at), "updated_at": _iso(b.updated_at), "finished_at": _iso(b.finished_at),
        "cancel_requested_at": _iso(b.cancel_requested_at),
        "links": {"self": f"/v1/batches/{b.batch_id}", "cancel": f"/v1/batches/{b.batch_id}/cancel",
                  "approve": f"/v1/batches/{b.batch_id}/approve", "page": f"/batch/{b.batch_id}"},
    }
    if detail:
        out["events"] = evs
        out["ledger"] = led["attempts"]
        out["plan"] = b.plan
    return out


def get(batch_id: str, who, *, admin: bool = False) -> dict:
    _load_batch(batch_id, who, admin_ok=admin)
    b = sync_batch(batch_id)
    return public(b, detail=True)


def list_for(who, *, status: str | None = None, limit: int = 50, offset: int = 0) -> list[dict]:
    q = select(Batch).order_by(Batch.created_at.desc())
    if who.account_id is not None:
        q = q.where(Batch.account_id == who.account_id)
    if status == "open":
        q = q.where(Batch.finished_at.is_(None))
    elif status:
        q = q.where(Batch.status == status)
    with normalize.SessionLocal() as s:
        rows = list(s.scalars(q.offset(offset).limit(limit)))
    out = []
    for b in rows:
        p = public(b)
        out.append({k: p[k] for k in ("batch_id", "name", "template", "status", "outcome", "finished", "sandbox",
                                      "simulated", "shards_total", "counts", "budget", "approval", "providers",
                                      "best", "created_at", "finished_at", "links")})
        out[-1]["approval"] = {k: p["approval"][k] for k in ("status", "required")}
    return out
