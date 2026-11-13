"""Resume, OOM escalation, Move, provider-initiated stops and the move suggestion (jobs/continuation.py, jobs/sync.py
_provider_stop; methodology/jobs.md).

Providers: the REAL Lambda adapter against fake Lambda APIs (tests/test_jobs.JobSim): `lambda` and the test-only
`lambdb` (tests/test_job_relaunch.LambdbAdapter). Every successor goes through jobs.orchestrator.submit -> select ->
quote -> engine.route like any job (SUPERVISED: it waits in pending_approval; nothing is launched unless approved).
Scratch DB og_test_job_resume_move. No network, no real provider.
Run:  .venv/bin/python tests/test_job_resume_move.py [test_name ...]
"""

from __future__ import annotations

import sys
import threading
import traceback
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_job_relaunch as RL  # noqa: E402  (imports test_jobs -> scratchdb first: credentials blanked, no network)
import test_jobs as T  # noqa: E402

T.DB = "og_test_job_resume_move"

import bench_fixtures as bf  # noqa: E402
from bench_fixtures import q, scalar  # noqa: E402

import normalize  # noqa: E402
from accounts import keys  # noqa: E402
from accounts.auth import OPERATOR, Principal  # noqa: E402
from config import settings  # noqa: E402
from routing import adapters, deployments, tracker  # noqa: E402
from routing.adapters.results import InstanceState  # noqa: E402
from sqlalchemy import text  # noqa: E402

from jobs import batches as B  # noqa: E402
from jobs import continuation as C  # noqa: E402
from jobs import orchestrator as O  # noqa: E402
from jobs import relaunch as R  # noqa: E402
from jobs import sync  # noqa: E402

G = T.G
H200 = "NVIDIA H200 141GB SXM5"
B200 = "NVIDIA B200 180GB SXM"          # listed, but the pinned PyTorch does not run on it: an incompatible tier
H100NVL = "NVIDIA H100 94GB NVL"         # the next tier above 80 GB, same family (H100)
A100 = "NVIDIA A100 80GB SXM4"


def world(**kw):
    for p in ("lambdh", "lambdx"):
        adapters.unregister(p)
        adapters.TRANSPORTS.pop(p, None)
    sim = RL.world(**kw)
    bf.market([A100, H100NVL, H200, B200], [])      # only the OOM test lists larger GPUs
    with normalize.SessionLocal.begin() as s:
        for t in ("job_moves", "job_runner_instructions", "batches", "batch_shards", "batch_attempts", "batch_events"):
            s.execute(text(f"DELETE FROM {t}"))
    settings.job_auto_relaunch_enabled = False
    settings.job_move_enabled = True
    settings.job_provider_stop_confirm_seconds = 60
    settings.job_move_checkpoint_timeout_minutes = 30
    settings.job_move_suggest_min_savings_pct = 20.0
    settings.job_move_overhead_minutes = 10.0
    C._SUGGEST_CACHE.clear()
    return sim


def refused(fn, code, status=None):
    try:
        fn()
    except (C.Refused, O.JobRefused, B.BatchRefused) as e:
        assert e.code == code, (e.code, e.message)
        assert status is None or e.status == status, (e.status, status, e.message)
        return e
    raise AssertionError(f"not refused ({code})")


def successors(jid):
    return RL.successors(jid)


def n_jobs():
    return scalar("SELECT count(*) FROM jobs")


def submit(w, **kw):
    kw.setdefault("retry", None)
    kw.setdefault("config", {"epochs": 5})
    code, out = O.submit(T.body(**kw), w)
    assert code == 202, (code, out)
    return out["job_id"]


def _read(dep_id, state, provider_status, at):
    return deployments.observe(dep_id, InstanceState(state=state, provider_status=provider_status,
                                                     instance_id=deployments.load_row(dep_id).provider_instance_id),
                               checked_at=at)


def _ev_kinds(jid):
    return [r[0] for r in q("SELECT evidence->>'relaunch_event' FROM job_events WHERE job_id = :j AND "
                            "evidence->>'relaunch_event' IS NOT NULL ORDER BY id", j=jid)]


# --------------------------------------------------------------------------
# 1. Provider-initiated stop = interruption (only on unambiguous evidence); a user stop never relaunches
# --------------------------------------------------------------------------

