"""Reconciliation: make OpenGrid's deployment records agree with what actually exists at each provider.

A job (`reconcile`, every settings.reconcile_interval_seconds, default 120 s) runs run_once(). For every
provider with an adapter, and for every credential the relevant deployments are PINNED to (plus the
OpenGrid-managed credential when configured), it lists every instance on that account
(adapter.list_instances(): all pages, or an error, never a partial list) and compares:

  unresolved launches      provider_timeout / launch_unknown / provisioning-without-instance-id (a worker
                           crash mid-provision) / orphan_suspected-without-id:
                           an instance named og-<deployment> exists -> ADOPT it (link the instance id, move to
                           the state the provider reports, evidence + alert); never a second launch.
                           Nothing found -> wait; only after settings.provisioning_timeout_minutes, and only
                           when the list AND find_instance(name) both prove absence -> provision_failed.
                           Two live instances with the name -> orphan_suspected + duplicate_launch orphans.
  running but gone         list omits the instance AND a status read says not_found/terminated (two
                           independent signals) -> terminated (provider_terminated), with the evidence.
  terminating              confirmed gone by the same two signals -> terminated; still there -> re-issue the
                           delete with exponential backoff (termination_retry_base_seconds x 2^n, capped at
                           1 h); after termination_retry_max attempts -> termination_failed + alert (and it
                           keeps retrying: the instance is still billing).
  deadline                 terminate_deadline_at passed on a live deployment -> terminate (actor system,
                           termination_reason max_runtime_exceeded).
  boot timeout             accepted but not running after the provisioning timeout -> alert; after 4x the
                           timeout -> terminate (boot_timeout; never-running time is not billed).
  credentials_unavailable  the pinned credential cannot be used -> the core's credentials_unavailable state
                           (state kept, never "terminated"), alert hourly, keep trying every run.
  orphans (og-* only)      an og-* instance with no deployment (og_no_deployment), a deployment that ended in
                           OpenGrid while its instance is alive (deployment_ended_alive), a second instance
                           for a live deployment (duplicate_launch) -> orphan_resources + alert. AUTO-TERMINATE
                           only an og-* instance whose deployment exists, is terminal, and was pinned to the very
                           credential that lists it (provably_ours). Never touch an instance OpenGrid cannot prove
                           it owns: anything not named og-* is ignored entirely.

  deadline (outages)       a deployment past terminate_deadline_at is marked past_deadline (deployment_watch
                           .past_deadline_at) and its terminate is re-issued EVERY run while the provider's
                           answers are failed/unknown (API down), whatever the backoff, with a re-escalating
                           past_deadline ops alert until termination is confirmed. The deadline pass is the
                           first thing every run does, so the first run after a restart enforces overdue
                           deadlines before anything else.
  ssh keys                 per credential: list_ssh_keys() vs provider_resources (routing/adapters/resources.py):
                           ids filled, abandoned og-* keys flagged (deleted only when provably ours, else an
                           unowned_provider_resource alert), then due keys of confirmed-terminated deployments
                           are deleted.
  cost exposure            every run ends with _exposures(): the alert catalogue of alerts/ops.exposure()
                           (resource_state_unknown, termination_failed, past_deadline, suspected_orphan,
                           provider_api_unavailable, overspend, active_without_billing,
                           unowned_provider_resource), raised / re-escalated while true, resolved only with
                           evidence that the condition is over.

Every pass is recorded in reconciliation_runs with its findings. Functions for the admin API (owned by the
core / metrics agents): run_once(provider=None), orphans(status=None), resolve_orphan(id, action, by),
runs(limit), last_run().
"""

from __future__ import annotations

import logging
import re
import zlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select, text

import normalize
from config import settings
from jobs import job
from routing import adapters, credentials, deployments, tracker, transactions
from routing.adapters import resources
from routing.adapters.base import AdapterError
from routing.adapters.results import InstanceState, instance_name
from routing.credentials import CredentialsUnavailable
from store.reconcile import DeploymentWatch, OrphanResource, ProviderResource, ReconciliationRun, UsageSlice
from store.routing import Deployment, ProvisionAttempt

log = logging.getLogger(__name__)

OG = re.compile(r"^og-[a-z0-9][a-z0-9-]*$")
RECENT_TERMINAL_DAYS = 7
MAX_BACKOFF_SECONDS = 3600
ORPHAN_ACTIONS = ("terminate", "ignore", "adopt")
TARGET = {"pending": "provisioning", "running": "running", "stopping": "stopping", "stopped": "stopped",
          "error": "degraded", "terminating": "terminating", "terminated": "terminated", "not_found": "terminated"}
_LOCK_KEY = zlib.crc32(b"opengrid:reconcile")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(t):
    return None if t is None else t.isoformat()


def og_name(st: InstanceState) -> str | None:
    """The og-* name an instance carries (its name, or a tag/label), else None."""
    for n in [st.name, *(st.labels or [])]:
        if n and OG.match(str(n)):
            return str(n)
    return None


def _ref(d: Deployment) -> str | None:
    if d.credential_ref:
        return d.credential_ref
    if d.credential_source == "opengrid" and d.provider:
        return f"platform:{credentials.credential_provider(d.provider)}"
    return None


def _term_requested(d) -> bool:
    return getattr(d, "terminate_requested_at", None) is not None or getattr(d, "requested_termination_at", None) is not None


def _creds(ref: str, provider: str) -> dict:
    return credentials.for_ref(ref, provider)


def _brief(st: InstanceState) -> dict:
    return {"instance_id": st.instance_id, "name": st.name, "state": st.state, "provider_status": st.provider_status,
            "observed_at": _iso(st.observed_at)}


class _Run:
    def __init__(self, trigger: str, provider: str | None):
        self.findings: list[dict] = []
        self.providers: dict = {}
        self.listed_ok: set = set()        # (provider, credential_ref) listed successfully this run
        self.list_failed: dict = {}        # (provider, credential_ref) -> error kind
        self.now = _now()
        self.trigger, self.provider = trigger, provider

    def find(self, kind: str, **kw) -> dict:
        f = {"kind": kind, "at": _now().isoformat(), **{k: v for k, v in kw.items() if v is not None}}
        self.findings.append(f)
        return f


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------

def run_once(provider: str | None = None, *, trigger: str = "manual") -> dict:
    """One reconciliation pass (all providers, or one). Returns the recorded run."""
    lock = normalize.SessionLocal()
    try:
        got = lock.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": _LOCK_KEY}).scalar()
        if not got:
            return {"skipped": True, "reason": "another reconciliation pass is running"}
        try:
            return _run(provider, trigger)
        finally:
            lock.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _LOCK_KEY})
    finally:
        lock.close()


def _run(provider: str | None, trigger: str) -> dict:
    run = _Run(trigger, provider)
    with normalize.SessionLocal.begin() as s:
        row = ReconciliationRun(started_at=run.now, trigger=trigger, provider=provider, status="running")
        s.add(row)
        s.flush()
        run_id = row.id
    error = None
    try:
        _enforce_deadlines(run, provider)
        names = [provider] if provider else sorted(adapters.ADAPTERS)
        for p in names:
            try:
                run.providers[p] = _provider(run, p)
            except Exception as exc:  # noqa: BLE001 - one provider never stops the others
                log.exception("reconcile %s failed", p)
                run.providers[p] = {"error": f"{type(exc).__name__}: {str(exc)[:300]}"}
                run.find("provider_error", provider=p, message=str(exc)[:300])
        try:
            run.providers.setdefault("_cost", {})["reconciled"] = transactions.reconcile_pending()
        except Exception:  # noqa: BLE001
            log.exception("cost reconciliation retries failed")
        try:
            cleaned = resources.cleanup_due()
            run.providers.setdefault("_resources", {})["cleanup"] = cleaned
            for c in cleaned:
                run.find("ssh_key_cleanup", resource_id=c.get("id"), outcome=c.get("outcome"),
                         attempts=c.get("attempts"))
        except Exception:  # noqa: BLE001
            log.exception("provider resource cleanup failed")
        try:
            run.providers.setdefault("_alerts", {})["exposure"] = _exposures(run)
        except Exception:  # noqa: BLE001
            log.exception("cost-exposure alert evaluation failed")
    except Exception as exc:  # noqa: BLE001
        log.exception("reconciliation pass failed")
        error = f"{type(exc).__name__}: {str(exc)[:500]}"
    errored = [p for p, v in run.providers.items() if isinstance(v, dict) and v.get("error")]
    if error or errored:
        tracker.alert("reconciliation_failed", f"run:{run_id}",
                      f"reconciliation run {run_id} errored: {error or ', '.join(errored)}",
                      detail={"run_id": run_id, "providers": errored})
    counts: dict = {}
    for f in run.findings:
        counts[f["kind"]] = counts.get(f["kind"], 0) + 1
    partial = any(isinstance(v, dict) and (v.get("error") or v.get("list_errors")) for v in run.providers.values())
    status = "failed" if error else ("partial" if partial else "ok")
    with normalize.SessionLocal.begin() as s:
        row = s.get(ReconciliationRun, run_id)
        row.finished_at, row.status, row.error = _now(), status, error
        row.providers, row.findings, row.counts = run.providers, run.findings[:2000], counts
    return {"run_id": run_id, "status": status, "started_at": run.now.isoformat(), "providers": run.providers,
            "counts": counts, "findings": run.findings, "error": error}


