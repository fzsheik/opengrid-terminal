"""Batch fan-out (jobs/batches.py, api/batches.py): validation + grid expansion and its cap, the placement plan and the
diversification cap, the budget ledger under concurrency (threads), the ONE batch approval (and re-approval when a
quote moves), the concurrency queue, interruption -> relaunch on another provider inside the batch budget,
stop_on_first_failure, cancel, sandbox end to end, idempotency, scopes and HTTP status codes, migration 0036.

Providers: the REAL Lambda adapter against fake Lambda APIs (tests/test_jobs.JobSim): `lambda`, plus test-only copies
`lambdb` / `lambdc` (registered LambdaAdapter subclasses with their own fake API and the lambda test key). Every shard
goes through jobs.orchestrator.submit -> workloads.select -> quote -> engine.route like any job.
Scratch DB og_test_batches. No network, no real provider.
Run:  .venv/bin/python tests/test_batches.py [test_name ...]
"""

from __future__ import annotations

import sys
import threading
import traceback
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_jobs as T  # noqa: E402  (imports scratchdb first: provider credentials blanked, network blocked)

T.DB = "og_test_batches"

import bench_fixtures as bf  # noqa: E402
from bench_fixtures import q, scalar  # noqa: E402

import normalize  # noqa: E402
from accounts import keys  # noqa: E402
from accounts.auth import OPERATOR, Principal  # noqa: E402
from config import settings  # noqa: E402
from routing import adapters, tracker  # noqa: E402
from routing.adapters.lambda_labs import LambdaAdapter  # noqa: E402
from sqlalchemy import text  # noqa: E402

from jobs import batches as B  # noqa: E402
from jobs import orchestrator as O  # noqa: E402
from jobs import relaunch as R  # noqa: E402
from jobs import sync  # noqa: E402

G = T.G
SCOPES = ["data:read", "route:preview", "route:execute", "deployments:read", "deployments:write"]
SIMS: dict[str, "T.JobSim"] = {}
PRICES = {"lambda": 2.49, "lambdb": 2.59, "lambdc": 2.69}
EXTRA = ("lambdb", "lambdc")


class LambdbAdapter(LambdaAdapter):
    CREDENTIAL_PROVIDER = "lambda"


class LambdcAdapter(LambdaAdapter):
    CREDENTIAL_PROVIDER = "lambda"


def world(providers=("lambda", "lambdb"), *, mode="SUPERVISED", prices=None):
    """Fresh tables, one fake API per provider, the market, SUPERVISED controls."""
    prices = {**PRICES, **(prices or {})}
    SIMS.clear()
    SIMS["lambda"] = T.world(mode=mode, price=prices["lambda"])
    with normalize.SessionLocal.begin() as s:
        for t in ("batches", "batch_shards", "batch_attempts", "batch_events", "job_relaunches"):
            s.execute(text(f"DELETE FROM {t}"))
        s.execute(text("DELETE FROM provider_execution_flags WHERE provider IN ('lambdb', 'lambdc')"))
    SIMS["lambda"].price_cents = int(round(prices["lambda"] * 100))
    rows = []
    for p in providers:
        if p != "lambda":
            adapters.register(p, {"lambdb": LambdbAdapter, "lambdc": LambdcAdapter}[p])
            SIMS[p] = T.JobSim()
            SIMS[p].price_cents = int(round(prices[p] * 100))
            adapters.TRANSPORTS[p] = SIMS[p].transport()
            bf.flags(p, validated=True, live=mode == "LIVE", supervised=True)
        r = bf.listing(p, prices[p], gpu=G, region="us-east-1", lid=f"{p}:" + T.SKU)
        r.sku = T.SKU
        rows.append(r)
    for p in EXTRA:
        if p not in providers:
            adapters.unregister(p)
            adapters.TRANSPORTS.pop(p, None)
    bf.market(G, rows)
    if mode == "LIVE":
        bf.flags("lambda", validated=True, live=True, supervised=True)
    settings.job_auto_relaunch_enabled = False
    settings.batch_queue_enabled = True
    settings.batch_max_shards = 64
    return SIMS


def owner(scopes=SCOPES) -> Principal:
    aid = bf.fresh_account()
    k = keys.create_key(aid, "batch-test", scopes=scopes)
    return Principal(kind="api_key", account_id=aid, key_id=k["id"], scopes=frozenset(scopes))


def bbody(n=4, **kw) -> dict:
    b = {"name": "sweep", "template": "pytorch-mnist", "job_type": "training",
         "config": {"epochs": 2, "batch_size": 128, "learning_rate": 0.001, "seed": 42},
         "shards": [{"config": {"learning_rate": round(0.001 * (i + 1), 4)}} for i in range(n)],
         "resources": {"gpu": "auto", "gpu_count": 1, "region": "US"},
         "routing": {"strategy": "CHEAPEST"},
         "limits": {"max_price_per_gpu_hour_usd": 3, "max_runtime_minutes": 30},
         "budget": {"max_total_usd": 20}, "concurrency": {"max_running": 4}}
    for k, v in kw.items():
        if isinstance(v, dict) and isinstance(b.get(k), dict):
            b[k] = {**b[k], **v}
        elif v is None:
            b.pop(k, None)
        else:
            b[k] = v
    return b


def refused(fn, code, status=None):
    try:
        fn()
    except (B.BatchRefused, O.JobRefused) as e:
        assert e.code == code, (e.code, e.message)
        assert status is None or e.status == status, (e.status, status)
        return e
    raise AssertionError(f"not refused ({code})")


def shards(bid):
    return {r[0]: {"status": r[1], "job_id": r[2], "attempts": r[3], "reason_code": r[4]}
            for r in q("SELECT shard_index, status, job_id, attempts, reason_code FROM batch_shards WHERE batch_id = :b "
                       "ORDER BY shard_index", b=bid)}


def provider_of(jid):
    d = T.dep_row(jid)
    return d.provider if d is not None else (T.job_row(jid).selection or {}).get("provider")


def creates():
    return {p: len(s.creates()) for p, s in SIMS.items()}


