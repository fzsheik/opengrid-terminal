"""Alert evaluation: edge-triggered with a cooldown.

Every ~3 minutes (job `alerts`), each active rule's metric is resolved (alerts.metrics) and its
condition checked, giving a state: true | false | unknown.

    fires when    state is true AND the last KNOWN state was not true (false, or never evaluated)
                  AND the cooldown since the last firing has passed
    never fires   on unknown. Unknown also does not overwrite last_known_state, so a data gap
                  between two true readings (true -> unknown -> true) does not re-fire.
    suppressed    a transition inside the cooldown is recorded as known-true without firing; the
                  rule fires again only after it goes false and back to true.

`evaluate(rule, dry=True)` (POST /v1/alerts/{id}/test) computes the same answer, writes nothing and
delivers nothing.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

import normalize
from alerts import metrics, notifier
from jobs import job
from store.accounts import AlertFiring, AlertRule

log = logging.getLogger(__name__)


def state_of(params: dict, reading: metrics.Reading) -> str:
    if reading.unknown:
        return "unknown"
    try:
        op, value = metrics.condition(params)
    except ValueError:
        return "unknown"
    return "true" if metrics.OPS[op](reading.value, value) else "false"


def decide(rule: AlertRule, state: str, now: datetime) -> tuple[bool, str]:
    """(fire?, why). Pure on the rule's stored state."""
    if state != "true":
        return False, state
    if rule.last_known_state == "true":
        return False, "still true (edge-triggered: fires on transition only)"
    if rule.last_fired_at is not None and now - rule.last_fired_at < timedelta(seconds=rule.cooldown_seconds or 0):
        return False, "suppressed by cooldown"
    return True, "transition to true"


def message(rule: AlertRule, reading: metrics.Reading) -> str:
    p = rule.params
    subject = " ".join(str(p[k]) for k in ("gpu", "provider", "region_group", "index_id") if p.get(k))
    try:
        op, value = metrics.condition(p)
        cond = f"{op} {value:g}"
    except ValueError:
        cond = ""
    return f"{rule.name or p['metric']}: {p['metric']} {subject} = {reading.value:.4g} ({cond}). {reading.detail}".strip()


def evaluate(rule: AlertRule, ctx: metrics.Context | None = None, dry: bool = False, session=None) -> dict:
    ctx = ctx or metrics.Context()
    reading = metrics.resolve(rule.params, ctx)
    state = state_of(rule.params, reading)
    fire, why = decide(rule, state, ctx.now)
    out = {"rule_id": rule.id, "state": state, "value": reading.value, "detail": reading.detail,
           "would_fire": fire, "reason": why, "evaluated_at": ctx.now.isoformat(), "dry_run": dry}
    if dry:
        return out
    rule.last_evaluated_at, rule.last_state, rule.last_value, rule.last_detail = ctx.now, state, reading.value, reading.detail[:2000]
    if state != "unknown":
        rule.last_known_state = state
    if fire:
        rule.last_fired_at = ctx.now
        msg = message(rule, reading)
        secret = None
        if rule.webhook_secret_encrypted:
            from accounts.credentials import decrypt

            secret = decrypt(rule.webhook_secret_encrypted)
        payload = {"type": "alert.fired", "rule_id": rule.id, "account_id": rule.account_id, "metric": rule.params.get("metric"),
                   "params": rule.params, "value": reading.value, "message": msg, "fired_at": ctx.now.isoformat()}
        via, status = notifier.deliver(rule.channels or [], payload, secret)
        f = AlertFiring(rule_id=rule.id, account_id=rule.account_id, fired_at=ctx.now, value=reading.value,
                        message=msg, delivered_via=via, delivery_status=status)
        session.add(f)
        out["firing"] = {"message": msg, "delivered_via": via, "delivery_status": status}
    return out


def run(now: datetime | None = None) -> dict:
    """Evaluate every active rule once, sharing one market snapshot."""
    ctx = metrics.Context(now=now or datetime.now(timezone.utc))
    with normalize.SessionLocal() as s:
        ids = list(s.scalars(select(AlertRule.id).where(AlertRule.status == "active").order_by(AlertRule.id)))
    counts = {"rules": len(ids), "fired": 0, "unknown": 0, "errors": 0}
    for rid in ids:  # one transaction per rule: one bad rule never blocks the rest
        try:
            with normalize.SessionLocal.begin() as s:
                rule = s.get(AlertRule, rid, with_for_update=True)
                if rule is None or rule.status != "active":
                    continue
                r = evaluate(rule, ctx, session=s)
            counts["fired"] += bool(r["would_fire"])
            counts["unknown"] += r["state"] == "unknown"
        except Exception:
            counts["errors"] += 1
            log.exception("alert rule %s failed", rid)
    return counts


@job("alerts", every_seconds=180, initial_delay_seconds=90)
def _alerts_job():
    return run()
