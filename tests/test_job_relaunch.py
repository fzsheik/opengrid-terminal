"""Automatic job relaunch (jobs/relaunch.py): the trigger, THE CORE RULE (never while the previous machine might still
bill), the money rule, uniqueness, owner re-verification, exclusions, SUPERVISED approval, the chain block.

Two providers, both the REAL Lambda adapter against fake Lambda APIs (tests/test_jobs.JobSim): `lambda` and
`lambdb` (a registered copy of LambdaAdapter that authenticates with the lambda test key; a test-only provider).
Every submission goes through jobs.orchestrator.submit -> workloads.select -> quote -> engine.route exactly like a
human one. Scratch DB og_test_job_relaunch. No network, no real provider.
Run:  .venv/bin/python tests/test_job_relaunch.py
"""

from __future__ import annotations

import sys
import threading
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_jobs as T  # noqa: E402  (imports scratchdb first: provider credentials blanked, network blocked)
from test_jobs import q, scalar  # noqa: E402

import bench_fixtures as bf  # noqa: E402
import normalize  # noqa: E402
from accounts import keys  # noqa: E402
from accounts.auth import OPERATOR, Principal  # noqa: E402
from config import settings  # noqa: E402
from routing import adapters, deployments, reliability, tracker  # noqa: E402
from routing.adapters.lambda_labs import LambdaAdapter  # noqa: E402
from sqlalchemy import text  # noqa: E402

from jobs import orchestrator as O  # noqa: E402
from jobs import relaunch as R  # noqa: E402
from jobs import sync  # noqa: E402
from jobs import artifacts as A  # noqa: E402

T.DB = "og_test_job_relaunch"
G = T.G
SCOPES = ["data:read", "route:preview", "route:execute", "deployments:read", "deployments:write"]
SIM2: "T.JobSim | None" = None


class LambdbAdapter(LambdaAdapter):
    """Test-only second provider: the real Lambda adapter, its own fake API, the lambda test credential."""
    CREDENTIAL_PROVIDER = "lambda"


def world(*, second=True, mode="SUPERVISED", second_price=2.59):
    global SIM2
    sim = T.world(mode=mode)
    with normalize.SessionLocal.begin() as s:
        s.execute(text("DELETE FROM job_relaunches"))
        s.execute(text("DELETE FROM provider_execution_flags WHERE provider = 'lambdb'"))
    rows = []
    r1 = bf.listing("lambda", T.PRICE, gpu=G, region="us-east-1", lid="lambda:" + T.SKU)
    r1.sku = T.SKU
    rows.append(r1)
    if second:
        adapters.register("lambdb", LambdbAdapter)
        SIM2 = T.JobSim()
        SIM2.price_cents = int(round(second_price * 100))
        adapters.TRANSPORTS["lambdb"] = SIM2.transport()
        r2 = bf.listing("lambdb", second_price, gpu=G, region="us-east-1", lid="lambdb:" + T.SKU)
        r2.sku = T.SKU
        rows.append(r2)
        bf.flags("lambdb", validated=True, live=False, supervised=True)
    else:
        adapters.unregister("lambdb")
        adapters.TRANSPORTS.pop("lambdb", None)
        SIM2 = None
    bf.market(G, rows)
    settings.job_auto_relaunch_enabled = True
    settings.job_checkpoint_keep = 2
    return sim


def owner(scopes=SCOPES) -> Principal:
    aid = bf.fresh_account()
    k = keys.create_key(aid, "relaunch-test", scopes=scopes)
    return Principal(kind="api_key", account_id=aid, key_id=k["id"], scopes=frozenset(scopes))


RETRY = {"max_attempts": 3, "max_total_usd": 6}


def submit(w, **kw):
    kw.setdefault("retry", RETRY)
    kw.setdefault("config", {"epochs": 5})
    code, out = O.submit(T.body(**kw), w)
    assert code == 202, (code, out)
    return out["job_id"]


