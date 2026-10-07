"""Route preview, route, approval and validation launches: the execution core's orchestration.

    preview(spec, who)        rank -> selected + alternatives + quote (observed price, persisted with an id
                              and expiry) -> audit. Never calls a provider.
    route(spec, who)          rank -> for the top settings.route_live_check_candidates provisionable,
                              permitted candidates: credentials -> launch spec / ssh policy -> live check
                              -> quote (persisted) -> cost guards -> deployment
                                  SUPERVISED provider  -> pending_approval (no provision call)
                                  LIVE provider        -> approved -> THE provision call (no per-launch approval)
                              Failover to the next candidate only after a DEFINITIVE rejection, as a NEW
                              deployment, at most settings.routing_max_attempts provision calls (default 1).
                              Ambiguous outcomes (timeout, 5xx, reset, unparseable) never fail over.
    approve(rr_id, who, quote_id=...)   admin: quote must match and re-validate (fresh live check, price within
                              settings.quote_price_tolerance, unexpired) -> guards (override only with a reason)
                              -> approved -> launch. A double approve launches once.
    reject(rr_id, who, reason=)         admin: pending -> rejected.
    create_validation_route(provider, listing_id, by=)   a purpose='validation' route for one provider: always
                              pending_approval, validation caps, max runtime <= settings.validation_max_runtime_minutes,
                              operator default ssh key allowed. Approved through approve() like any other.

Gates before any provision call (all required):
    1. control.launch_permission(provider, purpose) (mode x env ceiling x provider flags x kill switches)
    2. the caller holds route:execute (customer) / admin (approve, validation)        [api/routing.py]
    3. the account is not suspended (new routes only; stop/terminate always allowed)
    4. credentials resolve (BYO first; an unusable BYO credential fails closed)
    5. launch spec complete and the ssh key policy satisfied
    6. a valid, unexpired, re-validated quote; consumed by exactly one deployment
    7. cost guards (routing/guards.py), or an explicit admin override with a reason
    8. the deployment's single launch token (routing/deployments.launch)

`spec` is the validated request (api/routing.py): gpu, count, region_group, max_price_per_gpu_hour,
duration_hours, deadline_hours, mode, weights, preferences, launch, strict_region, quote_id,
max_runtime_minutes; for a family with allow_variants also family, variants.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import HTTPException

import normalize
from api.common import EXECUTION_PRICE, OBSERVED_MARKET_PRICE, QUOTE, gpu_slug
from config import settings
from routing import adapters, audit, control, credentials, deployments, guards, quotes, scoring
from routing.adapters.base import AdapterError, LaunchSpec, Offer
from routing.credentials import CredentialsUnavailable

log = logging.getLogger(__name__)

NOT_LIVE = "live provisioning disabled in this environment"


class RouteRefused(HTTPException):
    """A refusal with a machine-readable code (409 unless given)."""

    def __init__(self, code: str, message: str, status: int = 409, **extra):
        super().__init__(status, {"code": code, "message": message, **extra})


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _credential_account(who) -> int | None:
    """The account whose BYO credentials apply. The web UI stores the operator's on the operator account."""
    if who.account_id is not None or who.kind != "operator":
        return who.account_id
    return _operator_account()


def _operator_account() -> int | None:
    try:
        from accounts.accounts import operator_account_id
        return operator_account_id()
    except Exception:  # noqa: BLE001
        log.exception("operator account lookup failed; using OpenGrid-managed credentials only")
        return None


def _deployment_cred_account(d) -> int | None:
    if d.account_id is not None:
        return d.account_id
    return _operator_account()


def _actor(who) -> tuple[str, str]:
    aid = f"key:{who.key_id}" if who.key_id is not None else (who.kind or "operator")
    return ("admin" if who.has("admin") else "user"), aid


def _check_account_active(who) -> None:
    if who.account_id is None:
        return
    try:
        from accounts.accounts import get_account
        a = get_account(who.account_id)
    except Exception:  # noqa: BLE001
        log.exception("account lookup failed for %s; refusing a new route", who.account_id)
        raise RouteRefused("account_unavailable", "account status could not be read; no new route", 503)
    if a is None or a.get("status") != "active":
        raise RouteRefused("account_suspended", "this account is suspended: new routes are blocked. Existing "
                                                "deployments can still be stopped and terminated.", 403)


def _rank(spec: dict) -> dict:
    p = spec.get("preferences") or {}
    kw = dict(count=spec["count"], region_group=spec.get("region_group"),
              max_price=spec.get("max_price_per_gpu_hour"), mode=spec["mode"], weights=spec.get("weights"),
              exclude_providers=p.get("exclude_providers") or (), include_providers=p.get("include_providers"),
              require_level=p.get("require_level") or 0, require_available=bool(p.get("require_available")),
              strict_region=bool(spec.get("strict_region")), limit=100)
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


def _offer(c: dict, spec: dict) -> Offer:
    return Offer(provider=c["provider"], listing_id=c["listing_id"], sku=c["sku"], raw_gpu_name=c["raw_gpu_name"],
                 gpu=c["gpu"], gpu_count=c["gpu_count"], region=c["region"],
                 price_per_gpu_hour=c["price_per_gpu_hour"], price_per_instance_hour=c["price_per_instance_hour"],
                 provider_tier=c["provider_tier"], want_region_group=spec.get("region_group"))


