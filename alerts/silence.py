"""Alert on silence: a polled provider that stops producing data raises ONE ops alert, and one when it recovers.

Jarvislabs (Oct 25-30) and Nebius (since Oct 19) both broke silently: the health view showed them down, nobody
was told. This job (`silence_alerts`, every 10 minutes) watches three conditions per provider:

    provider_silent   no successful fetch (raw_snapshots.ok) for more than
                      max(SILENCE_ALERT_MIN_HOURS, SILENCE_ALERT_POLLS x polling interval)  (15 min poll -> 3h, 1h -> 6h)
    provider_empty    the provider had listings and now has 0 current ones (none seen within market.stale_after),
                      for SILENCE_ALERT_EMPTY_GRACE_MINUTES past the stale window
    capacity_silent   the provider had capacity rows and none was refreshed within the silence threshold, while its
                      prices are still arriving (a silent provider already has its own alert)

Each condition is a row in ops_alert_state (kind, subject = provider), the same table the cost-exposure alerts use,
so the state survives a restart: an open condition is alerted once when it starts, never repeated while it stays
open, and closed on evidence (the newer fetch / listing count) with one "recovered" alert. Delivery is the ops path
(alerts/ops.alert): a quality incident on /ops always, plus OPS_ALERT_WEBHOOK_URL when set.

Never alerted on (new conditions are not opened; an open one can still recover):
    * providers this process does not poll (poller.ACTIVE): no API key so the fetcher was not built, or
      POLLER_ENABLED off;
    * providers killed in routing/control (provider_execution_flags.killed) or listed in SILENCE_ALERT_MUTED.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

import market
import normalize
import poller
from config import settings
from jobs import job

log = logging.getLogger(__name__)

KINDS = ("provider_silent", "provider_empty", "capacity_silent")
# When this process first saw a provider it polls that has never had a successful fetch (in memory: after a
# restart the clock starts again, so a never-working provider is alerted one threshold after startup).
_WATCH_START: dict[str, datetime] = {}

_STATE = text("""
    SELECT (SELECT max(fetched_at) FROM raw_snapshots WHERE provider = :p AND ok) AS last_ok,
           (SELECT count(*) FROM compute_listings WHERE provider = :p) AS listings_total,
           (SELECT count(*) FROM compute_listings WHERE provider = :p AND observed_at >= :live_since) AS listings_now,
           (SELECT max(observed_at) FROM compute_listings WHERE provider = :p) AS listing_last_seen,
           (SELECT max(observed_at) FROM capacity_current WHERE provider = :p) AS capacity_last_seen
