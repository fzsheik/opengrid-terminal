"""Route preview and route execution.

    preview(spec, who)  rank -> selected + alternatives + quote -> audit. Never calls a provider.
    route(spec, who)    rank -> for each provisionable candidate in rank order:
                            credentials -> live availability check -> quote -> price guard
                            -> if settings.routing_live_provisioning is False: STOP, return
                               "not_provisioned" with the decision (no deployment is created)
                            -> else provision; on failure record the attempt and fail over
                        -> audit

Money guards, all required before provision() is ever called:
    1. settings.routing_live_provisioning is True (off by default, per environment)
    2. the caller holds route:execute (api/routing.py)
    3. credentials resolve for that provider (BYO or OpenGrid-managed)
    4. the launch spec has what the provider needs (checked before any network call)
    5. the live quote is within max_price_per_gpu_hour, when one was given

A provision call that TIMES OUT does not fail over: the provider may have created the
instance anyway, so the deployment is marked failed with needs_reconciliation instead of
risking a second paid instance elsewhere.

`spec` is the validated request (api/routing.py): gpu (canonical), count, region_group,
max_price_per_gpu_hour, duration_hours, deadline_hours, mode, weights, preferences, launch,
strict_region; and for a family routed with allow_variants: gpu = family id, family, variants.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from api.common import EXECUTION_PRICE, OBSERVED_MARKET_PRICE, QUOTE, gpu_slug
from config import settings
from routing import adapters, audit, credentials, deployments, scoring
from routing.adapters.base import TIMEOUT, UNKNOWN_STATE, AdapterError, LaunchSpec, Offer, timed

log = logging.getLogger(__name__)

NOT_LIVE = "live provisioning disabled in this environment"



def _credential_account(who) -> int | None:
    """The account whose BYO credentials apply. The web UI stores the operator's on the operator account."""
    if who.account_id is not None or who.kind != "operator":
        return who.account_id
    try:
        from accounts.accounts import operator_account_id
        return operator_account_id()
    except Exception:
        log.exception("operator account lookup failed; using OpenGrid-managed credentials only")
        return None

def _rank(spec: dict) -> dict:
    p = spec.get("preferences") or {}
    kw = dict(count=spec["count"], region_group=spec.get("region_group"),
              max_price=spec.get("max_price_per_gpu_hour"), mode=spec["mode"], weights=spec.get("weights"),
              exclude_providers=p.get("exclude_providers") or (), include_providers=p.get("include_providers"),
              require_level=p.get("require_level") or 0, require_available=bool(p.get("require_available")),
              strict_region=bool(spec.get("strict_region")), limit=500)
    if spec.get("variants"):  # a family with allow_variants: rank each variant, merge, tag each candidate
        return scoring.rank_variants(spec["variants"], family=spec["family"], **kw)
    return scoring.rank_listings(spec["gpu"], **kw)


def _family_fields(spec: dict, ranking: dict) -> dict:
    if not spec.get("variants"):
        return {}
    return {"family": spec["family"], "variants": spec["variants"], "by_variant": ranking["by_variant"],
            "variant_note": ranking["variant_note"]}


def hours_of(spec: dict) -> tuple[float | None, str | None]:
    if spec.get("duration_hours"):
        return float(spec["duration_hours"]), "duration_hours"
    if spec.get("deadline_hours"):
        return float(spec["deadline_hours"]), "deadline_hours (cost if it runs the full window)"
    return None, None


def quote_block(price: float, count: int, spec: dict, *, basis: str, at: str | None, market: dict) -> dict:
    hours, hours_from = hours_of(spec)
    q = {"kind": QUOTE, "basis": basis, "price_per_gpu_hour": round(price, 6), "gpu_count": count,
         "price_per_hour": round(price * count, 6), "hours": hours, "hours_from": hours_from,
         "expected_cost_usd": None if hours is None else round(price * count * hours, 2), "priced_at": at,
         "note": ("from OpenGrid's observed listing price; not a live provider quote"
                  if basis == "observed_listing" else "read from the provider API on this request")}
    med = market.get("median")
    if med:
        q["savings_vs_median"] = {
            "per_gpu_hour": round(med - price, 6), "pct": round((med - price) / med, 4),
            "total_usd": None if hours is None else round((med - price) * count * hours, 2),
            "basis": "market median is an observed market price; the quote is this route's price",
        }
    return q


def _candidate_public(c: dict | None) -> dict | None:
    if c is None:
        return None
    return {k: v for k, v in c.items() if k != "raw_gpu_name"}