def launch_spec_for(provider: str, request: dict | None, *, purpose: str, credential_source: str | None,
                    adapter_cls) -> tuple[LaunchSpec | None, str | None]:
    """The SSH key policy, then the merged LaunchSpec. (spec, None) or (None, problem).

    OpenGrid-managed credentials (customer): no key-name references into OpenGrid's provider account; the
    customer supplies ssh_public_key, which the adapter registers under og-<deployment> (SSH_KEY_REGISTRATION),
    else the provider is refused. BYO credentials: the customer's own key names are fine (their account).
    The operator's default key (routing_launch_defaults[...].ssh_key) is used ONLY for validation launches."""
    req = dict(request or {})
    defaults = dict((settings.routing_launch_defaults or {}).get(provider) or {})
    if purpose != "validation":
        # The operator's default key (by name OR as public-key material) never reaches a customer machine:
        # a customer without their own key gets "missing ssh_key", not root access for OpenGrid ops.
        defaults.pop("ssh_key", None)
        defaults.pop("ssh_public_key", None)
        if credential_source != "byo" and req.get("ssh_key"):
            return None, ("ssh_key_reference_forbidden: on OpenGrid-managed provider accounts a launch may not "
                          "reference key names; send launch.ssh_public_key instead")
        if req.get("ssh_public_key") and credential_source != "byo" and not getattr(adapter_cls, "SSH_KEY_REGISTRATION", False):
            return None, (f"ssh_key_registration_unsupported: {provider}'s adapter cannot register a per-deployment "
                          "public key on OpenGrid's account")
    spec = LaunchSpec.merged(req, defaults)
    return spec, None


def _call_check(a, offer, ctx: dict):
    """Live availability check + quote, logged. (avail, q) or raises AdapterError."""
    out, _ = deployments.provider_call("check_availability", a.check_availability, offer, provider=offer.provider,
                                       **ctx)
    if isinstance(out, Exception):
        if isinstance(out, AdapterError):
            raise out
        raise AdapterError("unknown_state", f"{offer.provider}: availability check failed ({type(out).__name__})")
    q = a.quote(offer, out)
    return out, q


def _build(provider: str, creds: dict | None, ctx: dict):
    a = adapters.build(provider, creds)
    if a is not None:
        deployments._set_log_context(a, **ctx)
    return a


# --------------------------------------------------------------------------
# preview
# --------------------------------------------------------------------------

def preview(spec: dict, who) -> dict:
    ranking = _rank(spec)
    cands = ranking["candidates"]
    selected = cands[0] if cands else None
    best_prov = next((c for c in cands if c["provisionable"]), None)
    rr_id = audit.open_request(who, spec, ranking, preview=True)
    hours, _ = hours_of(spec)
    q_rec = None
    if best_prov is not None:
        q_rec = quotes.issue(route_request_id=rr_id, account_id=who.account_id, offer=_offer(best_prov, spec),
                             observed_price=best_prov["price_per_gpu_hour"], quote_price=best_prov["price_per_gpu_hour"],
                             price_source="observed", duration_hours=hours, region_group=spec.get("region_group"),
                             adapter_cls=adapters.get(best_prov["provider"]))
    mode = control.effective_mode()
    # Cost guards at quote time (EXEC_BRIEF section 5): the same check a route runs, so preview shows which
    # listings would stay pending_approval (admin override needed) instead of launching.
    lim, use = guards.limits_for(who.account_id), guards.usage(who.account_id)
    limit_exclusions = []
    for c in cands[:6] + ([best_prov] if best_prov is not None and best_prov not in cands[:6] else []):
        price = c["price_per_gpu_hour"]
        c["limit_violations"] = guards.check(
            who.account_id, provider=c["provider"], gpu_count=c["gpu_count"], price_per_gpu_hour=price,
            est_total_cost=None if hours is None else price * c["gpu_count"] * hours, region=c["region"],
            region_group=spec.get("region_group"), purpose="customer",
            max_runtime_minutes=spec.get("max_runtime_minutes"), limits=lim, use=use)
        if c["limit_violations"]:
            limit_exclusions.append({
                "provider": c["provider"], "listing_id": c["listing_id"], "code": "account_limit",
                "limits": [v["code"] for v in c["limit_violations"]],
                "overridable": all(v["overridable"] for v in c["limit_violations"]),
                "reason": "; ".join(v["message"] for v in c["limit_violations"])})
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
        "quote_record": q_rec,
        "limit_violations": (best_prov or {}).get("limit_violations") or [],
        "limit_exclusions": limit_exclusions,
        "limits_note": ("account cost guards: a listing in limit_exclusions does not launch; its route stays "
                        "pending_approval until an admin approves with override_limits and a reason"),
        "exclusions_total": ranking["exclusions_total"], "exclusions_by_code": ranking["exclusions_by_code"],
        "exclusions": ranking["exclusions"][:50],
        "live_provisioning_enabled": settings.routing_live_provisioning,
        "execution_mode": mode,
        "price_concepts": {"candidate prices": OBSERVED_MARKET_PRICE, "quote": QUOTE,
                           "execution price": f"{EXECUTION_PRICE} (only after a real deployment)"},
        "not_used": ranking["not_used"],
    }
    if q_rec:
        out["quote_note"] = (f"quote {q_rec['quote_id']} (observed price, provider {q_rec['provider']}) expires at "
                             f"{q_rec['expires_at']}; POST /v1/route with quote_id re-validates it live before any launch")
    if selected is None:
        out["reason"] = "no eligible listing satisfies the request; see exclusions"
    elif best_prov is None:
        out["provisioning_note"] = "no candidate is on a provider OpenGrid can provision (level >= 2)"
    if hours is None:
        out["hours_note"] = "no duration_hours or deadline_hours given: hourly cost only"
    audit.close_request(rr_id, "previewed" if selected else "no_candidates", _summary(ranking))
    return out


