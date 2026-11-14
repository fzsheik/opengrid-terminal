"""What a customer may be SHOWN about internal-tier data (methodology/data-visibility.md).

Ranking may use internal listings to choose and order candidates (routing.scoring.rank_listings(tier="all")), but no
response to a non-admin caller may disclose an internal value: not its price, not anything computed from its price
(costs, fees, savings, scores, comparisons), not its stock or quantities, not raw internal fields (a provider's API
price, an AWS Spot Advisor band). Admin and operator callers see everything, labelled `visibility: internal`.

    can_see_internal(who) -> bool
    redact(payload, who)  -> payload safe for `who` (the same object, unchanged, for an admin / operator)

How an internal candidate is shown to everyone else:
    - price: the PUBLIC listing's price for the same product (same provider, sku and GPU count) when there is one,
      `price_basis: "public_listing"`; otherwise `price_per_gpu_hour: null`, `price_basis: "quote"` (OpenGrid quotes
      at execution time)
    - `availability: "available via OpenGrid"`; every other field is dropped unless it is on SAFE_KEYS
A dict "is" an internal candidate when it carries visibility "internal", or a (provider, listing_id) that is an
internal listing in compute_listings (quotes, exclusions, deployments, limit exclusions carry those). Values
computed from an internal price that sit OUTSIDE such a dict (the route preview's quote, account-limit messages,
alternatives' `vs_selected`, a recommendation's picks and its comparison sentences) are rewritten by the
shape-specific passes below. tests/test_visibility.py proves it by varying the internal prices: a non-admin
response must not change at all.
"""

from __future__ import annotations

import copy
import re

from sqlalchemy import text

import normalize
import provider_meta

LABEL = "available via OpenGrid"
QUOTE_NOTE = "priced by OpenGrid when it quotes at execution time"
# Fields of an internal candidate that say nothing about its price, stock or the provider's internal data.
SAFE_KEYS = {
    "provider", "provider_display", "listing_id", "gpu", "gpu_slug", "variant", "variant_slug", "gpu_count",
    "instances", "region", "country", "region_group", "rank", "option_id", "tier", "vram_gb", "form_factor",
    "architecture", "theoretical", "memory_fit", "count_choice", "integration_level", "provisionable", "execution",
    "market_type", "provider_tier", "interruptible", "kind", "code", "limits", "overridable", "visibility",
    # quote / deployment identity and status (never amounts)
    "quote_id", "route_request_id", "deployment_id", "status", "created_at", "expires_at", "expires_in_seconds",
    "purpose", "consumed_by_deployment_id", "superseded_by", "billing_unit", "minimum_commitment", "hours",
    "hours_from", "duration_hours", "account_id", "gpu_count",
}
_PRICED_LINE = re.compile(r"\$|cheaper|per hour|/h\b|whole job|balanced pick|price|cost", re.I)


def can_see_internal(who) -> bool:
    """The operator (site login) and platform-admin keys see internal data; nobody else."""
    if who is None:
        return False
    return getattr(who, "kind", None) == "operator" or bool(getattr(who, "has", lambda s: False)("admin"))


# ---------------------------------------------------------------- lookups