def run_ok(jid):
    """The provider reports running -> the runner trains, uploads, completes -> terminate -> reconcile."""
    for _ in range(3):
        if T.job_row(jid).status == "initializing":
            break
        T.workers(1)
    assert T.job_row(jid).status == "initializing", T.job_row(jid).status
    evs, last = T.happy_events()
    T.runner_events(jid, evs)
    manifest = T.upload_all(jid)
    T.runner_events(jid, [T.result_event(last + 1, manifest)])
    sync.complete(jid, {"outcome": "succeeded", "summary": {"exit_code": 0}})
    j = T.drive_to_end(jid, 8)
    assert j.status == "reconciled" and j.outcome == "succeeded", (j.status, j.outcome)


def committed(bid):
    with normalize.SessionLocal() as s:
        return B.ledger(s, bid)


# --------------------------------------------------------------------------
# validation, grid, preview
# --------------------------------------------------------------------------

def test_validation_and_grid():
    world()
    w = owner()
    pv = B.preview(bbody(sweep={"grid": {"learning_rate": [0.001, 0.002, 0.003], "batch_size": [64, 128]}},
                         shards=None), w)
    cfgs = [s["config"] for s in pv["shards"]]
    assert len(cfgs) == 6 and cfgs[0] == {"batch_size": 64, "learning_rate": 0.001} and \
        cfgs[-1] == {"batch_size": 128, "learning_rate": 0.003}, cfgs
    assert pv["submission"]["can_submit"] and pv["submission"]["would"] == "pending_approval", pv["submission"]
    assert pv["plan"]["placed"] == 6 and pv["request"]["shards_total"] == 6
    assert "ssh_public_key" not in pv["request"]
    # the hard cap, and the sweep's own cap, are checked BEFORE expanding
    settings.batch_max_shards = 4
    e = refused(lambda: B.preview(bbody(sweep={"grid": {"learning_rate": [0.001, 0.002, 0.003],
                                                         "batch_size": [64, 128]}}, shards=None), w), "too_many_shards")
    assert e.extra["shards"] == 6 and e.extra["max_shards"] == 4, e.extra
    refused(lambda: B.preview(bbody(n=5), w), "too_many_shards")
    settings.batch_max_shards = 64
    refused(lambda: B.preview(bbody(sweep={"grid": {"learning_rate": [0.001, 0.002, 0.003], "batch_size": [64, 128]},
                                           "max_shards": 5}, shards=None), w), "too_many_shards")
    big = {f"seed": list(range(60)), "batch_size": list(range(32, 92)), "epochs": [1, 2, 3, 4, 5]}
    e = refused(lambda: B.preview(bbody(sweep={"grid": big}, shards=None), w), "too_many_shards")
    assert e.extra["shards"] == 60 * 60 * 5
    # shape errors
    refused(lambda: B.preview(bbody(sweep={"grid": {"learning_rate": [0.001]}}), w), "shards_and_sweep")
    refused(lambda: B.preview(bbody(shards=None), w), "no_shards")
    refused(lambda: B.preview(bbody(shards=[]), w), "no_shards")
    refused(lambda: B.preview(bbody(sweep={"grid": {"learning_rate": [0.001, 0.001]}}, shards=None), w),
            "invalid_sweep")
    e = refused(lambda: B.preview(bbody(shards=[{"config": {}}, {"config": {"learning_rate": 5}}]), w),
                "config_out_of_bounds")
    assert e.extra["field"] == "shards[1].config.learning_rate" and e.extra["shard_index"] == 1, e.extra
    refused(lambda: B.preview(bbody(shards=[{"config": {"command": "rm -rf /"}}]), w), "invalid_config")
    refused(lambda: B.preview({**bbody(), "command": "x"}, w), "unknown_field")
    refused(lambda: B.preview(bbody(routing={"exclude_providers": ["lambda"]}), w), "unknown_field")
    refused(lambda: B.preview(bbody(placement={"max_share_per_provider": 0}), w), "invalid_placement")
    refused(lambda: B.preview(bbody(placement={"max_share_per_provider": 1.5}), w), "invalid_placement")
    refused(lambda: B.preview(bbody(placement={"include_providers": ["../x"]}), w), "invalid_placement")
    refused(lambda: B.preview(bbody(placement={"allow_interruptible": True}), w), "spot_not_supported")
    refused(lambda: B.preview(bbody(budget={"max_total_usd": -1}), w), "invalid_budget")
    refused(lambda: B.preview(bbody(concurrency={"max_running": 0}), w), "invalid_concurrency")
    refused(lambda: B.preview(bbody(policy={"max_attempts": 4}), w), "invalid_policy")
    refused(lambda: B.preview(bbody(template="pytorch-imagenet"), w), "unknown_template")
    refused(lambda: B.preview(bbody(name="x" * 60), w), "invalid_name")
    assert scalar("SELECT count(*) FROM batches") == 0 and scalar("SELECT count(*) FROM quotes") == 0, \
        "preview never writes"
    # an ordinary job is unchanged; routing.include_providers is validated like exclude_providers
    assert "batch" not in O.get(O.submit(T.body(sandbox=True), w)[1]["job_id"], w)
    T._refused(lambda: O.preview(T.body(routing={"include_providers": ["../x"]}), w), "invalid_include_providers")
    sel = O.preview(T.body(routing={"include_providers": ["lambdb"]}), w)
    assert sel["execution_ready"]["provider"] == "lambdb" and sel["exclusions_by_code"]["provider_not_included"] == 1


# --------------------------------------------------------------------------
# placement + diversification cap
# --------------------------------------------------------------------------