# --------------------------------------------------------------------------
# route
# --------------------------------------------------------------------------

def route(spec: dict, who, *, purpose: str = "customer") -> tuple[int, dict]:
    """(http_code, body). See the module docstring."""
    _check_account_active(who)
    mode = control.effective_mode()
    ranking = _rank(spec)
    cands = ranking["candidates"]
    rr_id = audit.open_request(who, spec, ranking, preview=False)
    considered: list[dict] = []
    ctx = {"route_request_id": rr_id}
    actor, actor_id = _actor(who)
    hours, _ = hours_of(spec)
    preview_only = mode in (control.DISABLED, control.PREVIEW_ONLY)
    status, reason, quote_out, dep_ids, code = None, None, None, [], 200
    checks, attempts = 0, 0
    max_checks = max(1, int(settings.route_live_check_candidates))
    max_attempts = max(1, int(settings.routing_max_attempts))
    max_runtime = spec.get("max_runtime_minutes") or settings.default_max_runtime_minutes

    def note(c, step, outcome, why, **extra):
        considered.append({"rank": c["rank"], "provider": c["provider"], "listing_id": c["listing_id"],
                           "step": step, "outcome": outcome, "reason": why, **extra})

    if spec.get("quote_id"):
        return _route_from_quote(spec, who, rr_id, ranking, mode, purpose)

    for c in cands:
        if attempts >= max_attempts:
            break
        if not c["provisionable"]:
            note(c, "capability", "skipped", f"OpenGrid cannot provision {c['provider']} "
                                             f"(integration level {c['integration_level']})")
            continue
        mode_used = None
        if not preview_only:
            allowed, mode_used, why = control.launch_permission(c["provider"], purpose=purpose, mode=mode)
            if not allowed:
                note(c, "execution_control", "skipped", why)
                continue
        try:
            resolved = credentials.resolve_for_launch(_credential_account(who), c["provider"])
        except CredentialsUnavailable as exc:
            note(c, "credentials", "skipped", "credential_unusable: " + exc.message)
            continue
        cls = adapters.get(c["provider"])
        a = _build(c["provider"], resolved.credentials if resolved else None, ctx)
        offer = _offer(c, spec)
        try:
            if a.CHECK_NEEDS_CREDENTIALS and a.missing_credentials():
                note(c, "credentials", "skipped", f"no credentials configured for {c['provider']}")
                continue
            launch = None
            if not preview_only:
                if resolved is None or a.missing_credentials():
                    note(c, "credentials", "skipped", f"no credentials configured for {c['provider']}")
                    continue
                launch, problem = launch_spec_for(c["provider"], spec.get("launch"), purpose=purpose,
                                                  credential_source=resolved.source, adapter_cls=cls)
                if problem:
                    note(c, "launch_spec", "skipped", problem)
                    continue
                missing = a.missing_launch(launch, offer)
                if missing:
                    note(c, "launch_spec", "skipped", "missing launch parameters: " + ", ".join(missing))
                    continue
            if checks >= max_checks:
                note(c, "availability_check", "not_checked",
                     f"route latency limit: at most {max_checks} live checks per route")
                break
            checks += 1
            try:
                avail, q = _call_check(a, offer, ctx)
            except AdapterError as exc:
                note(c, "availability_check", "error", f"{exc.kind}", error_kind=exc.kind)
                continue
            if avail.available is False:
                note(c, "availability_check", "unavailable", avail.note or "not available on live check")
                continue
            mp = spec.get("max_price_per_gpu_hour")
            if mp is not None and q.price_per_gpu_hour > mp:
                note(c, "quote", "over_max_price", f"live quote ${q.price_per_gpu_hour:.2f}/GPU-h is over ${mp:.2f}")
                continue
            if preview_only:
                quote_out = quote_block(q.price_per_gpu_hour, spec["count"], spec, basis=q.basis,
                                        at=q.quoted_at.isoformat(), market=scoring.variant_market(ranking, c))
                quote_out.update(provider=c["provider"], listing_id=c["listing_id"], region=q.region,
                                 availability={"available": avail.available, "live": avail.live, "note": avail.note})
                why = NOT_LIVE if not settings.routing_live_provisioning else f"execution mode is {mode}"
                note(c, "provision", "not_attempted", why, quote=quote_out["price_per_gpu_hour"])
                status, reason = "not_provisioned", why
                break
            qrec = quotes.issue(route_request_id=rr_id, account_id=who.account_id, offer=offer,
                                observed_price=c["price_per_gpu_hour"], quote_price=q.price_per_gpu_hour,
                                price_source="live_check" if q.basis == "live_provider_api" else "observed",
                                duration_hours=hours, region_group=spec.get("region_group"), availability=avail,
                                adapter_cls=cls, purpose=purpose, credential_source=resolved.source)
            quote_out = qrec
            violations = guards.check(who.account_id, provider=c["provider"], gpu_count=offer.gpu_count,
                                      price_per_gpu_hour=q.price_per_gpu_hour, est_total_cost=qrec["est_total_cost"],
                                      region=qrec["region"], region_group=spec.get("region_group"), purpose=purpose,
                                      max_runtime_minutes=max_runtime)
            auto = mode_used == control.LIVE and not violations
            dep_id = deployments.create(
                rr_id=rr_id, who=who, spec=spec, quote=qrec,
                candidate={"list_price_per_gpu_hour": avail.list_price_per_gpu_hour}, purpose=purpose,
                approval_mode=mode_used, limit_violations=violations, launch_request=spec.get("launch"),
                max_runtime_minutes=max_runtime, auto_approve=auto, actor=actor, actor_id=actor_id)
            dep_ids.append(dep_id)
            if not auto:
                note(c, "approval", "pending_approval",
                     "limit violations: " + ", ".join(v["code"] for v in violations) if violations
                     else f"{mode_used}: admin approval required", quote_id=qrec["quote_id"])
                status = "pending_approval"
                reason = ("cost guards exceeded: an admin may approve with override_limits and a reason"
                          if violations else "waiting for admin approval (SUPERVISED)")
                code = 202
                break
            attempts += 1
            res = deployments.launch(dep_id, adapter=a, offer=offer, availability=avail, launch_spec=launch,
                                     resolved=resolved, actor="system", actor_id="live-mode", rank=c["rank"],
                                     quote_id=qrec["quote_id"])
            note(c, "provision", res.get("outcome") or "not_called", res.get("reason") or res["status"],
                 error_kind=res.get("error_kind"), latency_ms=res.get("latency_ms"), deployment_id=dep_id)
            status = _route_status(res)
            if res.get("failover_allowed") and attempts < max_attempts:
                continue
            if res.get("failover_allowed"):
                reason = "provider definitively rejected the launch; no failover attempts left"
            elif res.get("outcome") == "unknown":
                reason = ("provision outcome unknown: the provider may have created an instance; NOT failing over. "
                          "Reconciliation resolves it by name before any retry")
                code = 202
            break
        finally:
            a.close()

    if status is None:
        status = "no_candidates" if not cands else "not_provisioned"
        reason = ("no eligible listing satisfies the request" if not cands else
                  "no candidate could be checked, quoted and launched under the current execution controls")
    deployment = deployments.public(dep_ids[-1]) if dep_ids else None
    out = {
        "route_request_id": rr_id, "status": status, "reason": reason,
        "live_provisioning_enabled": bool(settings.routing_live_provisioning), "execution_mode": mode,
        "purpose": purpose,
        "gpu": spec["gpu"], "gpu_slug": gpu_slug(spec["gpu"]), "count": spec["count"], "mode": ranking["mode"],
        "weights": ranking["weights"], "strict_region": bool(spec.get("strict_region")),
        **_family_fields(spec, ranking), "market": scoring.variant_market(ranking, cands[0] if cands else None),
        "selected": _candidate_public(cands[0]) if cands else None,
        "alternatives": [_candidate_public(c) for c in cands[1:6]],
        "considered": considered, "quote": quote_out, "deployment": deployment,
        "deployments": dep_ids,
        "exclusions_total": ranking["exclusions_total"], "exclusions_by_code": ranking["exclusions_by_code"],
        "not_used": ranking["not_used"],
    }
    if status == "pending_approval":
        out["approval"] = _approval_block(rr_id, deployment, quote_out)
    audit.close_request(rr_id, status, {**_summary(ranking), "considered": considered, "reason": reason,
                                        "deployment_id": dep_ids[-1] if dep_ids else None, "deployments": dep_ids,
                                        "quote_id": (quote_out or {}).get("quote_id")})
    return code, out


