"""Workload-aware GPU selection: which compatible listing is best for this job, and which one OpenGrid can
actually run right now. Used by the jobs preview and submission. Never calls a provider.

    select(spec, who, *, now=None) -> dict      (shape documented in methodology/workloads.md and below)

Pipeline
    1. validate the request (workloads.spec.validate) -> requirements
    2. eligible canonical GPUs (workloads.hardware_req.resolve): vendor, compute capability, VRAM, host
    3. for each eligible GPU with listings: the EXISTING routing.scoring.rank_listings (count, region,
       max_price, mode=strategy, weights, strict_region); candidates merged, exclusions kept with reasons
    4. workload filters: vCPU / RAM / disk minimums where the listing reports them, single-instance only;
       request.routing.exclude_providers (a relaunch: every provider that interrupted / lost an attempt) ->
       excluded with code provider_excluded, and passed on to the route as preferences.exclude_providers
    5. cost bounds: billed minutes = runtime ceiling rounded UP to the provider's billing unit, at least its
       minimum commitment (adapter CAPABILITIES; unknown -> per-hour assumed, said so);
       est_max_total = price x gpu_count x billed hours (> max_total_cost -> excluded, with the numbers)
    6. cross-GPU price factor: rank_listings scores price against ONE GPU's market; merged across GPUs the
       price factor is re-based on the median compatible price for this workload (each (gpu, provider)'s
       lowest), so a popular expensive GPU never wins a tiny job at its own market median. Every other
       factor is rank_listings' own. CHEAPEST orders by price.
    7. execution readiness per candidate (exact reasons when not ready) -> market_best vs execution_ready

Return shape
    {template, requirements, eligible_gpus[], ineligible_gpus[{gpu, code, reason}], market_best,
     execution_ready, alternatives[], exclusions[{provider, gpu, listing_id, code, reason, ...}],
     exclusions_total, exclusions_by_code, blockers[{code, reason, provider?, providers?}], estimate, strategy,
     weights, runtime, execution_mode, live_provisioning_enabled, sandbox, notes, as_of, market_data_freshness,
     candidates_total, methodology}
"""

from __future__ import annotations

import math
import re
import statistics
from datetime import datetime, timezone

from sqlalchemy import text

import normalize
from api.common import ESTIMATED, OBSERVED_MARKET_PRICE, gpu_slug
from config import settings
from routing import adapters, control, credentials, engine, guards, scoring
from workloads import hardware_req
from workloads import templates as T
from workloads.spec import Validated, validate

METHODOLOGY = "/methodology/workloads"
MAX_EXCLUSIONS = 300
# A structurally valid ed25519 public key used ONLY to evaluate launch-spec rules when the request has no key
# (the real ephemeral key is generated at submission). Never sent anywhere.
_PROBE_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAABAgMEBQYHCAkKCwwNDg8QERITFBUWFxgZGhscHR4f opengrid-probe"


def _iso(t):
    return None if t is None else t.isoformat()


# --------------------------------------------------------------------------
# Billing
# --------------------------------------------------------------------------