def interrupt(jid, sim, *, ckpts=(1, 2)):
    """Run to training, store checkpoints, then the PROVIDER ends the machine (gpu_interrupted), and drive the job to
    reconciled (the provider-confirmed end)."""
    T._run_until_training(jid)
    for e in ckpts:
        T._ckpt(jid, e)
    sim.instances[T.dep_row(jid).provider_instance_id]["status"] = "terminated"
    tracker.track()
    sync.sync_job(jid)
    j = T.drive_to_end(jid)
    assert j.status == "reconciled" and j.failure_code == "gpu_interrupted", (j.status, j.failure_code)
    return j


def successors(jid):
    return [r[0] for r in q("SELECT job_id FROM jobs WHERE retry_of = :j", j=jid)]


def rrow(jid):
    r = q("SELECT status, reason_code, reason, successor_job_id FROM job_relaunches WHERE failed_job_id = :j", j=jid)
    return r[0] if r else None


def n_jobs():
    return scalar("SELECT count(*) FROM jobs")


# --------------------------------------------------------------------------

def test_validation_and_preview():
    world()
    w = owner()
    for bad, code in (({"max_attempts": 4, "max_total_usd": 9}, "invalid_retry"),
                      ({"max_attempts": 0}, "invalid_retry"),
                      ({"max_attempts": 2}, "retry_budget_required"),
                      ({"max_attempts": 2, "max_total_usd": 1}, "invalid_retry"),        # below the first attempt
                      ({"max_attempts": 2, "max_total_usd": -3}, "invalid_retry"),
                      ({"max_attempts": 2, "max_total_usd": 9, "x": 1}, "unknown_field")):
        T._refused(lambda: O.preview(T.body(retry=bad), w), code)
    T._refused(lambda: O.preview(T.body(routing={"exclude_providers": ["../x"]}), w), "invalid_exclude_providers")
    # an ordinary request keeps exactly its old shape: no retry / exclude_providers keys
    from workloads.spec import validate
    plain = validate(T.body()).request
    assert "retry" not in plain and "exclude_providers" not in plain["routing"], plain
    pv = O.preview(T.body(retry=RETRY), w)
    sr = pv["submission"]["retry"]
    assert sr["max_attempts"] == 3 and sr["max_total_usd"] == 6 and sr["applies"] and sr["auto_relaunch_enabled"], sr
    assert pv["request"]["retry"] == {"max_attempts": 3, "max_total_usd": 6.0}
    settings.job_auto_relaunch_enabled = False
    sr = O.preview(T.body(retry=RETRY), w)["submission"]["retry"]
    assert not sr["applies"] and "switched off" in sr["note"], sr
    assert "retry" not in O.preview(T.body(), w)["submission"]
    # a sandbox job records the policy but is never relaunched (a simulation is never interrupted)
    settings.job_auto_relaunch_enabled = True
    code, sb = O.submit(T.body(sandbox=True, retry=RETRY), w)
    assert sb["chain"]["attempt"] == 1 and sb["chain"]["simulated"] and sb["chain"]["relaunch"] is None, sb["chain"]
    assert R.candidate_ids() == [] and R.consider(sb["job_id"])["decision"] == "skipped"
    # exclude_providers on an ordinary request: the provider is excluded from the selection and the route
    sel = O.preview(T.body(routing={"exclude_providers": ["lambda"]}), w)
    assert sel["execution_ready"]["provider"] == "lambdb", sel["execution_ready"]
    assert sel["exclusions_by_code"].get("provider_excluded") == 1, sel["exclusions_by_code"]
    assert sel["execution_ready"]["route_spec"]["preferences"]["exclude_providers"] == ["lambda"]