def _summary(ranking: dict) -> dict:
    c = ranking["candidates"]
    return {"candidates_total": ranking["candidates_total"], "exclusions_total": ranking["exclusions_total"],
            "selected": c[0]["provider"] + ":" + c[0]["listing_id"] if c else None}


def preview(spec: dict, who) -> dict:
    ranking = _rank(spec)
    cands = ranking["candidates"]
    selected = cands[0] if cands else None
    best_prov = next((c for c in cands if c["provisionable"]), None)
    rr_id = audit.open_request(who, spec, ranking, preview=True)
    out = {
        "route_request_id": rr_id, "preview": True, "gpu": spec["gpu"], "gpu_slug": gpu_slug(spec["gpu"]),
        "count": spec["count"], "count_semantics": ranking["count_semantics"], "mode": ranking["mode"],
        "weights": ranking["weights"], "region_group": spec.get("region_group"),
        "strict_region": bool(spec.get("strict_region")), **_family_fields(spec, ranking),
        "market": scoring.variant_market(ranking, selected),
        "selected": _candidate_public(selected),
        "can_provision_selected": bool(selected and selected["provisionable"]),
        "best_provisionable": None if (selected and selected["provisionable"]) else _candidate_public(best_prov),
        "alternatives": [_candidate_public(c) for c in cands[1:6]],
        "multi_instance_alternatives": [_candidate_public(c) for c in ranking["multi_instance_alternatives"][:5]],
        "quote": None if selected is None else quote_block(
            selected["price_per_gpu_hour"], spec["count"], spec, basis="observed_listing",
            at=selected["observed_at"], market=scoring.variant_market(ranking, selected)),
        "exclusions_total": ranking["exclusions_total"], "exclusions_by_code": ranking["exclusions_by_code"],
        "exclusions": ranking["exclusions"][:50],
        "live_provisioning_enabled": settings.routing_live_provisioning,
        "price_concepts": {"candidate prices": OBSERVED_MARKET_PRICE, "quote": QUOTE,
                           "execution price": f"{EXECUTION_PRICE} (only after a real deployment)"},
        "not_used": ranking["not_used"],
    }
    if selected is None:
        out["reason"] = "no eligible listing satisfies the request; see exclusions"
    elif best_prov is None:
        out["provisioning_note"] = "no candidate is on a provider OpenGrid can provision (level >= 2)"
    if hours_of(spec)[0] is None:
        out["quote_note"] = "no duration_hours or deadline_hours given: hourly cost only"
    audit.close_request(rr_id, "previewed" if selected else "no_candidates", _summary(ranking))
    return out


def _offer(c: dict, spec: dict) -> Offer:
    return Offer(provider=c["provider"], listing_id=c["listing_id"], sku=c["sku"], raw_gpu_name=c["raw_gpu_name"],
                 gpu=c["gpu"], gpu_count=c["gpu_count"], region=c["region"],
                 price_per_gpu_hour=c["price_per_gpu_hour"], price_per_instance_hour=c["price_per_instance_hour"],
                 provider_tier=c["provider_tier"], want_region_group=spec.get("region_group"))