def billing(provider: str) -> dict:
    """{unit, unit_minutes, minimum_minutes, assumed, basis} from the adapter's CAPABILITIES."""
    caps = adapters.capabilities(provider)
    if caps is None:
        return {"unit": "unknown", "unit_minutes": 60.0, "minimum_minutes": 0.0, "assumed": True,
                "basis": f"{provider} has no OpenGrid adapter: billing unit unknown, per-hour billing assumed "
                         "(the runtime ceiling is rounded up to whole hours)"}
    unit, ev = (caps.billing_unit + ("", ""))[:2]
    t = str(unit).lower()
    assumed = False
    m = re.search(r"(\d+)\s*min", t)
    if m:
        um = float(m.group(1))
    elif "second" in t:
        um = 1 / 60
    elif "minute" in t:
        um = 1.0
    elif "hour" in t:
        um = 60.0
    else:
        um, assumed = 60.0, True
    minimum, mbasis = 0.0, None
    mc = (caps.minimum_commitment + ("", ""))[:2]
    mv = str(mc[0]).strip().upper()
    if mv in ("NO", "NONE", "N/A"):
        minimum = 0.0
    else:
        mm = re.search(r"(\d+(?:\.\d+)?)\s*(hour|hr|h\b|min|second|sec)", str(mc[0]).lower())
        if mm:
            n = float(mm.group(1))
            minimum = n * 60 if mm.group(2).startswith("h") else n / 60 if mm.group(2).startswith("s") else n
            mbasis = f"minimum commitment {mc[0]}"
        else:
            minimum, assumed, mbasis = 60.0, True, "minimum commitment unknown: one hour assumed"
    ms = re.search(r"minimum charge of (\d+)\s*seconds", f"{unit} {ev}".lower())
    if ms:
        minimum = max(minimum, float(ms.group(1)) / 60)
        mbasis = mbasis or f"minimum charge of {ms.group(1)} seconds"
    if assumed and um == 60.0 and not m and "hour" not in t:
        basis = f"billing unit {unit!r} not established: per-hour billing assumed"
    else:
        basis = f"billing unit {unit!r} per the adapter's CAPABILITIES" + (f" ({ev})" if ev else "")
    if mbasis:
        basis += f"; {mbasis}"
    return {"unit": str(unit), "unit_minutes": um, "minimum_minutes": minimum, "assumed": assumed, "basis": basis}


def billed_minutes(minutes: float, b: dict) -> float:
    um = b["unit_minutes"]
    blocks = math.ceil(round(minutes / um, 9))
    return round(max(blocks * um, b["minimum_minutes"]), 4)


def cost(price: float, gpu_count: int, runtime_minutes: int, typical: tuple[float, float], b: dict) -> dict:
    billed = billed_minutes(runtime_minutes, b)
    lo = billed_minutes(min(typical[0], runtime_minutes), b)
    hi = billed_minutes(min(typical[1], runtime_minutes), b)
    per_h = price * gpu_count
    return {"kind": ESTIMATED, "price_per_gpu_hour": price, "price_kind": OBSERVED_MARKET_PRICE,
            "gpu_count": gpu_count, "runtime_ceiling_minutes": runtime_minutes, "billing_unit": b["unit"],
            "billing_unit_minutes": round(b["unit_minutes"], 6), "minimum_minutes": b["minimum_minutes"],
            "billing_assumed": b["assumed"], "billing_basis": b["basis"], "billed_minutes": billed,
            "est_max_total": round(per_h * billed / 60, 4),
            "est_typical_total": round(per_h * hi / 60, 4),
            "est_typical_range": [round(per_h * lo / 60, 4), round(per_h * hi / 60, 4)],
            "typical_minutes": [typical[0], typical[1]]}


# --------------------------------------------------------------------------
# Readiness
# --------------------------------------------------------------------------

def _global_blockers(mode: str) -> list[dict]:
    """Reasons no customer job can launch anywhere (effective mode x env ceiling)."""
    if mode not in (control.DISABLED, control.PREVIEW_ONLY):
        return []
    out = []
    if not settings.routing_live_provisioning:
        out.append({"code": "env_ceiling", "reason": "ROUTING_LIVE_PROVISIONING is off in this environment: the "
                                                     "effective mode is capped at PREVIEW_ONLY"})
    try:
        stored = control.stored_mode()["mode"]
    except Exception:  # noqa: BLE001
        stored = mode
    if mode == control.DISABLED:
        out.append({"code": "mode_disabled", "reason": "execution mode DISABLED (kill switch or operator setting): "
                                                       "an admin must set SUPERVISED (POST /v1/admin/execution/mode)"})
    elif stored in (control.PREVIEW_ONLY, control.DISABLED):
        out.append({"code": "mode_preview_only", "reason": "execution mode PREVIEW_ONLY: an admin must set "
                                                           "SUPERVISED (POST /v1/admin/execution/mode)"})
    return out