def test_provider_stop_classification():
    sim = world()
    settings.job_auto_relaunch_enabled = True
    w = RL.owner()
    jid = submit(w, retry=RL.RETRY)
    T._run_until_training(jid)
    T._ckpt(jid, 1)
    T._ckpt(jid, 2)
    dep_id = T.job_row(jid).deployment_id
    t0 = T._later(1)
    _read(dep_id, "stopped", "EXITED", t0)        # the provider says stopped; OpenGrid never asked
    d = deployments.load_row(dep_id)
    assert d.status == "stopped" and d.interruptions == 1, (d.status, d.interruptions)
    sync.sync_job(jid, now=t0 + timedelta(seconds=20))
    assert T.job_row(jid).status == "training" and not sim.terminates(), "one read is not proof: nothing yet"
    _read(dep_id, "stopped", "EXITED", t0 + timedelta(seconds=30))
    sync.sync_job(jid, now=t0 + timedelta(seconds=40))
    assert T.job_row(jid).status == "training", "confirmed only after job_provider_stop_confirm_seconds"
    _read(dep_id, "stopped", "EXITED", t0 + timedelta(seconds=75))
    sync.sync_job(jid, now=t0 + timedelta(seconds=80))
    j = T.job_row(jid)
    assert j.failure_code == "gpu_interrupted" and j.outcome == "failed", (j.status, j.failure_code)
    assert len(sim.terminates()) == 1, "the stopped machine is terminated through the existing path"
    ev = q("SELECT evidence FROM job_events WHERE job_id = :j AND to_status = 'runner_lost' AND "
           "from_status <> to_status", j=jid)[0][0]
    ps = ev["provider_stop"]
    assert ps["provider_status"] == "EXITED" and ps["confirm_seconds"] >= 60 and ps["from"] == "running", ps
    j = T.drive_to_end(jid, 8)
    assert j.status == "reconciled" and j.failure_code == "gpu_interrupted", (j.status, j.failure_code)
    assert R.run_all()["relaunched"] == 1, "the existing relaunch handles it"
    nj = successors(jid)[0]
    assert T.job_row(nj).selection["provider"] == "lambdb" and T.job_row(nj).succession_kind == "relaunch"
    assert T.job_row(nj).spec["resume_from"]["epoch"] == 2

    # ambiguous: the runner kept reporting after the provider said stopped -> current behaviour (nothing)
    jid = submit(w, retry=RL.RETRY)
    last = T._run_until_training(jid)
    dep_id = T.job_row(jid).deployment_id
    t0 = T._later(-1)                              # the stop read is older than the runner's next report
    _read(dep_id, "stopped", "EXITED", t0)
    T.runner_events(jid, [{"seq": last + 1, "type": "log", "at": None, "data": {"line": "still training"}}])
    _read(dep_id, "stopped", "EXITED", t0 + timedelta(seconds=120))
    sync.sync_job(jid, now=t0 + timedelta(seconds=130))
    assert T.job_row(jid).status == "training" and T.job_row(jid).failure_code is None
    # a manual transition (no provider read) is not provider evidence either
    jid2 = submit(w, retry=RL.RETRY)
    T._run_until_training(jid2)
    dep2 = T.job_row(jid2).deployment_id
    deployments.transition(dep2, "stopped", "stopped", actor="system")
    sync.sync_job(jid2, now=T._later(5))
    assert T.job_row(jid2).status == "training", "no provider status read: keep the silence rule"

    # a user / operator stop never relaunches: requested_action stop -> no interruption, ever
    jid = submit(w, retry=RL.RETRY)
    T._run_until_training(jid)
    T._ckpt(jid, 1)
    dep_id = T.job_row(jid).deployment_id
    with normalize.SessionLocal.begin() as s:
        s.execute(text("UPDATE deployments SET provider_metadata = coalesce(provider_metadata, '{}'::jsonb) || "
                       "'{\"requested_action\": \"stop\"}'::jsonb WHERE deployment_id = :d"), {"d": dep_id})
    t0 = T._later(1)
    _read(dep_id, "stopped", "EXITED", t0)
    _read(dep_id, "stopped", "EXITED", t0 + timedelta(seconds=300))
    sync.sync_job(jid, now=t0 + timedelta(seconds=310))
    j = T.job_row(jid)
    assert j.status == "training" and j.failure_code is None and deployments.load_row(dep_id).interruptions == 0
    deployments.terminate(dep_id, w)                # the user ends it
    j = T.drive_to_end(jid, 8)
    assert j.failure_code == "terminated_before_completion", j.failure_code
    R.run_all()
    assert R.consider(jid)["decision"] == "skipped" and successors(jid) == []
    # the switch off: only the silence rule applies
    settings.job_provider_stop_confirm_seconds = 0
    jid = submit(w, retry=RL.RETRY)
    T._run_until_training(jid)
    dep_id = T.job_row(jid).deployment_id
    t0 = T._later(1)
    _read(dep_id, "stopped", "EXITED", t0)
    _read(dep_id, "stopped", "EXITED", t0 + timedelta(seconds=120))
    sync.sync_job(jid, now=t0 + timedelta(seconds=130))
    assert T.job_row(jid).status == "training"


# --------------------------------------------------------------------------
# 2. Resume
# --------------------------------------------------------------------------

