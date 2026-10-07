"""Alert rules and firings: storage and validation. Evaluation lives in alerts.evaluator."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

import normalize
from alerts import metrics, notifier
from store.accounts import AlertFiring, AlertRule

STATUSES = ("active", "paused")
MIN_COOLDOWN, DEFAULT_COOLDOWN = 60, 3600


def as_dict(r: AlertRule) -> dict:
    """Never includes the webhook secret."""
    return {"id": r.id, "name": r.name, "kind": r.kind, "params": r.params, "channels": r.channels,
            "status": r.status, "cooldown_seconds": r.cooldown_seconds, "last_evaluated_at": r.last_evaluated_at,
            "last_state": r.last_state, "last_value": r.last_value, "last_detail": r.last_detail,
            "last_fired_at": r.last_fired_at, "created_at": r.created_at,
            "has_webhook_secret": r.webhook_secret_encrypted is not None}


def _cooldown(v) -> int:
    v = DEFAULT_COOLDOWN if v is None else int(v)
    if v < MIN_COOLDOWN:
        raise ValueError(f"cooldown_seconds must be >= {MIN_COOLDOWN}")
    return v


def create(account_id: int, params: dict, name: str | None = None, channels=None, cooldown_seconds=None,
           status: str = "active") -> dict:
    """Returns the rule; when it has a webhook channel, also `webhook_secret` (shown this once)."""
    from accounts.credentials import encrypt

    params = metrics.validate(dict(params))
    channels = notifier.validate_channels(channels)
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    secret = notifier.new_secret() if any(c["type"] == "webhook" for c in channels) else None
    with normalize.SessionLocal.begin() as s:
        r = AlertRule(account_id=account_id, name=name, kind=params["metric"], params=params, channels=channels,
                      status=status, cooldown_seconds=_cooldown(cooldown_seconds),
                      webhook_secret_encrypted=encrypt(secret) if secret else None)
        s.add(r)
        s.flush()
        out = as_dict(r)
    if secret:
        out["webhook_secret"] = secret
    return out


def _owned(s, account_id: int, rule_id: int) -> AlertRule:
    r = s.get(AlertRule, rule_id)
    if r is None or r.account_id != account_id:
        raise KeyError(rule_id)
    return r


def get_rule(account_id: int, rule_id: int) -> dict:
    with normalize.SessionLocal() as s:
        return as_dict(_owned(s, account_id, rule_id))


def all_for(account_id: int) -> list[dict]:
    with normalize.SessionLocal() as s:
        return [as_dict(r) for r in s.scalars(select(AlertRule).where(AlertRule.account_id == account_id).order_by(AlertRule.id))]


def update(account_id: int, rule_id: int, *, name=None, params=None, channels=None, cooldown_seconds=None, status=None) -> dict:
    from accounts.credentials import encrypt

    new_secret = None
    with normalize.SessionLocal.begin() as s:
        r = _owned(s, account_id, rule_id)
        if name is not None:
            r.name = name
        if params is not None:
            r.params = metrics.validate(dict(params))
            r.kind = r.params["metric"]
            r.last_state = r.last_known_state = None  # a new condition starts fresh
        if channels is not None:
            r.channels = notifier.validate_channels(channels)
            if any(c["type"] == "webhook" for c in r.channels) and r.webhook_secret_encrypted is None:
                new_secret = notifier.new_secret()
                r.webhook_secret_encrypted = encrypt(new_secret)
        if cooldown_seconds is not None:
            r.cooldown_seconds = _cooldown(cooldown_seconds)
        if status is not None:
            if status not in STATUSES:
                raise ValueError(f"status must be one of {STATUSES}")
            r.status = status
        out = as_dict(r)
    if new_secret:
        out["webhook_secret"] = new_secret
    return out


def remove(account_id: int, rule_id: int) -> None:
    with normalize.SessionLocal.begin() as s:
        s.delete(_owned(s, account_id, rule_id))


def test(account_id: int, rule_id: int) -> dict:
    """Evaluate now, dry: no state change, no delivery."""
    from alerts import evaluator

    with normalize.SessionLocal() as s:
        r = _owned(s, account_id, rule_id)
        return evaluator.evaluate(r, dry=True)


def firings(account_id: int, rule_id: int | None = None, since: datetime | None = None, limit: int = 100) -> list[dict]:
    with normalize.SessionLocal() as s:
        q = select(AlertFiring).where(AlertFiring.account_id == account_id)
        if rule_id is not None:
            q = q.where(AlertFiring.rule_id == rule_id)
        if since is not None:
            q = q.where(AlertFiring.fired_at >= since)
        rows = s.scalars(q.order_by(AlertFiring.fired_at.desc()).limit(max(1, min(limit, 1000))))
        return [{"id": f.id, "rule_id": f.rule_id, "fired_at": f.fired_at, "value": f.value, "message": f.message,
                 "delivered_via": list(f.delivered_via or []), "delivery_status": f.delivery_status} for f in rows]