def _credential_status(who, provider: str) -> tuple[str | None, str | None]:
    """(source 'byo'|'opengrid'|None, problem). Side-effect free (no last_used_at touch)."""
    cp = credentials.credential_provider(provider)
    try:
        acct = engine._credential_account(who)
    except Exception:  # noqa: BLE001
        acct = who.account_id
    if acct is not None:
        try:
            from sqlalchemy import select as sa_select

            from store.accounts import ProviderCredential as PC
            with normalize.SessionLocal() as s:
                row = s.scalars(sa_select(PC).where(PC.account_id == acct, PC.provider == cp, PC.revoked_at.is_(None))
                                .order_by(PC.id.desc()).limit(1)).first()
        except Exception:  # noqa: BLE001
            return None, f"credential lookup failed for {cp}"
        if row is not None:
            try:
                credentials._decrypt(row.secret_encrypted)
            except Exception:  # noqa: BLE001
                return None, (f"your {cp} credential could not be decrypted; OpenGrid will not fall back to its own "
                              "key: replace it")
            return "byo", None
    creds = credentials.platform(cp)
    if creds is not None and (creds or credentials._needs_no_credentials(provider)):
        return "opengrid", None
    return None, f"no credentials configured for {cp} ({cp.upper()}_API_KEY on the server, or a BYO {cp} key)"


def _caps_value(caps, row: str) -> str:
    v = getattr(caps, row, None) if caps is not None else None
    return str(v[0]).upper() if isinstance(v, (tuple, list)) and v else "UNKNOWN"


def provider_readiness(provider: str, who, mode: str, ssh_public_key: str | None) -> dict:
    """Everything about `provider` that decides whether a job can launch there now (listing-independent)."""
    reasons = []

    def no(code, reason):
        reasons.append({"code": code, "reason": reason, "provider": provider})

    cls = adapters.get(provider)
    lvl = adapters.level(provider)
    if cls is None or lvl < 2:
        no("not_provisionable", f"{provider}: market data only (OpenGrid integration level {lvl}): OpenGrid cannot "
                                "launch here")
        return {"provider": provider, "reasons": reasons, "approval_mode": None, "credential_source": None,
                "flags": None}
    try:
        f = control.provider_flags(provider)
    except Exception:  # noqa: BLE001 - unreadable control plane fails closed
        f = None
        no("flags_unreadable", f"{provider}: execution flags unreadable; failing closed")
    if f is not None:
        if f.get("killed"):
            no("provider_killed", f"{provider}: provider kill switch on ({f.get('kill_reason') or 'no reason'}); an "
                                  "admin must unkill it")
        if f.get("adapter_status") != "validated":
            no("adapter_not_validated", f"{provider}: adapter simulated; run the supervised validation first")
        if mode == control.LIVE:
            if not (f.get("live_enabled") or f.get("supervised_enabled")):
                no("provider_not_enabled", f"{provider}: neither live nor supervised launches are enabled "
                                           f"(POST /v1/admin/execution/providers/{provider})")
        elif not f.get("supervised_enabled"):
            no("supervised_not_enabled", f"{provider}: supervised launches not enabled (POST "
                                         f"/v1/admin/execution/providers/{provider} supervised_enabled=true)")
    caps = cls.CAPABILITIES
    if _caps_value(caps, "startup_script") != "YES":
        no("no_startup_script", f"{provider}: no startup-script support (CAPABILITIES.startup_script="
                                f"{_caps_value(caps, 'startup_script')}): the job cannot bootstrap")
    forced, _ = engine.forces_account_ssh_key(cls)
    if forced != "NO":
        no("forces_account_ssh_key", f"{provider}: may install account-level SSH keys (forces_account_ssh_key="
                                     f"{forced}): OpenGrid operators could log in to the job's machine")
    src, problem = _credential_status(who, provider)
    if problem:
        no("no_credentials", f"{provider}: {problem}")
    if src != "byo" and not engine.per_deployment_key_registration(cls):
        no("ssh_key_registration_unsupported", f"{provider}: cannot register the job's own SSH key per deployment on "
                                               "OpenGrid's account")
    approval = None
    if f is not None and not reasons:
        ok, used, why = control.launch_permission(provider, purpose="customer", mode=mode, flags=f)
        if ok:
            approval = used
        elif mode not in (control.DISABLED, control.PREVIEW_ONLY):
            no("launch_not_permitted", f"{provider}: {why}")
    return {"provider": provider, "reasons": reasons, "approval_mode": approval, "credential_source": src,
            "flags": None if f is None else {k: f.get(k) for k in ("adapter_status", "supervised_enabled",
                                                                    "live_enabled", "killed")}}