def _relevant(provider: str, now: datetime) -> list[Deployment]:
    since = now - timedelta(days=RECENT_TERMINAL_DAYS)
    with normalize.SessionLocal() as s:
        return list(s.scalars(select(Deployment).where(
            Deployment.provider == provider,
            Deployment.status.in_(deployments.LIVE_STATES)
            | (Deployment.status.in_(deployments.TERMINAL_STATES) & Deployment.launch_token.is_not(None)
               & (Deployment.state_changed_at >= since)))))


def _provider(run: _Run, p: str) -> dict:
    deps = _relevant(p, run.now)
    refs: dict[str, list[Deployment]] = {}
    for d in deps:
        r = _ref(d)
        if r:
            refs.setdefault(r, []).append(d)
        elif d.status in deployments.LIVE_STATES:
            run.find("no_pinned_credential", provider=p, deployment_id=d.deployment_id,
                     message="live deployment without a pinned credential; cannot be reconciled")
    cp = credentials.credential_provider(p)
    if credentials.platform(cp):
        refs.setdefault(f"platform:{cp}", [])
    out = {"credentials": len(refs), "deployments": len(deps), "listed": 0, "list_errors": 0}
    if not refs:
        out["skipped"] = "no credentials and no deployments"
        return out
    for ref, ref_deps in refs.items():
        try:
            creds = _creds(ref, p)
        except CredentialsUnavailable as exc:
            for d in ref_deps:
                if d.status in deployments.LIVE_STATES:
                    _credentials_unavailable(run, d, exc)
            run.find("credentials_unavailable", provider=p, credential_ref=ref, message=exc.message)
            continue
        # The ref names where the key comes from, not which key: a deployment launched with a DIFFERENT secret
        # (the managed key was replaced -- possibly by another provider account's key) must not be judged by
        # this listing (its absence there would look like termination).
        kept = []
        for d in ref_deps:
            try:
                credentials.check_pinned(creds, credentials.pinned_fingerprint(d), ref)
                kept.append(d)
            except CredentialsUnavailable as exc:
                if d.status in deployments.LIVE_STATES:
                    _credentials_unavailable(run, d, exc)
        ref_deps = kept
        a = adapters.build(p, creds)
        if a is None:
            continue
        a.log_context = {"job": "reconcile"}
        try:
            try:
                listing = a.list_instances()
                out["listed"] += len(listing)
                run.listed_ok.add((p, ref))
            except AdapterError as exc:
                listing = None
                out["list_errors"] += 1
                run.list_failed[(p, ref)] = exc.kind
                run.find("list_failed", provider=p, credential_ref=ref, error_kind=exc.kind,
                         message=a.scrub(exc.message)[:300])
            for d in ref_deps:
                try:
                    _deployment(run, a, d, listing)
                except Exception as exc:  # noqa: BLE001
                    log.exception("reconcile deployment %s failed", d.deployment_id)
                    run.find("deployment_error", provider=p, deployment_id=d.deployment_id,
                             message=f"{type(exc).__name__}: {str(exc)[:300]}")
            if listing is not None:
                _orphans(run, a, p, ref, listing)
            if getattr(a, "SSH_KEY_RESOURCE", False):
                try:
                    out["ssh_keys"] = resources.reconcile_keys(a, p, ref, find=run.find)
                except AdapterError as exc:
                    run.find("ssh_key_list_failed", provider=p, credential_ref=ref, error_kind=exc.kind,
                             message=a.scrub(exc.message)[:300])
                except Exception as exc:  # noqa: BLE001 - the key pass never breaks instance reconciliation
                    log.exception("ssh key reconciliation for %s failed", p)
                    run.find("ssh_key_list_failed", provider=p, credential_ref=ref, error_kind="internal",
                             message=f"{type(exc).__name__}"[:300])
        finally:
            a.close()
    return out


# --------------------------------------------------------------------------
# Per deployment
# --------------------------------------------------------------------------

def _deployment(run: _Run, a, d: Deployment, listing: list[InstanceState] | None) -> None:
    d = deployments.load_row(d.deployment_id)          # fresh (deadline pass may have changed it)
    if d is None:
        return
    name = d.client_name or instance_name(d.deployment_id)
    if d.status in deployments.TERMINAL_STATES:
        return                                          # orphan pass handles a terminal deployment's instance
    if d.status == "credentials_unavailable":
        # The credential works again (we are holding an adapter built from it): restore observation.
        st = a.status(d.provider_instance_id) if d.provider_instance_id else None
        if st is not None and st.state != "unknown":
            deployments.observe(d.deployment_id, st, actor="reconciler",
                                extra_evidence={"basis": "pinned credential usable again (reconciliation)"})
            run.find("credentials_restored", provider=d.provider, deployment_id=d.deployment_id, state=st.state)
            d = deployments.load_row(d.deployment_id)
    if not d.provider_instance_id:
        _unresolved(run, a, d, name, listing)
        return
    if listing is None:
        return
    iid = str(d.provider_instance_id)
    present = [st for st in listing if str(st.instance_id) == iid]
    dups = [st for st in listing if st.matches(name) and st.alive and str(st.instance_id) != iid]
    for st in dups:
        _orphan(run, d.provider, _ref(d), st, kind="duplicate_launch", deployment_id=d.deployment_id,
                provably_ours=False, message=f"a second live instance carries {name}")
    if d.status in ("terminating", "termination_failed"):
        if present and present[0].alive:
            _retry_terminate(run, a, d)
        else:
            _confirm_gone(run, a, d, present[0] if present else None, why="termination confirmed")
        return
    if not present or not present[0].alive:
        _confirm_gone(run, a, d, present[0] if present else None, why="provider_terminated")
        return
    try:
        tracker.record_lifecycle(d.deployment_id, present[0], present[0].observed_at or run.now)
    except Exception:  # noqa: BLE001
        log.exception("lifecycle timestamps for %s failed", d.deployment_id)
    if _term_requested(d) and d.status not in ("terminating", "termination_failed"):
        _terminate(run, d, "terminate requested earlier; instance now known")
        return
    if d.status == "provisioning":
        _boot_timeout(run, d)