""")


def threshold(interval_seconds: float) -> timedelta:
    """How long without a successful fetch before a provider counts as silent."""
    return max(timedelta(hours=float(settings.silence_alert_min_hours)),
               timedelta(seconds=float(interval_seconds) * int(settings.silence_alert_polls)))


def _interval(provider: str) -> float:
    from providers import PROVIDERS

    if provider in poller.ACTIVE:
        return poller.ACTIVE[provider]
    cls = PROVIDERS.get(provider)
    return float(cls.polling.interval_seconds) if cls else 900.0


def _human(td: timedelta | None) -> str | None:
    if td is None:
        return None
    m = max(0, int(td.total_seconds() // 60))
    return f"{m // 1440}d{(m % 1440) // 60}h" if m >= 1440 else f"{m // 60}h{m % 60:02d}m" if m >= 60 else f"{m}m"


def _muted() -> set[str]:
    return {p.strip().lower() for p in (settings.silence_alert_muted or "").split(",") if p.strip()}


def _killed() -> set[str]:
    with normalize.SessionLocal() as s:
        return set(s.execute(text("SELECT provider FROM provider_execution_flags WHERE killed")).scalars())


def _open_subjects() -> dict[str, set[str]]:
    from store.reconcile import OpsAlertState

    with normalize.SessionLocal() as s:
        rows = s.execute(select(OpsAlertState.kind, OpsAlertState.subject).where(
            OpsAlertState.kind.in_(KINDS), OpsAlertState.status == "open")).all()
    out: dict[str, set[str]] = {k: set() for k in KINDS}
    for kind, subject in rows:
        out[kind].add(subject)
    return out


# --------------------------------------------------------------------------
# State transitions (Postgres, row-locked, so two processes never both send the "started" alert)
# --------------------------------------------------------------------------

def _start(kind: str, provider: str, now: datetime, payload: dict) -> bool:
    """Open the condition. True only on the transition (new, or a resolved one recurring): that is when to send."""
    from store.reconcile import OpsAlertState

    with normalize.SessionLocal.begin() as s:
        new = s.execute(insert(OpsAlertState).values(
            kind=kind, subject=provider, provider=provider, status="open", first_at=now, last_seen_at=now,
            last_sent_at=now, sent_count=1, payload=payload, updated_at=now,
        ).on_conflict_do_nothing(constraint="uq_ops_alert_kind_subject").returning(OpsAlertState.id)).first()
        if new is not None:
            return True
        row = s.scalars(select(OpsAlertState).where(OpsAlertState.kind == kind, OpsAlertState.subject == provider)
                        .with_for_update()).one()
        row.last_seen_at, row.updated_at = now, now
        if row.status == "open":
            return False                      # already alerted for this episode: no repeat
        row.status, row.first_at, row.resolved_at, row.resolution = "open", now, None, None
        row.last_sent_at, row.sent_count, row.payload = now, 1, payload
        return True


def _recover(kind: str, provider: str, now: datetime, evidence: dict) -> datetime | None:
    """Close an open condition on evidence. Returns when it had started, or None if nothing was open."""
    from store.reconcile import OpsAlertState

    with normalize.SessionLocal.begin() as s:
        row = s.scalars(select(OpsAlertState).where(OpsAlertState.kind == kind, OpsAlertState.subject == provider,
                                                    OpsAlertState.status == "open").with_for_update()).first()
        if row is None:
            return None
        row.status, row.resolved_at, row.updated_at = "resolved", now, now
        row.resolution = {"by": "silence_watch", "at": now.isoformat(), **evidence}
        return row.first_at


def _close_incidents(kind: str, provider: str) -> None:
    from quality import incidents

    try:
        with normalize.SessionLocal.begin() as s:
            for k in (f"exec_{kind}", f"exec_{kind}_recovered"):
                incidents.resolve(s, k, provider)
    except Exception:  # noqa: BLE001
        log.exception("closing %s incidents for %s failed", kind, provider)


_TITLES = {
    "provider_silent": "{p}: no successful price fetch for {age}",
    "provider_empty": "{p}: 0 current listings (had {n})",
    "capacity_silent": "{p}: capacity feed silent for {age}",
}
_ACTIONS = {
    "provider_silent": "Check the provider's raw fetch errors on /ops (Data health); the page or API may have changed.",
    "provider_empty": "Fetches may succeed but parse to nothing: check the raw payload and the normalizer.",
    "capacity_silent": "Prices still arrive but capacity does not: check the capacity endpoint and its key.",
}


def _send_start(kind: str, provider: str, facts: dict, age: timedelta | None) -> None:
    from alerts import ops

    title = _TITLES[kind].format(p=provider, age=_human(age) or "ever", n=facts.get("listings_total"))
    ops.alert(kind, title, severity="notable", provider=provider, dedupe=provider,
              detail={**facts, "provider": provider, "time_in_state": _human(age), "suggested_action": _ACTIONS[kind]})


def _send_recovered(kind: str, provider: str, facts: dict, started: datetime, now: datetime) -> None:
    from alerts import ops

    what = {"provider_silent": "fetching again", "provider_empty": f"{facts.get('listings_now')} current listings",
            "capacity_silent": "capacity arriving again"}[kind]
    ops.alert(f"{kind}_recovered", f"Recovered: {provider} {what} (after {_human(now - started)})", severity="info",
              provider=provider, dedupe=provider,
              detail={**facts, "provider": provider, "time_in_state": _human(now - started)})
    _close_incidents(kind, provider)


# --------------------------------------------------------------------------
# The check
# --------------------------------------------------------------------------

def _iso(t):
    return t.isoformat() if t else None


def _capacity_limit(provider: str, limit: timedelta) -> timedelta:
    """A capacity feed declared slower than prices (capacity_interval_seconds) is silent after two of its gaps."""
    from providers import PROVIDERS

    cls = PROVIDERS.get(provider)
    slow = getattr(cls, "capacity_interval_seconds", None) if cls else None
    return max(limit, timedelta(seconds=2 * slow)) if slow else limit


def evaluate(provider: str, now: datetime) -> dict:
    """Pure-ish read: the three conditions for one provider. Each is True (alerting), False (fine) or None
    (cannot tell: e.g. never had listings, so 'empty' does not apply)."""
    interval = _interval(provider)
    limit = threshold(interval)
    stale = market.stale_after(provider)
    with normalize.SessionLocal() as s:
        r = s.execute(_STATE, {"p": provider, "live_since": now - stale}).one()
    ref = r.last_ok or _WATCH_START.get(provider)
    silent_for = None if ref is None else now - ref
    silent = None if silent_for is None else silent_for > limit
    empty = None
    if r.listings_total:
        if r.listings_now:
            empty = False
        else:   # it became empty when its newest listing went stale
            grace = timedelta(minutes=int(settings.silence_alert_empty_grace_minutes))
            empty = now - (r.listing_last_seen + stale) >= grace
    cap_age = None if r.capacity_last_seen is None else now - r.capacity_last_seen
    cap_limit = _capacity_limit(provider, limit)
    capacity = None if cap_age is None else cap_age > cap_limit
    facts = {"polling_interval_seconds": int(interval), "threshold": _human(limit), "last_ok_fetch": _iso(r.last_ok),
             "listings_total": r.listings_total, "listings_now": r.listings_now,
             "listing_last_seen": _iso(r.listing_last_seen), "capacity_last_seen": _iso(r.capacity_last_seen)}
    return {"provider_silent": (silent, silent_for), "provider_empty": (empty, None if not r.listing_last_seen
                                                                         else now - r.listing_last_seen),
            "capacity_silent": (capacity, cap_age), "facts": facts}


def run(now: datetime | None = None) -> dict:
    """One pass over every polled provider (and every provider with an open condition). Never raises per provider."""
    now = now or datetime.now(timezone.utc)
    out = {"started": [], "recovered": [], "open": 0, "checked": 0, "skipped": []}
    if not settings.silence_alerts_enabled:
        out["skipped"].append("disabled (SILENCE_ALERTS_ENABLED=false)")
        return out
    active = dict(poller.ACTIVE)
    for p in active:
        _WATCH_START.setdefault(p, now)
    open_ = _open_subjects()
    blocked = _muted() | _killed()
    for provider in sorted(set(active) | set().union(*open_.values())):
        may_open = provider in active and provider not in blocked
        try:
            state = evaluate(provider, now)
        except Exception:  # noqa: BLE001
            log.exception("silence check for %s failed", provider)
            continue
        out["checked"] += 1
        facts = state["facts"]
        silent_now = state["provider_silent"][0] is True
        for kind in KINDS:
            cond, age = state[kind]
            if kind == "capacity_silent" and silent_now:
                cond = None          # the provider alert already covers it; neither open nor close
            is_open = provider in open_[kind]
            try:
                if cond is True and may_open and not is_open:
                    if _start(kind, provider, now, facts):
                        _send_start(kind, provider, facts, age)
                        out["started"].append(f"{kind}:{provider}")
                elif cond is False and is_open:
                    started = _recover(kind, provider, now, {"evidence": facts})
                    if started is not None:
                        _send_recovered(kind, provider, facts, started, now)
                        out["recovered"].append(f"{kind}:{provider}")
            except Exception:  # noqa: BLE001 - one provider's failure never stops the others
                log.exception("silence alert %s for %s failed", kind, provider)
    out["open"] = sum(len(v) for v in _open_subjects().values())
    return out


@job("silence_alerts", every_seconds=600, initial_delay_seconds=300)
def silence_alerts():
    return run()