def _launch_problems(c: dict, src: str | None, ssh_public_key: str | None, route_spec: dict) -> list[dict]:
    """The engine's own launch-spec rules for this listing (key policy + the adapter's required fields)."""
    p = c["provider"]
    cls = adapters.get(p)
    if cls is None:
        return []
    req = {"ssh_public_key": ssh_public_key or _PROBE_KEY, "startup_script": "opengrid-job:preview"}
    try:
        spec, problem = engine.launch_spec_for(p, req, purpose="customer", credential_source=src, adapter_cls=cls)
        if problem:
            code = problem.split(":", 1)[0]
            if code == "ssh_key_registration_unsupported":
                return []   # already reported by provider_readiness
            return [{"code": code, "reason": f"{p}: {problem}", "provider": p}]
        offer = engine._offer({**c, "raw_gpu_name": c.get("raw_gpu_name") or c["gpu"]}, route_spec)
        missing = cls(None, provider=p).missing_launch(spec, offer)
    except Exception as exc:  # noqa: BLE001
        return [{"code": "launch_spec_unchecked", "reason": f"{p}: launch parameters could not be checked "
                                                            f"({type(exc).__name__})", "provider": p}]
    if missing:
        return [{"code": "missing_launch_parameters", "reason": f"{p}: missing launch parameters "
                                                                f"{', '.join(missing)} (operator launch defaults)",
                 "provider": p}]
    return []


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

_RES = text("SELECT provider, listing_id, vcpu, ram_gb, storage_gb FROM compute_listings "
            "WHERE canonical_gpu_name = ANY(:gpus)")


def _listed_gpus() -> set[str]:
    with normalize.SessionLocal() as s:
        return {r[0] for r in s.execute(text("SELECT DISTINCT canonical_gpu_name FROM compute_listings "
                                             "WHERE canonical_gpu_name IS NOT NULL"))}


def _resources(gpus: list[str]) -> dict:
    if not gpus:
        return {}
    with normalize.SessionLocal() as s:
        return {(r.provider, r.listing_id): r for r in s.execute(_RES, {"gpus": gpus})}


def _excl(c_or_e: dict, gpu: str, code: str, reason: str) -> dict:
    return {"provider": c_or_e["provider"], "gpu": gpu, "gpu_slug": gpu_slug(gpu), "listing_id": c_or_e["listing_id"],
            "region": c_or_e.get("region"), "price_per_gpu_hour": c_or_e.get("price_per_gpu_hour"), "code": code,
            "reason": reason}


def _resource_check(c: dict, row, req: dict) -> tuple[dict, list[tuple[str, str]]]:
    have = {"vcpu": None if row is None else row.vcpu, "ram_gb": None if row is None else row.ram_gb,
            "disk_gb": None if row is None else row.storage_gb}
    fails, unknown = [], []
    for key, need_key, code, unit, label in (("vcpu", "min_vcpu", "insufficient_vcpu", "vCPU", "vCPU"),
                                             ("ram_gb", "min_ram_gb", "insufficient_ram", "GB RAM", "RAM"),
                                             ("disk_gb", "min_disk_gb", "insufficient_disk", "GB disk", "disk")):
        need = req.get(need_key)
        v = have[key]
        if need is None:
            continue
        if v is None:
            unknown.append(label)
        elif float(v) < float(need) - 1e-9:
            fails.append((code, f"instance has {float(v):g} {unit}; the workload needs at least {float(need):g}"))
    out = {**{k: (None if v is None else float(v)) for k, v in have.items()},
           "unknown": unknown, "note": ("not reported by the provider: " + ", ".join(unknown) + " (not checked)")
           if unknown else "meets the template minimums"}
    return out, fails