def test_a_interrupted_relaunch_once():
    """(a)+(j) gpu_interrupted, provider-confirmed terminated, opted in -> exactly one successor, attempt 2, the
    interrupting provider excluded, resume_from the latest checkpoint; SUPERVISED: it waits in pending_approval."""
    sim = world()
    w = owner()
    jid = submit(w)
    interrupt(jid, sim, ckpts=(1, 2, 3))
    assert [c["epoch"] for c in A.checkpoints(jid)] == [3, 2]
    before = n_jobs()
    out = R.run_all()
    assert out["relaunched"] == 1, out
    succ = successors(jid)
    assert len(succ) == 1 and n_jobs() == before + 1, succ
    nj = succ[0]
    j2 = T.job_row(nj)
    assert (j2.retry_of, j2.root_job_id, j2.attempt) == (jid, jid, 2)
    assert j2.status == "pending_approval", j2.status          # (j) SUPERVISED: an admin approves; nothing launched
    assert SIM2.creates() == [] and len(sim.creates()) == 1
    assert j2.selection["provider"] == "lambdb" and j2.spec["routing"]["exclude_providers"] == ["lambda"], j2.spec
    rf = j2.spec["resume_from"]
    assert rf["job_id"] == jid and rf["epoch"] == 3 and rf["artifact"] == "ckpt-e0003.pt", rf
    assert j2.key_id == T.job_row(jid).key_id and j2.account_id == w.account_id and j2.principal_kind == "api_key"
    assert j2.spec["config"] == T.job_row(jid).spec["config"] and j2.spec["retry"] == T.job_row(jid).spec["retry"]
    assert j2.spec["limits"]["max_total_cost_usd"] <= 2 and j2.spec["limits"]["max_price_per_gpu_hour_usd"] == 3
    assert rrow(jid)[:2] == ("relaunched", "relaunched") and rrow(jid)[3] == nj
    # events, both ways
    old_note = q("SELECT reason, evidence FROM job_events WHERE job_id = :j AND evidence->>'relaunch_event' = "
                 "'relaunched'", j=jid)
    assert len(old_note) == 1 and old_note[0][0] == f"relaunched as {nj} on lambdb resuming from epoch 3", old_note
    created = q("SELECT reason, actor FROM job_events WHERE job_id = :j AND to_status = 'created'", j=nj)[0]
    assert created[0].startswith("attempt 2 of 3, relaunched after gpu_interrupted on lambda") and created[1] == "system"
    # repeated passes: nothing more
    for _ in range(3):
        R.run_all()
        assert R.consider(jid)["decision"] == "skipped"
    assert successors(jid) == [nj] and n_jobs() == before + 1
    # the chain block, both ways
    g1, g2 = O.get(jid, w)["chain"], O.get(nj, w)["chain"]
    assert g1["attempt"] == 1 and g1["next_job_id"] == nj and g1["relaunch"]["status"] == "relaunched", g1
    assert g2["attempt"] == 2 and g2["max_attempts"] == 3 and g2["previous_job_id"] == jid, g2
    ra = g2["relaunched_after"]
    assert ra["code"] == "gpu_interrupted" and ra["provider"] == "lambda" and ra["resume_from_epoch"] == 3, ra
    assert g2["spend_so_far_usd"] is not None and g2["spend_so_far_usd"] <= 6
    lst = {r["job_id"]: r for r in O.list_for(w)}
    assert lst[nj]["chain"]["previous_job_id"] == jid and lst[jid]["chain"]["next_job_id"] == nj
    # the approval is the existing one; the successor then launches on lambdb only
    T.to_running(nj)
    assert len(SIM2.creates()) == 1 and len(sim.creates()) == 1


