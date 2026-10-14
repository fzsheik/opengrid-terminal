"""Price-lock requests: validation, the indicative reference, transitions (methodology/reservations.md).

OpenGrid has NO provider capacity commitments of its own, so nothing here is a quote, a binding price or a
guaranteed reservation:
    * a request is the customer's ask (GPU, count, GPU-hours, start window, region, their price limit);
    * the reference attached to it is INDICATIVE: the OpenGrid index level, the best observed on-demand listings
      and the latest settlement fixing (when the index_fixings table exists), all observed public prices;
    * an operator offer REQUIRES a recorded supply basis (the provider capacity commitment that backs it);
    * acceptance records the customer's intent only: no launch, no charge, no payment. The order form is signed
      outside the API.

transition() is the ONLY writer of reservation_requests.status; every move writes a reservation_events row.
Tenant isolation: a key sees only its account's requests; the operator (account None) sees all.
"""

from __future__ import annotations

import logging
import math
import re
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text

import normalize
from jobs import job
from reservations import states as S
from store.reservations import ReservationEvent, ReservationRequest

log = logging.getLogger(__name__)

METHODOLOGY = "reservations"
INDICATIVE = "indicative"

# Bounds (mirrored by web/pages/reserve.js; the server is the authority).
MAX_GPU_COUNT = 1024
MAX_TOTAL_GPU_HOURS = 10_000_000
MAX_PRICE = 1000.0                      # $/GPU-h, both the customer's limit and an offer
MAX_NOTES = 2000
MAX_HORIZON = timedelta(days=731)       # start_before at most ~2 years out
MIN_WINDOW = timedelta(hours=1)         # start_before - start_after
CLOCK_SKEW = timedelta(minutes=5)       # start_after may be this far in the past
MAX_OPEN_PER_ACCOUNT = 20               # open (requested / under_review / offered) requests per account
MAX_OFFER_VALIDITY = timedelta(days=30)
MIN_OFFER_VALIDITY = timedelta(minutes=15)
MIN_SUPPLY_BASIS = 10                   # characters: "see email" is not a supply basis
MAX_TEXT = 4000
BEST_LISTINGS = 5

FIELDS = {"gpu", "gpu_count", "total_gpu_hours", "start_after", "start_before", "region_group", "region",
          "max_price_per_gpu_hour", "notes"}

INDICATIVE_NOTE = ("INDICATIVE only: observed public list prices and OpenGrid index levels at the time shown. "
                   "Not a quote, not an offer, not a binding price.")
ACCEPTED_MESSAGE = ("Accepted. OpenGrid will contact you to sign the order form; nothing is billed by this "
                    "action.")
NOT_A_CONTRACT = ("A price-lock request is a commercial request for compute delivered by OpenGrid, not a "
                  "financial contract, not tradable, no cash settlement. An offer is made only when provider "
                  "capacity backs it, and binds only in a signed order form.")


class Refused(Exception):
    """A refusal the API returns as HTTP `status` with {code, message, ...}."""

    def __init__(self, code: str, message: str, status: int = 422, **extra):
        super().__init__(message)
        self.code, self.message, self.status, self.extra = code, message, status, extra

    def detail(self) -> dict:
        return {"code": self.code, "message": self.message, **self.extra}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(t: datetime | None) -> str | None:
    return t.isoformat() if t else None


def new_request_id() -> str:
    return "rsv-" + secrets.token_hex(8)


def actor_of(who) -> tuple[str, str]:
    aid = f"key:{who.key_id}" if who.key_id is not None else (who.kind or "operator")
    return ("admin" if who.has("admin") else "user"), aid