def _pairs(obj, out: set) -> set:
    if isinstance(obj, dict):
        if isinstance(obj.get("provider"), str) and isinstance(obj.get("listing_id"), str):
            out.add((obj["provider"], obj["listing_id"]))
        if isinstance(obj.get("selected_provider"), str) and isinstance(obj.get("selected_listing_id"), str):
            out.add((obj["selected_provider"], obj["selected_listing_id"]))   # a stored routing decision
        for v in obj.values():
            _pairs(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _pairs(v, out)
    return out


_INTERNAL = text("""
    SELECT i.provider, i.listing_id, i.gpu_count,
           (SELECT p.price_per_gpu_hour FROM compute_listings p
             WHERE p.provider = i.provider AND p.sku = i.sku AND p.gpu_count = i.gpu_count
               AND p.visibility = 'public' AND p.price_per_gpu_hour > 0
               AND p.market_type IS NOT DISTINCT FROM i.market_type
             ORDER BY p.price_per_gpu_hour LIMIT 1) AS twin
    FROM compute_listings i
    WHERE i.visibility <> 'public' AND i.provider = ANY(:ps) AND i.listing_id = ANY(:ls)
""")


def internal_listings(pairs: set) -> dict:
    """{(provider, listing_id): public twin price per GPU-hour or None} for the internal ones among `pairs`."""
    if not pairs:
        return {}
    with normalize.SessionLocal() as s:
        rows = s.execute(_INTERNAL, {"ps": sorted({p for p, _ in pairs}), "ls": sorted({l for _, l in pairs})}).all()
    return {(r.provider, r.listing_id): (None if r.twin is None else float(r.twin))
            for r in rows if (r.provider, r.listing_id) in pairs}


# ---------------------------------------------------------------- masking

def _priced(d: dict, twin: float | None) -> dict:
    n = d.get("gpu_count") or 1
    out = {"price_per_gpu_hour": twin, "price_per_instance_hour": None if twin is None else round(twin * n, 6),
           "price_basis": "public_listing" if twin is not None else "quote", "availability": LABEL,
           "price_note": ("the public list price for this product; " if twin is not None else "") + QUOTE_NOTE}
    if "expected_cost_usd" in d or "price_per_hour" in d:   # a quote block
        hours = d.get("hours")
        out["price_per_hour"] = None if twin is None else round(twin * n, 6)
        out["expected_cost_usd"] = None if twin is None or hours is None else round(twin * n * hours, 2)
    return out


def mask_item(d: dict, twin: float | None) -> dict:
    out = {k: copy.deepcopy(v) for k, v in d.items() if k in SAFE_KEYS}
    out["visibility"] = provider_meta.INTERNAL
    out.update(_priced(d, twin))
    if isinstance(d.get("why"), list):
        out["why"] = [w for w in d["why"] if not _PRICED_LINE.search(str(w))]
    elif isinstance(d.get("why"), str):
        out["why"] = d["why"] if not _PRICED_LINE.search(d["why"]) else LABEL
    if "reason" in d and isinstance(d["reason"], str):
        out["reason"] = d["reason"] if not _PRICED_LINE.search(d["reason"]) else f"excluded ({d.get('code')})"
    return out


def _is_internal(d: dict, internal: dict) -> bool:
    return d.get("visibility") == provider_meta.INTERNAL or (d.get("provider"), d.get("listing_id")) in internal


def _walk(obj, internal: dict):
    if isinstance(obj, dict):
        if _is_internal(obj, internal):
            return mask_item(obj, internal.get((obj.get("provider"), obj.get("listing_id"))))
        out = {k: _walk(v, internal) for k, v in obj.items()}
        sel = (obj.get("selected_provider"), obj.get("selected_listing_id"))
        if sel in internal and "selected_observed_price_per_gpu_hour" in out:   # a stored routing decision
            out["selected_observed_price_per_gpu_hour"] = internal[sel]
            out["selected_price_basis"] = "public_listing" if internal[sel] is not None else "quote"
        return out
    if isinstance(obj, list):
        return [_walk(v, internal) for v in obj]
    return obj


def _strip(obj, key: str):
    if isinstance(obj, dict):
        return {k: _strip(v, key) for k, v in obj.items() if k != key}
    if isinstance(obj, list):
        return [_strip(v, key) for v in obj]
    return obj


def redact(payload, who):
    """`payload` with every internal value hidden from `who` (unchanged for an admin / operator)."""
    if can_see_internal(who):
        return payload
    internal = internal_listings(_pairs(payload, set()))
    flagged = _flagged(payload)
    if not internal and not flagged:
        return payload
    for k in flagged:
        internal.setdefault(k, None)
    # "+12% more expensive" is measured against the selected candidate, internal or not: dropped wherever an
    # internal row is present (route previews, their stored audit records, ranked lists)
    out = _strip(_walk(payload, internal), "vs_selected")
    if isinstance(payload, dict):
        out = _route_preview(payload, out, internal)
        out = _recommendation(payload, out)
        out = _job_selection(payload, out, internal)
    return out


def _flagged(obj, out=None) -> set:
    """(provider, listing_id) of dicts labelled internal (an internal row the database no longer has)."""
    out = set() if out is None else out
    if isinstance(obj, dict):
        if obj.get("visibility") == provider_meta.INTERNAL and obj.get("provider") and obj.get("listing_id"):
            out.add((obj["provider"], obj["listing_id"]))
        for v in obj.values():
            _flagged(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _flagged(v, out)
    return out


def _key(c) -> tuple | None:
    return (c.get("provider"), c.get("listing_id")) if isinstance(c, dict) else None


def _route_preview(src: dict, out: dict, internal: dict) -> dict:
    """Route preview / route: the quote, account-limit messages and comparisons built from the chosen candidate."""
    if "selected" not in src:
        return out
    sel = _key(src.get("selected"))
    chosen = src.get("best_provisionable") if src.get("best_provisionable") else src.get("selected")
    if sel in internal:
        if isinstance(src.get("quote"), dict):
            out["quote"] = {**{k: v for k, v in src["quote"].items() if k in SAFE_KEYS},
                            **_priced(src["quote"], internal[sel])}
    if _key(chosen) in internal:
        out["limit_violations"] = [{k: v for k, v in x.items() if k in ("code", "overridable")}
                                   for x in src.get("limit_violations") or []]
    return out


def _job_selection(src: dict, out: dict, internal: dict) -> dict:
    """Job preview / submit (workloads.select): the cost estimate is computed from the chosen candidate's price."""
    est = src.get("estimate")
    if not isinstance(est, dict) or "est_max_total" not in est:
        return out
    top = src.get("execution_ready") if est.get("for") == "execution_ready" else src.get("market_best")
    k = _key(top)
    if k not in internal:
        return out
    twin, n = internal[k], est.get("gpu_count") or 1
    out["estimate"] = {**{x: est.get(x) for x in ("for", "provider", "gpu", "gpu_count", "kind", "max_total_cost_usd",
                                                    "billed_minutes", "typical_minutes", "billing_unit")},
                       "price_per_gpu_hour": twin, "price_basis": "public_listing" if twin is not None else "quote",
                       "est_max_total": None if twin is None or est.get("billed_minutes") is None
                       else round(twin * n * est["billed_minutes"] / 60, 4),
                       "est_typical_total": None, "est_typical_range": None, "availability": LABEL,
                       "basis": QUOTE_NOTE}
    if isinstance(out.get("execution_ready"), dict) and "limit_violations" in (src.get("execution_ready") or {}):
        out["execution_ready"]["limit_violations"] = [
            {x: v for x, v in lv.items() if x in ("code", "overridable")}
            for lv in (src["execution_ready"].get("limit_violations") or [])]
    return out


def _recommendation(src: dict, out: dict) -> dict:
    """Recommendation: picks and comparison sentences anchored on an internal option."""
    if "options" not in src or "picks" not in src:
        return out
    hidden = {o["option_id"] for o in src.get("options") or [] if isinstance(o, dict) and o.get("visibility") == "internal"}
    if not hidden:
        return out
    # scores relative to the whole pool (value_score: price-normalised across options) are measured against
    # internal prices too: not shown when the pool has an internal option
    for o in out.get("options") or []:
        o.pop("value_score", None)
    for name, p in (out.get("picks") or {}).items():
        if isinstance(p, dict) and p.get("option_id") in hidden:
            out["picks"][name] = {"option_id": p["option_id"], "reason": f"{LABEL}; {QUOTE_NOTE}"}
    best = (src.get("picks") or {}).get("best_for_workload") or {}
    if best.get("option_id") in hidden:
        # every other option's summary and comparison lines are measured against the recommended option's price
        for o in out.get("options") or []:
            if o.get("visibility") != "internal":
                o["summary"] = "compared with the recommended option, which OpenGrid prices at execution time"
                o["why"] = [w for w in o.get("why") or [] if not _PRICED_LINE.search(w)]
    for o in out.get("options") or []:
        if o.get("visibility") == "internal":
            o["summary"] = "Recommended" if best.get("option_id") == o.get("option_id") else LABEL
    _recommendation_explanations(out, hidden, best.get("option_id") in hidden)
    return out


# Factor-by-factor comparisons (intelligence/explain.deltas) that say nothing about an internal option's price,
# stock or listing score: the only ones kept when the comparison is anchored on an internal option.
SAFE_DELTA_FACTORS = {"memory_headroom_pct", "effective_throughput", "decode_tokens_per_s", "gpu_link",
                      "executable_via_opengrid", "estimated_completion_hours"}


def _recommendation_explanations(out: dict, hidden: set, best_hidden: bool) -> None:
    """The workload-intelligence additions (decision, explanation, multi_gpu): an internal option's price, stock and
    score never show through a comparison with it."""
    by_id = {o.get("option_id"): o for o in out.get("options") or [] if isinstance(o, dict)}
    if best_hidden:
        for o in by_id.values():
            d = o.get("decision")
            if isinstance(d, dict) and isinstance(d.get("vs_recommended"), list):
                d["vs_recommended"] = [e for e in d["vs_recommended"] if e.get("factor") in SAFE_DELTA_FACTORS]
    ex = out.get("explanation")
    if isinstance(ex, dict):
        alts = []
        for a in ex.get("alternatives") or []:
            o = by_id.get(a.get("option_id")) or {}
            if a.get("option_id") in hidden:
                alts.append({"option_id": a.get("option_id"), "label": a.get("label"), "summary": LABEL})
                continue
            a = dict(a, summary=o.get("summary", a.get("summary")))
            if best_hidden:
                a["recommended_wins_on"] = [f for f in a.get("recommended_wins_on") or [] if f in SAFE_DELTA_FACTORS]
                a["this_wins_on"] = [f for f in a.get("this_wins_on") or [] if f in SAFE_DELTA_FACTORS]
            alts.append(a)
        if best_hidden:
            out["explanation"] = {"recommended_option_id": ex.get("recommended_option_id"), "label": ex.get("label"),
                                  "summary": f"{LABEL}; {QUOTE_NOTE}", "alternatives": alts, "kind": ex.get("kind")}
        else:
            ex["alternatives"] = alts
    mg = out.get("multi_gpu")
    if isinstance(mg, dict) and isinstance(mg.get("scale_up"), list):
        touched = False
        for p in mg["scale_up"]:
            if any(i in hidden for i in p.get("option_ids") or []) and p.get("job_cost_ratio") is not None:
                p["job_cost_ratio"] = None
                touched = True
        if touched and mg.get("verdict") == "scale_up_optional":
            ok = sorted({p["gpu"] for p in mg["scale_up"]
                         if p.get("job_cost_ratio") is not None and p["job_cost_ratio"] <= 1.0})
            if ok:
                mg["summary"] = ("fits on one GPU; for " + ", ".join(ok) + " more GPUs are estimated to finish "
                                 "sooner for the same or a lower whole-job cost")
            else:
                mg["verdict"] = "single_gpu"
                mg["summary"] = "fits on one GPU: stay on one unless you need it finished sooner"