def test_b_runner_lost_only_once_confirmed():
    """(b) runner_lost: nothing while terminating / termination_failed / orphan_suspected / degraded; once the
    provider confirms the termination, one successor."""
    sim = world()
    w = owner()
    jid = submit(w)
    T._run_until_training(jid)
    T._ckpt(jid, 1)
    sync.sync_job(jid, now=T._later(11))
    j = T.job_row(jid)
    assert j.failure_code == "runner_lost" and T.dep_row(jid).status == "terminating", (j.status, T.dep_row(jid).status)
    assert R.run_all()["considered"] == 0 and R.consider(jid)["decision"] == "skipped" and rrow(jid) is None
    dep_id = j.deployment_id
    for dst, jst in (("termination_failed", "termination_failed"), ("orphan_suspected", "orphan_suspected"),
                     ("credentials_unavailable", "credentials_unavailable"), ("degraded", "runner_lost"),
                     ("launch_unknown", "provisioning_uncertain"), ("provider_timeout", "provider_timeout")):
        with normalize.SessionLocal.begin() as s:
            s.execute(text("UPDATE deployments SET status = :d WHERE deployment_id = :i"), {"d": dst, "i": dep_id})
            s.execute(text("UPDATE jobs SET status = :s WHERE job_id = :j"), {"s": jst, "j": jid})
        r = R.consider(jid)
        assert r["decision"] == "skipped" and "confirmed" in r["reason"], (dst, r)
        assert R.run_all()["considered"] == 0 and successors(jid) == [] and rrow(jid) is None, dst
    # even a job that LOOKS confirmed is refused while the deployment row is not provider-confirmed terminated
    with normalize.SessionLocal.begin() as s:
        s.execute(text("UPDATE jobs SET status = 'termination_confirmed' WHERE job_id = :j"), {"j": jid})
        s.execute(text("UPDATE deployments SET status = 'terminating' WHERE deployment_id = :i"), {"i": dep_id})
    assert R.consider(jid)["decision"] == "skipped" and successors(jid) == []
    with normalize.SessionLocal.begin() as s:
        s.execute(text("UPDATE jobs SET status = 'terminating' WHERE job_id = :j"), {"j": jid})
    j = T.drive_to_end(jid, 8)
    assert j.status == "reconciled" and j.failure_code == "runner_lost", (j.status, j.failure_code)
    assert R.run_all()["relaunched"] == 1 and len(successors(jid)) == 1
    nj = successors(jid)[0]
    assert T.job_row(nj).spec["resume_from"]["epoch"] == 1
    assert "lambda" in T.job_row(nj).spec["routing"]["exclude_providers"]
    assert O.get(nj, w)["chain"]["relaunched_after"]["code"] == "runner_lost"


def test_c_never_for_other_failures():
    """(c) training_failed / OOM / bootstrap timeout / cancelled -> never relaunched."""
    sim = world()
    w = owner()
    ids = []
    for code in ("training_failed", "oom"):
        jid = submit(w)
        T._run_until_training(jid)
        T.runner_events(jid, [{"seq": 900, "type": "error", "at": "2026-10-13T10:00:00Z",
                               "data": {"code": code, "message": "boom"}}])
        ids.append(jid)
    jid = submit(w)
    T.to_running(jid)
    sync.sync_job(jid, now=T._later(settings.job_bootstrap_timeout_minutes + 1))
    ids.append(jid)
    jid = submit(w)
    T._run_until_training(jid)
    O.cancel(jid, w)
    ids.append(jid)
    for jid in ids:
        T.drive_to_end(jid, 8)
    got = {T.job_row(j).failure_code or T.job_row(j).outcome for j in ids}
    assert got == {"training_failed", "out_of_memory", "bootstrap_timeout", "cancelled"}, got
    assert all(T.job_row(j).status == "reconciled" for j in ids)
    assert R.run_all()["considered"] == 0
    for jid in ids:
        assert R.consider(jid)["decision"] == "skipped" and successors(jid) == [] and rrow(jid) is None
    # a cancelled job that was ALSO interrupted is never relaunched either
    jid = ids[-1]
    with normalize.SessionLocal.begin() as s:
        s.execute(text("UPDATE jobs SET failure_code = 'gpu_interrupted' WHERE job_id = :j"), {"j": jid})
    assert R.consider(jid)["decision"] == "skipped" and successors(jid) == []
    assert O.get(ids[0], w)["chain"]["relaunch"] is None