def _route_status(res: dict) -> str:
    if res.get("outcome") == "accepted":
        return "provisioned"
    return res.get("status") or "failed"


def _approval_block(rr_id: str, deployment: dict | None, q: dict | None) -> dict:
    return {"required": True, "approve": f"POST /v1/route/{rr_id}/approve", "reject": f"POST /v1/route/{rr_id}/reject",
            "body": {"quote_id": (q or {}).get("quote_id")},
            "provider": (deployment or {}).get("provider"), "region": (q or {}).get("region"),
            "gpu": (q or {}).get("gpu"), "gpu_count": (q or {}).get("gpu_count"),
            "quote_price_per_gpu_hour": (q or {}).get("quote_price_per_gpu_hour"),
            "est_hourly_cost": (q or {}).get("est_hourly_cost"), "est_total_cost": (q or {}).get("est_total_cost"),
            "fees": (q or {}).get("fees"), "quote_expires_at": (q or {}).get("expires_at"),
            "limit_violations": (deployment or {}).get("limit_violations") or []}


def _route_from_quote(spec: dict, who, rr_id: str, ranking: dict, mode: str, purpose: str) -> tuple[int, dict]:
    """POST /v1/route with quote_id: launch exactly that quoted listing (no reroute), after re-validation."""
    qrow = quotes.row(spec["quote_id"])
    if qrow is None or (who.account_id is not None and qrow.account_id != who.account_id):
        raise RouteRefused("quote_not_found", "quote not found", 404)
    provider = qrow.provider
    allowed, mode_used, why = control.launch_permission(provider, purpose=purpose, mode=mode)
    base = {"route_request_id": rr_id, "execution_mode": mode, "purpose": purpose,
            "live_provisioning_enabled": bool(settings.routing_live_provisioning), "quote_id": qrow.id}
    if not allowed:
        audit.close_request(rr_id, "not_provisioned", {"reason": why, "quote_id": qrow.id})
        return 200, {**base, "status": "not_provisioned", "reason": why, "deployment": None,
                     "quote": quotes.as_dict(qrow)}
    try:
        resolved = credentials.resolve_for_launch(_credential_account(who), provider)
    except CredentialsUnavailable as exc:
        raise RouteRefused("credential_unusable", exc.message)
    if resolved is None:
        raise RouteRefused("no_credentials", f"no credentials configured for {provider}")
    cls = adapters.get(provider)
    a = _build(provider, resolved.credentials, {"route_request_id": rr_id})
    try:
        launch, problem = launch_spec_for(provider, spec.get("launch"), purpose=purpose,
                                          credential_source=resolved.source, adapter_cls=cls)
        if problem:
            raise RouteRefused("launch_spec_invalid", problem, 422)
        offer = quotes.offer_of(qrow)
        missing = a.missing_launch(launch, offer)
        if missing:
            raise RouteRefused("launch_spec_invalid", "missing launch parameters: " + ", ".join(missing), 422)
        rv = quotes.revalidate(qrow, a)
        if not rv["ok"]:
            newq = _requote(qrow, a, rv, rr_id=rr_id, account_id=who.account_id, purpose=purpose,
                            credential_source=resolved.source)
            audit.close_request(rr_id, "quote_invalid", {"reason": rv["reason"], "quote_id": qrow.id,
                                                         "new_quote_id": (newq or {}).get("quote_id")})
            raise RouteRefused(rv["code"], rv["reason"], new_quote=newq)
        qd = quotes.as_dict(qrow)
        max_runtime = spec.get("max_runtime_minutes") or settings.default_max_runtime_minutes
        violations = guards.check(who.account_id, provider=provider, gpu_count=qrow.gpu_count,
                                  price_per_gpu_hour=float(qrow.quote_price_per_gpu_hour),
                                  est_total_cost=qd["est_total_cost"], region=qrow.region,
                                  region_group=qrow.region_group, purpose=purpose, max_runtime_minutes=max_runtime)
        auto = mode_used == control.LIVE and not violations
        actor, actor_id = _actor(who)
        dep_id = deployments.create(rr_id=rr_id, who=who, spec=spec, quote=qd,
                                    candidate={"list_price_per_gpu_hour": rv["availability"].list_price_per_gpu_hour},
                                    purpose=purpose, approval_mode=mode_used, limit_violations=violations,
                                    launch_request=spec.get("launch"), max_runtime_minutes=max_runtime,
                                    auto_approve=auto, actor=actor, actor_id=actor_id)
        if not auto:
            dep = deployments.public(dep_id)
            audit.close_request(rr_id, "pending_approval", {"deployment_id": dep_id, "quote_id": qrow.id})
            return 202, {**base, "status": "pending_approval", "deployment": dep, "quote": qd,
                         "approval": _approval_block(rr_id, dep, qd),
                         "reason": "cost guards exceeded" if violations else "waiting for admin approval (SUPERVISED)"}
        res = deployments.launch(dep_id, adapter=a, offer=offer, availability=rv["availability"], launch_spec=launch,
                                 resolved=resolved, actor="system", actor_id="live-mode", quote_id=qrow.id)
    finally:
        a.close()
    status = _route_status(res)
    audit.close_request(rr_id, status, {"deployment_id": dep_id, "quote_id": qrow.id, "launch": res})
    return (202 if res.get("outcome") == "unknown" else 200), {
        **base, "status": status, "launch": res, "deployment": deployments.public(dep_id), "quote": quotes.get(qrow.id),
        "reason": None if res.get("outcome") == "accepted" else res.get("reason") or res.get("status")}