def _unresolved(run: _Run, a, d: Deployment, name: str, listing) -> None:
    if listing is None:
        run.find("unresolved_list_failed", provider=d.provider, deployment_id=d.deployment_id, name=name)
        _unresolved_alert(run, d, name, "the provider's instance list could not be read")
        return
    matches = [st for st in listing if st.matches(name)]
    live = [st for st in matches if st.alive]
    if len(live) > 1:
        for st in live:
            _orphan(run, d.provider, _ref(d), st, kind="duplicate_launch", deployment_id=d.deployment_id,
                    provably_ours=False, message=f"{len(live)} live instances carry {name}")
        if deployments.can_transition(d.status, "orphan_suspected"):
            deployments.transition(d.deployment_id, "orphan_suspected", f"{len(live)} live instances named {name}",
                                   {"instances": [_brief(x) for x in live]}, actor="reconciler")
        tracker.alert("duplicate_launch", d.deployment_id, f"{len(live)} live instances named {name}",
                      dep_id=d.deployment_id, provider=d.provider,
                      detail={"instances": [x.instance_id for x in live]})
        run.find("duplicate_launch", provider=d.provider, deployment_id=d.deployment_id, name=name,
                 instances=[x.instance_id for x in live])
        return
    st = live[0] if live else (matches[0] if matches else None)
    if st is None:
        # Second, independent look: the provider's own name filter (falls back to the full list).
        try:
            st = a.find_instance(name)
        except AdapterError as exc:
            run.find("find_failed", provider=d.provider, deployment_id=d.deployment_id, name=name,
                     error_kind=exc.kind, message=a.scrub(exc.message)[:300])
            _unresolved_alert(run, d, name, f"find_instance failed ({exc.kind})")
            return
    if st is not None:
        _adopt(run, d, st, basis="instance found by name (list_instances / find_instance)")
        return
    started = _attempt_started(d.deployment_id) or d.state_changed_at or d.created_at
    age = run.now - started
    timeout = timedelta(minutes=settings.provisioning_timeout_minutes)
    if age < timeout:
        run.find("unresolved_waiting", provider=d.provider, deployment_id=d.deployment_id, name=name,
                 age_seconds=int(age.total_seconds()))
        return
    ev = {"basis": "absent from list_instances AND find_instance(name) after the provisioning timeout",
          "name": name, "listed_instances": len(listing), "checked_at": run.now.isoformat(),
          "provisioning_started_at": started.isoformat(), "timeout_minutes": settings.provisioning_timeout_minutes}
    if deployments.can_transition(d.status, "provision_failed"):
        deployments.transition(d.deployment_id, "provision_failed",
                               "reconciliation: no instance exists at the provider after the provisioning timeout",
                               ev, actor="reconciler")
        _close_attempt(d.deployment_id, None, "resolved by reconciliation: no instance exists (provision_failed)")
        run.find("resolved_absent", provider=d.provider, deployment_id=d.deployment_id, name=name)
    else:
        run.find("unresolved_cannot_close", provider=d.provider, deployment_id=d.deployment_id, status=d.status)
        _unresolved_alert(run, d, name, f"cannot close from {d.status}")


def _unresolved_alert(run: _Run, d: Deployment, name: str, why: str) -> None:
    """launch_unknown / provider_timeout still unresolved after the provisioning timeout -> ops alert."""
    started = _attempt_started(d.deployment_id) or d.state_changed_at or d.created_at
    if run.now - started < timedelta(minutes=settings.provisioning_timeout_minutes):
        return
    tracker.alert("launch_unresolved", d.deployment_id,
                  f"{d.status} on {d.provider} still unresolved after {settings.provisioning_timeout_minutes} min "
                  f"({why}); an instance named {name} may exist and bill", dep_id=d.deployment_id, provider=d.provider,
                  detail={"status": d.status, "client_name": name})


def _attempt_started(dep_id: str) -> datetime | None:
    with normalize.SessionLocal() as s:
        return s.scalar(select(ProvisionAttempt.started_at).where(ProvisionAttempt.deployment_id == dep_id)
                        .order_by(ProvisionAttempt.id.desc()).limit(1))


def _close_attempt(dep_id: str, instance_id: str | None, note: str) -> None:
    with normalize.SessionLocal.begin() as s:
        for att in s.scalars(select(ProvisionAttempt).where(ProvisionAttempt.deployment_id == dep_id,
                                                            ProvisionAttempt.outcome.in_(("provisioning", "unknown")))
                             .with_for_update()):
            if instance_id:
                att.instance_id = instance_id
            if att.finished_at is None:
                att.finished_at = _now()
            if att.outcome == "provisioning":
                att.outcome = "unknown"     # the call's own answer was never recorded
            att.error = ((att.error + " | ") if att.error else "") + note


def _adopt(run: _Run, d: Deployment, st: InstanceState, *, basis: str) -> None:
    target = TARGET.get(st.state)
    now = _now()
    ev = {"basis": basis, **_brief(st), "client_name": d.client_name}
    with normalize.SessionLocal.begin() as s:
        row = s.get(Deployment, d.deployment_id, with_for_update=True)
        if row.provider_instance_id and str(row.provider_instance_id) != str(st.instance_id):
            run.find("adopt_conflict", provider=d.provider, deployment_id=d.deployment_id,
                     instance_id=st.instance_id, existing=row.provider_instance_id)
            return
        row.provider_instance_id = str(st.instance_id)
        row.provisioned_at = row.provisioned_at or now
        if st.price_per_hour is not None and row.gpu_count:
            row.actual_price_per_gpu_hour = Decimal(str(round(float(st.price_per_hour) / row.gpu_count, 6)))
        md = dict(row.provider_metadata or {})
        md.update(needs_reconciliation=False, adopted_at=now.isoformat(), adopted=ev)
        row.provider_metadata = md
        deployments.note_event(s, row, "reconciliation adopted the instance (found by name)", ev, actor="reconciler")
        if target and target != row.status and deployments.can_transition(row.status, target):
            if target == "terminated":
                ev["ended_at"] = _iso(st.ended_at) or now.isoformat()
            deployments.transition(row, target, f"adopted: provider reports {st.state}", ev, actor="reconciler", s=s)
        elif target and target != row.status and row.status != "provisioning" \
                and deployments.can_transition(row.status, "provisioning"):
            deployments.transition(row, "provisioning", "adopted", ev, actor="reconciler", s=s)
        w = tracker.watch_row(s, d.deployment_id)
        w.last_observed_at, w.last_observed_state, w.updated_at = st.observed_at or now, st.state, now
        if st.state == "running":
            w.last_running_at = st.observed_at or now
        status = row.status
    _close_attempt(d.deployment_id, str(st.instance_id), f"adopted by reconciliation ({basis})")
    try:
        tracker.record_lifecycle(d.deployment_id, st, st.observed_at or now)
    except Exception:  # noqa: BLE001
        log.exception("lifecycle timestamps for %s failed", d.deployment_id)
    tracker.alert("adopted", d.deployment_id, f"instance {st.instance_id} adopted ({st.state}) after an ambiguous "
                                             f"launch", dep_id=d.deployment_id)
    run.find("adopted", provider=d.provider, deployment_id=d.deployment_id, instance_id=st.instance_id,
             state=st.state, status=status)
    if status == "terminated":
        transactions.bill(d.deployment_id)
        return
    fresh = deployments.load_row(d.deployment_id)
    if _term_requested(fresh) and fresh.status not in ("terminating", "terminated"):
        _terminate(run, fresh, "terminate was requested before the instance was known")


def _confirm_gone(run: _Run, a, d: Deployment, listed: InstanceState | None, *, why: str) -> None:
    """The list omits the instance (or lists it as ended). A status read is the second signal."""
    st = a.status(d.provider_instance_id)
    if st.state in ("not_found", "terminated"):
        ended = st.ended_at or (listed.ended_at if listed else None)
        ev = {"basis": "two signals: " + ("list_instances shows it ended" if listed else "list_instances omits it")
                       + f" AND status() = {st.state}",
              "list_checked_at": run.now.isoformat(), "status": _brief(st), "listed": _brief(listed) if listed else None,
              "ended_at": _iso(ended) or _iso(st.observed_at) or run.now.isoformat()}
        if ended is not None:
            try:
                tracker.record_lifecycle(d.deployment_id, InstanceState("terminated", instance_id=st.instance_id,
                                                                        ended_at=ended), run.now)
            except Exception:  # noqa: BLE001
                log.exception("lifecycle timestamps for %s failed", d.deployment_id)
        with normalize.SessionLocal.begin() as s:
            w = tracker.watch_row(s, d.deployment_id)
            w.ended_at = ended or st.observed_at or run.now
            w.ended_basis = "provider_reported" if ended else "first_observed"
            w.last_observed_at, w.last_observed_state, w.updated_at = st.observed_at or run.now, st.state, _now()
        if deployments.can_transition(d.status, "terminated"):
            deployments.transition(d.deployment_id, "terminated", f"reconciliation: {why}", ev, actor="reconciler")
        if why == "provider_terminated":
            tracker.alert("provider_terminated", d.deployment_id,
                          f"{d.provider} instance {d.provider_instance_id} is gone while OpenGrid had {d.status}",
                          dep_id=d.deployment_id, provider=d.provider, severity="notable")
        run.find("terminated_confirmed" if why != "provider_terminated" else "provider_terminated",
                 provider=d.provider, deployment_id=d.deployment_id, instance_id=d.provider_instance_id,
                 previous=d.status)
        return
    if st.state == "unknown":
        run.find("confirm_status_failed", provider=d.provider, deployment_id=d.deployment_id, error_kind=st.error_kind)
        return
    # The list omitted it but the provider says it is alive: one signal only. Never terminated.
    run.find("list_inconsistent", provider=d.provider, deployment_id=d.deployment_id, state=st.state)
    if d.status in ("terminating", "termination_failed"):
        _retry_terminate(run, a, d)