def route(spec: dict, who) -> dict:
    ranking = _rank(spec)
    cands = ranking["candidates"]
    live = bool(settings.routing_live_provisioning)
    rr_id = audit.open_request(who, spec, ranking, preview=False)
    considered: list[dict] = []
    dep_id, deployment, quote, status, reason = None, None, None, None, None
    attempts = 0
    max_attempts = max(1, int(settings.routing_max_attempts))

    def note(c, step, outcome, why, **extra):
        considered.append({"rank": c["rank"], "provider": c["provider"], "listing_id": c["listing_id"],
                           "step": step, "outcome": outcome, "reason": why, **extra})

    for c in cands:
        if attempts >= max_attempts:
            break
        if not c["provisionable"]:
            note(c, "capability", "skipped", f"OpenGrid cannot provision {c['provider']} "
                                             f"(integration level {c['integration_level']})")
            continue
        creds, source = credentials.resolve(_credential_account(who), c["provider"])
        a = adapters.build(c["provider"], creds)
        offer = _offer(c, spec)
        launch = LaunchSpec.merged(spec.get("launch"), (settings.routing_launch_defaults or {}).get(c["provider"]))
        try:
            if a.CHECK_NEEDS_CREDENTIALS and a.missing_credentials():
                note(c, "credentials", "skipped", f"no credentials configured for {c['provider']}")
                continue
            try:
                avail = a.check_availability(offer)
                q = a.quote(offer, avail)
            except AdapterError as exc:
                note(c, "availability_check", "error", exc.message, error_kind=exc.kind)
                continue
            if avail.available is False:
                note(c, "availability_check", "unavailable", avail.note or "not available on live check")
                continue
            mp = spec.get("max_price_per_gpu_hour")
            if mp is not None and q.price_per_gpu_hour > mp:
                note(c, "quote", "over_max_price", f"live quote ${q.price_per_gpu_hour:.2f}/GPU-h is over ${mp:.2f}")
                continue
            quote = quote_block(q.price_per_gpu_hour, spec["count"], spec, basis=q.basis,
                                at=q.quoted_at.isoformat(), market=scoring.variant_market(ranking, c))
            quote.update(provider=c["provider"], listing_id=c["listing_id"], region=q.region,
                         availability={"available": avail.available, "live": avail.live, "note": avail.note})
            if not live:
                note(c, "provision", "not_attempted", NOT_LIVE, quote=quote["price_per_gpu_hour"])
                status, reason = "not_provisioned", NOT_LIVE
                break
            if a.missing_credentials():
                note(c, "credentials", "skipped", f"no credentials configured for {c['provider']}")
                quote = None
                continue
            missing = a.missing_launch(launch, offer)
            if missing:
                note(c, "launch_spec", "skipped", "missing launch parameters: " + ", ".join(missing))
                quote = None
                continue
            if dep_id is None:
                dep_id = deployments.create(rr_id, who, {**spec, "gpu": c["gpu"]})  # the variant, not the family
            attempts += 1
            started = datetime.now(timezone.utc)
            inst, ms = timed(a.provision, offer, avail, launch, f"opengrid-{dep_id}")
            if isinstance(inst, Exception) and not isinstance(inst, AdapterError):
                log.exception("unexpected error provisioning on %s", c["provider"], exc_info=inst)
                inst = AdapterError(UNKNOWN_STATE, f"{c['provider']}: unexpected {type(inst).__name__}: {inst}")
            if isinstance(inst, AdapterError):
                deployments.record_attempt(dep_id, rr_id, c, started, ms, False, inst)
                note(c, "provision", "failed", inst.message, error_kind=inst.kind, latency_ms=ms)
                quote = None
                if inst.kind in (TIMEOUT, UNKNOWN_STATE):
                    status = "failed"
                    reason = (f"provision outcome unknown ({inst.kind}): {c['provider']} may have created an "
                              "instance; not failing over to avoid a second paid instance")
                    deployments.failed(dep_id, reason, attempts, needs_reconciliation=True,
                                       timed_out_provider=c["provider"])
                    break
                continue
            deployments.record_attempt(dep_id, rr_id, c, started, ms, True, None)
            deployments.provisioned(dep_id, c, inst, q, avail, source, ms, attempts)
            note(c, "provision", "ok", "provider accepted the launch", latency_ms=ms)
            status, reason = "provisioned", None
            break
        finally:
            a.close()

    if status is None:
        status = "failed" if dep_id else ("no_candidates" if not cands else "not_provisioned")
        reason = ("no eligible listing satisfies the request" if not cands else
                  "every provisionable candidate failed" if dep_id else
                  "no candidate could be checked and quoted for provisioning")
        if dep_id:
            deployments.failed(dep_id, reason + ": " + "; ".join(
                f"{x['provider']}: {x['reason']}" for x in considered if x["outcome"] == "failed"), attempts)
    if dep_id:
        deployment = deployments.public(dep_id)

    out = {
        "route_request_id": rr_id, "status": status, "reason": reason,
        "live_provisioning_enabled": live,
        "gpu": spec["gpu"], "gpu_slug": gpu_slug(spec["gpu"]), "count": spec["count"], "mode": ranking["mode"],
        "weights": ranking["weights"], "strict_region": bool(spec.get("strict_region")),
        **_family_fields(spec, ranking), "market": scoring.variant_market(ranking, cands[0] if cands else None),
        "selected": _candidate_public(cands[0]) if cands else None,
        "alternatives": [_candidate_public(c) for c in cands[1:6]],
        "considered": considered, "quote": quote, "deployment": deployment,
        "exclusions_total": ranking["exclusions_total"], "exclusions_by_code": ranking["exclusions_by_code"],
        "not_used": ranking["not_used"],
    }
    audit.close_request(rr_id, status, {**_summary(ranking), "considered": considered, "reason": reason,
                                        "deployment_id": dep_id, "quote": quote})
    return out
