"""The routing audit log: every preview and every route, with everything that was considered.

A route_request row is written before anything else happens, with its routing_decision
(every candidate's factor values and score, every exclusion and why, the weights, the
market snapshot used), so a decision can be reconstructed even if execution then fails.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select, update

import normalize
from routing.scoring import METHODOLOGY_VERSION
from store.routing import RouteRequest, RoutingDecision


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(10)}"


def new_deployment_id() -> str:
    """'dep-<hex>': only [a-z0-9-], so the provider instance name og-<deployment_id> needs no rewriting.
    (Deployments created before 0010 keep their 'dep_<hex>' ids; they stay readable.)"""
    return f"dep-{secrets.token_hex(10)}"


def redacted(spec: dict) -> dict:
    """The request as stored: launch env VALUES may be secrets, so only their names are kept."""
    out = dict(spec)
    if out.get("launch"):
        launch = dict(out["launch"])
        if launch.get("env"):
            launch["env"] = {k: "***" for k in launch["env"]}
        out["launch"] = launch
    return out


def open_request(who, spec: dict, ranking: dict, *, preview: bool) -> str:
    """Write route_requests + routing_decisions; returns the route_request_id."""
    rr_id = new_id("rr")
    now = datetime.now(timezone.utc)
    top = (ranking.get("candidates") or [None])[0]
    snapshot = {**ranking["market"], "ranking_as_of": ranking["as_of"],
                "history_last_hour": ranking.get("history_last_hour"),
                "selected_observed_at": top and top["observed_at"]}
    with normalize.SessionLocal.begin() as s:
        s.add(RouteRequest(id=rr_id, account_id=who.account_id, key_id=who.key_id, principal_kind=who.kind,
                           preview=preview, mode=ranking["mode"], gpu=spec["gpu"], request=redacted(spec),
                           status="previewed" if preview else "routing", created_at=now))
        s.add(RoutingDecision(
            route_request_id=rr_id, mode=ranking["mode"], weights=ranking["weights"],
            candidates=ranking["candidates"], multi_instance=ranking["multi_instance_alternatives"],
            exclusions=ranking["exclusions"], selected_provider=top and top["provider"],
            selected_listing_id=top and top["listing_id"],
            selected_observed_price_per_gpu_hour=None if not top else Decimal(str(top["price_per_gpu_hour"])),
            market_snapshot=snapshot, methodology_version=METHODOLOGY_VERSION, created_at=now))
    return rr_id


def close_request(rr_id: str, status: str, result: dict) -> None:
    with normalize.SessionLocal.begin() as s:
        s.execute(update(RouteRequest).where(RouteRequest.id == rr_id).values(status=status, result=result))


def get(rr_id: str, who) -> dict | None:
    """A request with its decision, if `who` may see it (operator sees all)."""
    with normalize.SessionLocal() as s:
        rr = s.get(RouteRequest, rr_id)
        if rr is None or (who.account_id is not None and rr.account_id != who.account_id):
            return None
        d = s.scalars(select(RoutingDecision).where(RoutingDecision.route_request_id == rr_id)).first()
    out = {"route_request_id": rr.id, "preview": rr.preview, "mode": rr.mode, "gpu": rr.gpu,
           "request": rr.request, "status": rr.status, "result": rr.result, "created_at": rr.created_at.isoformat()}
    if d is not None:
        out["decision"] = {
            "weights": d.weights, "candidates": d.candidates, "multi_instance_alternatives": d.multi_instance,
            "exclusions": d.exclusions, "selected_provider": d.selected_provider,
            "selected_listing_id": d.selected_listing_id,
            "selected_observed_price_per_gpu_hour": None if d.selected_observed_price_per_gpu_hour is None
            else float(d.selected_observed_price_per_gpu_hour),
            "market_snapshot": d.market_snapshot, "methodology_version": d.methodology_version,
        }
    return out