def _retry_terminate(run: _Run, a, d: Deployment, *, ignore_backoff: bool = False) -> None:
    now = _now()
    with normalize.SessionLocal() as s:
        w = s.get(DeploymentWatch, d.deployment_id)
        attempts = w.terminate_attempts if w else 0
        nxt = w.next_terminate_at if w else None
    if nxt is not None and now < nxt and not ignore_backoff:
        run.find("terminate_backoff", provider=d.provider, deployment_id=d.deployment_id, next_at=nxt.isoformat())
        return
    if attempts >= settings.termination_retry_max and d.status != "termination_failed" \
            and deployments.can_transition(d.status, "termination_failed"):
        deployments.transition(d.deployment_id, "termination_failed",
                               f"instance still present after {attempts} terminate attempts",
                               {"attempts": attempts}, actor="reconciler")
        tracker.alert("termination_failed", d.deployment_id,
                      f"{d.provider} instance {d.provider_instance_id} still exists after {attempts} terminate attempts",
                      dep_id=d.deployment_id, provider=d.provider,
                      detail={"instance_id": d.provider_instance_id, "attempts": attempts})
        run.find("termination_failed", provider=d.provider, deployment_id=d.deployment_id, attempts=attempts)
    tr = a.terminate(d.provider_instance_id)
    delay = min(MAX_BACKOFF_SECONDS, settings.termination_retry_base_seconds * (2 ** attempts))
    ev = {"outcome": tr.outcome, "error_kind": tr.error_kind, "status_code": tr.status_code,
          "message": (tr.message or "")[:300], "attempt": attempts + 1}
    with normalize.SessionLocal.begin() as s:
        w = tracker.watch_row(s, d.deployment_id)
        w.terminate_attempts = attempts + 1
        w.last_terminate_at, w.last_terminate_outcome = now, tr.outcome
        w.next_terminate_at = now + timedelta(seconds=delay)
        w.updated_at = now
        row = s.get(Deployment, d.deployment_id, with_for_update=True)
        # termination_failed stays until the provider confirms the instance gone (-> terminated directly).
        deployments.note_event(s, row, f"reconciler re-issued terminate: {tr.outcome}", ev, actor="reconciler")
    run.find("terminate_retried", provider=d.provider, deployment_id=d.deployment_id, outcome=tr.outcome,
             attempt=attempts + 1)


def _terminate(run: _Run, d: Deployment, reason: str, termination_reason: str | None = None) -> None:
    if termination_reason:
        with normalize.SessionLocal.begin() as s:
            row = s.get(Deployment, d.deployment_id, with_for_update=True)
            row.termination_reason = row.termination_reason or termination_reason
    try:
        r = deployments.terminate(d.deployment_id, None, reason=reason)
        run.find("terminate_issued", provider=d.provider, deployment_id=d.deployment_id, reason=reason,
                 outcome=(r.get("terminate") or {}).get("outcome"))
    except Exception as exc:  # noqa: BLE001 - HTTPException(409) credentials_unavailable, ...
        run.find("terminate_issue_failed", provider=d.provider, deployment_id=d.deployment_id, reason=reason,
                 message=str(getattr(exc, "detail", exc))[:300])