def test_resume_happy_path_and_refusals():
    sim = world()
    w = RL.owner()
    jid = submit(w)
    # running: not resumable
    T._run_until_training(jid)
    refused(lambda: C.resume(jid, w, {"max_total_cost_usd": 2}), "not_resumable", 409)
    T._ckpt(jid, 1)
    T._ckpt(jid, 2)
    # cancelled, the GPU not provider-confirmed gone yet -> refused, nothing submitted
    O.cancel(jid, w)
    assert T.dep_row(jid).status == "terminating"
    before = n_jobs()
    e = refused(lambda: C.resume(jid, w, {"max_total_cost_usd": 2}), "machine_may_exist", 409)
    assert "provider-confirmed" in e.message and n_jobs() == before
    assert O.get(jid, w)["actions"]["resume"]["code"] == "machine_may_exist"
    j = T.drive_to_end(jid, 8)
    assert j.status == "reconciled" and j.outcome == "cancelled"
    # not the owner -> 404 (no oracle); invalid bodies
    other = RL.owner()
    refused(lambda: C.resume(jid, other, {"max_total_cost_usd": 2}), "job_not_found", 404)
    refused(lambda: C.resume_preview(jid, other, {"max_total_cost_usd": 2}), "job_not_found", 404)
    refused(lambda: C.resume(jid, w, {}), "invalid_max_total_cost_usd", 422)
    refused(lambda: C.resume(jid, w, {"max_total_cost_usd": 2, "provider": "../x"}), "invalid_provider", 422)
    refused(lambda: C.resume(jid, w, {"max_total_cost_usd": 2, "x": 1}), "unknown_field", 422)
    # the preview: quote + checkpoint, no write
    nq = scalar("SELECT count(*) FROM quotes")
    pv = C.resume_preview(jid, w, {"provider": "same", "max_total_cost_usd": 2})
    assert pv["can_submit"] and pv["quote"]["provider"] == "lambda" and pv["checkpoint"]["epoch"] == 2, pv
    assert pv["checkpoint"]["age_seconds"] is not None and pv["checkpoint"]["verified"]
    assert pv["quote"]["est_max_total_usd"] <= 2 and scalar("SELECT count(*) FROM quotes") == nq and n_jobs() == before
    assert O.get(jid, w)["actions"]["resume"]["available"]
    # resume on the same provider, from the latest verified checkpoint
    code, out = C.resume(jid, w, {"provider": "same", "max_total_cost_usd": 2})
    nj = out["job_id"]
    assert code == 202 and out["status"] == "pending_approval", (code, out["status"])
    j2 = T.job_row(nj)
    assert (j2.retry_of, j2.root_job_id, j2.attempt, j2.succession_kind) == (jid, jid, 2, "resume")
    assert j2.spec["resume_from"]["epoch"] == 2 and j2.spec["resume_from"]["job_id"] == jid
    assert j2.spec["routing"]["include_providers"] == ["lambda"] and j2.selection["provider"] == "lambda"
    assert j2.spec["limits"]["max_total_cost_usd"] == 2 and j2.key_id == w.key_id
    assert len(sim.creates()) == 1, "SUPERVISED: nothing launched before the approval"
    assert "resumed" in _ev_kinds(jid) and "resumed_from" in _ev_kinds(nj)
    g = O.get(nj, w)["chain"]
    assert g["kind"] == "resume" and g["relaunched_after"]["kind"] == "resume" and g["previous_job_id"] == jid, g
    assert O.get(jid, w)["chain"]["next_job_id"] == nj
    # one successor per attempt: a second resume (or a relaunch) is refused
    e = refused(lambda: C.resume(jid, w, {"max_total_cost_usd": 2}), "already_continued", 409)
    assert e.extra["successor_job_id"] == nj
    chain = {"retry_of": jid, "root_job_id": jid, "attempt": 2, "max_attempts": 3}
    T._refused(lambda: O.submit(T.body(), w, chain=chain), "relaunch_exists", 409)

    # interrupted, no checkpoint: refused unless from_scratch; best provider
    jid = submit(w)
    RL.interrupt(jid, sim, ckpts=())
    refused(lambda: C.resume(jid, w, {"max_total_cost_usd": 2}), "no_verified_checkpoint", 409)
    code, out = C.resume(jid, w, {"max_total_cost_usd": 2, "from_scratch": True, "provider": "lambdb"})
    j3 = T.job_row(out["job_id"])
    assert "resume_from" not in j3.spec and j3.selection["provider"] == "lambdb" and j3.spec["routing"][
        "include_providers"] == ["lambdb"]
    # a corrupted checkpoint never counts as verified
    jid = submit(w)
    RL.interrupt(jid, sim, ckpts=(1,))
    with normalize.SessionLocal.begin() as s:
        s.execute(text("UPDATE job_artifacts SET bytes = :b WHERE job_id = :j"), {"b": b"tampered", "j": jid})
    refused(lambda: C.resume(jid, w, {"max_total_cost_usd": 2}), "no_verified_checkpoint", 409)
    # never for a training failure / OOM / a sandbox / a success
    jid = submit(w)
    T._run_until_training(jid)
    sync.complete(jid, {"outcome": "failed", "summary": {"reason": "training_failed", "exit_code": 2}})
    T.drive_to_end(jid, 8)
    refused(lambda: C.resume(jid, w, {"max_total_cost_usd": 2}), "not_resumable", 409)
    code, sb = O.submit(T.body(sandbox=True), w)
    refused(lambda: C.resume(sb["job_id"], w, {"max_total_cost_usd": 2}), "resume_not_real", 409)
    assert "actions" not in O.get(sb["job_id"], w), "a sandbox job's shape is unchanged"


def test_resume_http_scope_and_idempotency():
    import main  # noqa: F401
    from fastapi.testclient import TestClient

    from accounts import accounts as acc
    from accounts import ratelimit
    sim = world()
    settings.app_password = None
    settings.public_pages = False
    settings.environment = "development"
    keys.reset_cache()
    ratelimit.reset()
    acc.reset_cache()
    client = TestClient(main.app)
    aid = bf.fresh_account()
    k = keys.create_key(aid, "resume-api", RL.SCOPES, rate_limit_per_minute=1000)
    h = {"Authorization": f"Bearer {k['secret']}", "X-OpenGrid-Request": "1"}
    w = Principal(kind="api_key", account_id=aid, key_id=k["id"], scopes=frozenset(RL.SCOPES))
    jid = submit(w)
    RL.interrupt(jid, sim, ckpts=(1,))
    # scope: route:execute for the action, route:preview for the preview
    ro = keys.create_key(aid, "resume-ro", ["deployments:read", "route:preview"], rate_limit_per_minute=1000)
    hro = {"Authorization": f"Bearer {ro['secret']}", "X-OpenGrid-Request": "1", "Idempotency-Key": "k-ro-1"}
    r = client.post(f"/v1/jobs/{jid}/resume", headers=hro, json={"max_total_cost_usd": 2})
    assert r.status_code == 403, r.text
    r = client.post(f"/v1/jobs/{jid}/resume/preview", headers=hro, json={"max_total_cost_usd": 2})
    assert r.status_code == 200 and r.json()["data"]["quote"]["provider"], r.text
    # another account: 404 before any key is claimed
    oid = bf.fresh_account()
    ok = keys.create_key(oid, "other", RL.SCOPES, rate_limit_per_minute=1000)
    r = client.post(f"/v1/jobs/{jid}/resume", json={"max_total_cost_usd": 2},
                    headers={"Authorization": f"Bearer {ok['secret']}", "X-OpenGrid-Request": "1",
                             "Idempotency-Key": "k-other-1"})
    assert r.status_code == 404, r.text
    # Idempotency-Key required; same key + body replays the same successor
    r = client.post(f"/v1/jobs/{jid}/resume", headers=h, json={"max_total_cost_usd": 2})
    assert r.status_code in (400, 422, 428), r.text
    hk = {**h, "Idempotency-Key": "resume-key-0001"}
    r1 = client.post(f"/v1/jobs/{jid}/resume", headers=hk, json={"max_total_cost_usd": 2})
    assert r1.status_code == 202, r1.text
    r2 = client.post(f"/v1/jobs/{jid}/resume", headers=hk, json={"max_total_cost_usd": 2})
    assert r2.status_code == 202 and r2.headers.get("Idempotent-Replayed") == "true"
    assert r1.json()["data"]["job_id"] == r2.json()["data"]["job_id"] and len(successors(jid)) == 1
    r3 = client.post(f"/v1/jobs/{jid}/resume", headers={**h, "Idempotency-Key": "resume-key-0002"},
                     json={"max_total_cost_usd": 2})
    assert r3.status_code == 409 and r3.json()["detail"]["code"] == "already_continued", r3.text
    # the job read carries the actions block
    g = client.get(f"/v1/jobs/{jid}", headers=h).json()["data"]
    assert g["actions"]["resume"]["code"] == "already_continued" and g["chain"]["next_job_id"] == r1.json()["data"][
        "job_id"]
    # the runner channel: instructions on GET runner/instructions (job token only)
    r = client.get(f"/v1/jobs/{jid}/runner/instructions", headers=h)
    assert r.status_code == 401, r.text