def test_placement_and_diversification_cap():
    world(("lambda", "lambdb", "lambdc"))
    w = owner()
    pl = B.preview(bbody(n=4), w)["plan"]
    got = [s["provider"] for s in pl["shards"]]
    assert got == ["lambda", "lambdb", "lambda", "lambdb"], got        # cap 2 (50% of 4), interleaved, by rank
    assert pl["cap"]["max_shards_per_provider"] == 2 and not pl["unplaced"]
    by = {p["provider"]: p for p in pl["providers"]}
    assert by["lambda"]["shards"] == 2 and by["lambdb"]["shards"] == 2 and by["lambdc"]["shards"] == 0
    assert by["lambda"]["rank"] < by["lambdb"]["rank"] < by["lambdc"]["rank"]
    s0 = pl["shards"][0]
    assert abs(s0["est_max_total_usd"] - 2.49 * 0.5) < 1e-6 and s0["reservation_usd"] == 1.27, s0   # x 1.02, cents up
    t = pl["totals"]
    assert abs(t["est_max_total_usd"] - (2 * 1.245 + 2 * 1.295)) < 1e-6 and t["reservation_total_usd"] == 5.2, t
    alt = pl["alternatives"]
    assert alt["all_on_one"]["provider"] == "lambda" and abs(alt["all_on_one"]["est_max_total_usd"] - 4.98) < 1e-6
    assert abs(alt["savings_vs_all_on_one_usd"] + 0.1) < 1e-6 and abs(alt["diversification_premium_usd"] - 0.1) < 1e-6
    assert {a["provider"] for a in alt["single_provider"]} == {"lambda", "lambdb", "lambdc"}
    # share 25% -> 1 per provider, 3 providers: one shard cannot be placed -> refused, nothing written
    pv = B.preview(bbody(n=4, placement={"max_share_per_provider": 0.25}), w)
    assert pv["plan"]["placed"] == 3 and len(pv["plan"]["unplaced"]) == 1
    assert pv["submission"]["refusal"]["code"] == "placement_infeasible", pv["submission"]
    refused(lambda: B.submit(bbody(n=4, placement={"max_share_per_provider": 0.25}), w), "placement_infeasible")
    assert scalar("SELECT count(*) FROM batches") == 0 and scalar("SELECT count(*) FROM jobs") == 0
    # 100% share allows concentration; an explicit per-provider cap still binds
    assert {s["provider"] for s in B.preview(bbody(n=4, placement={"max_share_per_provider": 1}), w)["plan"]["shards"]} \
        == {"lambda"}
    pl = B.preview(bbody(n=3, placement={"max_share_per_provider": 1, "max_shards_per_provider": 1}), w)["plan"]
    assert sorted(s["provider"] for s in pl["shards"]) == ["lambda", "lambdb", "lambdc"]
    # allow / deny lists
    pl = B.preview(bbody(n=2, placement={"include_providers": ["lambdb", "lambdc"]}), w)["plan"]
    assert [s["provider"] for s in pl["shards"]] == ["lambdb", "lambdc"], pl["shards"]
    pl = B.preview(bbody(n=2, placement={"exclude_providers": ["lambda"]}), w)["plan"]
    assert {s["provider"] for s in pl["shards"]} == {"lambdb", "lambdc"}
    assert B.preview(bbody(n=2, placement={"exclude_regions": ["us-east-1"]}), w)["plan"]["placed"] == 0
    assert B.preview(bbody(n=2, placement={"include_regions": ["US"]}), w)["plan"]["placed"] == 2
    # listed capacity: lambda reports 1 instance -> at most 1 shard there
    with normalize.SessionLocal.begin() as s:
        s.execute(text("UPDATE compute_listings SET capacity = 1, capacity_unit = 'instance' WHERE provider = 'lambda'"))
    pl = B.preview(bbody(n=4, placement={"max_share_per_provider": 1}), w)["plan"]
    assert [s["provider"] for s in pl["shards"]].count("lambda") == 1, pl["shards"]
    assert next(p for p in pl["providers"] if p["provider"] == "lambda")["listed_capacity_shards"] == 1
    # one provider, default 50%: two shards cannot both go there
    world(("lambda",))
    refused(lambda: B.submit(bbody(n=2), w), "placement_infeasible")
    assert B.preview(bbody(n=2, placement={"max_share_per_provider": 1}), w)["submission"]["can_submit"]
    # the budget must cover every shard's reservation
    e = refused(lambda: B.submit(bbody(n=2, budget={"max_total_usd": 2.5}, placement={"max_share_per_provider": 1}), w),
                "batch_budget_exceeded")
    assert e.extra["totals"]["reservation_total_usd"] == 2.54, e.extra
    # nothing executable (PREVIEW_ONLY): the job refusal code, with the sandbox hint
    world(mode="PREVIEW_ONLY")
    e = refused(lambda: B.submit(bbody(n=2), w), "execution_disabled")
    assert "sandbox" in e.extra["hint"]


# --------------------------------------------------------------------------
# sandbox end to end
# --------------------------------------------------------------------------

def test_sandbox_end_to_end():
    world(mode="PREVIEW_ONLY")          # nothing real can run; the sandbox still does, SIMULATED
    w = owner()
    body = bbody(sweep={"grid": {"epochs": [1, 2, 3], "learning_rate": [0.001, 0.01]}}, shards=None, sandbox=True,
                 concurrency={"max_running": 2}, budget={"max_total_usd": 5},
                 placement={"max_share_per_provider": 1})
    code, out = B.submit(body, w)
    assert code == 201 and out["sandbox"] and out["simulated"] and "SIMULATED" in out["simulated_note"]
    assert out["status"] == "finished" and out["outcome"] == "simulated", (out["status"], out["outcome"])
    assert out["counts"] == {"succeeded": 6}, out["counts"]
    assert out["budget"]["committed_usd"] == 0 and out["budget"]["spent_usd"] == 0
    assert out["approval"]["status"] == "not_required" and not out["approval"]["required"]
    assert all(c == 0 for c in creates().values()) and SIMS["lambda"].log == [], "a sandbox never calls a provider"
    best = out["best"]
    assert best["config"]["epochs"] == 3 and best["simulated"] and best["metric"] == "test_accuracy", best
    jobs = [r["job_id"] for r in out["shards"]]
    assert len(set(jobs)) == 6 and all(T.job_row(j).sandbox for j in jobs)
    j0 = O.get(jobs[0], w)
    assert j0["batch"]["batch_id"] == out["batch_id"] and j0["batch"]["shard_index"] == 0, j0.get("batch")
    assert j0["config"]["epochs"] == 1 and j0["name"] == "sweep-s000"
    kinds = [e["kind"] for e in out["events"]]
    assert kinds[0] == "submitted" and kinds[-1] == "finished", kinds
    assert [b["batch_id"] for b in B.list_for(w)] == [out["batch_id"]]
    assert B.list_for(owner()) == [], "another account sees nothing"
    refused(lambda: B.get(out["batch_id"], owner()), "batch_not_found", 404)
    refused(lambda: B.approve(out["batch_id"], OPERATOR), "approval_not_required", 409)
    assert B.cancel(out["batch_id"], w)[0] == 200, "nothing to cancel"