def _boot_timeout(run: _Run, d: Deployment) -> None:
    started = d.provisioned_at or d.state_changed_at or d.created_at
    age = run.now - started
    t = timedelta(minutes=settings.provisioning_timeout_minutes)
    if age >= 4 * t:
        _terminate(run, d, f"boot timeout: not running {int(age.total_seconds() // 60)} min after launch",
                   termination_reason="boot_timeout")
        tracker.alert("boot_timeout", d.deployment_id, "terminated: never reached running", dep_id=d.deployment_id,
                      provider=d.provider)
    elif age >= t:
        run.find("boot_slow", provider=d.provider, deployment_id=d.deployment_id,
                 age_minutes=int(age.total_seconds() // 60))
        with normalize.SessionLocal() as s:
            w = s.get(DeploymentWatch, d.deployment_id)
            already = any(x.get("kind") == "boot_slow" for x in ((w.alerts if w else None) or []))
        if not already:
            tracker.alert("boot_slow", d.deployment_id, f"not running {int(age.total_seconds() // 60)} min after launch",
                          dep_id=d.deployment_id)


def _credentials_unavailable(run: _Run, d: Deployment, exc: CredentialsUnavailable) -> None:
    deployments.mark_credentials_unavailable(d.deployment_id, exc, actor="reconciler")
    now = _now()
    with normalize.SessionLocal.begin() as s:
        w = tracker.watch_row(s, d.deployment_id)
        due = w.credentials_unavailable_at is None or now - w.credentials_unavailable_at >= timedelta(hours=1)
        if due:
            w.credentials_unavailable_at = now
        w.updated_at = now
    if due:
        tracker.alert("credentials_unavailable", d.deployment_id,
                      f"{d.provider} deployment {d.deployment_id} ({d.status}): pinned credential unusable: {exc.message}",
                      dep_id=d.deployment_id, provider=d.provider, detail={"credential_ref": _ref(d)})
    run.find("credentials_unavailable", provider=d.provider, deployment_id=d.deployment_id,
             credential_ref=_ref(d), message=exc.message)


def _enforce_deadlines(run: _Run, provider: str | None) -> None:
    """Runs FIRST in every pass (so the first pass after a restart enforces overdue deadlines before anything
    else). Every live deployment past terminate_deadline_at: terminate it; if termination was already asked for
    and the provider's last answer was failed/unknown (API unavailable), re-issue it THIS run, whatever the
    backoff; mark past_deadline (deployment_watch.past_deadline_at). The past_deadline ops alert is raised and
    re-escalated by _exposures() until termination is confirmed."""
    with normalize.SessionLocal() as s:
        q = select(Deployment).where(Deployment.terminate_deadline_at.is_not(None),
                                     Deployment.terminate_deadline_at <= run.now,
                                     Deployment.status.in_(deployments.LIVE_STATES))
        if provider:
            q = q.where(Deployment.provider == provider)
        due = list(s.scalars(q.order_by(Deployment.terminate_deadline_at)))
    for d in due:
        with normalize.SessionLocal.begin() as s:
            w = tracker.watch_row(s, d.deployment_id)
            first_seen = w.past_deadline_at is None
            if first_seen:
                w.past_deadline_at = run.now
            w.deadline_retries = (w.deadline_retries or 0) + (0 if first_seen else 1)
            w.updated_at = run.now
            last_outcome = w.last_terminate_outcome
        if first_seen:
            row = deployments.load_row(d.deployment_id)
            with normalize.SessionLocal.begin() as s:
                r2 = s.get(Deployment, d.deployment_id, with_for_update=True)
                deployments.note_event(s, r2, "past_deadline: terminate_deadline_at passed while the deployment is "
                                              f"{r2.status}", {"deadline": d.terminate_deadline_at.isoformat(),
                                                                "max_runtime_minutes": d.max_runtime_minutes},
                                       actor="reconciler")
            del row
        if not _term_requested(d):
            _terminate(run, d, f"terminate_deadline_at {d.terminate_deadline_at.isoformat()} passed: auto-terminate "
                               f"(max_runtime_minutes={d.max_runtime_minutes})", termination_reason="max_runtime_exceeded")
            run.find("deadline_terminated", provider=d.provider, deployment_id=d.deployment_id,
                     deadline=d.terminate_deadline_at.isoformat())
            tracker.alert("deadline_terminate", d.deployment_id,
                          f"{d.provider} deployment {d.deployment_id} auto-terminated at its deadline "
                          f"({d.terminate_deadline_at.isoformat()})", dep_id=d.deployment_id, provider=d.provider,
                          severity="notable", detail={"max_runtime_minutes": d.max_runtime_minutes})
            continue
        if not d.provider_instance_id:
            run.find("past_deadline_instance_unknown", provider=d.provider, deployment_id=d.deployment_id,
                     status=d.status)
            continue      # terminated as soon as reconciliation adopts the instance
        prev = last_outcome or ((d.provider_metadata or {}).get("last_terminate") or {}).get("outcome")
        if d.status not in ("terminating", "termination_failed", "credentials_unavailable") or \
                prev in (None, "failed", "unknown"):
            _deadline_retry(run, d, prev)


def _deadline_retry(run: _Run, d: Deployment, prev: str | None) -> None:
    """Re-issue the terminate of a past-deadline deployment with its pinned credential, now."""
    try:
        a = deployments.adapter_for(d, job="reconcile_deadline")
    except CredentialsUnavailable as exc:
        run.find("past_deadline_credentials_unavailable", provider=d.provider, deployment_id=d.deployment_id,
                 message=exc.message)
        return
    except Exception as exc:  # noqa: BLE001 - no adapter
        run.find("past_deadline_no_adapter", provider=d.provider, deployment_id=d.deployment_id,
                 message=str(getattr(exc, "detail", exc))[:200])
        return
    try:
        if d.status not in ("terminating", "termination_failed") and deployments.can_transition(d.status, "terminating") \
                and d.status != "credentials_unavailable":
            deployments.transition(d.deployment_id, "terminating", "past deadline: terminate re-issued",
                                   {"previous_outcome": prev}, actor="reconciler")
        _retry_terminate(run, a, deployments.load_row(d.deployment_id), ignore_backoff=True)
        run.find("past_deadline_retry", provider=d.provider, deployment_id=d.deployment_id, previous_outcome=prev)
    finally:
        a.close()


# --------------------------------------------------------------------------
# Orphans
# --------------------------------------------------------------------------

def _orphans(run: _Run, a, p: str, ref: str, listing: list[InstanceState]) -> None:
    named = [(st, og_name(st)) for st in listing]
    named = [(st, n) for st, n in named if n]
    by_name: dict[str, Deployment] = {}
    if named:
        with normalize.SessionLocal() as s:
            for d in s.scalars(select(Deployment).where(Deployment.client_name.in_({n for _, n in named}))):
                by_name[d.client_name] = d
    seen_ids = {str(st.instance_id) for st in listing}
    for st, n in named:
        d = by_name.get(n)
        if d is None or d.provider != p:
            if st.alive:
                _orphan(run, p, ref, st, kind="og_no_deployment", deployment_id=None, provably_ours=False,
                        message=f"{n} has no OpenGrid deployment on {p}")
            continue
        if d.status in deployments.TERMINAL_STATES:
            if not st.alive:
                continue
            ours = _ref(d) == ref
            oid = _orphan(run, p, ref, st, kind="deployment_ended_alive", deployment_id=d.deployment_id,
                          provably_ours=ours, message=f"deployment {d.deployment_id} is {d.status} but {n} is alive")
            if not ours:
                continue
            if d.status in ("provision_failed", "provider_rejected") and not d.provider_instance_id:
                _reopen_and_terminate(run, d, st)
            else:
                _auto_terminate_orphan(run, a, oid, d, st)
        elif d.provider_instance_id and str(st.instance_id) != str(d.provider_instance_id) and st.alive:
            _orphan(run, p, ref, st, kind="duplicate_launch", deployment_id=d.deployment_id, provably_ours=False,
                    message=f"{n} has a second live instance")
    # Orphans previously seen on this account that the list no longer shows are gone.
    with normalize.SessionLocal.begin() as s:
        for o in s.scalars(select(OrphanResource).where(OrphanResource.provider == p, OrphanResource.credential_ref == ref,
                                                        OrphanResource.status.in_(("open", "terminating")))
                           .with_for_update()):
            if o.instance_id not in seen_ids:
                o.status = "terminated" if o.status == "terminating" else "gone"
                o.resolution_note = f"absent from list_instances at {run.now.isoformat()}"
                o.resolved_at, o.updated_at = o.resolved_at or run.now, run.now
                run.find("orphan_gone", provider=p, instance_id=o.instance_id, orphan_id=o.id, status=o.status)


def _orphan(run: _Run, p: str, ref: str | None, st: InstanceState, *, kind: str, deployment_id: str | None,
            provably_ours: bool, message: str) -> int:
    from sqlalchemy.dialects.postgresql import insert

    now = _now()
    ev = {"last": _brief(st), "message": message}
    vals = dict(provider=p, instance_id=str(st.instance_id), instance_name=og_name(st) or st.name, kind=kind,
                deployment_id=deployment_id, credential_ref=ref, provider_state=st.state,
                provider_status=(st.provider_status or "")[:64] or None,
                price_per_hour=None if st.price_per_hour is None else Decimal(str(st.price_per_hour)),
                first_seen_at=now, last_seen_at=now, seen_count=1, status="open", provably_ours=provably_ours,
                auto_terminated=False, terminate_attempts=0, evidence=ev, updated_at=now)
    with normalize.SessionLocal.begin() as s:
        stmt = insert(OrphanResource).values(**vals).on_conflict_do_update(
            constraint="uq_orphan_provider_instance",
            set_={"last_seen_at": now, "seen_count": OrphanResource.seen_count + 1, "provider_state": st.state,
                  "provider_status": vals["provider_status"], "evidence": ev, "updated_at": now,
                  "provably_ours": provably_ours, "kind": kind, "deployment_id": deployment_id,
                  # a re-appearing instance that had been marked gone is open again
                  "status": text("CASE WHEN orphan_resources.status IN ('gone', 'terminated') THEN 'open' "
                                 "ELSE orphan_resources.status END")},
        ).returning(OrphanResource.id, OrphanResource.seen_count)
        oid, seen = s.execute(stmt).one()
    if seen == 1:   # first sighting only: a stuck orphan does not page every run
        tracker.alert("orphan", deployment_id or f"{p}:{st.instance_id}", f"orphan on {p} ({kind}): {message}",
                      provider=p, detail={"instance_id": st.instance_id, "kind": kind, "provably_ours": provably_ours,
                                          "estimated_hourly_cost_usd": st.price_per_hour,
                                          "instance_name": og_name(st) or st.name})
    run.find("orphan", provider=p, kind_of_orphan=kind, instance_id=st.instance_id, deployment_id=deployment_id,
             orphan_id=oid, provably_ours=provably_ours)
    return oid


def _auto_terminate_orphan(run: _Run, a, oid: int, d: Deployment, st: InstanceState) -> None:
    now = _now()
    with normalize.SessionLocal() as s:
        o = s.get(OrphanResource, oid)
        if o.status in ("ignored", "adopted"):
            return
        if o.last_terminate_at is not None:
            wait = min(MAX_BACKOFF_SECONDS, settings.termination_retry_base_seconds * (2 ** max(0, o.terminate_attempts - 1)))
            if now - o.last_terminate_at < timedelta(seconds=wait):
                return
    tr = a.terminate(st.instance_id)
    with normalize.SessionLocal.begin() as s:
        o = s.get(OrphanResource, oid, with_for_update=True)
        o.terminate_attempts += 1
        o.last_terminate_at, o.last_terminate_outcome = now, tr.outcome
        o.auto_terminated = True
        o.action = o.action or "terminate"
        o.resolved_by = o.resolved_by or "system:reconciler"
        if tr.outcome in ("accepted", "already_gone", "unknown"):
            o.status = "terminating"
        o.updated_at = now
        row = s.get(Deployment, d.deployment_id, with_for_update=True)
        deployments.note_event(s, row, f"reconciler auto-terminated orphan instance {st.instance_id}: {tr.outcome}",
                               {"orphan_id": oid, "instance": _brief(st), "outcome": tr.outcome,
                                "message": (tr.message or "")[:300]}, actor="reconciler")
    run.find("orphan_auto_terminate", provider=d.provider, deployment_id=d.deployment_id,
             instance_id=st.instance_id, outcome=tr.outcome, orphan_id=oid)


def _reopen_and_terminate(run: _Run, d: Deployment, st: InstanceState) -> None:
    """A definitive rejection turned out to have created an instance: track it (orphan_suspected, linked) and
    terminate it through the state machine so it is confirmed and audited like any other."""
    now = _now()
    ev = {"basis": "og-* instance found for a deployment recorded as " + d.status, **_brief(st)}
    with normalize.SessionLocal.begin() as s:
        row = s.get(Deployment, d.deployment_id, with_for_update=True)
        if row.status not in ("provision_failed", "provider_rejected"):
            return
        row.provider_instance_id = str(st.instance_id)
        row.termination_reason = "orphan_auto_terminate"
        deployments.transition(row, "orphan_suspected", "an instance exists despite a definitive rejection", ev,
                               actor="reconciler", s=s)
        w = tracker.watch_row(s, d.deployment_id)
        w.last_observed_at, w.last_observed_state, w.updated_at = now, st.state, now
    tracker.alert("rejected_but_created", d.deployment_id,
                  f"{d.provider} reported a rejection but instance {st.instance_id} exists", dep_id=d.deployment_id,
                  provider=d.provider, detail={"instance_id": st.instance_id,
                                               "estimated_hourly_cost_usd": st.price_per_hour})
    _terminate(run, deployments.load_row(d.deployment_id), "orphan of a rejected launch: auto-terminate")


# --------------------------------------------------------------------------
# Cost-exposure alerts (alerts/ops.exposure): evaluated at the end of every pass
# --------------------------------------------------------------------------

UNCERTAIN = ("provider_timeout", "launch_unknown", "orphan_suspected", "credentials_unavailable")


def _hourly(d: Deployment) -> float | None:
    price = d.actual_price_per_gpu_hour if d.actual_price_per_gpu_hour is not None else d.quoted_price_per_gpu_hour
    return None if price is None else round(float(price) * (d.gpu_count or 1), 4)


def _ops():
    from alerts import ops
    return ops


def _expose(kind: str, d: Deployment | None, subject: str, title: str, since: datetime | None, action: str,
            run: _Run, *, provider: str | None = None, est: float | None = None, detail: dict | None = None) -> None:
    r = _ops().exposure(kind, subject, title, deployment_id=d.deployment_id if d else None,
                        provider=provider or (d.provider if d else None), account_id=d.account_id if d else None,
                        est_hourly_exposure_usd=est if est is not None else (_hourly(d) if d else None),
                        time_in_state_seconds=None if since is None else (run.now - since).total_seconds(),
                        suggested_action=action, detail=detail, now=run.now)
    run.find("exposure_alert", alert_kind=kind, subject=subject, sent=r.get("sent"), escalation=r.get("sent_count"))


def _resolve_missing(kind: str, active: set, evidence_for) -> int:
    """Resolve open conditions of `kind` that are no longer true, each WITH its evidence (evidence_for(subject)
    returns a dict, or None when there is no proof yet: then it stays open)."""
    n = 0
    for subject in _ops().open_subjects(kind):
        if subject in active:
            continue
        ev = evidence_for(subject)
        if ev:
            n += _ops().resolve(kind, subject, ev, by="system:reconciler")
    return n


def _dep_evidence(why_ok):
    def ev(subject: str):
        d = deployments.load_row(subject.split(":", 1)[1]) if subject.startswith("dep:") else None
        if d is None:
            return None
        return why_ok(d)
    return ev


def _exposures(run: _Run) -> dict:
    st = settings
    unknown_after = timedelta(minutes=int(getattr(st, "alert_unknown_minutes", 10)))
    pct = float(getattr(st, "alert_overspend_pct", 20.0))
    counts: dict = {}
    with normalize.SessionLocal() as s:
        live = list(s.scalars(select(Deployment).where(Deployment.status.in_(deployments.LIVE_STATES))))
        watches = {w.deployment_id: w for w in s.scalars(select(DeploymentWatch).where(
            DeploymentWatch.deployment_id.in_([d.deployment_id for d in live])))} if live else {}
        orphans_open = list(s.scalars(select(OrphanResource).where(OrphanResource.status.in_(("open", "terminating")))))
        unowned = list(s.scalars(select(ProviderResource).where(ProviderResource.status == "abandoned",
                                                                ProviderResource.recorded_by == "reconcile_unowned")))
    active = {k: set() for k in ("resource_state_unknown", "termination_failed", "past_deadline", "overspend",
                                 "active_without_billing", "suspected_orphan", "provider_api_unavailable",
                                 "unowned_provider_resource")}
    for d in live:
        subj = f"dep:{d.deployment_id}"
        w = watches.get(d.deployment_id)
        since = d.state_changed_at or d.created_at
        # 1. unknown / uncertain state too long
        stale_obs = (d.provider_instance_id and w is not None and (w.consecutive_errors or 0) > 0
                     and run.now - (w.last_observed_at or since) > unknown_after)
        if (d.status in UNCERTAIN and run.now - since > unknown_after) or stale_obs:
            active["resource_state_unknown"].add(subj)
            _expose("resource_state_unknown", d, subj,
                    f"{d.provider} {d.deployment_id}: state unknown ({d.status}"
                    + (f", {w.consecutive_errors} failed reads" if stale_obs else "") + ") - it may be billing",
                    (w.last_observed_at if stale_obs and w and w.last_observed_at else since),
                    "check the instance in the provider console; resolve the launch (adopt/terminate) or restore the "
                    "pinned credential", run, detail={"status": d.status, "client_name": d.client_name})
        # 2. termination failed
        if d.status == "termination_failed":
            active["termination_failed"].add(subj)
            _expose("termination_failed", d, subj, f"{d.provider} {d.deployment_id}: termination failed; instance "
                                                    f"{d.provider_instance_id} may still be billing", since,
                    "terminate the instance in the provider console; OpenGrid keeps retrying and confirms by list",
                    run, detail={"instance_id": d.provider_instance_id,
                                 "attempts": w.terminate_attempts if w else None})
        # 3. past deadline
        if d.terminate_deadline_at is not None and d.terminate_deadline_at <= run.now:
            active["past_deadline"].add(subj)
            _expose("past_deadline", d, subj, f"{d.provider} {d.deployment_id} is past its deadline "
                                               f"({d.terminate_deadline_at.isoformat()}) and still {d.status}",
                    d.terminate_deadline_at,
                    "OpenGrid re-issues terminate every run; if the provider API stays down terminate it in the "
                    "provider console", run, detail={"deadline": d.terminate_deadline_at.isoformat(),
                                                     "status": d.status, "instance_id": d.provider_instance_id,
                                                     "retries": w.deadline_retries if w else 0,
                                                     "last_terminate_outcome": w.last_terminate_outcome if w else None})
        # 4. overspend vs the quote
        _overspend(run, d, pct, active)
        # 5. live billable state without usage slices for > 2 intervals (1 h slices)
        if d.status in tracker.BILLED_STATES and d.provider_instance_id:
            start = d.billable_start or d.provisioned_at
            if start is not None:
                covered = (w.metered_through if w and w.metered_through else tracker._floor_hour(start))
                lag = tracker._floor_hour(run.now) - covered
                if lag > 2 * tracker.HOUR:
                    active["active_without_billing"].add(subj)
                    _expose("active_without_billing", d, subj,
                            f"{d.provider} {d.deployment_id} is {d.status} but has no usage slice since "
                            f"{covered.isoformat()}", covered,
                            "check the routing_tracker job and the deployment's price; usage is metered hourly",
                            run, detail={"metered_through": w.metered_through.isoformat() if w and w.metered_through
                                         else None, "billable_start": start.isoformat()})
    # 6. suspected orphans
    for o in orphans_open:
        subj = f"orphan:{o.id}"
        active["suspected_orphan"].add(subj)
        d = deployments.load_row(o.deployment_id) if o.deployment_id else None
        r = _ops().exposure("suspected_orphan", subj, f"suspected orphan on {o.provider}: {o.instance_name or o.instance_id} "
                                                      f"({o.kind})", deployment_id=o.deployment_id, provider=o.provider,
                            account_id=d.account_id if d else None,
                            est_hourly_exposure_usd=None if o.price_per_hour is None else float(o.price_per_hour),
                            time_in_state_seconds=(run.now - o.first_seen_at).total_seconds(),
                            suggested_action="POST /v1/admin/orphans/{id}/resolve (terminate | adopt | ignore); OpenGrid "
                                             "auto-terminates only provably-ours instances",
                            detail={"orphan_id": o.id, "instance_id": o.instance_id, "kind_of_orphan": o.kind,
                                    "provably_ours": o.provably_ours}, now=run.now)
        run.find("exposure_alert", alert_kind="suspected_orphan", subject=subj, sent=r.get("sent"))
    # 7. provider API unavailable while it has active deployments
    by_key: dict = {}
    for d in live:
        by_key.setdefault((d.provider, _ref(d)), []).append(d)
    for (p, ref), kind in run.list_failed.items():
        deps = by_key.get((p, ref)) or []
        if not deps:
            continue
        subj = f"provider:{p}:{ref}"
        active["provider_api_unavailable"].add(subj)
        est = sum(x for x in (_hourly(d) for d in deps) if x is not None)
        oldest = min((d.state_changed_at or d.created_at) for d in deps)
        r = _ops().exposure("provider_api_unavailable", subj,
                            f"{p} API unavailable ({kind}) with {len(deps)} active deployment(s)",
                            deployment_id=deps[0].deployment_id if len(deps) == 1 else None, provider=p,
                            account_id=deps[0].account_id if len({d.account_id for d in deps}) == 1 else None,
                            est_hourly_exposure_usd=est, time_in_state_seconds=_since_first(run, "provider_api_unavailable", subj),
                            suggested_action=f"check {p} status / the pinned credential {ref}; terminate in the provider "
                                             "console if deadlines pass",
                            detail={"credential_ref": ref, "deployments": [d.deployment_id for d in deps],
                                    "error_kind": kind, "oldest_state_change": oldest.isoformat()}, now=run.now)
        run.find("exposure_alert", alert_kind="provider_api_unavailable", subject=subj, sent=r.get("sent"))
    # 8. og-* provider resources without an ownership record
    for r0 in unowned:
        subj = f"provider_resource:{r0.id}"
        active["unowned_provider_resource"].add(subj)
        r = _ops().exposure("unowned_provider_resource", subj,
                            f"{r0.provider} ssh key {r0.name} exists with no OpenGrid ownership record",
                            deployment_id=None, provider=r0.provider, account_id=None, est_hourly_exposure_usd=0.0,
                            time_in_state_seconds=(run.now - r0.created_at).total_seconds(),
                            suggested_action="verify who created it; delete it in the provider console if it is not "
                                             "in use (OpenGrid never deletes keys it cannot prove it created)",
                            detail={"resource_id": r0.id, "key_id": r0.provider_resource_id, "name": r0.name,
                                    "credential_ref": r0.credential_ref}, now=run.now)
        run.find("exposure_alert", alert_kind="unowned_provider_resource", subject=subj, sent=r.get("sent"))
    # Resolution: only on evidence.
    resolved = 0

    def ended(d):
        if d.status in deployments.TERMINAL_STATES:
            return {"basis": f"deployment is {d.status}", "terminated_at": _iso(d.terminated_at)}
        return None

    resolved += _resolve_missing("termination_failed", active["termination_failed"], _dep_evidence(
        lambda d: ended(d) or ({"basis": f"status now {d.status}"} if d.status != "termination_failed" else None)))
    resolved += _resolve_missing("past_deadline", active["past_deadline"], _dep_evidence(ended))
    resolved += _resolve_missing("resource_state_unknown", active["resource_state_unknown"], _dep_evidence(
        lambda d: ended(d) or ({"basis": f"state known: {d.status}", "last_checked_at": _iso(d.last_checked_at)}
                               if d.status not in UNCERTAIN else None)))
    resolved += _resolve_missing("active_without_billing", active["active_without_billing"], _dep_evidence(
        lambda d: ended(d) or {"basis": "usage slices caught up", "metered_through": _iso(_metered_through(d))}))
    resolved += _resolve_missing("overspend", active["overspend"], _dep_evidence(
        lambda d: {"basis": "spend back within the quote tolerance", "status": d.status}))

    def orphan_ev(subject):
        o = orphans_by_id(int(subject.split(":", 1)[1]))
        if o and o.get("status") not in ("open", "terminating"):
            return {"basis": f"orphan {o['status']}", "resolution_note": o.get("resolution_note"),
                    "resolved_by": o.get("resolved_by")}
        return None

    resolved += _resolve_missing("suspected_orphan", active["suspected_orphan"], orphan_ev)
    resolved += _resolve_missing("provider_api_unavailable", active["provider_api_unavailable"], lambda subj: (
        {"basis": "list_instances succeeded", "at": run.now.isoformat()}
        if tuple(subj.split(":", 2)[1:]) in {(p, r) for p, r in run.listed_ok} else
        ({"basis": "no active deployments on this credential any more", "at": run.now.isoformat()}
         if tuple(subj.split(":", 2)[1:]) not in {(p, str(r)) for p, r in by_key} and
         tuple(subj.split(":", 2)[1:]) not in {(p, str(r)) for p, r in run.list_failed} else None)))

    def res_ev(subject):
        r = resources.get(int(subject.split(":", 1)[1]))
        if r and r["status"] != "abandoned":
            return {"basis": f"resource {r['status']}", "deleted_at": r.get("deleted_at")}
        return None

    resolved += _resolve_missing("unowned_provider_resource", active["unowned_provider_resource"], res_ev)
    counts = {k: len(v) for k, v in active.items()}
    counts["resolved"] = resolved
    return counts


def _since_first(run: _Run, kind: str, subject: str) -> float:
    from store.reconcile import OpsAlertState
    with normalize.SessionLocal() as s:
        t = s.scalar(select(OpsAlertState.first_at).where(OpsAlertState.kind == kind, OpsAlertState.subject == subject,
                                                          OpsAlertState.status == "open"))
    return 0.0 if t is None else (run.now - t).total_seconds()


def _metered_through(d: Deployment):
    with normalize.SessionLocal() as s:
        w = s.get(DeploymentWatch, d.deployment_id)
        return w.metered_through if w else None


def _overspend(run: _Run, d: Deployment, pct: float, active: dict) -> None:
    """Spend above the quote by more than pct: the execution price vs the quoted price, the metered cost vs
    quote x metered time, and the metered cost vs the quote's estimated total."""
    from sqlalchemy import func

    q = d.quoted_price_per_gpu_hour
    if q is None or float(q) <= 0:
        return
    q = float(q)
    with normalize.SessionLocal() as s:
        cost, secs = s.execute(select(func.coalesce(func.sum(UsageSlice.cost_usd), 0),
                                      func.coalesce(func.sum(UsageSlice.billable_seconds), 0))
                               .where(UsageSlice.deployment_id == d.deployment_id)).one()
        est_total = None
        if d.quote_id:
            from store.routing import QuoteRow
            qr = s.get(QuoteRow, d.quote_id)
            est_total = None if qr is None or qr.est_total_cost is None else float(qr.est_total_cost)
    cost, secs = float(cost), int(secs)
    expected = q * (d.gpu_count or 1) * secs / 3600
    reasons = []
    if d.actual_price_per_gpu_hour is not None and float(d.actual_price_per_gpu_hour) > q * (1 + pct / 100):
        reasons.append(f"execution price {float(d.actual_price_per_gpu_hour):.4f}/GPU-h vs quote {q:.4f}")
    if expected > 0 and cost > expected * (1 + pct / 100):
        reasons.append(f"metered cost ${cost:.2f} vs expected ${expected:.2f}")
    if est_total and cost > est_total * (1 + pct / 100):
        reasons.append(f"metered cost ${cost:.2f} vs quoted total ${est_total:.2f}")
    if not reasons:
        return
    subj = f"dep:{d.deployment_id}"
    active["overspend"].add(subj)
    _expose("overspend", d, subj, f"{d.provider} {d.deployment_id}: spend exceeds the quote by > {pct:g}%: "
                                  + "; ".join(reasons), d.billable_start or d.provisioned_at,
            "review the provider price and the quote; terminate if the workload does not justify it", run,
            detail={"reasons": reasons, "metered_cost_usd": round(cost, 4), "expected_cost_usd": round(expected, 4),
                    "quote_price_per_gpu_hour": q, "quote_est_total_cost": est_total})


# --------------------------------------------------------------------------
# Admin functions (the API layer is owned by the core / metrics agents)
# --------------------------------------------------------------------------

def _orphan_dict(o: OrphanResource) -> dict:
    return {"id": o.id, "provider": o.provider, "instance_id": o.instance_id, "instance_name": o.instance_name,
            "kind": o.kind, "deployment_id": o.deployment_id, "credential_ref": o.credential_ref,
            "provider_state": o.provider_state, "provider_status": o.provider_status,
            "price_per_hour": None if o.price_per_hour is None else float(o.price_per_hour),
            "first_seen_at": _iso(o.first_seen_at), "last_seen_at": _iso(o.last_seen_at), "seen_count": o.seen_count,
            "status": o.status, "provably_ours": o.provably_ours, "auto_terminated": o.auto_terminated,
            "terminate_attempts": o.terminate_attempts, "last_terminate_at": _iso(o.last_terminate_at),
            "last_terminate_outcome": o.last_terminate_outcome, "action": o.action, "resolved_by": o.resolved_by,
            "resolved_at": _iso(o.resolved_at), "resolution_note": o.resolution_note, "evidence": o.evidence}


def orphans(status: str | None = None, *, limit: int = 500) -> list[dict]:
    """Orphan resources, newest first. status: open | terminating | terminated | ignored | adopted | gone."""
    with normalize.SessionLocal() as s:
        q = select(OrphanResource).order_by(OrphanResource.last_seen_at.desc()).limit(limit)
        if status:
            q = q.where(OrphanResource.status == status)
        return [_orphan_dict(o) for o in s.scalars(q)]


def resolve_orphan(orphan_id: int, action: str, by: str, *, note: str | None = None) -> dict:
    """Operator decision on an orphan. terminate (calls the provider with the credential that listed it),
    ignore (stop alerting; the instance is someone's responsibility outside OpenGrid), adopt (link it to its
    deployment when that deployment is still unresolved: launch_unknown / provider_timeout / orphan_suspected /
    provisioning without an instance id). Raises ValueError for an impossible action."""
    if action not in ORPHAN_ACTIONS:
        raise ValueError(f"action must be one of {ORPHAN_ACTIONS}")
    if not by:
        raise ValueError("by (the operator) is required")
    with normalize.SessionLocal() as s:
        o = s.get(OrphanResource, orphan_id)
    if o is None:
        raise ValueError("no such orphan")
    now = _now()
    result: dict = {}
    if action == "terminate":
        if not o.credential_ref:
            raise ValueError("the orphan has no credential reference; terminate it in the provider console")
        a = adapters.build(o.provider, _creds(o.credential_ref, o.provider))
        if a is None:
            raise ValueError(f"no adapter for {o.provider}")
        try:
            tr = a.terminate(o.instance_id)
        finally:
            a.close()
        result = {"outcome": tr.outcome, "message": tr.message}
        status = "terminating" if tr.outcome in ("accepted", "already_gone", "unknown") else o.status
    elif action == "ignore":
        status = "ignored"
    else:
        d = deployments.load_row(o.deployment_id) if o.deployment_id else None
        if d is None:
            raise ValueError("adopt needs the orphan's deployment; this instance has none")
        if d.provider_instance_id or d.status not in ("launch_unknown", "provider_timeout", "orphan_suspected",
                                                      "provisioning", "credentials_unavailable"):
            raise ValueError(f"deployment {d.deployment_id} is {d.status} with instance {d.provider_instance_id}: "
                             "nothing to adopt")
        st = InstanceState(o.provider_state or "unknown", instance_id=o.instance_id, name=o.instance_name,
                           provider_status=o.provider_status, observed_at=o.last_seen_at)
        run = _Run("manual", o.provider)
        _adopt(run, d, st, basis=f"operator {by} adopted orphan {orphan_id}")
        result = {"findings": run.findings}
        status = "adopted"
    with normalize.SessionLocal.begin() as s:
        row = s.get(OrphanResource, orphan_id, with_for_update=True)
        before = row.status
        row.status, row.action, row.resolved_by, row.resolved_at = status, action, by, now
        row.resolution_note = note
        row.updated_at = now
    try:
        from routing import control
        control.record("resolve_orphan", f"orphan:{orphan_id}", before={"status": before},
                       after={"status": status, "action": action, **result}, reason=note or action, actor=by)
    except Exception:  # noqa: BLE001 - the control log is best-effort here; the orphan row records the decision
        log.exception("could not write the control log for orphan %s", orphan_id)
    out = orphans_by_id(orphan_id)
    out["result"] = result
    return out


def orphans_by_id(orphan_id: int) -> dict:
    with normalize.SessionLocal() as s:
        o = s.get(OrphanResource, orphan_id)
        return _orphan_dict(o) if o else {}


def runs(limit: int = 50) -> list[dict]:
    with normalize.SessionLocal() as s:
        return [{"id": r.id, "started_at": _iso(r.started_at), "finished_at": _iso(r.finished_at), "trigger": r.trigger,
                 "provider": r.provider, "status": r.status, "counts": r.counts, "providers": r.providers,
                 "error": r.error, "findings": r.findings}
                for r in s.scalars(select(ReconciliationRun).order_by(ReconciliationRun.id.desc()).limit(limit))]


def last_run() -> dict | None:
    r = runs(1)
    return r[0] if r else None


def watch(deployment_id: str) -> dict | None:
    """The reconciler's / tracker's memory for one deployment (admin view)."""
    with normalize.SessionLocal() as s:
        w = s.get(DeploymentWatch, deployment_id)
        if w is None:
            return None
        return {k: (_iso(v) if isinstance(v, datetime) else v) for k, v in
                ((c.name, getattr(w, c.name)) for c in DeploymentWatch.__table__.columns)}


@job("reconcile", every_seconds=settings.reconcile_interval_seconds, initial_delay_seconds=90)
def _reconcile_job():
    try:
        r = run_once(trigger="job")
    except Exception as exc:
        # A pass that cannot even start (DB error, ...) means deadlines are not enforced and orphans not
        # found: page the operator (first failure, then every 30th consecutive one), then let jobs.py record it.
        job_failed("reconcile", exc)
        raise
    job_ok("reconcile")
    return {k: r.get(k) for k in ("run_id", "status", "counts", "skipped")}


_JOB_FAILS: dict[str, int] = {}


def job_failed(name: str, exc: Exception) -> None:
    n = _JOB_FAILS[name] = _JOB_FAILS.get(name, 0) + 1
    if n == 1 or n % 30 == 0:
        try:
            tracker.alert("reconciliation_failed", f"job:{name}",
                          f"background job {name} failed {n} time(s) in a row: {type(exc).__name__}: {str(exc)[:200]}"
                          " - deadline auto-terminate, termination confirmation and orphan detection may be stalled",
                          detail={"job": name, "consecutive_failures": n})
        except Exception:  # noqa: BLE001
            log.exception("job failure alert for %s failed", name)


def job_ok(name: str) -> None:
    _JOB_FAILS.pop(name, None)