def _rebase_price(cands: list[dict], weights: dict) -> dict | None:
    """Re-base the price factor on the workload's compatible market (see module docstring, step 6)."""
    best: dict[tuple, float] = {}
    for c in cands:
        k = (c["gpu"], c["provider"])
        best[k] = min(best.get(k, math.inf), c["price_per_gpu_hour"])
    if not best:
        return None
    med = statistics.median(best.values())
    for c in cands:
        f = c["factors"]["price"]
        p = c["price_per_gpu_hour"]
        pct = (p - med) / med if med else 0.0
        gpu_med = (f.get("raw") or {}).get("market_median")
        note = (f"at the median compatible-GPU price for this workload (${med:.2f})" if abs(pct) < 0.005 else
                f"{abs(pct):.0%} {'below' if pct < 0 else 'above'} the median compatible-GPU price for this "
                f"workload (${med:.2f})")
        f["raw"] = {**(f.get("raw") or {}), "workload_median": round(med, 6), "pct_vs_workload_median": round(pct, 4),
                    "gpu_market_median": gpu_med}
        f["value"] = round(scoring.clamp(0.5 - pct), 4)
        f["note"] = note
        total = 0.0
        for name, fac in c["factors"].items():
            w = weights.get(name, 0.0)
            fac["weight"] = w
            v = fac["value"] if fac["value"] is not None else 0.5
            fac["contribution"] = round(w * v, 4) if w > 0 else 0.0
            total += w * v if w > 0 else 0.0
        c["score"] = round(total, 4)
        c["explanation"] = scoring.explanation(c)
    return {"median": round(med, 6), "pairs": len(best), "kind": OBSERVED_MARKET_PRICE,
            "rule": "median over compatible (GPU, provider) pairs of each pair's lowest candidate price"}


def _order(cands: list[dict], strategy: str) -> list[dict]:
    if strategy == "CHEAPEST":
        key = lambda c: (c["price_per_gpu_hour"], c["age_seconds"], c["gpu"], c["provider"], c["listing_id"])  # noqa: E731
    else:
        key = lambda c: (-c["score"], c["price_per_gpu_hour"], c["age_seconds"], c["gpu"], c["provider"],  # noqa: E731
                         c["listing_id"])
    cands.sort(key=key)
    for i, c in enumerate(cands, 1):
        c["rank"] = i
    return cands


_PUBLIC = ("rank", "provider", "provider_display", "listing_id", "sku", "gpu", "gpu_slug", "gpu_count", "region",
           "country", "price_per_gpu_hour", "price_per_instance_hour", "price_kind", "available", "observed_at",
           "age_seconds", "integration_level", "provisionable", "score", "factors", "explanation", "resources",
           "cost", "executable", "readiness")


def _public(c: dict | None, top: dict | None = None) -> dict | None:
    if c is None:
        return None
    out = {k: c.get(k) for k in _PUBLIC}
    if top is not None and top is not c:
        out["vs_selected"] = scoring.versus(c, top) + (f" (GPU {c['gpu']})" if c["gpu"] != top["gpu"] else "")
    if c.get("route_spec"):
        out["route_spec"] = c["route_spec"]
    return out


# --------------------------------------------------------------------------
# select
# --------------------------------------------------------------------------

