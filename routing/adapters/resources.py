"""Temporary provider-side resources OpenGrid creates for a deployment (today: per-deployment SSH keys).

Lifecycle of a per-deployment key (table provider_resources, store/reconcile.py, migration 0015):

    before_register   the row is written (status 'creating', name og-<deployment>, deployment_id,
                      credential_ref pinned at launch, SHA256 fingerprint of the PUBLIC key) and COMMITTED
                      before the provider call. If it cannot be written the registration is refused
                      (fail closed: OpenGrid never creates a key it has not recorded).
    after_register    status 'active' + provider key id.
    register_failed   the call never left -> 'not_created'; sent but failed/ambiguous -> stays 'creating'
                      with the error; reconciliation finds the key by name in list_ssh_keys() and fills the id.
    cleanup_due       once the deployment is CONFIRMED terminated (or definitively never launched an
                      instance: provision_failed / provider_rejected / rejected without an instance id), the
                      key is deleted with the deployment's PINNED credential. Failure -> 'deleting', retried
                      with backoff (ssh_key_delete_retry_base_seconds x 2^n, cap 1 h); after
                      ssh_key_delete_retry_max attempts -> 'delete_failed' + ops alert, retries continue.
    reconcile_keys    (from routing/reconcile.py, per provider credential) list_ssh_keys(): fills missing ids,
                      re-opens a 'deleted' record whose key is still listed, and flags og-* keys with no
                      record ('abandoned'): recorded as provably ours only when the name is og-<deployment> of
                      a deployment on that provider pinned to that very credential; otherwise surfaced as
                      unowned (ops alert) and NEVER deleted automatically.

Deletion rules (never relaxed): a key is deleted only when it is named og-* AND either OpenGrid recorded it at
registration, or it is provably associated with a deployment on the same provider and credential. Keys not
named og-* are ignored entirely.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

log = logging.getLogger(__name__)

OG = re.compile(r"^og-[a-z0-9][a-z0-9-]*$")
SSH_KEY = "ssh_key"
MAX_BACKOFF_SECONDS = 3600
CLEANUP_STATES = ("active", "creating", "deleting", "delete_failed", "abandoned")
OPEN_STATES = ("creating", "active", "deleting", "delete_failed", "abandoned")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _settings():
    from config import settings
    return settings


def fingerprint(public_key: str | None) -> str | None:
    """OpenSSH SHA256 fingerprint ("SHA256:<base64, no padding>") of a public key line; None if unparseable."""
    if not public_key:
        return None
    parts = str(public_key).strip().split()
    blob = parts[1] if len(parts) >= 2 else parts[0]
    try:
        raw = base64.b64decode(blob + "=" * (-len(blob) % 4), validate=True)
    except Exception:  # noqa: BLE001
        return None
    return "SHA256:" + base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")


def _ev(row, event: str, **kw) -> None:
    row.evidence = ([{"at": _now().isoformat(), "event": event, **{k: v for k, v in kw.items() if v is not None}}]
                    + list(row.evidence or []))[:30]


def _dep_id_from_name(name: str) -> str | None:
    return name[3:] if name and name.startswith("og-") else None


def _deployment(dep_id: str | None):
    if not dep_id:
        return None
    import normalize
    from store.routing import Deployment

    with normalize.SessionLocal() as s:
        return s.get(Deployment, dep_id)


def _dep_ref(d) -> str | None:
    if d is None:
        return None
    if d.credential_ref:
        return d.credential_ref
    if d.credential_source == "opengrid" and d.provider:
        from routing import credentials
        return f"platform:{credentials.credential_provider(d.provider)}"
    return None


# --------------------------------------------------------------------------
# Registration hooks (called by Adapter.register_ssh_key)
# --------------------------------------------------------------------------

def before_register(adapter, name: str, public_key: str) -> int | None:
    """Write-ahead record. Returns the row id (None outside a deployment context: direct adapter use in
    tools/tests, which the core never does for a launch). Raises AdapterError(sent=False) when it cannot record."""
    from routing.adapters.base import CONFIG, AdapterError

    ctx = getattr(adapter, "log_context", None) or {}
    dep_id = ctx.get("deployment_id")
    if not dep_id:
        log.warning("%s: ssh key %s registered outside a deployment context: not tracked", adapter.provider, name)
        return None
    try:
        import normalize
        from store.reconcile import ProviderResource

        d = _deployment(dep_id)
        ref = ctx.get("credential_ref") or _dep_ref(d) or ""
        now = _now()
        with normalize.SessionLocal.begin() as s:
            sid = s.execute(insert(ProviderResource).values(
                provider=adapter.provider, resource_type=SSH_KEY, name=name, deployment_id=dep_id, credential_ref=ref,
                fingerprint=fingerprint(public_key), recorded_by="register", created_at=now, status="creating",
                delete_attempts=0, evidence=[{"at": now.isoformat(), "event": "recorded before registration"}],
                updated_at=now).on_conflict_do_nothing(constraint="uq_provider_resource_name")
                .returning(ProviderResource.id)).scalar()
            if sid is None:
                row = s.scalars(select(ProviderResource).where(
                    ProviderResource.provider == adapter.provider, ProviderResource.credential_ref == ref,
                    ProviderResource.resource_type == SSH_KEY, ProviderResource.name == name).with_for_update()).one()
                if row.status not in ("deleted", "not_created"):
                    raise AdapterError(CONFIG, f"{adapter.provider}: an ssh key named {name} is already tracked "
                                               f"(status {row.status}); refusing a second registration", sent=False)
                row.status, row.provider_resource_id, row.deployment_id = "creating", None, dep_id
                row.fingerprint, row.recorded_by, row.deleted_at, row.delete_attempts = fingerprint(public_key), \
                    "register", None, 0
                row.updated_at = now
                _ev(row, "recorded before registration (re-used name)")
                sid = row.id
        return sid
    except AdapterError:
        raise
    except Exception as exc:  # noqa: BLE001 - fail closed
        log.exception("could not record ssh key %s before registration", name)
        raise AdapterError(CONFIG, f"{adapter.provider}: could not record the ssh key before registering it "
                                   f"({type(exc).__name__}); registration refused", sent=False)


def after_register(rid: int | None, ref) -> None:
    if rid is None:
        return
    import normalize
    from store.reconcile import ProviderResource

    for i in range(3):
        try:
            with normalize.SessionLocal.begin() as s:
                row = s.get(ProviderResource, rid, with_for_update=True)
                row.provider_resource_id = str(ref.key_id) if ref.key_id else None
                row.status, row.registered_at, row.updated_at = "active", _now(), _now()
                if ref.fingerprint:
                    row.fingerprint = ref.fingerprint
                _ev(row, "registered", key_id=row.provider_resource_id)
            return
        except Exception:  # noqa: BLE001 - the 'creating' row stays; reconciliation fills the id by name
            log.exception("recording ssh key %s registration failed (try %d)", rid, i + 1)


def register_failed(rid: int | None, exc, message: str) -> None:
    if rid is None:
        return
    import normalize
    from store.reconcile import ProviderResource

    try:
        with normalize.SessionLocal.begin() as s:
            row = s.get(ProviderResource, rid, with_for_update=True)
            row.last_error = (message or "")[:1000]
            if getattr(exc, "sent", None) is False:
                row.status = "not_created"
                _ev(row, "registration never reached the provider")
            else:
                _ev(row, "registration answer failed/ambiguous: the key may exist (resolved by name)",
                    kind=getattr(exc, "kind", None))
            row.updated_at = _now()
    except Exception:  # noqa: BLE001
        log.exception("recording ssh key registration failure %s failed", rid)


# --------------------------------------------------------------------------
# Cleanup after confirmed termination
# --------------------------------------------------------------------------

def _deployment_done(d) -> tuple[bool, str]:
    """(True, why) when the deployment can no longer need its key: confirmed terminated, or definitively never
    created an instance."""
    if d is None:
        return False, "no deployment"
    if d.status == "terminated":
        return True, "deployment confirmed terminated"
    if d.status in ("provision_failed", "provider_rejected", "rejected") and not d.provider_instance_id:
        return True, f"deployment {d.status}: no instance was created"
    return False, f"deployment is {d.status}"


def provably_ours(row) -> tuple[bool, str]:
    """A key may be deleted only if named og-* AND recorded at registration, or provably associated."""
    if not row.name or not OG.match(row.name):
        return False, "not named og-*"
    if row.recorded_by == "register" and row.deployment_id:
        return True, "recorded by OpenGrid before registration"
    if row.recorded_by == "reconcile_found" and row.deployment_id:
        d = _deployment(row.deployment_id)
        if d is not None and d.provider == row.provider and _dep_ref(d) == row.credential_ref \
                and (d.client_name or f"og-{d.deployment_id}") == row.name:
            return True, "og-<deployment> of a deployment pinned to the same provider credential"
    return False, "no OpenGrid ownership record"


def _adapter(provider: str, ref: str, pinned: str | None = None):
    from routing import adapters, credentials

    creds = credentials.for_ref(ref, provider)
    credentials.check_pinned(creds, pinned, ref)   # never delete with a replaced (other-account) key
    a = adapters.build(provider, creds)
    if a is not None:
        a.log_context = {"job": "resource_cleanup"}
    return a


def cleanup_due(*, now: datetime | None = None, resource_id: int | None = None, force: bool = False) -> list[dict]:
    """Delete every due key whose deployment is done. Idempotent. Returns per-row outcomes."""
    import normalize
    from store.reconcile import ProviderResource

    now = now or _now()
    with normalize.SessionLocal() as s:
        q = select(ProviderResource).where(ProviderResource.resource_type == SSH_KEY,
                                           ProviderResource.status.in_(CLEANUP_STATES))
        if resource_id is not None:
            q = q.where(ProviderResource.id == resource_id)
        rows = list(s.scalars(q.order_by(ProviderResource.id).limit(200)))
    out = []
    for row in rows:
        try:
            r = _cleanup_one(row, now, force=force)
        except Exception as exc:  # noqa: BLE001 - one key never stops the others
            log.exception("ssh key cleanup %s failed", row.id)
            r = {"id": row.id, "outcome": "error", "message": f"{type(exc).__name__}: {str(exc)[:200]}"}
        if r:
            out.append(r)
    return out


def _cleanup_one(row, now: datetime, *, force: bool = False) -> dict | None:
    import normalize
    from store.reconcile import ProviderResource

    ours, why_ours = provably_ours(row)
    if not ours:
        return {"id": row.id, "outcome": "skipped", "reason": why_ours} if force else None
    dep = _deployment(row.deployment_id)
    done, why = _deployment_done(dep)
    if not done:
        return {"id": row.id, "outcome": "skipped", "reason": why} if force else None
    if not force and row.next_delete_at is not None and now < row.next_delete_at:
        return None
    from routing.credentials import CredentialsUnavailable

    try:
        from routing import credentials
        pinned = credentials.pinned_fingerprint(dep) if _dep_ref(dep) == row.credential_ref else None
        a = _adapter(row.provider, row.credential_ref, pinned)
    except CredentialsUnavailable as exc:
        return _delete_result(row.id, "failed", f"pinned credential unavailable: {exc.message}", now)
    if a is None:
        return _delete_result(row.id, "failed", f"no adapter for {row.provider}", now)
    try:
        key_id = row.provider_resource_id
        if not key_id:
            # crash between the registration call and recording its id: find it by exact name
            from routing.adapters.base import AdapterError
            try:
                found = [k for k in a.list_ssh_keys() if k.name == row.name]
            except AdapterError as exc:
                return _delete_result(row.id, "failed", f"list_ssh_keys failed: {a.scrub(exc.message)[:200]}", now)
            if not found:
                with normalize.SessionLocal.begin() as s:
                    r = s.get(ProviderResource, row.id, with_for_update=True)
                    r.status = "not_created" if r.status == "creating" else "deleted"
                    r.deleted_at = r.deleted_at or now if r.status == "deleted" else None
                    r.updated_at = now
                    _ev(r, "absent from list_ssh_keys: nothing to delete", basis="list_ssh_keys by name")
                return {"id": row.id, "outcome": "absent"}
            if len(found) > 1:
                return _delete_result(row.id, "failed", f"{len(found)} keys named {row.name}: not deleted", now)
            key_id = found[0].key_id
            with normalize.SessionLocal.begin() as s:
                r = s.get(ProviderResource, row.id, with_for_update=True)
                r.provider_resource_id = str(key_id)
                _ev(r, "key id found by name", key_id=str(key_id))
        with normalize.SessionLocal.begin() as s:
            r = s.get(ProviderResource, row.id, with_for_update=True)
            r.delete_requested_at = r.delete_requested_at or now
            if r.status not in ("delete_failed",):
                r.status = "deleting"
            r.updated_at = now
        res = a.delete_ssh_key(key_id)
    finally:
        a.close()
    return _delete_result(row.id, res.outcome, res.message, now, why=why)


def _delete_result(rid: int, outcome: str, message: str, now: datetime, *, why: str | None = None) -> dict:
    import normalize
    from store.reconcile import ProviderResource

    st = _settings()
    alert = None
    with normalize.SessionLocal.begin() as s:
        r = s.get(ProviderResource, rid, with_for_update=True)
        if outcome in ("accepted", "already_gone"):
            r.status, r.deleted_at, r.last_error, r.next_delete_at = "deleted", now, None, None
            _ev(r, f"deleted ({outcome})", basis=why)
        else:
            r.delete_attempts = (r.delete_attempts or 0) + 1
            delay = min(MAX_BACKOFF_SECONDS, int(st.ssh_key_delete_retry_base_seconds) * (2 ** (r.delete_attempts - 1)))
            r.next_delete_at = now + timedelta(seconds=delay)
            r.last_error = f"{outcome}: {(message or '')[:500]}"
            if r.delete_attempts >= int(st.ssh_key_delete_retry_max):
                r.status = "delete_failed"
                alert = {"provider": r.provider, "deployment_id": r.deployment_id, "name": r.name,
                         "key_id": r.provider_resource_id, "attempts": r.delete_attempts, "first_at": r.delete_requested_at}
            else:
                r.status = "deleting"
            _ev(r, f"delete {outcome}", attempt=r.delete_attempts, message=(message or "")[:200])
        r.updated_at = now
        attempts = r.delete_attempts
    if alert:
        from alerts import ops
        d = _deployment(alert["deployment_id"])
        ops.exposure("resource_delete_failed", f"provider_resource:{rid}",
                     f"{alert['provider']} ssh key {alert['name']} could not be deleted after {alert['attempts']} attempts",
                     deployment_id=alert["deployment_id"], provider=alert["provider"],
                     account_id=getattr(d, "account_id", None), est_hourly_exposure_usd=0.0,
                     time_in_state_seconds=(now - alert["first_at"]).total_seconds() if alert["first_at"] else None,
                     suggested_action=f"delete ssh key {alert['name']} (id {alert['key_id']}) in the {alert['provider']} "
                                      "console, then POST /v1/admin/resources/{id}/cleanup to record it",
                     detail={"resource_id": rid, "key_name": alert["name"], "key_id": alert["key_id"],
                             "last_error": (message or "")[:200]})
    elif outcome in ("accepted", "already_gone"):
        try:
            from alerts import ops
            ops.resolve("resource_delete_failed", f"provider_resource:{rid}",
                        {"basis": f"provider delete {outcome}", "at": now.isoformat()})
        except Exception:  # noqa: BLE001
            log.exception("resolving delete alert %s failed", rid)
    return {"id": rid, "outcome": outcome, "attempts": attempts, "message": (message or "")[:200]}


# --------------------------------------------------------------------------
# Reconciliation: the provider's key list vs our records
# --------------------------------------------------------------------------

def reconcile_keys(adapter, provider: str, ref: str, *, find) -> dict:
    """Compare list_ssh_keys() with provider_resources for one provider credential. `find(kind, **kw)` records
    a reconciliation finding. Raises AdapterError when the list fails (the caller records it)."""
    import normalize
    from store.reconcile import ProviderResource
    from store.routing import Deployment

    keys = adapter.list_ssh_keys()
    now = _now()
    og = [k for k in keys if k.name and OG.match(k.name)]
    out = {"keys": len(keys), "og_keys": len(og), "abandoned": 0, "unowned": 0, "ids_filled": 0, "reopened": 0}
    with normalize.SessionLocal() as s:
        recs = list(s.scalars(select(ProviderResource).where(ProviderResource.provider == provider,
                                                             ProviderResource.credential_ref == ref,
                                                             ProviderResource.resource_type == SSH_KEY)))
        deps = {d.client_name: d for d in s.scalars(select(Deployment).where(
            Deployment.client_name.in_({k.name for k in og}))) } if og else {}
    by_id = {r.provider_resource_id: r for r in recs if r.provider_resource_id}
    by_name = {r.name: r for r in recs}
    listed_names = {k.name for k in keys}
    for k in og:
        rec = by_id.get(str(k.key_id)) or by_name.get(k.name)
        if rec is not None:
            with normalize.SessionLocal.begin() as s:
                r = s.get(ProviderResource, rec.id, with_for_update=True)
                if not r.provider_resource_id:
                    r.provider_resource_id = str(k.key_id)
                    if r.status == "creating":
                        r.status = "active"
                    _ev(r, "key id found in list_ssh_keys", key_id=str(k.key_id))
                    out["ids_filled"] += 1
                elif r.status in ("deleted", "not_created") and str(r.provider_resource_id) == str(k.key_id):
                    r.status, r.next_delete_at = "deleting", None
                    _ev(r, "recorded as deleted but still listed by the provider: re-opened")
                    out["reopened"] += 1
                    find("ssh_key_still_listed", provider=provider, name=k.name, key_id=k.key_id, resource_id=r.id)
                r.updated_at = now
                st, dep_id = r.status, r.deployment_id
            done, _ = _deployment_done(_deployment(dep_id))
            if done and st in CLEANUP_STATES:
                find("ssh_key_abandoned", provider=provider, name=k.name, key_id=k.key_id, deployment_id=dep_id,
                     resource_id=rec.id, provably_ours=True)
                out["abandoned"] += 1
            continue
        d = deps.get(k.name)
        provable = d is not None and d.provider == provider and _dep_ref(d) == ref
        vals = dict(provider=provider, resource_type=SSH_KEY, provider_resource_id=str(k.key_id), name=k.name,
                    deployment_id=d.deployment_id if provable else None, credential_ref=ref,
                    fingerprint=k.fingerprint, recorded_by="reconcile_found" if provable else "reconcile_unowned",
                    created_at=now, registered_at=k.created_at, status="abandoned", delete_attempts=0,
                    evidence=[{"at": now.isoformat(), "event": "og-* key listed with no OpenGrid record",
                               "provable": provable}], updated_at=now)
        with normalize.SessionLocal.begin() as s:
            rid = s.execute(insert(ProviderResource).values(**vals).on_conflict_do_nothing(
                constraint="uq_provider_resource_name").returning(ProviderResource.id)).scalar()
        out["abandoned"] += 1
        find("ssh_key_abandoned", provider=provider, name=k.name, key_id=k.key_id,
             deployment_id=vals["deployment_id"], resource_id=rid, provably_ours=provable)
        if not provable:
            out["unowned"] += 1
    # Records whose registration was ambiguous and the key is not listed: never created.
    stale = now - timedelta(minutes=int(getattr(_settings(), "provisioning_timeout_minutes", 15)))
    for r0 in recs:
        if r0.status == "creating" and not r0.provider_resource_id and r0.name not in listed_names \
                and r0.created_at < stale:
            with normalize.SessionLocal.begin() as s:
                r = s.get(ProviderResource, r0.id, with_for_update=True)
                if r.status == "creating" and not r.provider_resource_id:
                    r.status, r.updated_at = "not_created", now
                    _ev(r, "absent from list_ssh_keys after the provisioning timeout: never created")
    return out


def unowned(provider: str | None = None) -> list[dict]:
    """og-* keys found at a provider without an OpenGrid ownership record (never auto-deleted)."""
    return [r for r in resources(type_=SSH_KEY, status="abandoned")
            if r["recorded_by"] == "reconcile_unowned" and (provider is None or r["provider"] == provider)]


# --------------------------------------------------------------------------
# Admin
# --------------------------------------------------------------------------

def _dict(r) -> dict:
    iso = lambda t: None if t is None else t.isoformat()  # noqa: E731
    ours, why = provably_ours(r)
    return {"id": r.id, "provider": r.provider, "resource_type": r.resource_type,
            "provider_resource_id": r.provider_resource_id, "name": r.name, "deployment_id": r.deployment_id,
            "credential_ref": r.credential_ref, "fingerprint": r.fingerprint, "recorded_by": r.recorded_by,
            "status": r.status, "created_at": iso(r.created_at), "registered_at": iso(r.registered_at),
            "delete_requested_at": iso(r.delete_requested_at), "deleted_at": iso(r.deleted_at),
            "delete_attempts": r.delete_attempts, "next_delete_at": iso(r.next_delete_at), "last_error": r.last_error,
            "provably_ours": ours, "ownership_basis": why, "evidence": r.evidence}


def resources(*, type_: str | None = None, status: str | None = None, leftover: bool = False,
              limit: int = 500) -> list[dict]:
    """provider_resources rows, newest first. leftover=True: everything not yet deleted / not_created."""
    import normalize
    from store.reconcile import ProviderResource

    with normalize.SessionLocal() as s:
        q = select(ProviderResource).order_by(ProviderResource.id.desc()).limit(limit)
        if type_:
            q = q.where(ProviderResource.resource_type == type_)
        if status:
            q = q.where(ProviderResource.status == status)
        elif leftover:
            q = q.where(ProviderResource.status.in_(OPEN_STATES))
        return [_dict(r) for r in s.scalars(q)]


def get(resource_id: int) -> dict | None:
    import normalize
    from store.reconcile import ProviderResource

    with normalize.SessionLocal() as s:
        r = s.get(ProviderResource, resource_id)
        return _dict(r) if r else None


def cleanup(resource_id: int, by: str) -> dict:
    """Operator-requested cleanup of one resource NOW (ignores the backoff). Only provably-ours resources whose
    deployment is done are ever deleted; anything else raises ValueError (delete it in the provider console)."""
    import normalize
    from store.reconcile import ProviderResource

    with normalize.SessionLocal() as s:
        row = s.get(ProviderResource, resource_id)
    if row is None:
        raise LookupError("no such resource")
    if row.status in ("deleted", "not_created"):
        return {**_dict(row), "result": {"outcome": "noop", "reason": f"already {row.status}"}}
    ours, why = provably_ours(row)
    if not ours:
        raise ValueError(f"not provably OpenGrid's ({why}): OpenGrid never deletes it; remove it in the provider "
                         "console if it is yours")
    done, why_done = _deployment_done(_deployment(row.deployment_id))
    if not done:
        raise ValueError(f"not deletable yet: {why_done} (keys are deleted only after confirmed termination)")
    out = cleanup_due(resource_id=resource_id, force=True)
    try:
        from routing import control
        control.record("resource_cleanup", f"provider_resource:{resource_id}", before={"status": row.status},
                       after={"result": out}, reason="operator cleanup", actor=by)
    except Exception:  # noqa: BLE001
        log.exception("control log for resource cleanup %s failed", resource_id)
    return {**(get(resource_id) or {}), "result": out[0] if out else {"outcome": "noop"}}