# --------------------------------------------------------------------------
# 3. OOM escalation
# --------------------------------------------------------------------------

class LambdhAdapter(RL.LambdaAdapter):
    """Test-only providers selling one larger GPU each (their own fake API priced like their listing)."""
    CREDENTIAL_PROVIDER = "lambda"


class LambdxAdapter(RL.LambdaAdapter):
    CREDENTIAL_PROVIDER = "lambda"


SIMS_X: dict = {}


def _oom_market(nvl_price=3.0, h200_price=3.5):
    rows = []
    for p, cls, price in (("lambdh", LambdhAdapter, nvl_price), ("lambdx", LambdxAdapter, h200_price)):
        adapters.register(p, cls)
        SIMS_X[p] = T.JobSim()
        SIMS_X[p].price_cents = int(round(price * 100))
        adapters.TRANSPORTS[p] = SIMS_X[p].transport()
        bf.flags(p, validated=True, live=False, supervised=True)
    for p, price, gpu, lid in (("lambda", T.PRICE, G, "lambda:" + T.SKU), ("lambdb", 2.59, G, "lambdb:" + T.SKU),
                               ("lambda", 1.0, A100, "lambda:a100"), ("lambdh", nvl_price, H100NVL, "lambdh:nvl"),
                               ("lambdx", h200_price, H200, "lambdx:h200"), ("lambdx", 3.0, B200, "lambdx:b200")):
        r = bf.listing(p, price, gpu=gpu, region="us-east-1", lid=lid)
        r.sku = T.SKU
        rows.append(r)
    bf.market([G, A100, H100NVL, H200, B200], rows)


def _oom(jid, *, ckpts=(1,)):
    last = T._run_until_training(jid)
    for e in ckpts:
        T._ckpt(jid, e)
    T.runner_events(jid, [{"seq": last + 1, "type": "error", "at": None, "data": {"code": "oom",
                                                                                 "message": "CUDA out of memory"}}])
    j = T.drive_to_end(jid, 8)
    assert j.status == "reconciled" and j.failure_code == "out_of_memory", (j.status, j.failure_code)


OOM = {"max_attempts": 1, "max_total_usd": 8, "on_oom": "escalate"}
LIM = {"max_price_per_gpu_hour_usd": 6, "max_total_cost_usd": 2.5, "max_runtime_minutes": 30}


def test_oom_escalation():
    sim = world()
    _oom_market()
    settings.job_auto_relaunch_enabled = True
    w = RL.owner()
    from workloads.spec import validate
    for bad, code in (({"max_attempts": 1, "on_oom": "escalate"}, "retry_budget_required"),
                      ({"max_attempts": 1, "max_total_usd": 8, "on_oom": "escalate", "max_oom_escalations": 3},
                       "invalid_retry"),
                      ({"max_attempts": 1, "max_total_usd": 8, "max_oom_escalations": 1}, "invalid_retry"),
                      ({"max_attempts": 1, "max_total_usd": 8, "on_oom": "sometimes"}, "invalid_request")):
        try:
            validate(T.body(retry=bad, limits=LIM))
            raise AssertionError(f"accepted {bad}")
        except Exception as e:  # noqa: BLE001
            assert getattr(e, "code", None) in (code, "invalid_request"), (bad, getattr(e, "code", e))
    assert validate(T.body(retry=RL.RETRY)).request["retry"] == {"max_attempts": 3, "max_total_usd": 6.0}, \
        "an ordinary retry block keeps its old shape"
    jid = submit(w, retry=OOM, limits=LIM, resources={"gpu": G})
    assert T.job_row(jid).selection["gpu"] == G
    _oom(jid, ckpts=(1, 2))
    before = n_jobs()
    out = R.run_all()
    assert out.get("oom", {}).get("escalated") == 1, (out, q("SELECT reason, detail FROM job_relaunches"))
    nj = successors(jid)
    assert len(nj) == 1 and n_jobs() == before + 1, nj
    nj = nj[0]
    j2 = T.job_row(nj)
    assert j2.succession_kind == "oom_escalation" and j2.retry_of == jid and j2.attempt == 2
    assert j2.selection["gpu"] == H100NVL, ("the next VRAM tier up (94 GB, same family), not H200 / not the cheaper "
                                            "80 GB A100", j2.selection["gpu"])
    assert j2.spec["resume_from"]["epoch"] == 2 and j2.status == "pending_approval"
    row = q("SELECT status, reason_code, kind, reason FROM job_relaunches WHERE failed_job_id = :j", j=jid)[0]
    assert tuple(row[:3]) == ("relaunched", "oom_escalated", "oom_escalation") and "94" in row[3], row
    ev = q("SELECT evidence FROM job_events WHERE job_id = :j AND evidence->>'relaunch_event' = 'oom_escalated'",
           j=jid)[0][0]
    assert ev["from_gpu"] == G and ev["to_gpu"] == H100NVL and ev["from_vram_gb"] == 80 and ev["to_vram_gb"] == 94, ev
    for _ in range(2):
        R.run_all()
    assert successors(jid) == [nj], "exactly once"
    # only once per chain: the escalated attempt OOMs too -> stopped, nothing submitted
    _oom(nj, ckpts=())
    R.run_all()
    row = q("SELECT status, reason_code FROM job_relaunches WHERE failed_job_id = :j", j=nj)[0]
    assert tuple(row) == ("stopped", "oom_escalations_exhausted"), row
    assert successors(nj) == []

    # budget: the next tier's ceiling over retry.max_total_usd -> stopped, nothing quoted
    jid = submit(w, retry={**OOM, "max_total_usd": 2.6}, limits=LIM, resources={"gpu": G})
    _oom(jid)
    with normalize.SessionLocal.begin() as s:      # the provider reported more than metered: the max counts
        s.execute(text("UPDATE deployments SET provider_reported_cost = 1.5 WHERE deployment_id = :d"),
                  {"d": T.job_row(jid).deployment_id})
    nq = scalar("SELECT count(*) FROM quotes")
    R.run_all()
    row = q("SELECT status, reason_code FROM job_relaunches WHERE failed_job_id = :j", j=jid)[0]
    assert tuple(row) == ("stopped", "retry_budget_exhausted"), (row, q("SELECT detail->>'tiers_tried' FROM job_relaunches WHERE "
                                                                        "failed_job_id = :j", j=jid))
    assert successors(jid) == [] and scalar("SELECT count(*) FROM quotes") == nq

    # never for another failure, never without opting in, never with the flag off
    jid = submit(w, retry=OOM, limits=LIM, resources={"gpu": G})
    T._run_until_training(jid)
    sync.complete(jid, {"outcome": "failed", "summary": {"reason": "training_failed", "exit_code": 2}})
    T.drive_to_end(jid, 8)
    plain = submit(w, retry={"max_attempts": 1, "max_total_usd": 8}, limits=LIM, resources={"gpu": G})
    _oom(plain)
    R.run_all()
    assert successors(jid) == [] and successors(plain) == []
    assert C.consider_oom(jid)["decision"] == "skipped" and C.consider_oom(plain)["decision"] == "skipped"
    off = submit(w, retry=OOM, limits=LIM, resources={"gpu": G})
    _oom(off)
    settings.job_auto_relaunch_enabled = False
    assert C.consider_oom(off)["decision"] == "skipped" and successors(off) == []
    settings.job_auto_relaunch_enabled = True
    # two escalations when the policy allows it (cap 2): 80 -> 94 -> 141 GB (B200 is listed but incompatible)
    jid = submit(w, retry={**OOM, "max_oom_escalations": 2, "max_total_usd": 12}, limits=LIM, resources={"gpu": G})
    _oom(jid)
    R.run_all()
    n1 = successors(jid)[0]
    _oom(n1)
    R.run_all()
    n2 = successors(n1)
    assert len(n2) == 1 and T.job_row(n2[0]).selection["gpu"] == H200, (n2, q(
        "SELECT status, reason_code, reason, detail->>'tiers_tried' FROM job_relaunches WHERE failed_job_id = :j", j=n1))
    # the larger-tier order itself
    tiers = [t["gpu"] for t in C.larger_tiers(G)]
    assert tiers[:3] == [H100NVL, H200, B200] and A100 not in tiers and G not in tiers, tiers