def select(spec, who, *, now: datetime | None = None) -> dict:
    v: Validated = validate(spec)
    R, req, tmpl = v.request, v.requirements, v.template
    now = now or datetime.now(timezone.utc)
    res, lim, rt = R["resources"], R["limits"], R["routing"]
    strategy, weights = rt["strategy"], rt["weights"]
    count, region, strict = res["gpu_count"], res["region"], res["strict_region"]
    max_price, budget = lim["max_price_per_gpu_hour_usd"], lim["max_total_cost_usd"]

    gpus = hardware_req.resolve(req, res["gpu"])
    ceiling = guards.runtime_ceiling(who.account_id, lim["max_runtime_minutes"])
    runtime = int(ceiling["effective_max_runtime_minutes"])
    typical = T.expected_minutes(tmpl, R["config"])
    mode = control.effective_mode()
    notes = list(v.notes)
    if ceiling.get("note"):
        notes.append(ceiling["note"])

    listed = _listed_gpus()
    pool, excl, eligible_out = [], [], []
    rank_weights = scoring.effective_weights(strategy, weights, bool(region))
    for g in gpus["eligible"]:
        info = {k: g[k] for k in ("gpu", "slug", "vendor", "architecture", "compute_capability",
                                  "compute_capability_basis", "vram_gb", "form_factor")}
        if g["gpu"] not in listed:
            eligible_out.append({**info, "listings": 0, "candidates": 0, "market": None})
            continue
        r = scoring.rank_listings(g["gpu"], count=count, region_group=region, max_price=max_price, mode=strategy,
                                  weights=weights, strict_region=strict, limit=1000, now=now)
        rank_weights = r["weights"]
        excl += [{**_excl(e, g["gpu"], e["code"], e["reason"])} for e in r["exclusions"]]
        for c in r["multi_instance_alternatives"]:
            excl.append(_excl(c, g["gpu"], "multi_instance", c.get("note") or "needs several instances"))
        for c in r["candidates"]:
            c["gpu_slug"] = gpu_slug(c["gpu"])
            c.pop("vs_selected", None)
            pool.append(c)
        eligible_out.append({**info, "listings": r["candidates_total"] + r["exclusions_total"]
                             + len(r["multi_instance_alternatives"]),
                             "candidates": r["candidates_total"], "market": r["market"]})

    # workload filters: resources, then cost bounds
    rows = _resources([g["gpu"] for g in gpus["eligible"] if g["gpu"] in listed])
    kept = []
    bills: dict[str, dict] = {}
    excluded_providers = set(rt.get("exclude_providers") or ())
    for c in pool:
        if c["provider"] in excluded_providers:
            excl.append(_excl(c, c["gpu"], "provider_excluded", f"{c['provider']} is excluded by "
                                                                "routing.exclude_providers"))
            continue
        c["resources"], fails = _resource_check(c, rows.get((c["provider"], c["listing_id"])), req)
        if fails:
            excl += [_excl(c, c["gpu"], code, why) for code, why in fails]
            continue
        b = bills.setdefault(c["provider"], billing(c["provider"]))
        c["cost"] = cost(c["price_per_gpu_hour"], count, runtime, typical, b)
        if c["cost"]["est_max_total"] > budget + 1e-9:
            k = c["cost"]
            excl.append(_excl(c, c["gpu"], "over_budget",
                              f"est. max ${k['est_max_total']:.2f} = ${c['price_per_gpu_hour']:.4f}/GPU-h x {count} GPU x "
                              f"{k['billed_minutes'] / 60:g} h billed ({runtime} min ceiling, {b['basis']}) is over "
                              f"max_total_cost ${budget:.2f}"))
            continue
        kept.append(c)

    workload_market = _rebase_price(kept, rank_weights)
    _order(kept, strategy)

    # readiness
    pr: dict[str, dict] = {}
    glob = _global_blockers(mode)
    for c in kept:
        p = pr.get(c["provider"]) or pr.setdefault(c["provider"], provider_readiness(c["provider"], who, mode,
                                                                                     R["ssh_public_key"]))
        c["route_spec"] = {"gpu": c["gpu"], "count": count, "region_group": region, "strict_region": strict,
                           "max_price_per_gpu_hour": max_price, "mode": strategy, "weights": weights,
                           "preferences": {"include_providers": [c["provider"]],
                                           **({"exclude_providers": sorted(excluded_providers)}
                                              if excluded_providers else {})},
                           "max_runtime_minutes": lim["max_runtime_minutes"],
                           "duration_hours": round(c["cost"]["billed_minutes"] / 60, 6),
                           "listing_id": c["listing_id"]}
        reasons = list(glob) + list(p["reasons"])
        if not p["reasons"] and adapters.get(c["provider"]) is not None:
            reasons += _launch_problems(c, p["credential_source"], R["ssh_public_key"], c["route_spec"])
        c["executable"] = not reasons
        c["readiness"] = {"ready": not reasons, "reasons": reasons,
                          "approval_mode": p["approval_mode"] if not reasons else None,
                          "approval_note": ("an admin approves this launch (POST /v1/route/{id}/approve)"
                                            if p["approval_mode"] == control.SUPERVISED and not reasons else None),
                          "provider_flags": p["flags"], "credential_source": p["credential_source"]}
        if not c["executable"]:
            c.pop("route_spec", None)

    market_best = kept[0] if kept else None
    executable = [c for c in kept if c["executable"]]
    ready = executable[0] if executable else None
    if ready is not None:
        alts = executable[1:4]
        if not alts:
            alts = [c for c in kept if c is not ready and c is not market_best][:3]
    else:
        alts = kept[1:4]
    top = ready or market_best

    out_mb, out_ready = _public(market_best), _public(ready)
    n_gpus = len({c["gpu"] for c in kept})
    if out_mb is not None:
        out_mb["why"] = (f"{strategy}: rank 1 of {len(kept)} compatible listings across {n_gpus} GPU model(s); "
                         f"{market_best['explanation']}; est. max ${market_best['cost']['est_max_total']:.2f} within "
                         f"the ${budget:.2f} budget" + ("" if market_best["executable"] else
                                                        "; NOT executable now: " + "; ".join(
                                                            r["reason"] for r in market_best["readiness"]["reasons"])))
    if out_ready is not None:
        out_ready["why"] = (("the market best is also executable now: " if ready is market_best else
                             f"best executable candidate (rank {ready['rank']}; the market best "
                             f"{market_best['provider']} / {market_best['gpu']} is not executable now): ")
                            + ready["explanation"] + f"; est. max ${ready['cost']['est_max_total']:.2f} within the "
                            f"${budget:.2f} budget")
        try:
            out_ready["limit_violations"] = guards.check(
                who.account_id, provider=ready["provider"], gpu_count=count,
                price_per_gpu_hour=ready["price_per_gpu_hour"], est_total_cost=ready["cost"]["est_max_total"],
                region=ready["region"], region_group=region, purpose="customer", max_runtime_minutes=runtime)
        except Exception:  # noqa: BLE001 - informational here; the route re-checks atomically
            out_ready["limit_violations"] = None

    excl.sort(key=lambda e: (e["gpu"], e["provider"], str(e["listing_id"]), e["code"]))
    by_code: dict[str, int] = {}
    for e in excl:
        by_code[e["code"]] = by_code.get(e["code"], 0) + 1

    blockers = [] if ready is not None else _blockers(gpus, eligible_out, kept, by_code)
    est = None
    if top is not None:
        k = top["cost"]
        est = {**{x: k[x] for x in ("price_per_gpu_hour", "price_kind", "gpu_count", "billed_minutes",
                                    "est_max_total", "est_typical_total", "est_typical_range", "typical_minutes",
                                    "billing_unit", "billing_assumed")},
               "for": "execution_ready" if ready is not None else "market_best", "provider": top["provider"],
               "gpu": top["gpu"], "kind": ESTIMATED, "max_total_cost_usd": budget,
               "basis": (f"est_max_total = ${k['price_per_gpu_hour']:.4f}/GPU-h x {count} GPU x {k['billed_minutes']:g} "
                         f"billed min / 60; billed min = the {runtime} min runtime ceiling rounded up to the billing "
                         f"unit ({k['billing_basis']}). est_typical_total uses the template's expected runtime "
                         f"({typical[0]:g}-{typical[1]:g} min, an estimate, not a benchmark). Observed listing price, "
                         f"not a quote: the route quotes and re-validates before any launch")}

    ages = [c["age_seconds"] for c in kept]
    obs = [c["observed_at"] for c in kept]
    return {
        "template": {"id": tmpl["id"], "version": tmpl["version"], "title": tmpl["title"],
                     "expected_runtime_minutes": list(typical), "expected_runtime_kind": ESTIMATED},
        "request": R,
        "requirements": req,
        "eligible_gpus": eligible_out,
        "ineligible_gpus": gpus["ineligible"],
        "gpu_selection": gpus["mode"],
        "candidates_total": len(kept),
        "market_best": out_mb,
        "execution_ready": out_ready,
        "alternatives": [{**_public(c, top), "label": "executable" if c["executable"] else "not executable"}
                         for c in alts],
        "exclusions": excl[:MAX_EXCLUSIONS],
        "exclusions_total": len(excl),
        "exclusions_by_code": by_code,
        "blockers": blockers,
        "estimate": est,
        "strategy": strategy,
        "weights": rank_weights,
        "workload_market": workload_market,
        "runtime": ceiling,
        "execution_mode": mode,
        "live_provisioning_enabled": bool(settings.routing_live_provisioning),
        "sandbox": R["sandbox"],
        "sandbox_note": ("sandbox: no provider is called; this selection is shown for information only"
                         if R["sandbox"] else None),
        "notes": notes,
        "as_of": now.isoformat(),
        "market_data_freshness": {
            "newest_observed_at": max(obs) if obs else None, "oldest_observed_at": min(obs) if obs else None,
            "selected_age_seconds": top["age_seconds"] if top else None,
            "max_candidate_age_seconds": max(ages) if ages else None,
            "stale_excluded": by_code.get("stale", 0),
            "rule": "a listing counts only until its last poll + market.stale_after(provider); older ones are "
                    "excluded (code stale)"},
        "price_concepts": {"candidate prices": OBSERVED_MARKET_PRICE, "estimate": ESTIMATED,
                           "quote": "issued by the route at submission, re-validated live before launch"},
        "methodology": METHODOLOGY,
    }