# --------------------------------------------------------------------------
# SUPERVISED: one approval, the queue, deferred approval within the approved terms
# --------------------------------------------------------------------------

def test_one_approval_and_queue():
    world()
    w = owner()
    code, out = B.submit(bbody(n=4, concurrency={"max_running": 2}), w)
    bid = out["batch_id"]
    assert code == 202 and out["status"] == "active"
    sh = shards(bid)
    assert [sh[i]["status"] for i in range(4)] == ["pending_approval", "pending_approval", "queued", "queued"], sh
    assert provider_of(sh[0]["job_id"]) == "lambda" and provider_of(sh[1]["job_id"]) == "lambdb"
    assert creates() == {"lambda": 0, "lambdb": 0}, "nothing launches before the batch approval"
    assert out["approval"]["required"] and len(out["approval"]["waiting"]) == 2
    assert out["budget"]["committed_usd"] == 1.27 + 1.33, out["budget"]
    B.advance(bid)
    assert scalar("SELECT count(*) FROM jobs") == 2, "no free slot: the queue waits"
    # an ordinary key cannot approve a batch through the API path's rule; the operator approves ONCE
    code, ap = B.approve(bid, OPERATOR)
    res = {r["shard_index"]: r["result"] for r in ap["approval_result"]["results"]}
    assert res == {0: "launched", 1: "launched"}, ap["approval_result"]
    assert sorted(ap["approval_result"]["approved_shards"]) == [0, 1, 2, 3]
    assert creates() == {"lambda": 1, "lambdb": 1}
    terms = {r["shard_index"]: r["approved"] for r in ap["shards"]}
    assert terms[2]["basis"] == "plan" and terms[2]["provider"] == "lambda" and terms[0]["basis"] == "quote"
    run_ok(sh[0]["job_id"])
    run_ok(sh[1]["job_id"])
    B.advance(bid)          # slots free: shards 2 and 3 are submitted AND launched under the same approval
    sh = shards(bid)
    assert creates() == {"lambda": 2, "lambdb": 2}, creates()
    for i in (2, 3):
        d = T.dep_row(sh[i]["job_id"])
        assert d.approved_by == "operator" and d.status in ("provisioning", "running"), (d.approved_by, d.status)
    assert provider_of(sh[2]["job_id"]) == "lambda" and provider_of(sh[3]["job_id"]) == "lambdb"
    run_ok(sh[2]["job_id"])
    run_ok(sh[3]["job_id"])
    B.advance(bid)
    g = B.get(bid, w)
    assert g["status"] == "finished" and g["outcome"] == "succeeded" and g["counts"] == {"succeeded": 4}, g["counts"]
    assert scalar("SELECT count(*) FROM jobs") == 4 and len({r["job_id"] for r in g["shards"]}) == 4
    assert g["budget"]["spent_usd"] <= g["budget"]["committed_usd"] <= 20
    assert g["best"]["metric"] == "test_accuracy" and not g["best"]["simulated"]
    prov = {p["provider"]: p for p in g["providers"]}
    assert prov["lambda"]["shards"] == 2 and prov["lambdb"]["shards"] == 2
    assert prov["lambda"]["by_state"] == {"succeeded": 2}
    assert all(a["status"] == "attached" for a in g["ledger"]) and len(g["ledger"]) == 4
    # every shard job was submitted with its reservation as the cost limit (the price cap binds it)
    for r in g["shards"]:
        assert T.job_row(r["job_id"]).spec["limits"]["max_total_cost_usd"] in (1.27, 1.33)


def test_requote_needs_reapproval():
    """A quote that moved past the tolerance is re-quoted by the engine and needs re-approval; a queued shard whose
    fresh quote is outside the approved terms waits for re-approval; nothing launches silently."""
    world()
    w = owner()
    code, out = B.submit(bbody(n=2), w)
    bid = out["batch_id"]
    SIMS["lambda"].price_cents = 274                  # +10% live: over the 2% tolerance
    code, ap = B.approve(bid, OPERATOR)
    res = {r["shard_index"]: r for r in ap["approval_result"]["results"]}
    assert res[1]["result"] == "launched" and res[0]["result"] == "needs_reapproval", res
    assert res[0]["code"] == "quote_invalid" and res[0]["new_quote"]["quote_price_per_gpu_hour"] == 2.74, res[0]
    assert creates() == {"lambda": 0, "lambdb": 1}
    g = B.get(bid, w)
    waiting = g["approval"]["waiting"]
    assert len(waiting) == 1 and waiting[0]["needs_reapproval"] and waiting[0]["quote_price_per_gpu_hour"] == 2.74
    B.advance(bid)
    assert creates()["lambda"] == 0, "the queue pass never approves a re-quote beyond the tolerance"
    # the re-approval is refused by the existing price cap (the reservation binds): still nothing launched
    code, ap = B.approve(bid, OPERATOR)
    r0 = ap["approval_result"]["results"][0]
    assert r0["result"] == "needs_reapproval" and r0["code"] == "over_max_price", r0
    assert creates()["lambda"] == 0
    # the price comes back: re-quoted again, then approved and launched
    SIMS["lambda"].price_cents = 249
    B.approve(bid, OPERATOR)
    code, ap = B.approve(bid, OPERATOR, quotes={"0": shards_quote(bid, 0)})
    assert creates() == {"lambda": 1, "lambdb": 1}, (creates(), ap["approval_result"])
    # a queued shard whose fresh quote is outside the approved terms (provider moved) waits for re-approval
    world()
    w = owner()
    code, out = B.submit(bbody(n=3, concurrency={"max_running": 2},
                               placement={"max_share_per_provider": 1, "max_shards_per_provider": 2}), w)
    bid = out["batch_id"]
    assert [shards(bid)[i]["status"] for i in range(3)] == ["pending_approval", "pending_approval", "queued"]
    assert out["plan_summary"]["totals"]["reservation_total_usd"] == 1.27 * 2 + 1.33
    B.approve(bid, OPERATOR)
    with normalize.SessionLocal.begin() as s:          # lambda's price rises 4.4% on the market and live
        s.execute(text("UPDATE compute_listings SET price_per_gpu_hour = 2.60, price_per_instance_hour = 2.60 "
                       "WHERE provider = 'lambda'"))
    SIMS["lambda"].price_cents = 260
    run_ok(shards(bid)[0]["job_id"])
    B.advance(bid)
    sh = shards(bid)
    assert provider_of(sh[2]["job_id"]) == "lambdb", "re-placed: lambdb is now cheaper"
    assert sh[2]["status"] == "needs_reapproval" and sh[2]["reason_code"] == "outside_approved_terms", sh[2]
    assert creates() == {"lambda": 1, "lambdb": 1}
    B.approve(bid, OPERATOR)
    assert creates() == {"lambda": 1, "lambdb": 2}