def _request_max_price(rr_id: str | None) -> float | None:
    """The max_price_per_gpu_hour the customer asked for on this route request, if any."""
    if not rr_id:
        return None
    from store.routing import RouteRequest

    with normalize.SessionLocal() as s:
        rr = s.get(RouteRequest, rr_id)
    mp = ((rr.request if rr is not None else None) or {}).get("max_price_per_gpu_hour")
    return None if mp is None else float(mp)


def _requote(qrow, a, rv: dict, *, rr_id: str | None, account_id, purpose: str, credential_source: str | None) -> dict | None:
    """After a failed re-validation: a NEW quote from the fresh live check (for re-approval); the old one is
    superseded. None when the listing is gone/unavailable (nothing to re-approve)."""
    avail, q = rv.get("availability"), rv.get("quote")
    if avail is None or avail.available is False:
        quotes.expire(qrow.id)
        return None
    offer = rv.get("offer") or quotes.offer_of(qrow)
    if q is None:
        try:
            q = a.quote(offer, avail)
        except AdapterError:
            return None
    newq = quotes.issue(route_request_id=rr_id or qrow.route_request_id, account_id=account_id, offer=offer,
                        observed_price=None if qrow.observed_price_per_gpu_hour is None else float(qrow.observed_price_per_gpu_hour),
                        quote_price=q.price_per_gpu_hour,
                        price_source="live_check" if q.basis == "live_provider_api" else "observed",
                        duration_hours=None if qrow.duration_hours is None else float(qrow.duration_hours),
                        region_group=qrow.region_group, availability=avail, adapter_cls=adapters.get(qrow.provider),
                        purpose=purpose, credential_source=credential_source)
    quotes.supersede(qrow.id, newq["quote_id"])
    return newq