def _blockers(gpus: dict, eligible_out: list, kept: list, by_code: dict) -> list[dict]:
    """Why nothing is executable: deduplicated, actionable."""
    out: list[dict] = []
    if not gpus["eligible"]:
        out.append({"code": "no_compatible_gpu", "reason": "no canonical GPU meets the template's hardware "
                                                           "requirements; see ineligible_gpus"})
        return out
    if not kept:
        listed = sum(1 for g in eligible_out if g["listings"])
        summary = ", ".join(f"{k} {n}" for k, n in sorted(by_code.items()))
        hints = [h for code, h in (("over_budget", "raise limits.max_total_cost_usd or lower max_runtime_minutes"),
                                   ("over_max_price", "raise limits.max_price_per_gpu_hour_usd"),
                                   ("wrong_region", "relax resources.region"),
                                   ("region_unconfirmed", "turn off strict_region"),
                                   ("stale", "market data is stale: listings refresh when the provider poller runs"),
                                   ("sold_out", "compatible GPUs are sold out: try later"))
                 if by_code.get(code)]
        if not listed:
            hints.append("no compatible GPU model has any listing: choose gpu 'auto' or another GPU")
        out.append({"code": "no_eligible_listing",
                    "reason": (f"no listing of a compatible GPU satisfies the workload ({len(gpus['eligible'])} "
                               f"compatible GPU models, {listed} with listings; exclusions: {summary or 'none'})"
                               + (". " + "; ".join(hints) if hints else ""))})
        return out
    seen, market_only = set(), []
    for c in kept:
        for r in c["readiness"]["reasons"]:
            if r["code"] == "not_provisionable":
                if r["provider"] not in market_only:
                    market_only.append(r["provider"])
                continue
            key = (r["code"], r.get("provider"))
            if key in seen:
                continue
            seen.add(key)
            out.append(dict(r))
    out.sort(key=lambda r: (r.get("provider") is not None, r.get("provider") or "", r["code"]))
    if market_only:
        out.append({"code": "not_provisionable", "providers": sorted(market_only),
                    "reason": f"market data only (no OpenGrid adapter): {', '.join(sorted(market_only))}"})
    return out