# --------------------------------------------------------------------------
# 4. Move
# --------------------------------------------------------------------------

def _running(w, sim, *, epochs=5):
    jid = submit(w, config={"epochs": epochs})
    T._run_until_training(jid)          # metrics for epochs 1 and 2
    T._ckpt(jid, 1)                     # an OLD checkpoint (before any move request): never counts
    return jid


def test_move_order_and_abort():
    sim = world()
    w = RL.owner()
    jid = _running(w, sim)
    settings.job_move_enabled = False
    refused(lambda: C.request_move(jid, w, {"max_total_cost_usd": 2, "reason": "cheaper"}), "move_disabled", 409)
    assert C.run_moves() == {"enabled": False}
    settings.job_move_enabled = True
    refused(lambda: C.request_move(jid, w, {"max_total_cost_usd": 2}), "reason_required", 422)
    refused(lambda: C.request_move(jid, w, {"max_total_cost_usd": 2, "reason": "x", "provider": "lambda"}),
            "same_provider", 422)
    refused(lambda: C.request_move(jid, RL.owner(), {"max_total_cost_usd": 2, "reason": "x"}), "job_not_found", 404)
    pv = C.move_preview(jid, w, {"max_total_cost_usd": 2, "reason": "cheaper"})
    assert pv["can_submit"] and pv["quote"]["provider"] == "lambdb" and pv["latest_checkpoint"]["epoch"] == 1, pv
    before = n_jobs()
    code, out = C.request_move(jid, w, {"max_total_cost_usd": 2, "reason": "cheaper elsewhere"})
    assert code == 202 and out["move_request"]["status"] == "checkpointing", out.get("move_request")
    mv = C.open_move(jid)
    ins = C.pending_instructions(jid)
    assert [i["kind"] for i in ins] == ["checkpoint_now"] and ins[0]["ref"] == mv.move_id, ins
    # the runner channel: the events response carries the instruction
    assert out["actions"]["move"]["latest"]["status"] == "checkpointing"
    # no new checkpoint yet: waiting, nothing terminated
    r = C.advance_move(mv.move_id)
    assert not r["acted"] and r.get("waiting") == "checkpoint" and not sim.terminates()
    # a second move while one is open: refused
    refused(lambda: C.request_move(jid, w, {"max_total_cost_usd": 2, "reason": "again"}), "move_in_progress", 409)
    # the runner saves + uploads a NEW checkpoint after the request -> verified -> terminate (and only then)
    T._ckpt(jid, 3)
    r = C.advance_move(mv.move_id)
    assert r["acted"] and r["status"] == "terminating", r
    j = T.job_row(jid)
    assert j.status == "terminating" and j.failure_code == "moved" and j.outcome == "cancelled", (j.status, j.failure_code)
    assert len(sim.terminates()) == 1 and n_jobs() == before, "terminated, no successor before the provider confirms"
    assert C.pending_instructions(jid) == [], "the instruction is closed"
    r = C.advance_move(mv.move_id)
    assert r.get("waiting") == "provider_confirmation" and n_jobs() == before and len(sim.terminates()) == 1
    # the provider confirms -> the successor, resuming from that checkpoint, on another provider
    T.drive_to_end(jid, 8)
    r = C.advance_move(mv.move_id)
    assert r["status"] == "moved", r
    nj = successors(jid)
    assert len(nj) == 1 and n_jobs() == before + 1
    j2 = T.job_row(nj[0])
    assert j2.succession_kind == "move" and j2.spec["resume_from"]["epoch"] == 3 and j2.selection["provider"] == "lambdb"
    assert "lambda" in j2.spec["routing"]["exclude_providers"] and j2.spec["limits"]["max_total_cost_usd"] == 2
    kinds = _ev_kinds(jid)
    order = ["move_requested", "move_checkpoint_verified", "move_terminated", "moved"]
    assert [k for k in kinds if k in order] == order, kinds
    ids = {r[1]: r[0] for r in q("SELECT id, coalesce(evidence->>'relaunch_event', to_status) FROM job_events WHERE "
                                 "job_id = :j ORDER BY id", j=jid)}
    assert ids["move_checkpoint_verified"] < ids["terminating"] < ids["termination_confirmed"] < ids["moved"], ids
    assert C.advance_move(mv.move_id)["acted"] is False and len(successors(jid)) == 1
    mv = C.latest_move(jid)
    assert mv.status == "moved" and mv.successor_job_id == nj[0] and mv.checkpoint["epoch"] == 3
    assert R.consider(jid)["decision"] == "skipped", "a moved job is never auto-relaunched"
    refused(lambda: C.resume(jid, w, {"max_total_cost_usd": 2}), "already_continued", 409)

    # abort on checkpoint timeout: the job keeps running, nothing terminated, instruction cancelled
    sim = world()
    jid = _running(w, sim)
    C.request_move(jid, w, {"max_total_cost_usd": 2, "reason": "cheaper"})
    mv = C.open_move(jid)
    r = C.advance_move(mv.move_id, now=mv.checkpoint_deadline_at + timedelta(seconds=1))
    assert r["status"] == "aborted", r
    j = T.job_row(jid)
    assert j.status == "training" and j.failure_code is None and not sim.terminates() and successors(jid) == []
    assert C.latest_move(jid).outcome_code == "checkpoint_timeout" and C.pending_instructions(jid) == []
    assert "move_aborted" in _ev_kinds(jid)
    T._ckpt(jid, 3)                      # a late checkpoint after the abort changes nothing
    C.run_moves()
    assert T.job_row(jid).status == "training" and not sim.terminates()
    # the job ended on its own while the move waited: aborted, nothing else
    C.request_move(jid, w, {"max_total_cost_usd": 2, "reason": "again"})
    mv = C.open_move(jid)
    O.cancel(jid, w)
    C.advance_move(mv.move_id)
    assert C.latest_move(jid).outcome_code == "job_cancelled" and successors(jid) == []
    # no eligible target when the checkpoint arrives: aborted BEFORE anything is terminated
    jid = _running(w, sim)
    C.request_move(jid, w, {"max_total_cost_usd": 2, "reason": "x"})
    mv = C.open_move(jid)
    n_term = len(sim.terminates())
    T._ckpt(jid, 3)
    adapters.unregister("lambdb")
    try:
        r = C.advance_move(mv.move_id)
    finally:
        adapters.register("lambdb", RL.LambdbAdapter)
    assert r["status"] == "aborted" and T.job_row(jid).status == "training" and len(sim.terminates()) == n_term, r
    # last epoch: nothing to move
    jid = _running(w, sim, epochs=3)
    refused(lambda: C.request_move(jid, w, {"max_total_cost_usd": 2, "reason": "x"}), "nothing_to_move", 409)


