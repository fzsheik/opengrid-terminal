"""The first-real-route checklist, computed LIVE from the running system (never stored, never ticked by hand).

    GET /v1/admin/checklist?provider=lambda[&route_request_id=rr_...][&probe=false]

Each item is green | red | unknown with the evidence it was judged on. The overall status is green
only when every REQUIRED item is green; unknown counts as not green (if the system cannot prove
it, it is not ready). Tables other agents own are read defensively: a missing table or column
makes the item "unknown" with "unavailable: <reason>", never an exception.
See methodology/first-live-route.md for what each item means and how to turn it green.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

import jobs
import normalize
import observability as obs
from config import settings

log = logging.getLogger(__name__)

GREEN, RED, UNKNOWN = "green", "red", "unknown"
KILL_WINDOW = timedelta(days=7)


def _item(id_, label, status, evidence, *, required=True, fix=None):
    return {"id": id_, "label": label, "status": status, "required": required, "evidence": evidence,
            **({"fix": fix} if fix and status != GREEN else {})}


def _now():
    return datetime.now(timezone.utc)


def _ts(v):
    if v is None or isinstance(v, datetime):
        return v
    t = datetime.fromisoformat(str(v))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------- items

def pepper():
    if settings.api_key_pepper:
        if len(settings.api_key_pepper) < 32:
            return _item("api_key_pepper", "API_KEY_PEPPER set (not dev-derived)", RED,
                         f"set but short ({len(settings.api_key_pepper)} chars; use >= 32 random chars)",
                         fix="set API_KEY_PEPPER to a long random secret (python -c \"import secrets; "
                             "print(secrets.token_urlsafe(48))\")")
        return _item("api_key_pepper", "API_KEY_PEPPER set (not dev-derived)", GREEN,
                     f"set ({len(settings.api_key_pepper)} chars)")
    return _item("api_key_pepper", "API_KEY_PEPPER set (not dev-derived)", RED,
                 "unset: API keys are hashed with a pepper derived from DATABASE_URL (dev only)",
                 fix="set API_KEY_PEPPER")


def fernet_key():
    label = "Credentials encryption key is a real Fernet key"
    raw = settings.credentials_encryption_key
    if not raw:
        return _item("credentials_encryption_key", label, RED, "unset: a dev key derived from DATABASE_URL is used",
                     fix="set CREDENTIALS_ENCRYPTION_KEY to Fernet.generate_key()")
    try:
        from cryptography.fernet import Fernet

        Fernet(raw.encode())
    except Exception:
        return _item("credentials_encryption_key", label, RED, "set, but it is a passphrase, not a Fernet key",
                     fix="replace with cryptography.fernet.Fernet.generate_key() and rotate stored secrets")
    return _item("credentials_encryption_key", label, GREEN, "set; parses as a Fernet key")


def migrations():
    label = "Database migrations at head"
    try:
        import db
        from alembic.runtime.migration import MigrationContext

        head = db.head_revision()
        with normalize.SessionLocal() as s:
            cur = MigrationContext.configure(s.connection()).get_current_revision()
    except Exception as e:
        return _item("migrations_at_head", label, UNKNOWN, f"unavailable: {type(e).__name__}: {e}"[:300])
    if cur == head:
        return _item("migrations_at_head", label, GREEN, f"at {head}")
    return _item("migrations_at_head", label, RED, f"database at {cur or '(none)'}, code head {head}",
                 fix="restart the app (init_db migrates) or run alembic upgrade head")


def _platform_credentials(provider: str):
    try:
        from routing import credentials as rc

        if hasattr(rc, "platform"):
            return rc.platform(rc.credential_provider(provider) if hasattr(rc, "credential_provider") else provider)
    except Exception:
        log.exception("platform credential lookup failed")
    key = getattr(settings, f"{provider}_api_key", None)
    return {"api_key": key} if key else None


def provider_credential(provider: str, probe: bool):
    label = f"{provider}: credential present and validated by a read-only API call"
    from routing import adapters

    cls = adapters.get(provider)
    if cls is None:
        return _item("provider_credential", label, RED, f"no execution adapter for {provider!r}")
    creds = _platform_credentials(provider)
    if not creds and tuple(getattr(cls, "CREDENTIALS", ("api_key",)) or ()):
        return _item("provider_credential", label, RED, "no OpenGrid-managed credential configured",
                     fix=f"set the {provider} API key in the environment")
    if not probe:
        return _item("provider_credential", label, UNKNOWN, "credential present; read-only probe skipped (probe=false)")
    a = adapters.build(provider, creds)
    try:
        if hasattr(a, "list_instances"):
            n = len(a.list_instances())
            return _item("provider_credential", label, GREEN,
                         f"list_instances() succeeded ({n} instance(s) on the account) at {_now().isoformat()}")
        return _item("provider_credential", label, UNKNOWN, "adapter has no read-only list call")
    except Exception as e:
        msg = obs.redact(f"{type(e).__name__}: {getattr(e, 'message', e)}")[:300]
        return _item("provider_credential", label, RED, f"read-only call failed: {msg}",
                     fix="check the key's permissions / validity at the provider")
    finally:
        try:
            a.close()
        except Exception:
            pass


def _launch_defaults(provider: str) -> dict:
    d = settings.routing_launch_defaults or {}
    return dict(d.get(provider) or {})


def ssh_key(provider: str):
    label = f"{provider}: SSH public key configured for validation deployments"
    d = _launch_defaults(provider)
    have = [k for k in ("ssh_public_key", "ssh_key") if d.get(k)]
    for name in ("validation_ssh_public_key", "operator_ssh_public_key"):
        if getattr(settings, name, None):
            have.append(f"settings.{name}")
    if have:
        return _item("ssh_key", label, GREEN, f"configured: {', '.join(have)}")
    return _item("ssh_key", label, RED, "no ssh_public_key / ssh_key in routing_launch_defaults for this provider",
                 fix=f'ROUTING_LAUNCH_DEFAULTS={{"{provider}": {{"ssh_public_key": "ssh-ed25519 ..."}}}}')


def launch_params(provider: str):
    label = f"{provider}: launch parameters configured (image, region/environment)"
    from routing import adapters

    cls = adapters.get(provider)
    if cls is None:
        return _item("launch_params", label, RED, "no adapter")
    req = [f for f in getattr(cls, "REQUIRED_LAUNCH", ()) if f != "ssh_key"]
    d = _launch_defaults(provider)
    missing = [f for f in req if not d.get(f)]
    if missing:
        return _item("launch_params", label, RED, f"adapter requires {req}; missing in launch defaults: {missing}",
                     fix=f"add {missing} to ROUTING_LAUNCH_DEFAULTS[{provider!r}]")
    extra = sorted(k for k in d if k not in ("ssh_key", "ssh_public_key"))
    return _item("launch_params", label, GREEN, f"required {list(req) or 'none'} present; configured keys {extra}")


def spend_limits(account_id: int | None):
    label = "Max spend limits set"
    defaults = {k: getattr(settings, k) for k in ("default_max_hourly_cost", "default_max_gpus",
                                                  "default_max_active_deployments", "default_monthly_spend_limit")
                if getattr(settings, k, None) is not None}
    with normalize.SessionLocal() as s:
        if not obs.has_table(s, "account_limits"):
            if defaults:
                return _item("spend_limits", label, UNKNOWN, f"defaults {defaults}; account_limits table unavailable "
                                                             "(execution core migration not applied), so they are "
                                                             "not enforced yet")
            return _item("spend_limits", label, RED, "unavailable: no account_limits table and no default limits")
        row = obs.rows(s, "account_limits", "account_id = :a", {"a": account_id}, None, 1) if account_id else []
    if row:
        set_ = {k: v for k, v in row[0].items() if k.startswith("max_") or k == "monthly_spend_limit"}
        return _item("spend_limits", label, GREEN, f"account {account_id} limits {set_}")
    if defaults:
        return _item("spend_limits", label, GREEN, f"global defaults apply {defaults}"
                     + ("" if account_id else " (no account given)"))
    return _item("spend_limits", label, RED, "no account limits and no defaults")


def _control():
    try:
        from routing import control

        return control
    except Exception:
        return None


def execution_mode(provider: str):
    label = f"Execution mode SUPERVISED and {provider} supervised flag on"
    c = _control()
    mode, flags = None, None
    if c is not None:
        try:
            mode = c.effective_mode()
            flags = c.provider_flags(provider)
        except Exception as e:
            return _item("execution_mode", label, UNKNOWN, f"unavailable: control plane error {type(e).__name__}: {e}"[:300])
    else:
        with normalize.SessionLocal() as s:
            if not obs.has_table(s, "provider_execution_flags"):
                return _item("execution_mode", label, UNKNOWN,
                             "unavailable: execution control plane (routing/control.py) not present")
            r = obs.rows(s, "provider_execution_flags", "provider = :p", {"p": provider}, None, 1)
            flags = r[0] if r else {}
    ev = f"effective mode {mode}; flags {json.dumps({k: (flags or {}).get(k) for k in ('adapter_status', 'supervised_enabled', 'live_enabled', 'killed')}, default=str)}"
    if not settings.routing_live_provisioning:
        return _item("execution_mode", label, RED, "ROUTING_LIVE_PROVISIONING is false (env ceiling: PREVIEW_ONLY); "
                     + ev, fix="set ROUTING_LIVE_PROVISIONING=true for this environment")
    if mode != "SUPERVISED":
        return _item("execution_mode", label, RED, ev + ("; the first live route must be SUPERVISED, not LIVE"
                                                        if mode == "LIVE" else ""),
                     fix="POST the admin mode endpoint: mode SUPERVISED with a reason")
    if not (flags or {}).get("supervised_enabled") or (flags or {}).get("killed"):
        return _item("execution_mode", label, RED, ev, fix=f"enable supervised execution for {provider}")
    return _item("execution_mode", label, GREEN, ev)


def provider_validated(provider: str):
    label = f"{provider} adapter validated (a complete validation cycle recorded)"
    c = _control()
    flags = None
    try:
        if c is not None:
            flags = c.provider_flags(provider)
        else:
            with normalize.SessionLocal() as s:
                if obs.has_table(s, "provider_execution_flags"):
                    r = obs.rows(s, "provider_execution_flags", "provider = :p", {"p": provider}, None, 1)
                    flags = r[0] if r else {}
    except Exception as e:
        return _item("provider_validated", label, UNKNOWN, f"unavailable: {type(e).__name__}: {e}"[:300])
    if flags is None:
        return _item("provider_validated", label, UNKNOWN, "unavailable: provider_execution_flags not present")
    if (flags or {}).get("adapter_status") == "validated":
        return _item("provider_validated", label, GREEN, f"validated_at {flags.get('validated_at')}, "
                                                         f"validation deployment {flags.get('validation_deployment_id')}")
    return _item("provider_validated", label, RED, f"adapter_status {(flags or {}).get('adapter_status') or 'simulated'}",
                 fix="run a validation deployment (purpose=validation) through launch, running, terminate and "
                     "cost reconciliation")


def _row_time(r: dict):
    for k in ("at", "created_at", "changed_at", "updated_at", "ts"):
        if r.get(k):
            return _ts(r[k])
    return None


def _mode_of(v):
    if isinstance(v, dict):
        v = v.get("mode")
    return v.upper() if isinstance(v, str) else None


def _is_kill(r: dict, provider: str) -> bool | None:
    """True = a kill, False = an un-kill / resume, None = unrelated. Reads routing/control.py's log rows
    (action kill_all | kill_provider | unkill_provider | set_mode, before/after jsonb), tolerantly."""
    target = r.get("target")
    if target not in (None, "mode", "all", "global", provider):
        return None  # another provider's kill switch
    action = str(r.get("action") or r.get("event") or "").lower()
    if any(w in action for w in ("unkill", "resume", "unpause", "revive")):
        return False
    if "kill" in action or "pause" in action:
        return True
    new = _mode_of(r.get("after")) or _mode_of(r.get("new_mode")) or _mode_of(r.get("mode"))
    old = _mode_of(r.get("before")) or _mode_of(r.get("old_mode"))
    if new == "DISABLED":
        return True
    if new and old == "DISABLED":
        return False
    after = r.get("after") if isinstance(r.get("after"), dict) else {}
    if isinstance(after.get("killed"), bool):
        return after["killed"]
    return None


def kill_switch_tested(provider: str):
    label = "Kill switch tested (a kill and an un-kill in the last 7 days)"
    with normalize.SessionLocal() as s:
        if not obs.has_table(s, "execution_control_log"):
            return _item("kill_switch_tested", label, UNKNOWN,
                         "unavailable: execution_control_log table not present (execution core migration)")
        rows = obs.rows(s, "execution_control_log", "true", None, "id DESC" if "id" in obs.columns(
            s, "execution_control_log") else None, 2000)
    since = _now() - KILL_WINDOW
    recent = sorted([r for r in rows if (_row_time(r) or since) >= since], key=lambda r: _row_time(r) or since)
    kill_at, killed_mode = None, False
    for r in recent:
        k = _is_kill(r, provider)
        if k is None and killed_mode and _mode_of(r.get("after") or r.get("mode")) not in (None, "DISABLED"):
            k = False  # a plain mode change away from DISABLED
        if k is True:
            kill_at = _row_time(r)
            killed_mode = True
        elif k is False and kill_at is not None:
            return _item("kill_switch_tested", label, GREEN,
                         f"kill at {kill_at.isoformat() if kill_at else '?'}, un-kill at {_row_time(r).isoformat()}")
    return _item("kill_switch_tested", label, RED,
                 f"{len(recent)} control change(s) in 7 days; no kill followed by an un-kill",
                 fix="kill_all with a reason, confirm a preview still works and a launch is refused, then restore")


def _job(name: str):
    return jobs.JOBS.get(name) or next((v for k, v in jobs.JOBS.items() if name in k), None)


def _job_ev(j, interval: float, what: str = "reconciliation"):
    """(ok | None, evidence) from the in-process jobs registry; None when the job is not registered here."""
    if j is None:
        return None, f"no {what} job registered in this process"
    if j.last_finished is None and not j.runs:
        return None, f"job {j.name}: registered but not run in this process (jobs run elsewhere or disabled)"
    fin = j.last_finished
    fresh = fin is not None and _now() - fin < timedelta(seconds=2 * max(interval, j.every_seconds))
    return (fresh and not j.last_error), (f"job {j.name}: last finished {fin.isoformat() if fin else 'never'}, error "
                                          f"{j.last_error!r}, runs {j.runs}, failures {j.failures}")


def _heartbeat(name: str, interval: float):
    """(ok | None, evidence) from the durable heartbeat (execution_controls 'job:<name>'); None when absent."""
    try:
        from routing import control as rc

        with normalize.SessionLocal() as s:
            if not obs.has_table(s, "execution_controls"):
                return None, None
            r = obs.rows(s, "execution_controls", "key = :k", {"k": f"job:{name}"}, None, 1)
        if not r:
            return None, None
        h = rc.job_health(name, interval)
        return h["ok"], "heartbeat: " + h["reason"]
    except Exception as e:  # noqa: BLE001
        return None, f"heartbeat unavailable: {type(e).__name__}"


def reconciliation_running(provider: str | None = None):
    """Fresh, successful reconciliation. Only the SELECTED provider's errors count: a list failure at an
    unrelated provider (a 'partial' run) does not turn this item red."""
    label = "Reconciliation worker running (last run < 2x interval, no failure)"
    interval = float(getattr(settings, "reconcile_interval_seconds", 120))
    ev = []
    ok_job, e = _job_ev(_job("reconcil"), interval)
    ev.append(e)
    ok_hb, e = _heartbeat("reconcile", interval)
    if e:
        ev.append(e)
    ok_db = None
    with normalize.SessionLocal() as s:
        if obs.has_table(s, "reconciliation_runs"):
            cols = obs.columns(s, "reconciliation_runs")
            tcol = next((c for c in ("finished_at", "started_at", "created_at", "at") if c in cols), None)
            last = obs.rows(s, "reconciliation_runs", "true", None, f"{tcol} DESC" if tcol else None, 1)
            if last:
                r = last[0]
                t = _ts(r.get(tcol)) if tcol else None
                status = str(r.get("status") or "ok").lower()
                provs = r.get("providers") if isinstance(r.get("providers"), dict) else {}
                mine = provs.get(provider) if provider else None
                mine_err = isinstance(mine, dict) and bool(mine.get("error") or mine.get("list_errors"))
                if status == "partial":
                    # some provider errored in that pass: red only when it is the selected provider (or unknown)
                    failed = mine_err or (provider is None)
                else:
                    failed = status in ("failed", "error") or (bool(r.get("error")) and status != "ok") or mine_err
                ok_db = t is not None and _now() - t < timedelta(seconds=2 * interval) and not failed
                ev.append(f"last reconciliation_runs row {t.isoformat() if t else '?'} status {r.get('status')!r}"
                          f" error {r.get('error')!r}" + (f"; {provider}: {json.dumps(mine, default=str)}"
                                                          if provider and mine is not None else ""))
            else:
                ok_db = False
                ev.append("reconciliation_runs is empty")
        else:
            ev.append("reconciliation_runs table not present")
    proc = ok_job if ok_job is not None else ok_hb
    if proc or ok_db:
        if proc is False or ok_db is False:
            return _item("reconciliation_running", label, RED, "; ".join(ev))
        return _item("reconciliation_running", label, GREEN, "; ".join(ev))
    if proc is None and ok_db is None:
        return _item("reconciliation_running", label, UNKNOWN, "unavailable: " + "; ".join(ev))
    return _item("reconciliation_running", label, RED, "; ".join(ev),
                 fix="the reconciliation job must run (OPENGRID_NO_JOBS unset in one process) and succeed")


def tracker_running():
    """The routing_tracker job (status polling, usage metering, billing retries) ran successfully recently."""
    label = "Tracker worker running (status polling + metering; last run < 2x interval, no failure)"
    interval = float(getattr(settings, "tracker_interval_seconds", 60))
    ok_job, e1 = _job_ev(_job("routing_tracker"), interval, "tracker")
    ok_hb, e2 = _heartbeat("routing_tracker", interval)
    ev = "; ".join(x for x in (e1 if ok_job is not None else None, e2) if x) or (e1 or "no tracker evidence")
    ok = ok_job if ok_job is not None else ok_hb
    if ok is None:
        return _item("tracker_running", label, UNKNOWN, "unavailable: " + ev,
                     fix="the routing_tracker job must run (OPENGRID_NO_JOBS unset in one process) and succeed")
    if ok and ok_hb is not False:
        return _item("tracker_running", label, GREEN, ev)
    return _item("tracker_running", label, RED, ev,
                 fix="the routing_tracker job must run and succeed (see /v1/ops jobs and ERROR logs)")


def termination_tested(provider: str):
    label = f"{provider}: termination tested (validation deployment terminated with provider confirmation)"
    with normalize.SessionLocal() as s:
        cols = obs.columns(s, "deployments")
        if "purpose" not in cols:
            return _item("termination_tested", label, UNKNOWN,
                         "unavailable: deployments.purpose column not present (execution core migration)")
        deps = obs.rows(s, "deployments", "provider = :p AND purpose = 'validation' AND status = 'terminated'",
                        {"p": provider}, "created_at DESC", 20)
        for d in deps:
            evs = obs.rows(s, "deployment_events", "deployment_id = :d AND to_status = 'terminated'",
                           {"d": d["deployment_id"]}, "at DESC", 1)
            e = evs[0] if evs else {}
            evidence = e.get("evidence") or (e.get("detail") or {}).get("evidence") or (e.get("detail") or {}).get(
                "confirmation")
            if evidence:
                return _item("termination_tested", label, GREEN,
                             f"{d['deployment_id']} terminated at {e.get('at')} with provider evidence")
    if deps:
        return _item("termination_tested", label, RED, f"{len(deps)} terminated validation deployment(s), none with "
                                                       "recorded provider confirmation evidence")
    return _item("termination_tested", label, RED, "no terminated validation deployment for this provider",
                 fix="run a validation deployment and terminate it; the reconciler must confirm it gone")


def logging_active():
    label = "Structured logging active (JSON lines + request ids)"
    mw, js = obs.INSTALLED["middleware"], obs.INSTALLED["json"]
    ev = f"request-id middleware {'installed' if mw else 'NOT installed'}; JSON logs {'on' if js else 'off'}"
    if mw and js:
        return _item("logging_active", label, GREEN, ev)
    return _item("logging_active", label, RED, ev, fix="LOG_JSON=true (default when deployed)")


def alerts_active():
    label = "Ops alerts active for orphans / termination failures"
    names = [n for n in ("ops_alert_webhook_url", "ops_webhook_url", "alerts_ops_webhook_url", "ops_alert_email",
                         "ops_alerts_webhook") if getattr(settings, n, None)]
    if names:
        return _item("alerts_active", label, GREEN, f"configured: {', '.join('settings.' + n for n in names)}")
    import importlib

    for mod in ("alerts.ops", "alerts"):
        try:
            m = importlib.import_module(mod)
        except Exception:
            continue
        hook = getattr(m, "ops_channel_configured", None) or getattr(m, "channel_configured", None)
        try:
            if callable(hook) and hook():
                return _item("alerts_active", label, GREEN, f"{mod}.{hook.__name__}() is true")
        except Exception as e:
            return _item("alerts_active", label, UNKNOWN, f"{mod}.{hook.__name__}() failed: {type(e).__name__}")
    return _item("alerts_active", label, UNKNOWN,
                 "no outbound ops alert channel found in settings; orphans and termination failures surface only in "
                 "the admin API (/v1/admin/execution/overview, /v1/ops/summary) and ERROR log lines",
                 fix="configure the ops alert webhook (security / alerts agent setting) and send a test alert")


def quote_approved(route_request_id: str | None):
    label = "User approved an unexpired quote for this route"
    if not route_request_id:
        return _item("quote_approved", label, UNKNOWN, "pass route_request_id=rr_... to check a specific route")
    with normalize.SessionLocal() as s:
        if not obs.has_table(s, "quotes"):
            return _item("quote_approved", label, UNKNOWN, "unavailable: quotes table not present (execution core)")
        quotes = obs.rows(s, "quotes", "route_request_id = :r", {"r": route_request_id}, "created_at DESC", 20)
        cols = obs.columns(s, "deployments")
        deps = obs.rows(s, "deployments", "route_request_id = :r", {"r": route_request_id}, "created_at DESC", 20)
    if not quotes:
        return _item("quote_approved", label, RED, f"no quote for {route_request_id}")
    now = _now()
    for d in deps:
        if "approved_at" in cols and d.get("approved_at") and d.get("quote_id"):
            q = next((q for q in quotes if q.get("id") == d["quote_id"]), None)
            if q is None:
                continue
            exp = _ts(q.get("expires_at"))
            consumed = q.get("status") == "consumed" and q.get("consumed_by_deployment_id") == d["deployment_id"]
            if consumed or (q.get("status") == "active" and exp and exp > now):
                return _item("quote_approved", label, GREEN,
                             f"quote {q['id']} ({q.get('status')}, expires {q.get('expires_at')}) approved by "
                             f"{d.get('approved_by')} at {d.get('approved_at')} for {d['deployment_id']}")
            return _item("quote_approved", label, RED, f"quote {q['id']} approved but {q.get('status')}, "
                                                        f"expires {q.get('expires_at')}: re-quote and re-approve")
    active = [q for q in quotes if q.get("status") == "active" and (_ts(q.get("expires_at")) or now) > now]
    return _item("quote_approved", label, RED, f"{len(quotes)} quote(s), {len(active)} active, none approved",
                 fix="the operator approves the quote (POST /v1/route/{id}/approve) before it expires")


def checklist(provider: str, route_request_id: str | None = None, account_id: int | None = None,
              probe: bool = True) -> dict:
    provider = provider.lower()
    makers = [pepper, fernet_key, migrations, lambda: provider_credential(provider, probe), lambda: ssh_key(provider),
              lambda: launch_params(provider), lambda: spend_limits(account_id), lambda: execution_mode(provider),
              lambda: provider_validated(provider), lambda: kill_switch_tested(provider),
              lambda: reconciliation_running(provider), tracker_running,
              lambda: termination_tested(provider), logging_active, alerts_active,
              lambda: quote_approved(route_request_id)]
    items = []
    for m in makers:
        try:
            items.append(m())
        except Exception as e:  # one broken check must not hide the others
            log.exception("checklist item failed")
            items.append(_item(getattr(m, "__name__", "item"), "check failed", UNKNOWN,
                               f"unavailable: {type(e).__name__}: {e}"[:300]))
    required = [i for i in items if i["required"]]
    overall = GREEN if all(i["status"] == GREEN for i in required) else (
        RED if any(i["status"] == RED for i in required) else UNKNOWN)
    return {"provider": provider, "route_request_id": route_request_id, "overall": overall,
            "green": sum(i["status"] == GREEN for i in items), "red": sum(i["status"] == RED for i in items),
            "unknown": sum(i["status"] == UNKNOWN for i in items), "items": items, "computed_at": _now().isoformat(),
            "rule": "overall is green only when every required item is green; unknown is not green"}