def test_d_flag_off_or_not_opted_in():
    """(d) the global flag off, or a job without retry.max_attempts > 1 -> never."""
    sim = world()
    w = owner()
    a = submit(w)
    interrupt(a, sim)
    settings.job_auto_relaunch_enabled = False
    assert R.run_all() == {"enabled": False}
    assert R.consider(a)["decision"] == "skipped" and successors(a) == [] and rrow(a) is None
    ch = O.get(a, w)["chain"]
    assert ch["relaunch"]["status"] == "not_relaunched" and "switched off" in ch["relaunch"]["reason"], ch
    settings.job_auto_relaunch_enabled = True
    b = submit(w, retry={"max_attempts": 1})
    interrupt(b, sim)
    c = submit(w, retry=None)
    interrupt(c, sim)
    before = n_jobs()
    R.run_all()
    assert successors(b) == [] and successors(c) == [] and rrow(b) is None and rrow(c) is None
    assert "chain" not in O.get(c, w), "an ordinary job's shape is unchanged"
    assert successors(a) != [] and n_jobs() == before + 1, "only the opted-in job"


def test_e_budget_exhausted():
    """(e) spend so far + the next attempt's estimate over max_total_usd -> the chain stops, nothing submitted."""
    sim = world()
    w = owner()
    jid = submit(w, retry={"max_attempts": 3, "max_total_usd": 2.2})
    interrupt(jid, sim)
    dep_id = T.job_row(jid).deployment_id
    with normalize.SessionLocal.begin() as s:   # the provider reported a higher cost than metered: the max counts
        s.execute(text("UPDATE deployments SET provider_reported_cost = 1.1 WHERE deployment_id = :i"), {"i": dep_id})
    before = n_jobs()
    R.run_all()
    st = rrow(jid)
    assert st[0] == "stopped" and st[1] == "retry_budget_exhausted", st
    assert n_jobs() == before and successors(jid) == [] and SIM2.creates() == []
    assert scalar("SELECT count(*) FROM quotes") == 1, "no quote for a refused relaunch"
    note = q("SELECT reason FROM job_events WHERE job_id = :j AND evidence->>'relaunch_event' = 'chain_stopped'", j=jid)
    assert len(note) == 1 and note[0][0].startswith("not relaunched: retry budget exhausted"), note
    ch = O.get(jid, w)["chain"]
    assert ch["stopped"]["code"] == "retry_budget_exhausted" and ch["spend_so_far_usd"] >= 1.1, ch
    R.run_all()
    assert scalar("SELECT count(*) FROM job_events WHERE job_id = :j AND evidence->>'relaunch_event' = "
                  "'chain_stopped'", j=jid) == 1, "a stop is recorded once"
    # spend = the attempt's est_max_total_usd while it is not reconciled yet (an upper bound)
    sp = R.attempt_spend(T.job_row(jid), T.dep_row(jid))
    assert sp["usd"] >= 1.1 and sp["basis"].startswith("reconciled"), sp
    with normalize.SessionLocal.begin() as s:
        s.execute(text("UPDATE deployments SET reconciled_at = NULL WHERE deployment_id = :i"), {"i": dep_id})
    sp = R.attempt_spend(T.job_row(jid), T.dep_row(jid))
    assert sp["usd"] >= float(T.job_row(jid).est_max_total_usd) and "upper bound" in sp["basis"], sp


def _second_attempt(w, sim, retry):
    jid = submit(w, retry=retry)
    interrupt(jid, sim, ckpts=(1,))
    R.run_all()
    nj = successors(jid)[0]
    return jid, nj