# --------------------------------------------------------------------------
# approve / reject (admin)
# --------------------------------------------------------------------------

POST_APPROVAL = ("approved",) + deployments.LIVE_STATES + ("terminated", "provision_failed", "provider_rejected")


def approve(rr_id: str, who, *, quote_id: str, override_limits: bool = False, reason: str | None = None) -> tuple[int, dict]:
    if override_limits and not (reason and reason.strip()):
        raise RouteRefused("reason_required", "override_limits needs a reason", 422)
    d = deployments.for_request(rr_id)
    if d is None:
        raise RouteRefused("not_found", "no deployment for this route request", 404)
    dep_id = d.deployment_id
    if d.status in POST_APPROVAL and not (d.status == "approved" and d.launch_token is None):
        return 200, {"route_request_id": rr_id, "status": d.status, "already_approved": True,
                     "note": "this route was already approved; its single provision call is not repeated",
                     "deployment": deployments.public(dep_id, operator=True)}
    if d.status not in ("pending_approval", "quote_expired", "approved"):
        raise RouteRefused("not_approvable", f"deployment is {d.status}")
    if quote_id != d.quote_id:
        raise RouteRefused("quote_mismatch", "quote_id is not this route's current quote",
                           current_quote=quotes.get(d.quote_id))
    allowed, mode_used, why = control.launch_permission(d.provider, purpose=d.purpose or "customer")
    if not allowed:
        raise RouteRefused("launch_not_permitted", why)
    try:
        resolved = credentials.resolve_for_launch(_deployment_cred_account(d), d.provider)
    except CredentialsUnavailable as exc:
        raise RouteRefused("credential_unusable", exc.message)
    if resolved is None:
        raise RouteRefused("no_credentials", f"no credentials configured for {d.provider}")
    cls = adapters.get(d.provider)
    actor, actor_id = "admin", _actor(who)[1]
    a = _build(d.provider, resolved.credentials, {"route_request_id": rr_id, "deployment_id": dep_id})
    if a is None:
        raise RouteRefused("no_adapter", f"no adapter for {d.provider}")
    try:
        req = {k: v for k, v in (d.launch or {}).items() if k not in ("env", "env_sealed")}
        env = deployments._open_env((d.launch or {}).get("env_sealed"))
        if env:
            req["env"] = env
        launch, problem = launch_spec_for(d.provider, req, purpose=d.purpose or "customer",
                                          credential_source=resolved.source, adapter_cls=cls)
        if problem:
            raise RouteRefused("launch_spec_invalid", problem, 422)
        qrow = quotes.row(quote_id)
        rv = quotes.revalidate(qrow, a)
        if not rv["ok"]:
            newq = _requote(qrow, a, rv, rr_id=rr_id, account_id=d.account_id, purpose=d.purpose or "customer",
                            credential_source=resolved.source)
            _swap_quote(dep_id, newq, rv, actor_id)
            raise RouteRefused(rv["code"], rv["reason"] + ("; a new quote was issued: approve it to launch" if newq
                                                           else "; the listing is not available"), new_quote=newq,
                               deployment=deployments.public(dep_id, detail=False))
        offer = rv["offer"]
        qd = quotes.as_dict(qrow)
        # The customer's own ceiling binds every quote, including a re-quote approved later.
        cap = _request_max_price(rr_id or d.route_request_id)
        if cap is not None and float(qrow.quote_price_per_gpu_hour) > cap + 1e-9:
            raise RouteRefused("over_max_price",
                               f"quote ${float(qrow.quote_price_per_gpu_hour):.4f}/GPU-h is over the request's "
                               f"max_price_per_gpu_hour ${cap:.4f}; nothing was launched", 422,
                               deployment=deployments.public(dep_id, detail=False))
        violations = guards.check(d.account_id, provider=d.provider, gpu_count=d.gpu_count,
                                  price_per_gpu_hour=float(qrow.quote_price_per_gpu_hour),
                                  est_total_cost=qd["est_total_cost"], region=qrow.region, region_group=qrow.region_group,
                                  purpose=d.purpose or "customer", max_runtime_minutes=d.max_runtime_minutes,
                                  exclude_deployment_id=dep_id)
        still = guards.blocking(violations, override=override_limits)
        with normalize.SessionLocal.begin() as s:
            row = s.get(deployments.Deployment, dep_id, with_for_update=True)
            row.limit_violations = violations or None
            if still:
                deployments.note_event(s, row, "approval refused: limits exceeded", {"violations": still},
                                       actor="admin", actor_id=actor_id)
            elif row.status in ("pending_approval", "quote_expired"):
                if row.status == "quote_expired":
                    deployments._apply(s, row, "pending_approval", reason="quote re-validated", actor="admin",
                                       actor_id=actor_id, now=_now(), evidence=None)
                row.approved_by, row.approved_at = actor_id, _now()
                row.override_limits = bool(override_limits and violations)
                row.override_reason = reason if override_limits and violations else None
                deployments._apply(s, row, "approved", reason=reason or "admin approved", actor="admin",
                                   actor_id=actor_id, now=_now(),
                                   evidence={"quote_id": quote_id, "revalidated_price": rv.get("price"),
                                             "override_limits": bool(override_limits and violations),
                                             "violations_overridden": violations if override_limits else []})
                control.log_action(s, "approve_launch", f"deployment:{dep_id}", None,
                                   {"quote_id": quote_id, "override_limits": bool(override_limits and violations),
                                    "violations": violations}, reason or "approved", actor_id)
            elif row.status != "approved":
                # a concurrent approval won; its launch is the only launch
                return 200, {"route_request_id": rr_id, "status": row.status, "already_approved": True,
                             "deployment": None}
        if still:
            raise RouteRefused("limits_exceeded", "cost guards block this launch", violations=still,
                               overridable=all(v.get("overridable") for v in still))
        res = deployments.launch(dep_id, adapter=a, offer=offer, availability=rv["availability"], launch_spec=launch,
                                 resolved=resolved, actor="admin", actor_id=actor_id, quote_id=quote_id)
    finally:
        a.close()
    status = _route_status(res) if res.get("launched") else res.get("status")
    audit.close_request(rr_id, status or "approved", {"deployment_id": dep_id, "quote_id": quote_id, "launch": res})
    body = {"route_request_id": rr_id, "status": status, "launch": res,
            "deployment": deployments.public(dep_id, operator=True)}
    if not res.get("launched") and res.get("status") == "approved":
        body["already_approved"] = True
    if res.get("outcome") == "unknown":
        body["reason"] = ("provision outcome unknown: the provider may have created an instance; reconciliation "
                          "resolves it by name before any retry")
        return 202, body
    return 200, body