def test_live_queue_switch_owner_and_recovery():
    """LIVE providers launch without an approval (as a plain job); the queue OFF switch; the background pass submits
    as the batch's owner re-verified (a revoked key submits nothing); a submission that crashed before creating a job
    goes back to the queue with its reservation released."""
    world(mode="LIVE")
    w = owner()
    code, out = B.submit(bbody(n=4, concurrency={"max_running": 2}), w)
    bid = out["batch_id"]
    assert creates() == {"lambda": 1, "lambdb": 1} and not out["approval"]["required"], out["approval"]
    assert out["plan_summary"]["would"] == "launch"
    sh = shards(bid)
    settings.batch_queue_enabled = False
    run_ok(sh[0]["job_id"])
    run_ok(sh[1]["job_id"])
    adv = B.advance(bid)
    assert "disabled" in adv["queue"] and scalar("SELECT count(*) FROM jobs") == 2
    settings.batch_queue_enabled = True
    # a crashed submission (claimed + reserved, no job) older than the stale window is put back
    with normalize.SessionLocal.begin() as s:
        s.execute(text("UPDATE batch_shards SET status = 'submitting', updated_at = now() - interval '1 hour' "
                       "WHERE batch_id = :b AND shard_index = 2"), {"b": bid})
        s.execute(text("INSERT INTO batch_attempts (batch_id, shard_index, ref, kind, reserved_usd, status, created_at, "
                       "updated_at) VALUES (:b, 2, 'crashed', 'submit', 1.27, 'reserved', now(), now())"), {"b": bid})
    before = committed(bid)["committed_usd"]
    assert before >= 1.27, "an unattached reservation counts in full"
    B.sync_batch(bid)
    assert shards(bid)[2]["status"] == "queued"
    assert abs(committed(bid)["committed_usd"] - (before - 1.27)) < 1e-6
    assert scalar("SELECT status FROM batch_attempts WHERE ref = 'crashed'") == "released"
    keys.revoke_key(w.key_id)
    B.advance(bid)
    sh = shards(bid)
    assert sh[2]["status"] == sh[3]["status"] == "refused" and sh[2]["reason_code"] == "owner_key_revoked", sh
    assert scalar("SELECT count(*) FROM jobs") == 2 and creates() == {"lambda": 1, "lambdb": 1}
    g = B.get(bid, OPERATOR)
    assert g["status"] == "finished" and g["outcome"] == "partial", (g["status"], g["outcome"])


def shards_quote(bid, idx):
    return T.dep_row(shards(bid)[idx]["job_id"]).quote_id


# --------------------------------------------------------------------------
# money: the batch budget under concurrency
# --------------------------------------------------------------------------