def test_f_max_attempts_and_i_no_provider_left():
    """(f) attempt 2 of 2 interrupted -> stop max_attempts_reached. (i) attempt 2 of 3 interrupted, both providers
    excluded -> stop no_eligible_provider. Reliability: each deployment counted once."""
    sim = world()
    w = owner()
    jid, nj = _second_attempt(w, sim, {"max_attempts": 2, "max_total_usd": 6})
    interrupt(nj, SIM2, ckpts=(2,))
    R.run_all()
    assert rrow(nj)[:2] == ("stopped", "max_attempts_reached"), rrow(nj)
    assert successors(nj) == [] and O.get(nj, w)["chain"]["stopped"]["code"] == "max_attempts_reached"
    # reliability reads deployments: the interrupted attempt once, the successor's launch normally
    deps = reliability.load()
    ids = [d["deployment_id"] for d in deps]
    assert len(ids) == len(set(ids)) and T.job_row(jid).deployment_id in ids and T.job_row(nj).deployment_id in ids
    m = reliability.compute(deps)
    for p in ("lambda", "lambdb"):
        pm = m.get(p) or (m.get("providers") or {}).get(p)
        assert pm is not None, m.keys()
        n = pm["launches"]["n"] if isinstance(pm.get("launches"), dict) else pm.get("launches")
        assert n == 1, (p, pm.get("launches"))

    jid, nj = _second_attempt(w, sim, {"max_attempts": 3, "max_total_usd": 6})
    interrupt(nj, SIM2, ckpts=(2,))
    before = n_jobs()
    R.run_all()
    st = rrow(nj)
    assert st[:2] == ("stopped", "no_eligible_provider"), st
    assert "lambda, lambdb" in st[2], st[2]
    assert n_jobs() == before and successors(nj) == []
    # the newest checkpoint of the chain would have been used: attempt 2's (epoch 2) beats attempt 1's (epoch 1)
    resume, _ = R.pick_resume(T.job_row(nj), [T.job_row(jid), T.job_row(nj)], 5)
    assert resume["job_id"] == nj and resume["epoch"] == 2


def test_i_single_provider():
    """(i) the only provider interrupted the job: nothing eligible remains, the chain stops cleanly."""
    sim = world(second=False)
    w = owner()
    jid = submit(w)
    interrupt(jid, sim)
    before = n_jobs()
    R.run_all()
    st = rrow(jid)
    assert st[:2] == ("stopped", "no_eligible_provider") and n_jobs() == before and len(sim.creates()) == 1, st


def test_g_concurrent_passes_one_successor():
    """(g) concurrent workers / repeated passes -> one successor (claim row PK + UNIQUE(retry_of))."""
    sim = world()
    w = owner()
    jid = submit(w)
    interrupt(jid, sim)
    before = n_jobs()
    res, errs = [], []
    barrier = threading.Barrier(5)

    def go():
        try:
            barrier.wait()
            res.append(R.consider(jid))
        except Exception as exc:  # noqa: BLE001
            errs.append(exc)

    ts = [threading.Thread(target=go) for _ in range(5)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errs, errs
    assert [r["decision"] for r in res].count("relaunched") == 1, res
    assert len(successors(jid)) == 1 and n_jobs() == before + 1
    # the database refuses a second successor of the same failed attempt even on a direct submit
    chain = {"retry_of": jid, "root_job_id": jid, "attempt": 2, "max_attempts": 3}
    body = T.body(retry=RETRY, routing={"exclude_providers": ["lambda"]})
    T._refused(lambda: O.submit(body, w, chain=chain), "relaunch_exists", 409)
    assert len(successors(jid)) == 1
    # a re-claimed (stale / crashed) decision finds the existing successor instead of submitting again
    with normalize.SessionLocal.begin() as s:
        s.execute(text("UPDATE job_relaunches SET status = 'blocked' WHERE failed_job_id = :j"), {"j": jid})
    r = R.consider(jid)
    assert r["decision"] == "relaunched" and r.get("replayed") and len(successors(jid)) == 1, r


def test_h_owner_revoked_or_scope_lost():
    """(h) revoked key / lost scope / suspended account -> no relaunch, reason recorded; never another principal."""
    sim = world()
    cases = []
    for how in ("revoke", "scope", "suspend", "expire"):
        w = owner()
        jid = submit(w)
        interrupt(jid, sim)
        cases.append((how, w, jid))
    for how, w, jid in cases:
        with normalize.SessionLocal.begin() as s:
            if how == "scope":
                s.execute(text("UPDATE api_keys SET scopes = ARRAY['data:read','route:preview'] WHERE id = :k"),
                          {"k": w.key_id})
            elif how == "suspend":
                s.execute(text("UPDATE accounts SET status = 'suspended' WHERE id = :a"), {"a": w.account_id})
            elif how == "expire":
                s.execute(text("UPDATE api_keys SET expires_at = now() - interval '1 minute' WHERE id = :k"),
                          {"k": w.key_id})
        if how == "revoke":
            keys.revoke_key(w.key_id)
    before = n_jobs()
    R.run_all()
    want = {"revoke": "owner_key_revoked", "scope": "owner_scope_missing", "suspend": "account_suspended",
            "expire": "owner_key_expired"}
    for how, w, jid in cases:
        st = rrow(jid)
        assert st[:2] == ("stopped", want[how]), (how, st)
    assert n_jobs() == before and SIM2.creates() == []
    # a job whose principal cannot be re-verified (no key row) is never relaunched under another principal
    w = T.who()           # a Principal with no api_keys row behind it
    jid = submit(w)
    interrupt(jid, sim)
    R.run_all()
    assert rrow(jid)[:2] == ("stopped", "owner_unavailable") and successors(jid) == []
    # the operator's own job relaunches as the operator (its original principal), never a customer's
    jid = submit(OPERATOR)
    interrupt(jid, sim)
    R.run_all()
    nj = successors(jid)[0]
    assert T.job_row(nj).principal_kind == "operator" and T.job_row(nj).account_id is None


def test_internal_error_blocked_and_alerted():
    """An internal error while a relaunch is due: blocked (retried next pass), an ops alert, nothing submitted."""
    from alerts import ops
    sim = world()
    w = owner()
    jid = submit(w)
    interrupt(jid, sim)
    sent = []
    real_alert, real_select = ops.alert, None
    import workloads.select as WS
    real_select = WS.select
    ops.alert = lambda kind, title, **kw: sent.append((kind, title, kw)) or {"recorded": True}
    WS.select = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("market table unreadable"))
    try:
        r = R.consider(jid)
    finally:
        WS.select = real_select
        ops.alert = real_alert
    assert r["decision"] == "blocked" and rrow(jid)[0] == "blocked", r
    assert sent and sent[0][0] == "relaunch_blocked" and sent[0][2]["detail"]["job_id"] == jid, sent
    assert successors(jid) == []
    assert R.consider(jid)["decision"] == "relaunched" and len(successors(jid)) == 1, "retried on the next pass"