def _now():
    return datetime.now(timezone.utc)


def _swap_quote(dep_id: str, newq: dict | None, rv: dict, actor_id: str) -> None:
    with normalize.SessionLocal.begin() as s:
        row = s.get(deployments.Deployment, dep_id, with_for_update=True)
        if rv.get("code") == "quote_expired" and deployments.can_transition(row.status, "quote_expired"):
            deployments._apply(s, row, "quote_expired", reason=rv["reason"], actor="system", actor_id=None, now=_now())
        if newq:
            row.quote_id = newq["quote_id"]
            row.quoted_price_per_gpu_hour = deployments._d(newq["quote_price_per_gpu_hour"])
            row.quote_basis = newq["price_source"]
            if row.status in ("quote_expired", "approved"):
                deployments._apply(s, row, "pending_approval", reason="re-quoted: needs re-approval", actor="system",
                                   actor_id=None, now=_now(), evidence={"new_quote_id": newq["quote_id"]})
            deployments.note_event(s, row, f"quote re-validation failed ({rv['reason']}); new quote {newq['quote_id']}",
                                   {"new_quote_id": newq["quote_id"], "price": newq["quote_price_per_gpu_hour"]},
                                   actor="system")
        else:
            deployments.note_event(s, row, f"quote re-validation failed ({rv['reason']})", None, actor="system")


def reject(rr_id: str, who, *, reason: str) -> dict:
    if not reason or not reason.strip():
        raise RouteRefused("reason_required", "a reason is required", 422)
    d = deployments.for_request(rr_id)
    if d is None:
        raise RouteRefused("not_found", "no deployment for this route request", 404)
    actor_id = _actor(who)[1]
    with normalize.SessionLocal.begin() as s:
        row = s.get(deployments.Deployment, d.deployment_id, with_for_update=True)
        if row.status == "rejected":
            pass
        elif row.status in deployments.PRE_LAUNCH_STATES and row.launch_token is None:
            deployments._apply(s, row, "rejected", reason=reason, actor="admin", actor_id=actor_id, now=_now())
            control.log_action(s, "reject_launch", f"deployment:{row.deployment_id}", None, None, reason, actor_id)
        else:
            raise RouteRefused("not_rejectable", f"deployment is {row.status}; terminate it instead")
        qid = row.quote_id
    if qid:
        quotes.expire(qid)
    audit.close_request(rr_id, "rejected", {"deployment_id": d.deployment_id, "reason": reason})
    return {"route_request_id": rr_id, "status": "rejected", "deployment": deployments.public(d.deployment_id)}


# --------------------------------------------------------------------------
# validation launches (admin)
# --------------------------------------------------------------------------