def test_budget_atomic_under_concurrency():
    world()
    w = owner()
    # (a) concurrent reservations (relaunch-style) against one batch: never past the budget
    code, out = B.submit(bbody(n=2, budget={"max_total_usd": 6}), w)
    bid = out["batch_id"]
    assert committed(bid)["committed_usd"] == 2.6
    ok, refusals, errs = [], [], []
    barrier = threading.Barrier(12)

    def grab(i):
        try:
            barrier.wait()
            ok.append(B.reserve(bid, i % 2, amount=0.5, kind="relaunch", ref=f"t:{i}"))
        except B.BatchRefused as e:
            refusals.append(e.code)
        except Exception as exc:  # noqa: BLE001
            errs.append(exc)

    ts = [threading.Thread(target=grab, args=(i,)) for i in range(12)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errs, errs
    assert len(ok) == 6 and refusals == ["batch_budget_exhausted"] * 6, (len(ok), refusals)
    assert committed(bid)["committed_usd"] == 5.6 <= 6
    # (b) concurrent queue passes: every shard submitted exactly once, the budget exactly covers them
    world()
    w = owner()
    settings.batch_queue_enabled = False             # submit records the batch; the threads below fill the queue
    code, out = B.submit(bbody(n=6, budget={"max_total_usd": 7.8}, concurrency={"max_running": 6}), w)
    bid = out["batch_id"]
    assert scalar("SELECT count(*) FROM jobs") == 0 and out["counts"] == {"queued": 6}
    settings.batch_queue_enabled = True
    errs = []
    barrier = threading.Barrier(6)

    def go():
        try:
            barrier.wait()
            B.advance(bid)
        except Exception as exc:  # noqa: BLE001
            errs.append(exc)

    ts = [threading.Thread(target=go) for _ in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errs, errs
    sh = shards(bid)
    assert all(v["attempts"] == 1 and v["job_id"] for v in sh.values()), sh
    assert scalar("SELECT count(*) FROM jobs") == 6 and scalar("SELECT count(*) FROM quotes") == 6
    assert scalar("SELECT count(*) FROM deployments") == 6
    led = committed(bid)
    assert led["committed_usd"] == 7.8 and len([a for a in led["attempts"] if a["status"] == "attached"]) == 6, led
    refused(lambda: B.reserve(bid, 0, amount=0.01, kind="relaunch", ref="late"), "batch_budget_exhausted")
    # the database refuses a second job for a shard even if code tried: a used reservation cannot attach again
    a = q("SELECT id, job_id FROM batch_attempts WHERE batch_id = :b ORDER BY id LIMIT 1", b=bid)[0]
    try:
        with normalize.SessionLocal.begin() as s:
            B.attach(s, a[0], "job-00000000deadbeef")
        raise AssertionError("a used reservation attached a second job")
    except O.JobRefused as e:
        assert e.code == "batch_shard_conflict"
    from sqlalchemy.exc import IntegrityError
    try:
        with normalize.SessionLocal.begin() as s:
            s.execute(text("INSERT INTO batch_shards (batch_id, shard_index, params, status, attempts, tries, "
                           "created_at, updated_at) VALUES (:b, 0, '{}', 'queued', 0, 0, now(), now())"), {"b": bid})
        raise AssertionError("UNIQUE(batch_id, shard_index) missing")
    except IntegrityError:
        pass


# --------------------------------------------------------------------------
# interruption -> relaunch through jobs/relaunch.py, the batch budget as the outer ceiling
# --------------------------------------------------------------------------

def _interrupt(jid):
    p = provider_of(jid)
    for _ in range(3):
        if T.job_row(jid).status == "initializing":
            break
        T.workers(1)
    evs, _ = T.happy_events()
    T.runner_events(jid, evs[:-1])
    T._ckpt(jid, 1)
    SIMS[p].instances[T.dep_row(jid).provider_instance_id]["status"] = "terminated"
    tracker.track()
    sync.sync_job(jid)
    j = T.drive_to_end(jid, 8)
    assert j.status == "reconciled" and j.failure_code == "gpu_interrupted", (j.status, j.failure_code)


def test_interruption_relaunch_within_batch_budget():
    world()
    settings.job_auto_relaunch_enabled = True
    w = owner()
    code, out = B.submit(bbody(n=2, config={"epochs": 3}, policy={"max_attempts": 2}, budget={"max_total_usd": 10}), w)
    bid = out["batch_id"]
    B.approve(bid, OPERATOR)
    j0 = shards(bid)[0]["job_id"]
    assert provider_of(j0) == "lambda" and T.job_row(j0).spec["retry"]["max_attempts"] == 2
    _interrupt(j0)
    B.advance(bid)
    assert shards(bid)[0]["status"] == "relaunching", shards(bid)[0]
    assert R.run_all()["relaunched"] == 1
    sh = shards(bid)
    succ = sh[0]["job_id"]
    assert succ != j0 and sh[0]["attempts"] == 2 and T.job_row(succ).retry_of == j0
    js = T.job_row(succ)
    assert js.selection["provider"] == "lambdb", "the interrupting provider is excluded"
    assert "include_providers" not in js.spec["routing"] and js.spec["routing"]["exclude_providers"] == ["lambda"]
    assert js.spec["resume_from"]["epoch"] == 1
    res = q("SELECT reserved_usd, kind FROM batch_attempts WHERE job_id = :j", j=succ)[0]
    assert res[1] == "relaunch" and float(res[0]) == js.spec["limits"]["max_total_cost_usd"] == 1.33, res
    B.advance(bid)            # another provider than the approved one: re-approval, never launched under the old terms
    assert shards(bid)[0]["status"] == "needs_reapproval" and creates()["lambdb"] == 1
    B.approve(bid, OPERATOR)
    assert creates()["lambdb"] == 2 and provider_of(succ) == "lambdb"
    led = committed(bid)
    assert len(led["attempts"]) == 3 and led["committed_usd"] <= 10
    g = B.get(bid, w)
    assert g["shards"][0]["attempts"] == 2 and g["shards"][0]["root_job_id"] == j0
    # the batch budget is the outer ceiling: here it leaves too little for a relaunch
    world()
    settings.job_auto_relaunch_enabled = True
    w = owner()
    code, out = B.submit(bbody(n=2, policy={"max_attempts": 2}, budget={"max_total_usd": 3.0}), w)
    bid = out["batch_id"]
    B.approve(bid, OPERATOR)
    j0 = shards(bid)[0]["job_id"]
    _interrupt(j0)
    with normalize.SessionLocal.begin() as s:     # the provider reported a higher cost than metered: it counts
        s.execute(text("UPDATE deployments SET provider_reported_cost = 1.1 WHERE deployment_id = :d"),
                  {"d": T.job_row(j0).deployment_id})
    n_quotes = scalar("SELECT count(*) FROM quotes")
    R.run_all()
    st = q("SELECT status, reason_code FROM job_relaunches WHERE failed_job_id = :j", j=j0)[0]
    assert tuple(st) == ("stopped", "batch_budget_exhausted"), st
    assert scalar("SELECT count(*) FROM quotes") == n_quotes and scalar("SELECT count(*) FROM jobs WHERE retry_of = :j",
                                                                        j=j0) == 0
    assert scalar("SELECT count(*) FROM batch_attempts WHERE batch_id = :b AND status = 'reserved'", b=bid) == 0
    B.advance(bid)
    s0 = shards(bid)[0]
    assert s0["status"] == "failed" and s0["reason_code"] == "batch_budget_exhausted", s0
    assert committed(bid)["committed_usd"] <= 3.0
    # a cancelled batch is never relaunched
    world()
    settings.job_auto_relaunch_enabled = True
    w = owner()
    code, out = B.submit(bbody(n=2, policy={"max_attempts": 2}), w)
    bid = out["batch_id"]
    B.approve(bid, OPERATOR)
    j0 = shards(bid)[0]["job_id"]
    _interrupt(j0)
    B.cancel(bid, w)
    R.run_all()
    assert q("SELECT reason_code FROM job_relaunches WHERE failed_job_id = :j", j=j0)[0][0] == "batch_stopped"
    assert shards(bid)[0]["status"] == "cancelled"


# --------------------------------------------------------------------------
# failure policy, cancel
# --------------------------------------------------------------------------

def test_stop_on_first_failure_and_continue():
    world()
    w = owner()
    code, out = B.submit(bbody(n=4, concurrency={"max_running": 2}, policy={"on_failure": "stop_on_first_failure"}), w)
    bid = out["batch_id"]
    B.approve(bid, OPERATOR)
    sh = shards(bid)
    j0, j1 = sh[0]["job_id"], sh[1]["job_id"]
    T.workers(2)
    T.runner_events(j0, T.happy_events()[0][:-1])
    sync.complete(j0, {"outcome": "failed", "summary": {"reason": "training_failed", "exit_code": 2}})
    B.advance(bid)
    g = B.get(bid, w)
    assert g["status"] == "stopping" and "stop_on_first_failure" in g["policy"]["stop_reason"], g["policy"]
    sh = shards(bid)
    assert sh[2]["status"] == "skipped" and sh[3]["status"] == "skipped", sh
    assert T.job_row(j1).cancel_requested_at is not None, "the running shard is cancelled through the jobs cancel path"
    for jid in (j0, j1):
        T.drive_to_end(jid, 8)
    B.advance(bid)
    g = B.get(bid, w)
    assert g["status"] == "finished" and g["outcome"] == "stopped", (g["status"], g["outcome"])
    assert g["counts"] == {"failed": 1, "cancelled": 1, "skipped": 2}, g["counts"]
    assert creates() == {"lambda": 1, "lambdb": 1} and scalar("SELECT count(*) FROM jobs") == 2
    assert R.run_all() == {"enabled": False}
    # continue: one failure is reported, the other shard finishes, outcome partial
    world()
    w = owner()
    code, out = B.submit(bbody(n=2), w)
    bid = out["batch_id"]
    B.approve(bid, OPERATOR)
    sh = shards(bid)
    T.workers(2)
    sync.complete(sh[0]["job_id"], {"outcome": "failed", "summary": {"reason": "training_failed", "exit_code": 2}})
    run_ok(sh[1]["job_id"])
    T.drive_to_end(sh[0]["job_id"], 8)
    B.advance(bid)
    g = B.get(bid, w)
    assert g["outcome"] == "partial" and g["counts"] == {"failed": 1, "succeeded": 1}, (g["outcome"], g["counts"])
    assert g["shards"][0]["reason_code"] == "training_failed" and g["best"]["shard_index"] == 1


def test_cancel():
    world()
    w = owner()
    code, out = B.submit(bbody(n=4, concurrency={"max_running": 3}), w)
    bid = out["batch_id"]
    sh = shards(bid)
    assert [sh[i]["status"] for i in range(4)] == ["pending_approval"] * 3 + ["queued"]
    # approving only shard 0's exact quote: the others are not approved
    code, ap = B.approve(bid, OPERATOR, quotes={"0": shards_quote(bid, 0)})
    res = {r["shard_index"]: r["result"] for r in ap["approval_result"]["results"]}
    assert res == {0: "launched", 1: "not_in_request", 2: "not_in_request"}, res
    code, ap = B.approve(bid, OPERATOR, quotes={"1": "q_wrong", "2": shards_quote(bid, 2)})
    res = {r["shard_index"]: r["result"] for r in ap["approval_result"]["results"]}
    assert res[1] == "quote_mismatch" and res[2] == "launched", res
    assert creates() == {"lambda": 2, "lambdb": 0}
    T.workers(2)
    refused(lambda: B.cancel(bid, owner()), "batch_not_found", 404)
    code, c = B.cancel(bid, w)
    assert code == 202 and c["cancel"]["queued"] == 1 and len(c["cancel"]["jobs"]) == 3, c["cancel"]
    sh = shards(bid)
    assert sh[3]["status"] == "cancelled" and sh[3]["job_id"] is None
    assert sh[1]["status"] == "cancelled", sh[1]
    assert T.dep_row(sh[1]["job_id"]).status == "rejected", "pending shard: the existing reject; nothing launched"
    for i in (0, 2):
        assert T.job_row(sh[i]["job_id"]).outcome == "cancelled"
    B.advance(bid)
    assert scalar("SELECT count(*) FROM jobs") == 3, "nothing submitted after a cancel"
    refused(lambda: B.reserve(bid, 3, amount=0.1, kind="submit", claim_shard=True), "batch_not_active", 409)
    refused(lambda: B.approve(bid, OPERATOR), "batch_not_active", 409)
    for i in (0, 2):
        T.drive_to_end(sh[i]["job_id"], 8)
    B.advance(bid)
    g = B.get(bid, w)
    assert g["status"] == "finished" and g["outcome"] == "cancelled" and g["counts"] == {"cancelled": 4}, g["counts"]
    assert len(SIMS["lambda"].terminates()) == 2
    assert B.cancel(bid, w)[0] == 200


# --------------------------------------------------------------------------
# HTTP API: status codes, scopes, idempotency
# --------------------------------------------------------------------------

def test_api_scopes_idempotency_status_codes():
    import main  # noqa: F401
    from fastapi.testclient import TestClient

    from accounts import accounts as acc
    from accounts import ratelimit
    world()
    settings.app_password = None
    settings.public_pages = False
    settings.environment = "development"
    keys.reset_cache()
    ratelimit.reset()
    acc.reset_cache()
    client = TestClient(main.app)
    HDR = {"X-OpenGrid-Request": "1"}

    def key(scopes):
        aid = bf.fresh_account()
        k = keys.create_key(aid, "batch-api", scopes, rate_limit_per_minute=1000)
        return {"Authorization": f"Bearer {k['secret']}"}

    h = key(SCOPES)
    sb = bbody(n=2, sandbox=True, placement={"max_share_per_provider": 1})
    r = client.post("/v1/batches/preview", headers=h, json=bbody(n=2))
    assert r.status_code == 200 and r.json()["meta"]["kind"] == "estimated" and \
        r.json()["meta"]["methodology"] == "/methodology/batches", r.text
    r = client.post("/v1/batches/preview", headers=h, json=bbody(shards=None))
    assert r.status_code == 422 and r.json()["detail"]["code"] == "no_shards", r.text
    assert client.post("/v1/batches", headers=h, json=sb).status_code == 428
    ik = str(uuid.uuid4())
    r1 = client.post("/v1/batches", headers={**h, "Idempotency-Key": ik}, json=sb)
    assert r1.status_code == 201 and r1.json()["meta"]["kind"] == "simulated", r1.text
    r2 = client.post("/v1/batches", headers={**h, "Idempotency-Key": ik}, json=sb)
    assert r2.status_code == 201 and r2.headers.get("Idempotent-Replayed") == "true"
    assert r2.json()["data"]["batch_id"] == r1.json()["data"]["batch_id"]
    assert scalar("SELECT count(*) FROM batches") == 1 and scalar("SELECT count(*) FROM jobs") == 2
    r3 = client.post("/v1/batches", headers={**h, "Idempotency-Key": ik}, json={**sb, "name": "other"})
    assert r3.status_code == 422 and r3.json()["detail"]["code"] == "idempotency_key_reused", r3.text
    bid = r1.json()["data"]["batch_id"]
    assert client.get(f"/v1/batches/{bid}", headers=h).json()["data"]["outcome"] == "simulated"
    assert [b["batch_id"] for b in client.get("/v1/batches", headers=h).json()["data"]] == [bid]
    assert client.get("/v1/batches?status=bogus", headers=h).status_code == 422
    assert client.get("/v1/batches/bat-nope", headers=h).status_code == 404
    other = key(SCOPES)
    assert client.get(f"/v1/batches/{bid}", headers=other).status_code == 404, "tenant isolation"
    assert client.post(f"/v1/batches/{bid}/cancel", headers={**other, "Idempotency-Key": "k1"}).status_code == 404
    ro = key(["data:read"])
    assert client.post("/v1/batches/preview", headers=ro, json=bbody(n=2)).status_code == 403
    assert client.post("/v1/batches", headers={**ro, "Idempotency-Key": "x" * 10}, json=sb).status_code == 403
    assert client.get("/v1/batches", headers=ro).status_code == 403
    pv = key(["route:preview"])
    assert client.post("/v1/batches/preview", headers=pv, json=bbody(n=2)).status_code == 200
    assert client.post("/v1/batches", headers={**pv, "Idempotency-Key": "y" * 10}, json=sb).status_code == 403
    rd = key(["deployments:read"])
    assert client.get("/v1/batches", headers=rd).json()["data"] == [], "a reader sees only its own account"
    assert client.get(f"/v1/batches/{bid}", headers=rd).status_code == 404, "another account's reader"
    assert client.post(f"/v1/batches/{bid}/approve", headers={**h, "Idempotency-Key": "z" * 10}, json={}).status_code \
        == 403, "approve needs admin"
    assert client.post("/v1/batches", json=sb).status_code == 403, "CSRF: operator POST without the header"
    r = client.post(f"/v1/batches/{bid}/approve", headers={**HDR, "Idempotency-Key": str(uuid.uuid4())}, json={})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "approval_not_required", r.text
    assert client.post(f"/v1/batches/{bid}/cancel", headers=h).status_code == 428
    r = client.post(f"/v1/batches/{bid}/cancel", headers={**h, "Idempotency-Key": str(uuid.uuid4())})
    assert r.status_code == 200 and r.json()["data"]["cancel"]["requested"] is False, r.text
    # a real batch over HTTP: 202, then the operator's ONE approval (Idempotency-Key, admin)
    r = client.post("/v1/batches", headers={**h, "Idempotency-Key": str(uuid.uuid4())}, json=bbody(n=2))
    assert r.status_code == 202 and r.json()["meta"]["kind"] == "transaction", r.text
    rb = r.json()["data"]
    assert rb["approval"]["required"] and set(rb["approval"]["body"]["quotes"]) == {"0", "1"}
    r = client.post(f"/v1/batches/{rb['batch_id']}/approve", headers={**HDR, "Idempotency-Key": str(uuid.uuid4())},
                    json={"quotes": rb["approval"]["body"]["quotes"], "reason": "sweep ok"})
    assert r.status_code == 200, r.text
    assert {x["result"] for x in r.json()["data"]["approval_result"]["results"]} == {"launched"}
    assert creates() == {"lambda": 1, "lambdb": 1}
    r = client.post(f"/v1/batches/{rb['batch_id']}/approve", headers={**HDR, "Idempotency-Key": "a" * 12},
                    json={"bogus": 1})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "unknown_field"
    r = client.post("/v1/batches", headers={**h, "Idempotency-Key": str(uuid.uuid4())},
                    json=bbody(n=2, placement={"max_share_per_provider": 1}, budget={"max_total_usd": 2}))
    assert r.status_code == 422 and r.json()["detail"]["code"] == "batch_budget_exceeded", r.text
    r = client.post("/v1/batches", headers={**h, "Idempotency-Key": str(uuid.uuid4())},
                    json=bbody(n=2, budget={"max_total_usd": 1}))     # no single shard fits the per-shard limit
    assert r.status_code == 422 and r.json()["detail"]["code"] == "budget_exceeded", r.text
    # a shard's job links back to its batch
    jid = rb["shards"][0]["job_id"]
    assert client.get(f"/v1/jobs/{jid}", headers=h).json()["data"]["batch"]["batch_id"] == rb["batch_id"]


# --------------------------------------------------------------------------
# migration 0036 matches the models
# --------------------------------------------------------------------------

def test_migration_matches_models():
    import scratchdb
    from alembic import command
    from sqlalchemy import create_engine, inspect

    import config
    import db
    from store.batches import Batch, BatchAttempt, BatchEvent, BatchShard
    name = "og_test_batches_mig"
    url = scratchdb.create(name)
    try:
        cfg = db._alembic_config()
        cfg.set_main_option("sqlalchemy.url", url)
        real = config.settings.database_url
        config.settings.database_url = url
        try:
            command.upgrade(cfg, "head")
        finally:
            config.settings.database_url = real
        e = create_engine(url)
        insp = inspect(e)
        for model in (Batch, BatchShard, BatchAttempt, BatchEvent):
            have = {c["name"] for c in insp.get_columns(model.__tablename__)}
            want = {c.name for c in model.__table__.columns}
            assert want == have, (model.__tablename__, want ^ have)
        idx = {i["name"]: i for i in insp.get_indexes("batch_shards")}
        assert idx["ux_batch_shards_index"]["unique"] and idx["ux_batch_shards_index"]["column_names"] == \
            ["batch_id", "shard_index"]
        assert {i["name"]: i for i in insp.get_indexes("batch_attempts")}["ux_batch_attempts_ref"]["unique"]
        e.dispose()
    finally:
        scratchdb.drop(name)


TESTS = [test_validation_and_grid, test_placement_and_diversification_cap, test_sandbox_end_to_end,
         test_one_approval_and_queue, test_requote_needs_reapproval, test_live_queue_switch_owner_and_recovery,
         test_budget_atomic_under_concurrency,
         test_interruption_relaunch_within_batch_budget, test_stop_on_first_failure_and_continue, test_cancel,
         test_api_scopes_idempotency_status_codes, test_migration_matches_models]


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
        settings.batch_queue_enabled = True
        for p in EXTRA:
            adapters.unregister(p)
            adapters.TRANSPORTS.pop(p, None)
        if T.S_ is not None:
            bf.drop(T.S_, T.DB)
    ran = len([t for t in TESTS if not only or t.__name__ in only])
    print(f"{ran - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or None))