def test_feed_posts_relaunch_and_stop():
    """alerts/channels.py: the jobs feed posts relaunches and chain stops (they are notes, not transitions)."""
    from alerts import channels
    sim = world()
    w = owner()
    jid = submit(w)
    interrupt(jid, sim)
    from datetime import datetime, timedelta, timezone
    since = datetime.now(timezone.utc) - timedelta(minutes=1)
    R.run_all()
    sql, build = channels._QUERIES["jobs"]
    with normalize.SessionLocal() as s:
        rows = [dict(r._mapping) for r in s.execute(text(sql), {"since": since, "n": 200})]
    ev = [r for r in rows if r.get("relaunch_event")]
    kinds = sorted(r["relaunch_event"] for r in ev)
    assert kinds == ["relaunched", "relaunched_from"], kinds
    titles = [build(r)["embeds"][0]["title"] for r in ev]
    assert any("relaunched" in t.lower() for t in titles), titles


TESTS = [test_validation_and_preview, test_a_interrupted_relaunch_once, test_b_runner_lost_only_once_confirmed,
         test_c_never_for_other_failures, test_d_flag_off_or_not_opted_in, test_e_budget_exhausted,
         test_f_max_attempts_and_i_no_provider_left, test_i_single_provider, test_g_concurrent_passes_one_successor,
         test_h_owner_revoked_or_scope_lost, test_internal_error_blocked_and_alerted, test_feed_posts_relaunch_and_stop]


def main(only=None) -> int:
    failed = 0
    try:
        for t in TESTS:
            if only and t.__name__ not in only:
                continue
            try:
                t()
                print(f"PASS {t.__name__}")
            except Exception:  # noqa: BLE001
                failed += 1
                print(f"FAIL {t.__name__}")
                traceback.print_exc()
    finally:
        settings.job_auto_relaunch_enabled = False
        adapters.unregister("lambdb")
        adapters.TRANSPORTS.pop("lambdb", None)
        if T.S_ is not None:
            bf.drop(T.S_, T.DB)
    print(f"{len(TESTS) - failed if not only else '-'} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or None))