def create_validation_route(provider: str, offer_listing_id_or_none: str | None = None, *, by: str,
                            max_runtime_minutes: int | None = None, launch: dict | None = None) -> str:
    """A purpose='validation' route for `provider`: ONE instance, within the validation caps, always pending
    admin approval (approve() then re-validates and launches). Returns the route_request_id. Raises RouteRefused
    when the control plane, credentials or listing do not allow one."""
    from sqlalchemy import select

    from accounts.auth import OPERATOR
    from tables import ComputeListingRow

    provider = (provider or "").lower()
    cls = adapters.get(provider)
    if cls is None or adapters.level(provider) < 2:
        raise RouteRefused("no_adapter", f"OpenGrid cannot provision {provider}")
    allowed, mode_used, why = control.launch_permission(provider, purpose="validation")
    if not allowed:
        raise RouteRefused("launch_not_permitted", why)
    runtime = int(max_runtime_minutes or settings.validation_max_runtime_minutes)
    if runtime > settings.validation_max_runtime_minutes or runtime <= 0:
        raise RouteRefused("validation_max_runtime_minutes",
                           f"validation runtime must be 1..{settings.validation_max_runtime_minutes} minutes", 422)
    with normalize.SessionLocal() as s:
        q = select(ComputeListingRow).where(ComputeListingRow.provider == provider,
                                            ComputeListingRow.canonical_gpu_name.is_not(None),
                                            ComputeListingRow.price_per_gpu_hour.is_not(None))
        if offer_listing_id_or_none:
            q = q.where(ComputeListingRow.listing_id == offer_listing_id_or_none)
        else:
            q = q.where(ComputeListingRow.available.is_not(False)).order_by(ComputeListingRow.price_per_instance_hour)
        row = s.scalars(q.limit(1)).first()
    if row is None:
        raise RouteRefused("listing_not_found", f"no priced listing for {provider}" +
                           (f" with id {offer_listing_id_or_none}" if offer_listing_id_or_none else ""), 404)
    price = float(row.price_per_gpu_hour)
    c = {"rank": 1, "provider": provider, "listing_id": row.listing_id, "sku": row.sku, "raw_gpu_name": row.raw_gpu_name,
         "gpu": row.canonical_gpu_name, "gpu_count": row.gpu_count, "region": row.region, "price_per_gpu_hour": price,
         "price_per_instance_hour": float(row.price_per_instance_hour or price * row.gpu_count),
         "provider_tier": row.provider_tier, "observed_at": row.observed_at.isoformat() if row.observed_at else None}
    spec = {"gpu": row.canonical_gpu_name, "count": row.gpu_count, "region_group": None, "mode": "CHEAPEST",
            "purpose": "validation", "validation_by": by, "max_runtime_minutes": runtime, "launch": launch}
    ranking = {"mode": "CHEAPEST", "weights": {}, "candidates": [c], "multi_instance_alternatives": [], "exclusions": [],
               "market": {}, "as_of": _now().isoformat()}
    rr_id = audit.open_request(OPERATOR, spec, ranking, preview=False)
    try:
        resolved = credentials.resolve_for_launch(_operator_account(), provider)
    except CredentialsUnavailable as exc:
        audit.close_request(rr_id, "not_provisioned", {"reason": exc.message})
        raise RouteRefused("credential_unusable", exc.message)
    if resolved is None:
        audit.close_request(rr_id, "not_provisioned", {"reason": "no credentials"})
        raise RouteRefused("no_credentials", f"no credentials configured for {provider}")
    a = _build(provider, resolved.credentials, {"route_request_id": rr_id})
    try:
        offer = _offer(c, spec)
        lspec, problem = launch_spec_for(provider, launch, purpose="validation", credential_source=resolved.source,
                                         adapter_cls=cls)
        missing = a.missing_launch(lspec, offer) if lspec else []
        if problem or missing:
            audit.close_request(rr_id, "not_provisioned", {"reason": problem or missing})
            raise RouteRefused("launch_spec_invalid", problem or ("missing launch parameters: " + ", ".join(missing)), 422)
        try:
            avail, q = _call_check(a, offer, {"route_request_id": rr_id})
        except AdapterError as exc:
            audit.close_request(rr_id, "not_provisioned", {"reason": exc.kind})
            raise RouteRefused("availability_check_failed", f"{provider}: availability check failed ({exc.kind})")
    finally:
        a.close()
    if avail.available is False:
        audit.close_request(rr_id, "not_provisioned", {"reason": "unavailable"})
        raise RouteRefused("unavailable", f"{provider} listing {row.listing_id} is not available on live check")
    qrec = quotes.issue(route_request_id=rr_id, account_id=None, offer=offer, observed_price=price,
                        quote_price=q.price_per_gpu_hour,
                        price_source="live_check" if q.basis == "live_provider_api" else "observed",
                        duration_hours=runtime / 60, region_group=None, availability=avail, adapter_cls=cls,
                        purpose="validation", credential_source=resolved.source)
    violations = guards.check(None, provider=provider, gpu_count=offer.gpu_count, price_per_gpu_hour=q.price_per_gpu_hour,
                              est_total_cost=qrec["est_total_cost"], region=qrec["region"], purpose="validation",
                              max_runtime_minutes=runtime)
    dep_id = deployments.create(rr_id=rr_id, who=OPERATOR, spec=spec, quote=qrec,
                                candidate={"list_price_per_gpu_hour": avail.list_price_per_gpu_hour}, purpose="validation",
                                approval_mode=control.SUPERVISED, limit_violations=violations, launch_request=launch,
                                max_runtime_minutes=runtime, auto_approve=False, actor="admin", actor_id=by)
    control.record("create_validation_route", f"provider:{provider}", after={"route_request_id": rr_id,
                   "deployment_id": dep_id, "quote_id": qrec["quote_id"]}, reason="validation launch requested", actor=by)
    audit.close_request(rr_id, "pending_approval", {"deployment_id": dep_id, "quote_id": qrec["quote_id"],
                                                    "purpose": "validation"})
    return rr_id