def test_move_no_double_move_concurrency():
    sim = world()
    w = RL.owner()
    jid = _running(w, sim)
    res, errs = [], []
    barrier = threading.Barrier(6)

    def go():
        try:
            barrier.wait()
            res.append(C.request_move(jid, w, {"max_total_cost_usd": 2, "reason": "race"})[0])
        except C.Refused as e:
            res.append(e.code)
        except Exception as e:  # noqa: BLE001
            errs.append(e)

    ts = [threading.Thread(target=go) for _ in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errs, errs
    assert res.count(202) == 1 and res.count("move_in_progress") == 5, res
    assert scalar("SELECT count(*) FROM job_moves WHERE job_id = :j", j=jid) == 1
    assert scalar("SELECT count(*) FROM job_runner_instructions WHERE job_id = :j", j=jid) == 1
    mv = C.open_move(jid)
    T._ckpt(jid, 3)
    out, errs = [], []
    barrier = threading.Barrier(5)

    def step():
        try:
            barrier.wait()
            out.append(C.advance_move(mv.move_id))
        except Exception as e:  # noqa: BLE001
            errs.append(e)

    ts = [threading.Thread(target=step) for _ in range(5)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errs, errs
    assert len(sim.terminates()) == 1, "one terminate, whoever wins the step"
    T.drive_to_end(jid, 8)
    out, errs = [], []
    barrier = threading.Barrier(5)
    ts = [threading.Thread(target=step) for _ in range(5)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errs, errs
    assert len(successors(jid)) == 1, "one successor"
    assert C.latest_move(jid).status == "moved"

    # HTTP: Idempotency-Key replays the same move; a new key while open -> 409
    import main  # noqa: F401
    from fastapi.testclient import TestClient

    from accounts import accounts as acc
    from accounts import ratelimit
    settings.app_password = None
    settings.public_pages = False
    settings.environment = "development"
    keys.reset_cache()
    ratelimit.reset()
    acc.reset_cache()
    client = TestClient(main.app)
    aid = bf.fresh_account()
    k = keys.create_key(aid, "move-api", RL.SCOPES, rate_limit_per_minute=1000)
    h = {"Authorization": f"Bearer {k['secret']}", "X-OpenGrid-Request": "1"}
    w2 = Principal(kind="api_key", account_id=aid, key_id=k["id"], scopes=frozenset(RL.SCOPES))
    jid = _running(w2, sim)
    body = {"max_total_cost_usd": 2, "reason": "cheaper"}
    r1 = client.post(f"/v1/jobs/{jid}/move", headers={**h, "Idempotency-Key": "move-key-0001"}, json=body)
    r2 = client.post(f"/v1/jobs/{jid}/move", headers={**h, "Idempotency-Key": "move-key-0001"}, json=body)
    assert r1.status_code == 202 and r2.status_code == 202 and r2.headers.get("Idempotent-Replayed") == "true", r1.text
    r3 = client.post(f"/v1/jobs/{jid}/move", headers={**h, "Idempotency-Key": "move-key-0002"}, json=body)
    assert r3.status_code == 409 and r3.json()["detail"]["code"] == "move_in_progress", r3.text
    # the runner channel carries the instruction (job token only)
    tok = T.token_for(jid)
    rh = {"Authorization": f"Bearer ogj_{jid}.{tok}"}
    r = client.get(f"/v1/jobs/{jid}/runner/instructions", headers=rh)
    assert r.status_code == 200 and r.json()["instructions"][0]["kind"] == "checkpoint_now", r.text
    r = client.post(f"/v1/jobs/{jid}/runner/events", headers=rh,
                    json={"events": [{"seq": 9000, "type": "log", "data": {"line": "x"}}]})
    assert r.status_code == 200 and r.json()["instructions"][0]["ref"] == C.open_move(jid).move_id, r.text
    r = client.post(f"/v1/jobs/{jid}/move/preview", headers=h, json=body)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "move_in_progress", r.text


def test_runner_side_instruction():
    """runner/reporter.py turns checkpoint_now into OUT/.og-checkpoint-now (once per id); the trainer reads it."""
    import json
    import tempfile

    from runner import mnist_train, reporter
    got = reporter.parse_instructions(json.dumps({"ok": True, "instructions": [
        {"id": 7, "kind": "checkpoint_now", "ref": "mv-1"}, {"id": "x", "kind": "checkpoint_now"},
        {"id": 8, "kind": "rm -rf"}]}).encode())
    assert got == [{"id": 7, "kind": "checkpoint_now", "ref": "mv-1"}], got
    assert reporter.parse_instructions(b"not json") == [] and reporter.parse_instructions(None) == []
    with tempfile.TemporaryDirectory() as d:
        done = set()
        assert mnist_train.checkpoint_requested(d) is None
        assert len(reporter.apply_instructions(got, d, done)) == 1
        assert mnist_train.checkpoint_requested(d) == "7"
        (Path(d) / reporter.CHECKPOINT_REQUEST).unlink()
        assert reporter.apply_instructions(got, d, done) == [], "an id is applied once"
        assert mnist_train.checkpoint_requested(d) is None
    assert reporter.CHECKPOINT_REQUEST == mnist_train.CHECKPOINT_REQUEST


# --------------------------------------------------------------------------
# 5. Batch shards: the batch ledger is the outer ceiling
# --------------------------------------------------------------------------

def _bbody(n=2, **kw):
    b = {"name": "sweep", "template": "pytorch-mnist", "job_type": "training",
         "config": {"epochs": 5, "batch_size": 128, "learning_rate": 0.001, "seed": 42},
         "shards": [{"config": {"learning_rate": round(0.001 * (i + 1), 4)}} for i in range(n)],
         "resources": {"gpu": "auto", "gpu_count": 1, "region": "US"}, "routing": {"strategy": "CHEAPEST"},
         "limits": {"max_price_per_gpu_hour_usd": 3, "max_runtime_minutes": 30},
         "budget": {"max_total_usd": 20}, "concurrency": {"max_running": 4}}
    b.update(kw)
    return b


def _shard_job(bid, i=0):
    return q("SELECT job_id, status FROM batch_shards WHERE batch_id = :b AND shard_index = :i", b=bid, i=i)[0]


def test_batch_shard_budget():
    sim = world()
    w = RL.owner()
    code, out = B.submit(_bbody(), w)
    bid = out["batch_id"]
    B.approve(bid, OPERATOR)
    j0 = _shard_job(bid)[0]
    for _ in range(3):
        T.workers(1)
    evs, _ = T.happy_events()
    T.runner_events(j0, evs[:-1])
    T._ckpt(j0, 1)
    p = T.dep_row(j0).provider
    (sim if p == "lambda" else RL.SIM2).instances[T.dep_row(j0).provider_instance_id]["status"] = "terminated"
    tracker.track()
    sync.sync_job(j0)
    assert T.drive_to_end(j0, 8).failure_code == "gpu_interrupted"
    B.advance(bid)
    # over the per-shard limit / what the batch has left -> refused before anything is reserved
    refused(lambda: C.resume(j0, w, {"max_total_cost_usd": 50}), "batch_budget_exhausted", 422)
    n_res = scalar("SELECT count(*) FROM batch_attempts WHERE batch_id = :b", b=bid)
    code, res = C.resume(j0, w, {"max_total_cost_usd": 2})
    succ = res["job_id"]
    a = q("SELECT kind, status, reserved_usd, prev_job_id FROM batch_attempts WHERE job_id = :j", j=succ)[0]
    assert a[0] == "resume" and a[1] == "attached" and a[3] == j0 and float(a[2]) <= 2, a
    assert scalar("SELECT count(*) FROM batch_attempts WHERE batch_id = :b", b=bid) == n_res + 1
    assert _shard_job(bid)[0] == succ, "the successor is the shard's current job (compare-and-set)"
    with normalize.SessionLocal() as s:
        led = B.ledger(s, bid)
    assert led["committed_usd"] <= 20 and any(x["kind"] == "resume" for x in led["attempts"]), led

    # a batch whose budget is spent: the resume is refused, nothing reserved
    sim = world()
    w = RL.owner()
    code, out = B.submit(_bbody(n=1, budget={"max_total_usd": 2.0}), w)
    bid = out["batch_id"]
    B.approve(bid, OPERATOR)
    j0 = _shard_job(bid)[0]
    for _ in range(3):
        T.workers(1)
    evs, _ = T.happy_events()
    T.runner_events(j0, evs[:-1])
    T._ckpt(j0, 1)
    sim.instances[T.dep_row(j0).provider_instance_id]["status"] = "terminated"
    tracker.track()
    sync.sync_job(j0)
    T.drive_to_end(j0, 8)
    with normalize.SessionLocal.begin() as s:
        s.execute(text("UPDATE deployments SET provider_reported_cost = 1.9 WHERE deployment_id = :d"),
                  {"d": T.job_row(j0).deployment_id})
    n_res = scalar("SELECT count(*) FROM batch_attempts WHERE batch_id = :b", b=bid)
    refused(lambda: C.resume(j0, w, {"max_total_cost_usd": 1}), "batch_budget_exhausted")
    assert scalar("SELECT count(*) FROM batch_attempts WHERE batch_id = :b AND status <> 'released'", b=bid) == n_res
    assert successors(j0) == []

    # a move of a running shard: reserved in the ledger BEFORE the old GPU is terminated; the shard stays relaunching
    sim = world()
    w = RL.owner()
    code, out = B.submit(_bbody(n=1), w)
    bid = out["batch_id"]
    B.approve(bid, OPERATOR)
    j0 = _shard_job(bid)[0]
    for _ in range(3):
        T.workers(1)
    evs, _ = T.happy_events()
    T.runner_events(j0, evs[:-1])
    T._ckpt(j0, 1)
    C.request_move(j0, w, {"max_total_cost_usd": 2, "reason": "cheaper"})
    mv = C.open_move(j0)
    T._ckpt(j0, 3)
    C.advance_move(mv.move_id)
    a = q("SELECT kind, status, job_id FROM batch_attempts WHERE batch_id = :b AND kind = 'move'", b=bid)
    assert len(a) == 1 and a[0][1] == "reserved" and a[0][2] is None, a
    T.drive_to_end(j0, 8)
    B.advance(bid)
    assert _shard_job(bid)[1] == "relaunching", _shard_job(bid)
    C.advance_move(mv.move_id)
    succ = successors(j0)[0]
    a = q("SELECT kind, status, job_id FROM batch_attempts WHERE batch_id = :b AND kind = 'move'", b=bid)[0]
    assert a[1] == "attached" and a[2] == succ and _shard_job(bid)[0] == succ, a
    B.advance(bid)
    assert _shard_job(bid)[1] in ("pending_approval", "needs_reapproval", "running"), _shard_job(bid)


# --------------------------------------------------------------------------
# 6. The move suggestion (read-only)
# --------------------------------------------------------------------------

def _price(provider, price):
    rows = []
    for p, pr in (("lambda", T.PRICE), ("lambdb", 2.59)):
        r = bf.listing(p, price if p == provider else pr, gpu=G, region="us-east-1", lid=f"{p}:" + T.SKU)
        r.sku = T.SKU
        rows.append(r)
    bf.market(G, rows)
    C._SUGGEST_CACHE.clear()


def test_move_suggestion():
    sim = world()
    w = RL.owner()
    jid = _running(w, sim, epochs=6)
    g = O.get(jid, w)
    assert "move_suggestion" not in g, "lambdb is dearer: no suggestion"
    assert g["actions"]["move"]["available"] and g["actions"]["resume"]["available"] is False
    _price("lambdb", 0.5)
    g = O.get(jid, w)
    sg = g.get("move_suggestion")
    assert sg and sg["reason"] == "cheaper" and sg["to"]["provider"] == "lambdb" and sg["savings_pct"] >= 20, sg
    assert sg["auto"] is False and sg["est_remaining_cost_after_move_usd"] < sg["est_remaining_cost_now_usd"]
    assert sg["move_overhead_usd"] > 0 and not sim.terminates() and C.open_move(jid) is None, "never acts"
    # a small saving (< 20 % after the overhead) is not suggested
    _price("lambdb", 2.3)
    assert "move_suggestion" not in O.get(jid, w)
    # a degraded current provider: suggested even without a saving
    with normalize.SessionLocal.begin() as s:
        s.execute(text("UPDATE deployments SET status = 'degraded' WHERE deployment_id = :d"),
                  {"d": T.job_row(jid).deployment_id})
    C._SUGGEST_CACHE.clear()
    sg = O.get(jid, w).get("move_suggestion")
    assert sg and sg["reason"] == "provider_degraded" and sg["degraded"], sg
    # a finished job: none
    with normalize.SessionLocal.begin() as s:
        s.execute(text("UPDATE deployments SET status = 'running' WHERE deployment_id = :d"),
                  {"d": T.job_row(jid).deployment_id})
    O.cancel(jid, w)
    C._SUGGEST_CACHE.clear()
    assert "move_suggestion" not in O.get(jid, w)


# --------------------------------------------------------------------------
# migration 0037 matches the models
# --------------------------------------------------------------------------

def test_migration_matches_models():
    import scratchdb
    from alembic import command
    from sqlalchemy import create_engine, inspect

    import config
    import db
    from store.jobs import Job, JobMove, JobRelaunch, JobRunnerInstruction
    name = "og_test_job_resume_move_mig"
    url = scratchdb.create(name)
    try:
        cfg = db._alembic_config()
        cfg.set_main_option("sqlalchemy.url", url)
        real = config.settings.database_url
        config.settings.database_url = url
        try:
            command.upgrade(cfg, "head")
            command.downgrade(cfg, "0036_batches")     # the scratch DB only (settings point at it here)
            command.upgrade(cfg, "head")
        finally:
            config.settings.database_url = real
        e = create_engine(url)
        insp = inspect(e)
        for model in (Job, JobRelaunch, JobMove, JobRunnerInstruction):
            have = {c["name"] for c in insp.get_columns(model.__tablename__)}
            want = {c.name for c in model.__table__.columns}
            assert want == have, (model.__tablename__, want ^ have)
        idx = {i["name"]: i for i in insp.get_indexes("job_moves")}
        assert idx["ux_job_moves_open"]["unique"] and idx["ux_job_moves_open"]["column_names"] == ["job_id"]
        e.dispose()
    finally:
        scratchdb.drop(name)


TESTS = [test_provider_stop_classification, test_resume_happy_path_and_refusals, test_resume_http_scope_and_idempotency,
         test_oom_escalation, test_move_order_and_abort, test_move_no_double_move_concurrency,
         test_runner_side_instruction, test_batch_shard_budget, test_move_suggestion, test_migration_matches_models]


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
        settings.job_move_enabled = False
        for p in ("lambdb", "lambdh", "lambdx"):
            adapters.unregister(p)
            adapters.TRANSPORTS.pop(p, None)
        if T.S_ is not None:
            bf.drop(T.S_, T.DB)
    print(f"{len(TESTS) - failed if not only else '-'} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or None))