# --------------------------------------------------------------------------
# Validation (pure apart from the GPU catalogue)
# --------------------------------------------------------------------------

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _num(body: dict, field: str, *, integer: bool = False, lo=None, hi=None, lo_exclusive: bool = False,
         required: bool = True):
    v = body.get(field)
    if v is None:
        if required:
            raise Refused("missing_field", f"{field} is required", field=field)
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or (isinstance(v, float) and not math.isfinite(v)):
        raise Refused("invalid_number", f"{field} must be a number", field=field)
    if integer and (isinstance(v, float) and not v.is_integer()):
        raise Refused("invalid_number", f"{field} must be a whole number", field=field)
    if lo is not None and (v <= lo if lo_exclusive else v < lo):
        raise Refused("out_of_range", f"{field} must be {'>' if lo_exclusive else '>='} {lo}", field=field)
    if hi is not None and v > hi:
        raise Refused("out_of_range", f"{field} must be <= {hi}", field=field)
    return int(v) if integer else float(v)


def _time(body: dict, field: str) -> datetime:
    v = body.get(field)
    if not isinstance(v, str) or not v.strip():
        raise Refused("missing_field", f"{field} is required (ISO 8601 with a timezone, e.g. "
                                       f"2026-11-01T00:00:00Z)", field=field)
    try:
        t = datetime.fromisoformat(v.strip().replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        raise Refused("invalid_time", f"{field} is not an ISO 8601 time", field=field) from None
    if t.tzinfo is None:
        raise Refused("timezone_required", f"{field} needs a timezone (e.g. Z or +00:00)", field=field)
    return t.astimezone(timezone.utc)


def _text(v, field: str, *, limit: int, required: bool = False, min_len: int = 0) -> str | None:
    if v is None or (isinstance(v, str) and not v.strip()):
        if required:
            raise Refused("missing_field", f"{field} is required", field=field)
        return None
    if not isinstance(v, str):
        raise Refused("invalid_text", f"{field} must be a string", field=field)
    v = v.strip()
    if _CTRL.search(v):
        raise Refused("invalid_text", f"{field} contains control characters", field=field)
    if len(v) > limit:
        raise Refused("out_of_range", f"{field} is over {limit} characters", field=field)
    if len(v) < min_len:
        raise Refused("out_of_range", f"{field} needs at least {min_len} characters", field=field)
    return v


def _region(v) -> str | None:
    if v is None or v == "":
        return None
    from regions import REGION_GROUPS
    if isinstance(v, str):
        for g in REGION_GROUPS:
            if g.lower() == v.strip().lower():
                return g
    raise Refused("unknown_region", f"unknown region group {v!r}; one of {', '.join(REGION_GROUPS)}",
                  field="region_group")


def resolve_gpu(v) -> str:
    if not isinstance(v, str) or not v.strip() or len(v) > 128:
        raise Refused("missing_field", "gpu is required (a GPU slug or canonical name, see /v1/gpus)", field="gpu")
    from api.common import resolve_gpu as rg
    name = rg(v.strip(), strict=False)
    if name is None:
        raise Refused("unknown_gpu", f"unknown GPU {v!r}: name one GPU variant (see /v1/gpus); families and "
                                     f"'auto' are not accepted for a price lock", field="gpu")
    return name


def validate(body: dict, *, now: datetime | None = None) -> dict:
    """The request body -> a normalized spec. Raises Refused (422) with {code, message, field}."""
    now = now or _now()
    if not isinstance(body, dict):
        raise Refused("invalid_request", "the request must be a JSON object")
    extra = sorted(set(body) - FIELDS)
    if extra:
        raise Refused("unknown_field", f"unknown field(s): {', '.join(extra)}", field=extra[0])
    if "region" in body and "region_group" in body:
        raise Refused("invalid_request", "send region_group (or its alias region), not both", field="region")
    gpu = resolve_gpu(body.get("gpu"))
    count = _num(body, "gpu_count", integer=True, lo=1, hi=MAX_GPU_COUNT)
    hours = _num(body, "total_gpu_hours", lo=1, hi=MAX_TOTAL_GPU_HOURS)
    if hours < count:
        raise Refused("out_of_range", f"total_gpu_hours ({hours:g}) must cover at least one hour on each of the "
                                      f"{count} GPUs", field="total_gpu_hours")
    if round(hours, 2) != hours:
        raise Refused("invalid_number", "total_gpu_hours has at most 2 decimals", field="total_gpu_hours")
    start_after, start_before = _time(body, "start_after"), _time(body, "start_before")
    if start_after < now - CLOCK_SKEW:
        raise Refused("start_in_past", "start_after is in the past", field="start_after")
    if start_before - start_after < MIN_WINDOW:
        raise Refused("window_too_short", "start_before must be at least 1 hour after start_after",
                      field="start_before")
    if start_before > now + MAX_HORIZON:
        raise Refused("out_of_range", f"start_before is more than {MAX_HORIZON.days} days ahead", field="start_before")
    price = _num(body, "max_price_per_gpu_hour", lo=0, lo_exclusive=True, hi=MAX_PRICE)
    return {"gpu": gpu, "gpu_count": count, "total_gpu_hours": hours, "start_after": start_after,
            "start_before": start_before, "region_group": _region(body.get("region_group", body.get("region"))),
            "max_price_per_gpu_hour": price, "notes": _text(body.get("notes"), "notes", limit=MAX_NOTES)}


# --------------------------------------------------------------------------
# The INDICATIVE reference (never a quote)
# --------------------------------------------------------------------------

def _index_ref(gpu: str) -> dict:
    from analytics import indices as I

    iid = I.gpu_index_id(gpu)
    d = I.REGISTRY.get(iid)
    if d is None:
        return {"available": False, "index_id": None, "label": INDICATIVE,
                "reason": "OpenGrid publishes no on-demand index for this GPU"}
    try:
        lv = I.index_level(iid) or {}
    except Exception:  # noqa: BLE001 - a reference is best-effort; the request does not depend on it
        log.exception("reservation reference: index level for %s failed", iid)
        lv = {"reason": "index level unavailable right now"}
    return {"available": lv.get("level") is not None, "index_id": iid, "name": d.name, "label": INDICATIVE,
            "price_concept": "observed_market_price", "unit": d.unit, "level": lv.get("level"),
            "raw_level": lv.get("raw_level"), "published": bool(lv.get("published")), "hour": lv.get("hour"),
            "constituents": lv.get("constituents"), "methodology_version": lv.get("methodology_version"),
            "last_published": lv.get("last_published"), "reason": lv.get("reason"),
            "link": f"/indices/{iid}", "methodology": "/methodology/indices"}


def _best_ref(gpu: str, gpu_count: int, region_group: str | None) -> dict:
    from routing import scoring

    out = {"label": INDICATIVE, "price_concept": "observed_market_price", "region_group": region_group,
           "listings": [], "market": None, "reason": None, "link": f"/v1/best/{_slug(gpu)}"}
    try:
        r = scoring.rank_listings(gpu, count=min(gpu_count, 8), region_group=region_group, mode="CHEAPEST",
                                  limit=BEST_LISTINGS)
    except Exception as e:  # noqa: BLE001 - best-effort, never blocks the request
        log.warning("reservation reference: ranking %s failed: %s", gpu, e)
        out["reason"] = "the listing ranking is unavailable right now"
        return out
    cands = sorted(r["candidates"] + r["multi_instance_alternatives"],
                   key=lambda c: (c["price_per_gpu_hour"], c["age_seconds"], c["provider"]))
    out["listings"] = [{
        "provider": c["provider"], "provider_display": c.get("provider_display"), "listing_id": c["listing_id"],
        "gpu_count": c["gpu_count"], "region": c.get("region"), "country": c.get("country"),
        "price_per_gpu_hour": c["price_per_gpu_hour"], "price_kind": "observed_market_price",
        "available": c.get("available"), "observed_at": c.get("observed_at"), "age_seconds": c.get("age_seconds"),
        "label": INDICATIVE} for c in cands[:BEST_LISTINGS]]
    m = r.get("market") or {}
    out["market"] = {"median": m.get("median"), "low": m.get("low"), "low_provider": m.get("low_provider"),
                     "providers": m.get("providers"), "as_of": m.get("as_of"), "kind": "observed_market_price"}
    out["as_of"] = r.get("as_of")
    if not out["listings"]:
        out["reason"] = ("no current on-demand listing" + (f" in {region_group}" if region_group else "")
                         + " passes the eligibility checks (see the link for exclusions)")
    return out


_FIX_COLS = ("fixing_id", "index_id", "fixing_type", "fixing_time", "status", "value", "reason",
             "methodology_version", "flags", "restates", "created_at")


def _fixing_ref(index_id: str | None) -> dict:
    """The latest daily and hourly settlement fixing, read from index_fixings when that table exists."""
    out = {"label": INDICATIVE, "available": False, "daily": None, "hourly": None, "reason": None,
           "methodology": "/methodology/settlement"}
    if not index_id:
        out["reason"] = "no OpenGrid index for this GPU, so no settlement fixing"
        return out
    try:
        with normalize.SessionLocal() as s:
            if s.execute(text("SELECT to_regclass('index_fixings')")).scalar() is None:
                out["reason"] = "settlement fixings are not available on this server"
                return out
            cols = set(s.execute(text("""SELECT column_name FROM information_schema.columns
                                         WHERE table_name = 'index_fixings'""")).scalars())
            want = [c for c in _FIX_COLS if c in cols]
            if not {"index_id", "fixing_type", "fixing_time"} <= set(want):
                out["reason"] = "settlement fixings table has an unexpected shape"
                return out
            order = "fixing_time DESC" + (", seq DESC" if "seq" in cols else "")
            for t in ("daily", "hourly"):
                r = s.execute(text(f"SELECT {', '.join(want)} FROM index_fixings "
                                   f"WHERE index_id = :i AND fixing_type = :t ORDER BY {order} LIMIT 1"),
                              {"i": index_id, "t": t}).first()
                if r is not None:
                    d = {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in r._mapping.items()}
                    if d.get("fixing_id"):
                        d["link"] = f"/v1/fixings/{d['fixing_id']}"
                        d["verify"] = f"/v1/fixings/{d['fixing_id']}/verify"
                    out[t] = d
    except Exception:  # noqa: BLE001 - tolerate absence / a concurrent migration; never fail the request
        log.exception("reservation reference: reading index_fixings failed")
        out["reason"] = "settlement fixings could not be read"
        return out
    out["available"] = bool(out["daily"] or out["hourly"])
    if not out["available"]:
        out["reason"] = "no settlement fixing has been published for this index yet"
    return out


def _slug(gpu: str) -> str:
    from api.common import gpu_slug
    return gpu_slug(gpu)


def reference(gpu: str, gpu_count: int = 1, region_group: str | None = None,
              max_price: float | None = None) -> dict:
    """What the market shows now for this GPU: labelled indicative everywhere, never a quote."""
    idx = _index_ref(gpu)
    out = {"label": INDICATIVE, "binding": False, "is_quote": False, "note": INDICATIVE_NOTE, "as_of": _iso(_now()),
           "gpu": gpu, "slug": _slug(gpu), "gpu_count": gpu_count, "region_group": region_group,
           "index": idx, "best_on_demand": _best_ref(gpu, gpu_count, region_group),
           "settlement_fixing": _fixing_ref(idx.get("index_id"))}
    if max_price is not None and idx.get("level"):
        out["limit_vs_index"] = {"customer_limit": max_price, "index_level": idx["level"],
                                 "pct": round(max_price / idx["level"] - 1, 4), "label": INDICATIVE}
    return out


# --------------------------------------------------------------------------
# Public shape
# --------------------------------------------------------------------------

def _f(v):
    return None if v is None else float(v)


def public(r: ReservationRequest, *, events: list | None = None, viewer=None) -> dict:
    now = _now()
    offer = None
    if r.offer_seq and r.offer_price_per_gpu_hour is not None:
        p = float(r.offer_price_per_gpu_hour)
        total = round(p * float(r.total_gpu_hours), 2)
        offer = {"offer_seq": r.offer_seq, "price_per_gpu_hour": p, "total_gpu_hours": float(r.total_gpu_hours),
                 "total_usd": total, "total_kind": "offer (not billed; binds only in a signed order form)",
                 "valid_until": _iso(r.offer_valid_until),
                 "valid": r.status == "offered" and r.offer_valid_until is not None and r.offer_valid_until > now,
                 "terms": r.offer_terms, "supply_basis": r.offer_supply_basis, "offered_at": _iso(r.offered_at),
                 "above_customer_limit": p > float(r.max_price_per_gpu_hour), "binding": False,
                 "price_concept": "offer"}
    out = {
        "request_id": r.request_id, "status": r.status, "terminal": S.is_terminal(r.status),
        "account_id": r.account_id,
        "gpu": r.gpu, "slug": _slug(r.gpu), "gpu_count": r.gpu_count, "total_gpu_hours": float(r.total_gpu_hours),
        "start_after": _iso(r.start_after), "start_before": _iso(r.start_before), "region_group": r.region_group,
        "max_price_per_gpu_hour": float(r.max_price_per_gpu_hour),
        "max_total_usd": round(float(r.max_price_per_gpu_hour) * float(r.total_gpu_hours), 2),
        "notes": r.notes, "indicative": r.indicative, "offer": offer,
        "accepted_at": _iso(r.accepted_at), "closed_reason": r.closed_reason,
        "created_at": _iso(r.created_at), "updated_at": _iso(r.updated_at),
        "state_changed_at": _iso(r.state_changed_at),
        "launched": False, "billed": False, "note": NOT_A_CONTRACT,
        "actions": _actions(r, viewer, now),
    }
    if events is not None:
        out["events"] = events
    return out


def _actions(r: ReservationRequest, who, now: datetime) -> list[str]:
    """What the viewer can do now (the UI shows exactly these buttons; the server re-checks)."""
    if who is None:
        return []
    acts = []
    owner = who.account_id == r.account_id
    if owner and r.status in S.OPEN:
        acts.append("withdraw")
    if owner and r.status == "offered" and r.offer_valid_until and r.offer_valid_until > now:
        acts.append("accept")
    if who.has("admin"):
        if r.status == "requested":
            acts.append("review")
        if r.status in ("requested", "under_review", "offered"):
            acts += ["offer", "decline"]
        if r.status == "accepted":
            acts += ["fulfil", "cancel"]
    return acts


def _events(s, request_id: str) -> list[dict]:
    return [{"at": _iso(e.at), "from": e.from_status, "to": e.to_status, "actor": e.actor, "actor_id": e.actor_id,
             "reason": e.reason, "evidence": e.evidence}
            for e in s.scalars(select(ReservationEvent).where(ReservationEvent.request_id == request_id)
                               .order_by(ReservationEvent.at, ReservationEvent.id))]


# --------------------------------------------------------------------------
# Transitions
# --------------------------------------------------------------------------

def transition(s, r: ReservationRequest, to: str, *, actor: str, actor_id: str | None, reason: str | None,
               evidence: dict | None = None, at: datetime | None = None) -> None:
    """The only writer of status. Checks ALLOWED; writes the event row in the same transaction."""
    if not S.can(r.status, to):
        raise Refused("invalid_transition", f"a {r.status.replace('_', ' ')} request cannot become "
                                            f"{to.replace('_', ' ')}", 409, current_status=r.status)
    at = at or _now()
    s.add(ReservationEvent(request_id=r.request_id, at=at, from_status=r.status, to_status=to, actor=actor,
                           actor_id=actor_id, reason=reason, evidence=evidence))
    r.status, r.updated_at, r.state_changed_at = to, at, at
    if S.is_terminal(to) and reason:
        r.closed_reason = reason


def _due(r: ReservationRequest, now: datetime) -> str | None:
    """The expiry reason if this open request is past due, else None."""
    if r.status == "offered" and r.offer_valid_until is not None and r.offer_valid_until <= now:
        return f"offer {r.offer_seq} validity ended {r.offer_valid_until.isoformat()}"
    if r.status in ("requested", "under_review") and r.start_before <= now:
        return "the start window passed with no offer"
    return None


def expire_due(*, now: datetime | None = None, request_id: str | None = None) -> int:
    """Move every past-due open request to `expired` (system actor). Idempotent; returns how many moved."""
    now = now or _now()
    q = select(ReservationRequest).where(ReservationRequest.status.in_(S.OPEN))
    if request_id is not None:
        q = q.where(ReservationRequest.request_id == request_id)
    n = 0
    with normalize.SessionLocal.begin() as s:
        for r in s.scalars(q.with_for_update(skip_locked=True)):
            why = _due(r, now)
            if why:
                transition(s, r, "expired", actor="system", actor_id="expiry", reason=why, at=now)
                n += 1
    return n


def _visible(r: ReservationRequest | None, who) -> bool:
    return r is not None and (who.account_id is None or r.account_id == who.account_id)


def _load(s, request_id: str, who, *, lock: bool = False, admin: bool = False) -> ReservationRequest:
    r = s.get(ReservationRequest, request_id, with_for_update=lock)
    if r is None or (not admin and not _visible(r, who)):
        raise Refused("reservation_not_found", "price-lock request not found", 404)
    return r


def _check_active(who) -> None:
    """New requests / acceptances need an active account (keys.verify already refuses suspended keys' POSTs)."""
    if who.account_id is None:
        return
    try:
        from accounts.accounts import get_account
        a = get_account(who.account_id)
    except Exception:  # noqa: BLE001
        log.exception("account lookup failed for %s", who.account_id)
        raise Refused("account_unavailable", "account status could not be read", 503) from None
    if a is None or a.get("status") != "active":
        raise Refused("account_suspended", "this account is suspended: price-lock requests are read-only", 403)


# --------------------------------------------------------------------------
# Customer side
# --------------------------------------------------------------------------

def create(body: dict, who) -> tuple[int, dict]:
    _check_active(who)
    spec = validate(body)
    with normalize.SessionLocal() as s:
        acct = (ReservationRequest.account_id.is_(None) if who.account_id is None
                else ReservationRequest.account_id == who.account_id)
        open_n = s.execute(select(ReservationRequest.request_id).where(
            acct, ReservationRequest.status.in_(S.OPEN)).limit(MAX_OPEN_PER_ACCOUNT)).all()
    if len(open_n) >= MAX_OPEN_PER_ACCOUNT:
        raise Refused("too_many_open_requests", f"this account already has {MAX_OPEN_PER_ACCOUNT} open price-lock "
                                                f"requests; withdraw one or wait for a decision", 409)
    ref = reference(spec["gpu"], spec["gpu_count"], spec["region_group"], spec["max_price_per_gpu_hour"])
    now = _now()
    kind, aid = actor_of(who)
    r = ReservationRequest(request_id=new_request_id(), account_id=who.account_id, key_id=who.key_id,
                           principal_kind=who.kind, status="requested", indicative=ref, offer_seq=0,
                           created_at=now, updated_at=now, state_changed_at=now, **spec)
    with normalize.SessionLocal.begin() as s:
        s.add(r)
        s.add(ReservationEvent(request_id=r.request_id, at=now, from_status=None, to_status="requested",
                               actor="user" if kind == "user" else kind, actor_id=aid, reason="request submitted",
                               evidence={"max_price_per_gpu_hour": spec["max_price_per_gpu_hour"],
                                         "index_level": (ref.get("index") or {}).get("level")}))
    return 201, get(r.request_id, who)


def get(request_id: str, who, *, admin: bool = False) -> dict:
    expire_due(request_id=request_id)
    with normalize.SessionLocal() as s:
        r = _load(s, request_id, who, admin=admin)
        return public(r, events=_events(s, request_id), viewer=who)


def list_for(who, *, status: str | None = None, limit: int = 100, offset: int = 0, admin: bool = False) -> list[dict]:
    expire_due()
    q = select(ReservationRequest).order_by(ReservationRequest.created_at.desc())
    if not admin and who.account_id is not None:
        q = q.where(ReservationRequest.account_id == who.account_id)
    if status == "open":
        q = q.where(ReservationRequest.status.in_(S.OPEN))
    elif status:
        q = q.where(ReservationRequest.status == status)
    with normalize.SessionLocal() as s:
        return [public(r, viewer=who) for r in s.scalars(q.offset(offset).limit(limit))]


def _owner(r: ReservationRequest, who, what: str) -> None:
    if r.account_id != who.account_id:
        raise Refused("not_owner", f"only the requesting account can {what} this request", 403)


def accept(request_id: str, body: dict, who) -> tuple[int, dict]:
    """Records the customer's intent to take the CURRENT offer. Launches nothing, bills nothing."""
    _check_active(who)
    seq = body.get("offer_seq") if isinstance(body, dict) else None
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        raise Refused("missing_field", "offer_seq is required: the offer number you accept (the offer may have "
                                       "been revised since you read it)", field="offer_seq")
    extra = sorted(set(body) - {"offer_seq", "note"})
    if extra:
        raise Refused("unknown_field", f"unknown field(s): {', '.join(extra)}", field=extra[0])
    note = _text(body.get("note"), "note", limit=MAX_NOTES)
    expire_due(request_id=request_id)
    now = _now()
    _, aid = actor_of(who)
    with normalize.SessionLocal.begin() as s:
        r = _load(s, request_id, who, lock=True)
        _owner(r, who, "accept")
        if r.status == "expired" and r.offer_seq:
            raise Refused("offer_expired", "the offer's validity ended before it was accepted", 409, current_status=r.status)
        if r.status != "offered":
            raise Refused("no_open_offer", f"there is no open offer to accept (status {r.status})", 409,
                          current_status=r.status)
        if seq != r.offer_seq:
            raise Refused("offer_changed", f"offer {seq} is no longer current: OpenGrid revised it (current offer "
                                           f"{r.offer_seq}); read it again before accepting", 409,
                          current_offer_seq=r.offer_seq)
        transition(s, r, "accepted", actor="user", actor_id=aid, reason=note or "offer accepted",
                   evidence={"offer_seq": r.offer_seq, "price_per_gpu_hour": _f(r.offer_price_per_gpu_hour),
                             "valid_until": _iso(r.offer_valid_until), "launched": False, "billed": False},
                   at=now)
        r.accepted_at, r.accepted_by = now, aid
    out = get(request_id, who)
    out.update(message=ACCEPTED_MESSAGE, launched=False, billed=False, deployment_id=None, charge=None)
    return 200, out


def withdraw(request_id: str, body: dict, who) -> tuple[int, dict]:
    reason = _text((body or {}).get("reason"), "reason", limit=MAX_NOTES) or "withdrawn by the customer"
    expire_due(request_id=request_id)
    _, aid = actor_of(who)
    with normalize.SessionLocal.begin() as s:
        r = _load(s, request_id, who, lock=True)
        _owner(r, who, "withdraw")
        if r.status not in S.OPEN:
            raise Refused("invalid_transition", f"a {r.status.replace('_', ' ')} request cannot be withdrawn"
                          + (" (after acceptance, contact OpenGrid: the order form governs)" if r.status == "accepted"
                             else ""), 409, current_status=r.status)
        transition(s, r, "withdrawn", actor="user", actor_id=aid, reason=reason)
    return 200, get(request_id, who)


# --------------------------------------------------------------------------
# Operator side (scope admin)
# --------------------------------------------------------------------------

def _reason(body: dict, *, required: bool = True) -> str | None:
    return _text((body or {}).get("reason"), "reason", limit=MAX_NOTES, required=required, min_len=3 if required else 0)


def review(request_id: str, body: dict, who) -> dict:
    reason = _reason(body, required=False) or "under review"
    _, aid = actor_of(who)
    with normalize.SessionLocal.begin() as s:
        r = _load(s, request_id, who, lock=True, admin=True)
        transition(s, r, "under_review", actor="admin", actor_id=aid, reason=reason)
    return get(request_id, who, admin=True)


def offer(request_id: str, body: dict, who) -> dict:
    """Make (or revise) an offer. supply_basis is REQUIRED: no offer without provider capacity behind it."""
    if not isinstance(body, dict):
        raise Refused("invalid_request", "the offer must be a JSON object")
    extra = sorted(set(body) - {"price_per_gpu_hour", "valid_until", "terms", "supply_basis", "reason"})
    if extra:
        raise Refused("unknown_field", f"unknown field(s): {', '.join(extra)}", field=extra[0])
    basis = _text(body.get("supply_basis"), "supply_basis", limit=MAX_TEXT)
    if basis is None or len(basis) < MIN_SUPPLY_BASIS:
        raise Refused("supply_basis_required", "an offer needs a recorded supply basis: the provider capacity "
                                               "commitment that backs it (e.g. 'Provider X written capacity "
                                               "commitment ref PX-2026-114, 64x H100 Nov 1-30')",
                      field="supply_basis")
    price = _num(body, "price_per_gpu_hour", lo=0, lo_exclusive=True, hi=MAX_PRICE)
    if round(price, 6) != price:
        raise Refused("invalid_number", "price_per_gpu_hour has at most 6 decimals", field="price_per_gpu_hour")
    now = _now()
    until = _time(body, "valid_until")
    if until < now + MIN_OFFER_VALIDITY:
        raise Refused("out_of_range", f"valid_until must be at least {int(MIN_OFFER_VALIDITY.total_seconds() // 60)} "
                                      f"minutes ahead", field="valid_until")
    if until > now + MAX_OFFER_VALIDITY:
        raise Refused("out_of_range", f"valid_until is more than {MAX_OFFER_VALIDITY.days} days ahead",
                      field="valid_until")
    terms = _text(body.get("terms"), "terms", limit=MAX_TEXT, required=True, min_len=10)
    reason = _reason(body, required=False)
    expire_due(request_id=request_id)
    _, aid = actor_of(who)
    with normalize.SessionLocal.begin() as s:
        r = _load(s, request_id, who, lock=True, admin=True)
        if r.status not in ("requested", "under_review", "offered"):
            raise Refused("invalid_transition", f"a {r.status.replace('_', ' ')} request cannot get an offer", 409,
                          current_status=r.status)
        if r.status == "requested":
            transition(s, r, "under_review", actor="admin", actor_id=aid, reason="reviewed for an offer", at=now)
        prev = ({"offer_seq": r.offer_seq, "price_per_gpu_hour": _f(r.offer_price_per_gpu_hour),
                 "valid_until": _iso(r.offer_valid_until)} if r.offer_seq else None)
        r.offer_seq = (r.offer_seq or 0) + 1
        r.offer_price_per_gpu_hour, r.offer_valid_until, r.offer_terms = price, until, terms
        r.offer_supply_basis, r.offered_at, r.offered_by = basis, now, aid
        transition(s, r, "offered", actor="admin", actor_id=aid,
                   reason=reason or ("offer revised" if prev else "offer made"),
                   evidence={"offer_seq": r.offer_seq, "price_per_gpu_hour": price, "valid_until": _iso(until),
                             "supply_basis": basis, "above_customer_limit": price > float(r.max_price_per_gpu_hour),
                             "supersedes": prev}, at=now)
    return get(request_id, who, admin=True)


def _close(request_id: str, body: dict, who, to: str, allowed_from: tuple) -> dict:
    reason = _reason(body)
    expire_due(request_id=request_id)
    _, aid = actor_of(who)
    with normalize.SessionLocal.begin() as s:
        r = _load(s, request_id, who, lock=True, admin=True)
        if r.status not in allowed_from:
            raise Refused("invalid_transition", f"a {r.status.replace('_', ' ')} request cannot become "
                                                f"{to.replace('_', ' ')}", 409, current_status=r.status)
        transition(s, r, to, actor="admin", actor_id=aid, reason=reason)
    return get(request_id, who, admin=True)


def decline(request_id: str, body: dict, who) -> dict:
    return _close(request_id, body, who, "declined", S.OPEN)


def fulfil(request_id: str, body: dict, who) -> dict:
    return _close(request_id, body, who, "fulfilled", ("accepted",))


def cancel(request_id: str, body: dict, who) -> dict:
    return _close(request_id, body, who, "cancelled", ("accepted",))


# --------------------------------------------------------------------------
# Background expiry
# --------------------------------------------------------------------------

@job("reservation_expiry", every_seconds=300, initial_delay_seconds=120)
def _expiry_job():
    return {"expired": expire_due()}
