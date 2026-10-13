"""Settlement-grade fixings (analytics/settlement.py): the frozen math against the live index math, flags and
the fallback waterfall, then the full pipeline on synthetic history in a scratch DB: fixing only after
finality, hourly = published index level, daily TWAP, the hash chain detecting tampering, the append-only
trigger, restatements (new row, original kept, window, admin-only), verification, the API, methodology
changes never rewriting fixings, and raw-evidence retention.

Scratch DB og_test_settlement, TestClient without lifespan. No network.
Run:  .venv/Scripts/python tests/test_settlement.py
"""

import os
import random
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("OPENGRID_NO_JOBS", "1")
os.environ.setdefault("POLLER_ENABLED", "false")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import scratchdb  # noqa: E402

DB = "og_test_settlement"
os.environ["DATABASE_URL"] = scratchdb.url(DB)  # anything reading db.SessionLocal (storage.py) uses the scratch DB

import fixtures  # noqa: E402
import normalize  # noqa: E402
from sqlalchemy import text  # noqa: E402

from analytics import indices, rollups  # noqa: E402
from analytics import settlement as S  # noqa: E402
from analytics.indices import IndexDef, step_base, step_composite  # noqa: E402
from tables import RawSnapshot  # noqa: E402

UTC = timezone.utc
T0 = datetime(2026, 9, 1, tzinfo=UTC)
P = S._gpu_params()


def H(n):
    return T0 + timedelta(hours=n)


# --------------------------------------------------------------------------
# pure
# --------------------------------------------------------------------------

def _inputs_from(votes, down, state):
    cons = [{"provider": p, "price": round(v, 6), "status": "excluded_feed_down" if p in down else "included"}
            for p, v in sorted(votes.items())]
    link = None if state is None else {"hour": state["hour"].isoformat(), "level": state["level"],
                                       "segment_no": state["segment_no"], "prices": dict(state["prices"])}
    return {"kind": "gpu", "fixing_type": "hourly", "params": P, "constituents": cons, "link": link}


def test_frozen_math_matches_index_math():
    """S1 recompute == analytics.indices.step_base over random chains (joins, leaves, outliers, feed-downs)."""
    rng = random.Random(3)
    provs = [f"p{i}" for i in range(9)]
    for trial in range(40):
        state = None
        for hour in range(30):
            votes = {p: round(rng.uniform(1, 4) * (10 if rng.random() < 0.05 else 1), 6)
                     for p in provs if rng.random() < 0.7}
            down = {p for p in votes if rng.random() < 0.1}
            inputs = _inputs_from(votes, down, state)
            row, new = step_base(state, {p: (v, None) for p, v in votes.items()}, down, H(hour))
            res = S.recompute_gpu(inputs)
            assert S._close(res["value"], row["level"]), (trial, hour, res, row)
            if row["published"]:
                assert res["segment_no"] == row["segment_no"]
                assert {d["provider"]: d["status"] for d in row["detail"]} == res["statuses"]
            state = new
    # composite
    d = IndexDef("c", "C", "composite", unit="points", components=(("x", 1), ("y", 1), ("z", 1)))
    pub = lambda lvl, seg=1: {"published": True, "level": lvl, "segment_no": seg}  # noqa: E731
    seq = [{"x": pub(1.0), "y": pub(1.0), "z": pub(1.0)}, {"x": pub(1.3), "y": pub(1.0)},
           {"x": pub(1.3), "y": pub(1.0), "z": pub(7.0)}, {"x": pub(1.3), "y": pub(1.1), "z": pub(99.0, 2)}]
    state = None
    for i, ch in enumerate(seq):
        row, new = step_composite(d, state, ch, H(i))
        comps = [{"index_id": c, "weight": round(1 / 3, 6), "status": "included" if c in ch else "no_data",
                  "level": ch[c]["level"] if c in ch else None, "segment_no": ch[c]["segment_no"] if c in ch else None}
                 for c in ("x", "y", "z")]
        link = None if state is None else {"hour": "x", "level": state["level"], "segment_no": state["segment_no"],
                                           "children": {k: list(v) for k, v in state["children"].items()}}
        res = S.recompute_composite({"params": S._comp_params(), "components": comps, "link": link})
        assert S._close(res["value"], row["level"]), (i, res, row)
        state = new


def test_stat_weights_and_concentration():
    w = S.stat_weights({"a": 1, "b": 2, "c": 3, "d": 4}, P)
    assert w == {"a": 0, "b": 0.5, "c": 0.5, "d": 0}, w
    assert S.stat_weights({"a": 1, "b": 2, "c": 3}, P)["b"] == 1.0
    w5 = S.stat_weights({k: i for i, k in enumerate("abcde")}, P)
    assert w5["a"] == 0 and w5["e"] == 0 and abs(w5["c"] - 1 / 3) < 1e-12
    inp = _inputs_from({"a": 2.0, "b": 2.1, "c": 2.2, "d": 2.3}, set(), None)
    flags = S.hourly_flags(inp, S.recompute_gpu(inp))
    conc = [f for f in flags if f["code"] == "concentration"]
    assert conc and conc[0]["severity"] == "warn" and conc[0]["weight"] == 0.5
    inp = _inputs_from({k: 2 + i / 10 for i, k in enumerate("abcdef")}, set(), None)
    assert not [f for f in S.hourly_flags(inp, S.recompute_gpu(inp)) if f["code"] == "concentration"]


def test_hourly_flags():
    st = {"hour": H(0), "level": 2.0, "segment_no": 1, "prices": {"a": 2.0, "b": 2.0, "c": 2.0, "d": 2.0}}
    inp = _inputs_from({"a": 2.0, "b": 2.0, "c": 2.7, "d": 2.0, "e": 2.0, "f": 2.1}, set(), st)
    res = S.recompute_gpu(inp)
    codes = {f["code"]: f for f in S.hourly_flags(inp, res)}
    assert codes["price_jump"]["provider"] == "c" and codes["price_jump"]["change"] > 0.25
    assert "new_constituent" in codes and codes["new_constituent"]["severity"] == "info"
    assert "evidence_untraceable" in codes, "no evidence attached in a pure test"
    inp = _inputs_from({"a": 2.0, "b": 2.0, "c": 2.0, "d": 2.0, "e": 2.0, "f": 2.0}, {"a", "b", "c"}, st)
    f = [x for x in S.hourly_flags(inp, S.recompute_gpu(inp)) if x["code"] == "feed_outage"][0]
    assert f["severity"] == "severe", "half the candidate feeds down"
    inp = _inputs_from({"a": 2.0, "b": 2.0, "c": 2.0}, set(), None)
    f = [x for x in S.hourly_flags(inp, S.recompute_gpu(inp)) if x["code"] == "low_constituents"]
    assert f and f[0]["severity"] == "severe"


def _day(values, *, severe_at=(), seg_change_at=None, jump_at=None, jump_weight=0.5):
    hours = []
    for i, v in enumerate(values):
        flags = []
        if jump_at is not None and i == jump_at:
            flags.append({"code": "price_jump", "severity": "warn", "provider": "zz", "change": 0.4, "weight": jump_weight})
        hours.append({"fixing_id": f"fx-{i:024x}", "fixing_time": H(i).isoformat(),
                      "status": "final" if v is not None else "not_published", "value": v,
                      "segment_no": 2 if seg_change_at is not None and i >= seg_change_at else 1,
                      "severe": i in severe_at, "row_hash": "0" * 64, "flags": flags})
    return {"kind": "gpu", "fixing_type": "daily", "fixing_time": H(24).isoformat(),
            "params": {"hours": 24, "min_hours": S.MIN_DAILY_HOURS, "statistic": "arithmetic_mean"}, "hours": hours}


def test_daily_twap_and_waterfall():
    vals = [2.0 + 0.01 * i for i in range(24)]
    r = S.recompute_daily(_day(vals))
    assert abs(r["value"] - statistics.fmean(vals)) < 1e-12 and r["hours_used"] == 24
    # severe hours are left out of the average
    r = S.recompute_daily(_day(vals, severe_at=(0, 1, 2)))
    assert abs(r["value"] - statistics.fmean(vals[3:])) < 1e-12 and r["hours_used"] == 21
    # unpublished hours are left out; 18 is enough, 17 is not (step 2: not published, no value)
    v18 = vals[:18] + [None] * 6
    assert abs(S.recompute_daily(_day(v18))["value"] - statistics.fmean(vals[:18])) < 1e-12
    r = S.recompute_daily(_day(vals[:17] + [None] * 7))
    assert r["value"] is None and "insufficient eligible hours: 17 of 24" in r["reason"]
    # a rebase inside the day: only the last segment is averaged (here 20 hours)
    r = S.recompute_daily(_day(vals, seg_change_at=4))
    assert r["hours_used"] == 20 and abs(r["value"] - statistics.fmean(vals[4:])) < 1e-12
    r = S.recompute_daily(_day(vals, seg_change_at=12))
    assert r["value"] is None, "12 hours in the last segment is not enough"
    # surveillance: an influential jump right before the fix holds the daily fixing
    inp = _day(vals, jump_at=22)
    o = S.outcome_daily(None, inp, "1.0")
    assert o["status"] == "not_published" and "surveillance hold" in o["reason"] and o["value"] is None
    assert any(f["code"] == "pre_fix_influential_jump" for f in o["flags"])
    # a jump by a non-influential constituent is only a warning (flag), never a hold
    flags = S.daily_flags(_day(vals, jump_at=22, jump_weight=0.0), S.recompute_daily(_day(vals)))
    assert [f["severity"] for f in flags if f["code"] == "pre_fix_jump"] == ["warn"]
    # a jump earlier in the day is not "immediately before the fix"
    assert not [f for f in S.daily_flags(_day(vals, jump_at=10), {}) if "jump" in f["code"]]


def test_hashing_and_windows():
    assert S.canonical({"b": 1, "a": [1.5, None]}) == '{"a":[1.5,null],"b":1}'
    r = {"fixing_id": "fx-1", "index_id": "i", "fixing_type": "hourly", "fixing_time": T0, "status": "final",
         "value": 2.5, "reason": None, "methodology_version": "1.0", "settlement_version": "S1.0", "input_hash": "x",
         "flags": [], "restates": None, "actor": "system", "prev_hash": S.GENESIS, "created_at": T0}
    h = S.row_hash(r)
    assert S.row_hash({**r, "fixing_time": T0.astimezone(timezone(timedelta(hours=2)))}) == h, "tz-independent"
    assert S.row_hash({**r, "value": 2.5000001}) != h
    fri = datetime(2026, 10, 9, 15, tzinfo=UTC)  # a Friday
    assert S.correction_deadline("daily", fri) == datetime(2026, 10, 13, 15, tzinfo=UTC), "Fri + 2 business days = Tue"
    assert S.correction_deadline("hourly", fri) == fri + timedelta(hours=24)
    assert S.FINALITY_LAG == rollups.OVERLAP + S.SAFETY_MARGIN == timedelta(hours=5)
    assert S.SETTLEMENT_INDICES[-1] == "gpu-compute" and "h100-80gb-sxm5" in S.SETTLEMENT_INDICES


# --------------------------------------------------------------------------
# pipeline (scratch DB)
# --------------------------------------------------------------------------

W = {}


def _q(sql, **kw):
    with W["Session"]() as s:
        return s.execute(text(sql), kw).all()


def run_all(now, start=None):
    tot = {"hourly": 0, "daily": 0}
    for _ in range(30):
        r = S.run(now=now, start=start)
        tot["hourly"] += r["hourly"]
        tot["daily"] += r["daily"]
        if not r["hourly"] and not r["daily"]:
            break
    return tot


def setup_world():
    url = scratchdb.create(DB)
    Session = fixtures.session(url)
    W["Session"] = Session
    real_now = datetime.now(UTC).replace(microsecond=0)
    fixtures.seed(Session, days=10, now=real_now)  # providers join over the first 8 days
    with Session.begin() as s:
        s.execute(text("UPDATE listing_observations SET available = true"))
        s.execute(text("UPDATE compute_listings SET available = true"))
        # make B200 thin: 3 providers (index-publishable, below the settlement minimum of 4)
        b200 = [r[0] for r in s.execute(text("SELECT DISTINCT provider FROM compute_listings "
                                             "WHERE canonical_gpu_name = 'NVIDIA B200 180GB SXM' ORDER BY 1"))]
        for p in b200[3:]:
            s.execute(text("DELETE FROM compute_listings WHERE provider = :p AND canonical_gpu_name = "
                           "'NVIDIA B200 180GB SXM'"), {"p": p})
        # one successful raw snapshot per provider per hour (the evidence), one failing hour for syn_alpha
        start = rollups.floor_hour(real_now) - timedelta(days=10)
        down_hour = rollups.floor_hour(real_now) - timedelta(hours=30)
        W["down_hour"] = down_hour
        snaps = []
        for p in fixtures.PROVIDERS:
            t = start
            while t <= real_now:
                ok = not (p == "syn_alpha" and t == down_hour)
                snaps.append(RawSnapshot(provider=p, endpoint="/synthetic/hourly", fetched_at=t - timedelta(minutes=10),
                                         ok=ok, status_code=200 if ok else 503, error=None if ok else "503",
                                         payload={"provider": p, "at": t.isoformat()} if ok else None))
                t += timedelta(hours=1)
        s.add_all(snaps)
    normalize.SessionLocal = Session
    rollups.refresh()  # AFTER_REFRESH computes index_levels
    W["now"] = real_now
    W["now1"] = rollups.floor_hour(real_now) - timedelta(hours=20)  # top of an hour: +59 min stays in it
    W["start"] = S.floor_day(W["now1"] - S.FINALITY_LAG) - timedelta(days=2)
    rich = _q("""SELECT index_id, max(constituents) AS n FROM index_levels WHERE index_id = ANY(:ids)
                 AND hour > :t GROUP BY index_id ORDER BY 2 DESC, 1""",
              ids=list(S.SETTLEMENT_INDICES[:-1]), t=W["start"])
    W["rich"] = rich[0].index_id
    assert rich[0].n >= 4, rich


def test_fixing_only_after_finality():
    now1 = W["now1"]
    r = run_all(now1, W["start"])
    assert r["hourly"] > 0 and r["daily"] > 0, r
    with W["Session"]() as s:
        ft1 = S.final_through(s, now1)
    W["ft1"] = ft1
    assert ft1 == rollups.floor_hour(now1) - timedelta(hours=5)
    mx = _q("SELECT max(fixing_time) AS mt FROM index_fixings WHERE fixing_type = 'hourly'")[0].mt
    assert mx == ft1, (mx, ft1)
    il = _q("SELECT max(hour) AS mt FROM index_levels")[0].mt
    assert il > ft1 + timedelta(hours=10), "index levels exist for hours that are not final; no fixing for them"
    n_hours = int((ft1 - W["start"]) / S.HOUR) + 1
    per = {x.index_id: x.n for x in _q("""SELECT index_id, count(*) AS n FROM index_fixings
                                           WHERE fixing_type = 'hourly' GROUP BY 1""")}
    assert per == {i: n_hours for i in S.SETTLEMENT_INDICES}, (per, n_hours)
    # idempotent: nothing more to take at the same time
    assert run_all(now1) == {"hourly": 0, "daily": 0}
    # the next hour becomes due exactly when the frontier passes it by FINALITY_LAG
    assert run_all(now1 + timedelta(minutes=59)) == {"hourly": 0, "daily": 0}
    r = S.run(now=now1 + timedelta(hours=1))
    assert r["hourly"] == len(S.SETTLEMENT_INDICES), r
    W["ft1"] = ft1 + S.HOUR


def test_hourly_equals_published_index():
    rows = _q("""SELECT f.index_id, f.fixing_time, f.status, f.value, f.reason, f.flags, l.level, l.published
                 FROM index_fixings f LEFT JOIN index_levels l ON l.index_id = f.index_id AND l.hour = f.fixing_time
                 WHERE f.fixing_type = 'hourly'""")
    finals = [r for r in rows if r.status == "final"]
    assert finals
    for r in finals:
        assert r.published and S._close(r.value, r.level), r
    assert not [r for r in rows if any(f["code"] == "recompute_mismatch" for f in r.flags)]
    # never a stale value: a not_published row has no value and says why
    for r in rows:
        if r.status == "not_published":
            assert r.value is None and r.reason, r
    b200 = [r for r in rows if r.index_id == "b200-180gb-sxm"]
    assert b200 and all(r.status == "not_published" for r in b200)
    assert any("settlement data sufficiency: 3 constituent(s) included, 4 required" in r.reason for r in b200), \
        {r.reason for r in b200}
    # the failing feed hour is recorded on the fixing
    dh = [r for r in rows if r.fixing_time == W["down_hour"] and r.index_id == W["rich"]]
    assert dh, "the failing-feed hour is inside the fixed range"
    assert any(f["code"] == "feed_outage" and "syn_alpha" in f["providers"] for f in dh[0].flags), dh[0].flags
    # evidence: included constituents are traced to raw snapshots
    ev = _q("SELECT count(*) AS n FROM fixing_evidence")[0].n
    assert ev > 0
    f = _q("SELECT inputs FROM index_fixings WHERE index_id = :i AND status = 'final' AND fixing_type = 'hourly' "
           "ORDER BY seq LIMIT 1", i=W["rich"])[0].inputs
    inc = [c for c in f["constituents"] if c["status"] == "included"]
    assert inc and all(c["evidence"]["traceable"] and c["evidence"]["snapshots"] for c in inc), inc


def test_daily_twap_from_db():
    days = _q("SELECT * FROM index_fixings WHERE fixing_type = 'daily' ORDER BY seq")
    assert days
    W["daily"] = days
    for d in days:
        assert d.fixing_time.astimezone(UTC).hour == 0
        hrs = _q("""SELECT DISTINCT ON (fixing_time) fixing_time, status, value, flags, inputs FROM index_fixings
                    WHERE index_id = :i AND fixing_type = 'hourly' AND fixing_time >= :a AND fixing_time < :b
                    ORDER BY fixing_time, seq DESC""", i=d.index_id, a=d.fixing_time - S.DAY, b=d.fixing_time)
        elig = [h for h in hrs if h.value is not None and not S.severe(h.flags)]
        segs = {h.fixing_time: S.recompute(h.inputs)["segment_no"] for h in elig}
        if elig:
            last_seg = segs[max(segs)]
            elig = [h for h in elig if segs[h.fixing_time] == last_seg]
        if len(elig) >= S.MIN_DAILY_HOURS and not S.severe(d.flags):
            assert d.status == "final" and abs(d.value - statistics.fmean(h.value for h in elig)) < 1e-9, d
        else:
            assert d.status == "not_published" and d.value is None and d.reason, d
    assert any(d.status == "final" for d in days), [(d.index_id, d.reason) for d in days]
    assert all(d.status == "not_published" for d in days if d.index_id == "b200-180gb-sxm")
    # ops alert raised for the daily not_published
    inc = _q("SELECT count(*) AS n FROM quality_incidents WHERE kind = 'exec_fixing_not_published'")[0].n
    assert inc >= 1


def test_chain_and_trigger():
    v = S.verify_chain()
    assert v["ok"] and v["checked"] == v["ledger_rows"] and v["head_hash"], v
    paged, q, n = None, None, 0
    while True:
        paged = S.verify_chain(q, limit=7)
        assert paged["ok"]
        n += paged["checked"]
        q = paged["next_seq"]
        if q is None:
            break
    assert n == v["ledger_rows"] and paged["head_hash"] == v["head_hash"]
    first = _q("SELECT seq, fixing_id FROM index_fixings WHERE status = 'final' ORDER BY seq LIMIT 1")[0]
    for sql in ("UPDATE index_fixings SET value = 1 WHERE seq = :q", "DELETE FROM index_fixings WHERE seq = :q",
                "UPDATE index_fixings SET status = 'not_published' WHERE seq = :q",
                "DELETE FROM fixing_evidence WHERE fixing_seq = :q", "TRUNCATE index_fixings",
                "TRUNCATE fixing_evidence"):
        try:
            with W["Session"].begin() as s:
                s.execute(text(sql), {"q": first.seq})
            raise AssertionError(f"not blocked: {sql}")
        except AssertionError:
            raise
        except Exception as e:  # noqa: BLE001
            assert "append-only ledger" in str(e), e
    assert S.verify_chain()["ok"]


def _tamper(sql, **kw):
    with W["Session"].begin() as s:
        s.execute(text("SET LOCAL session_replication_role = replica"))  # bypasses the trigger (superuser only)
        s.execute(text(sql), kw)


def test_verify_detects_tampering():
    import json

    row = _q("""SELECT * FROM index_fixings WHERE index_id = :i AND status = 'final' AND fixing_type = 'hourly'
                ORDER BY seq DESC LIMIT 1""", i=W["rich"])[0]
    v = S.verify_fixing(row.fixing_id)
    assert v["ok"] and v["result"] == "ok" and all(v["checks"].values()), v
    # 1. a constituent's price edited in the stored inputs
    inc = [i for i, c in enumerate(row.inputs["constituents"]) if c["status"] == "included"][0]
    orig = json.dumps(row.inputs)
    _tamper(f"UPDATE index_fixings SET inputs = jsonb_set(inputs, '{{constituents,{inc},price}}', "
            "to_jsonb(CAST(:p AS float8))) WHERE seq = :q", p=row.inputs["constituents"][inc]["price"] * 1.5, q=row.seq)
    v = S.verify_fixing(row.fixing_id)
    assert not v["ok"] and v["result"] == "mismatch" and not v["checks"]["input_hash"], v
    ch = S.verify_chain()
    assert not ch["ok"] and ch["breaks"][0]["seq"] == row.seq and "input_hash" in " ".join(ch["breaks"][0]["problems"])
    _tamper("UPDATE index_fixings SET inputs = CAST(:j AS jsonb) WHERE seq = :q", j=orig, q=row.seq)
    assert S.verify_fixing(row.fixing_id)["ok"] and S.verify_chain()["ok"], "restored exactly"
    # 2. the value edited
    _tamper("UPDATE index_fixings SET value = value + 0.01 WHERE seq = :q", q=row.seq)
    v = S.verify_fixing(row.fixing_id)
    assert not v["checks"]["value"] and not v["checks"]["row_hash"] and v["checks"]["input_hash"], v
    _tamper("UPDATE index_fixings SET value = :v WHERE seq = :q", v=row.value, q=row.seq)
    assert S.verify_fixing(row.fixing_id)["ok"]
    # 3. a row deleted from the middle of the ledger: the next row no longer links
    mid = _q("SELECT * FROM index_fixings ORDER BY seq LIMIT 1 OFFSET 5")[0]
    _tamper("DELETE FROM index_fixings WHERE seq = :q", q=mid.seq)
    ch = S.verify_chain()
    assert not ch["ok"] and "prev_hash" in ch["breaks"][0]["problems"][0], ch["breaks"]
    from store.settlement import IndexFixing
    with W["Session"].begin() as s:
        s.execute(text("SET LOCAL session_replication_role = replica"))
        s.execute(IndexFixing.__table__.insert().values(**dict(mid._mapping)))
    assert S.verify_chain()["ok"]


def test_restatement():
    from fastapi.testclient import TestClient

    import main
    from accounts import accounts as acc
    from accounts import keys, ratelimit
    from config import settings

    settings.app_password, settings.public_pages, settings.environment = None, False, "development"
    keys.reset_cache(); ratelimit.reset(); acc.reset_cache()
    c = TestClient(main.app)
    hdr = {"X-OpenGrid-Request": "1"}
    # a fixing where excluding one constituent changes the value (chain-linked: only a constituent that MOVED
    # this hour can change it), and the remaining set still meets the settlement minimum
    import copy
    row = victim = None
    for cand in _q("""SELECT * FROM index_fixings WHERE status = 'final' AND fixing_type = 'hourly'
                      AND inputs->>'kind' = 'gpu' AND jsonb_array_length(inputs->'constituents') >= 5
                      ORDER BY seq DESC"""):
        for c_ in cand.inputs["constituents"]:
            if c_["status"] != "included":
                continue
            alt = copy.deepcopy(cand.inputs)
            [x for x in alt["constituents"] if x["provider"] == c_["provider"]][0]["status"] = "excluded_correction"
            res = S.recompute_gpu(alt)
            if res["value"] is not None and len(res["included"]) >= 4 and not S._close(res["value"], cand.value):
                row, victim = cand, c_["provider"]
                break
        if row is not None:
            break
    assert row is not None, "no fixing where a single exclusion changes the value"
    # a data:read key cannot restate
    a = acc.create_account("settlement-test")
    k = keys.create_key(a["id"], "ro", ["data:read"], rate_limit_per_minute=1000)
    ro = {"Authorization": f"Bearer {k['secret']}"}
    W["ro"] = ro
    body = {"reason": "provider published an erroneous price, confirmed with provider", "exclude": [
        {"provider": victim, "reason": "erroneous list price"}]}
    r = c.post(f"/v1/admin/fixings/{row.fixing_id}/restate", headers={**ro, **hdr}, json=body)
    assert r.status_code == 403, r.text
    # an admin key without platform_admin has the admin scope stripped
    k2 = keys.create_key(a["id"], "adm", ["data:read", "admin"], rate_limit_per_minute=1000)
    r = c.post(f"/v1/admin/fixings/{row.fixing_id}/restate",
               headers={"Authorization": f"Bearer {k2['secret']}", **hdr}, json=body)
    assert r.status_code == 403, r.text
    # validation
    r = c.post(f"/v1/admin/fixings/{row.fixing_id}/restate", headers=hdr, json={"reason": "short"})
    assert r.status_code == 422
    r = c.post(f"/v1/admin/fixings/{row.fixing_id}/restate", headers=hdr,
               json={"reason": "a long enough reason here", "exclude": [{"provider": "nobody", "reason": "x y z"}]})
    assert r.status_code == 422 and "not a constituent" in r.text, r.text
    # the operator restates
    r = c.post(f"/v1/admin/fixings/{row.fixing_id}/restate", headers=hdr, json=body)
    assert r.status_code == 201, r.text
    new = r.json()["data"]
    assert new["status"] == "restated" and new["restates"] == row.fixing_id and new["actor"] == "operator"
    assert new["fixing_id"] != row.fixing_id and new["effective"] is True
    cons = {x["provider"]: x for x in new["inputs"]["constituents"]}
    assert cons[victim]["status"] == "excluded_correction"
    remaining = {x["provider"]: x["price"] for x in row.inputs["constituents"]
                 if x["status"] == "included" and x["provider"] != victim}
    assert new["value"] is not None and not S._close(new["value"], row.value)
    assert S.verify_fixing(new["fixing_id"])["ok"]
    res = S.recompute(new["inputs"])
    assert set(res["included"]) <= set(remaining) and S._close(res["value"], new["value"])
    # the original is untouched and now marked superseded
    o = _q("SELECT * FROM index_fixings WHERE fixing_id = :f", f=row.fixing_id)[0]
    assert o.row_hash == row.row_hash and o.value == row.value and o.status == "final"
    orig = c.get(f"/v1/fixings/{row.fixing_id}").json()["data"]
    assert orig["superseded_by"] == [new["fixing_id"]] and orig["effective"] is False
    # effective_only history shows the restated value for that hour
    hist = c.get(f"/v1/indices/{row.index_id}/fixings", params={"type": "hourly", "effective": "true",
                                                            "from": row.fixing_time.isoformat(),
                                                            "to": row.fixing_time.isoformat()}).json()["data"]
    assert [f["fixing_id"] for f in hist["fixings"]] == [new["fixing_id"]]
    # listed publicly, with the correction
    rs = c.get("/v1/fixings/restatements", headers=ro).json()["data"]
    assert rs[0]["fixing_id"] == new["fixing_id"] and rs[0]["correction"]["reason"] == body["reason"]
    # ops alert
    assert _q("SELECT count(*) AS n FROM quality_incidents WHERE kind = 'exec_fixing_restated'")[0].n == 1
    # re-include (restating the restatement keeps pointing at the original)
    r = c.post(f"/v1/admin/fixings/{new['fixing_id']}/restate", headers=hdr,
               json={"reason": "provider confirmed the price was genuine after all", "include": [victim]})
    assert r.status_code == 201, r.text
    back = r.json()["data"]
    assert back["restates"] == row.fixing_id and S._close(back["value"], row.value)
    assert back["inputs"]["correction"]["supersedes"] == new["fixing_id"]
    # the correction window: 24 h after publication for hourly
    try:
        S.restate(row.fixing_id, actor="operator", reason="too late for this correction", rederive=True,
                  now=row.created_at + timedelta(hours=24, seconds=1))
        raise AssertionError("window not enforced")
    except S.Refused as e:
        assert e.status == 409 and "correction window" in e.detail
    # a daily: exclude/include refused; re-derivation allowed within 2 business days
    d = [x for x in W["daily"] if x.index_id == W["rich"]][-1]
    r = c.post(f"/v1/admin/fixings/{d.fixing_id}/restate", headers=hdr, json=body)
    assert r.status_code == 422 and "hourly" in r.text
    r = c.post(f"/v1/admin/fixings/{d.fixing_id}/restate", headers=hdr,
               json={"reason": "re-derive after the hourly correction of that day"})
    assert r.status_code == 201, r.text
    assert r.json()["data"]["status"] == "restated" and S.verify_fixing(r.json()["data"]["fixing_id"])["ok"]
    try:
        S.restate(d.fixing_id, actor="operator", reason="too late for this one", now=S.add_business_days(d.created_at, 3))
        raise AssertionError("daily window not enforced")
    except S.Refused as e:
        assert e.status == 409
    assert S.verify_chain()["ok"]


def test_api_shapes_and_scopes():
    from fastapi.testclient import TestClient

    import main
    from config import settings

    c = TestClient(main.app)
    ro = W["ro"]
    r = c.get("/v1/fixings", headers=ro)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["meta"]["methodology"] == "/methodology/settlement" and body["meta"]["kind"] == "observed"
    d = body["data"]
    assert [x["index_id"] for x in d["indices"]] == list(S.SETTLEMENT_INDICES)
    assert d["ledger_head"]["row_hash"] == S.verify_chain()["head_hash"]
    rich = [x for x in d["indices"] if x["index_id"] == W["rich"]][0]
    assert rich["hourly"]["status"] in ("final", "restated", "not_published") and rich["daily"]
    assert rich["provisional"]["status"] == "provisional" and "not a fixing" in rich["provisional"]["note"]
    h = c.get(f"/v1/indices/{W['rich']}/fixings", headers=ro, params={"type": "daily"}).json()
    assert h["data"]["settlement_index"] and all(f["fixing_type"] == "daily" for f in h["data"]["fixings"])
    assert h["meta"]["pagination"]["total"] >= 1
    fid = h["data"]["fixings"][0]["fixing_id"]
    e = c.get(f"/v1/fixings/{fid}", headers=ro).json()["data"]
    assert e["inputs"]["fixing_type"] == "daily" and e["links"]["verify"] == f"/v1/fixings/{fid}/verify"
    v = c.get(f"/v1/fixings/{fid}/verify", headers=ro).json()["data"]
    assert v["result"] == "ok" and v["checks"]["hourly_references"], v
    hourly = c.get(f"/v1/indices/{W['rich']}/fixings", headers=ro,
                   params={"type": "hourly", "status": "final", "limit": 1}).json()["data"]["fixings"][0]
    e = c.get(f"/v1/fixings/{hourly['fixing_id']}", headers=ro).json()["data"]
    assert e["evidence"] and all(x["retained"] for x in e["evidence"])
    assert c.get("/v1/fixings/chain/verify", headers=ro, params={"limit": 3}).json()["data"]["next_seq"] is not None
    m = c.get("/v1/fixings/methodology", headers=ro).json()["data"]
    assert m["daily"]["min_hours"] == 18 and {n["scope"] for n in m["notices"]} == {"indices", "settlement"}
    # errors
    assert c.get("/v1/fixings/fx-" + "0" * 24, headers=ro).status_code == 404
    assert c.get("/v1/fixings/not-an-id", headers=ro).status_code == 422
    assert c.get("/v1/indices/nope/fixings", headers=ro).status_code == 404
    other = c.get("/v1/indices/h100-80gb-sxm5.spot/fixings", headers=ro).json()["data"]
    assert other["settlement_index"] is False and other["fixings"] == []
    assert c.get("/v1/indices/h100-80gb-sxm5", headers=ro).status_code == 200, "existing index endpoint intact"
    r = c.post("/v1/admin/methodology/notices", headers={**ro, "X-OpenGrid-Request": "1"},
               json={"scope": "indices", "version": "9.9", "effective_from": "2030-01-01T00:00:00Z",
                     "notice_days": 0, "summary": "not allowed for a read key"})
    assert r.status_code == 403
    # public pages: fixings readable without a key, admin writes not
    settings.app_password, settings.public_pages = "pw-for-test", True
    try:
        assert c.get("/v1/fixings").status_code == 200
        assert c.get(f"/v1/fixings/{fid}/verify").status_code == 200
        assert c.post(f"/v1/admin/fixings/{fid}/restate", json={"reason": "x" * 20}).status_code == 401
    finally:
        settings.app_password, settings.public_pages = None, False


def test_more_hours_never_change_old_fixings():
    before = {r.fixing_id: (r.row_hash, r.value) for r in _q("SELECT fixing_id, row_hash, value FROM index_fixings")}
    rollups.refresh()  # an incremental refresh rewrites the index tail; fixings are not touched
    now2 = W["now"] - timedelta(hours=10)
    r = run_all(now2)
    assert r["hourly"] == 9 * len(S.SETTLEMENT_INDICES), r
    after = {x.fixing_id: (x.row_hash, x.value) for x in _q("SELECT fixing_id, row_hash, value FROM index_fixings")}
    assert all(after[k] == v for k, v in before.items())
    assert S.verify_chain()["ok"]
    W["now2"] = now2


def test_methodology_change_never_rewrites_fixings():
    from fastapi.testclient import TestClient

    import main

    before = {r.fixing_id: (r.row_hash, r.value, r.methodology_version)
              for r in _q("SELECT fixing_id, row_hash, value, methodology_version FROM index_fixings")}
    old = indices.METHODOLOGY_VERSION
    indices.METHODOLOGY_VERSION = "1.1"
    try:
        out = indices.refresh()
        assert out["full"], "a version change rebuilds index_levels"
        assert {r.v for r in _q("SELECT DISTINCT methodology_version AS v FROM index_levels")} == {indices.stored_version()}
        r = run_all(W["now"])
        assert r["hourly"] > 0
        after = {x.fixing_id: (x.row_hash, x.value, x.methodology_version)
                 for x in _q("SELECT fixing_id, row_hash, value, methodology_version FROM index_fixings")}
        assert all(after[k] == v for k, v in before.items()), "past fixings untouched"
        new = _q("SELECT * FROM index_fixings WHERE fixing_type = 'hourly' AND methodology_version LIKE '1.1%'")
        assert new and all(x.status == "not_published" and x.value is None for x in new)
        # data sufficiency is checked first (thin B200); every other index is held by governance
        assert all("no recorded change notice" in x.reason for x in new if x.index_id != "b200-180gb-sxm"), \
            {x.reason for x in new}
        c = TestClient(main.app)
        hdr = {"X-OpenGrid-Request": "1"}
        now = datetime.now(UTC)
        r = c.post("/v1/admin/methodology/notices", headers=hdr,
                   json={"scope": "indices", "version": "1.1", "effective_from": now.isoformat(), "notice_days": 7,
                         "summary": "Trim 25% instead of 20%"})
        assert r.status_code == 422, "effective_from must respect the notice period"
        r = c.post("/v1/admin/methodology/notices", headers=hdr,
                   json={"scope": "indices", "version": "1.1", "effective_from": (now + timedelta(days=7)).isoformat(),
                         "notice_days": 7, "summary": "Trim 25% instead of 20%"})
        assert r.status_code == 201, r.text
        r = c.post("/v1/admin/methodology/notices", headers=hdr,
                   json={"scope": "indices", "version": "1.1", "effective_from": (now + timedelta(days=8)).isoformat(),
                         "notice_days": 7, "summary": "duplicate notice"})
        assert r.status_code == 409
        with W["Session"]() as s:
            assert "effective only from" in S._governance_gap(s, "1.1+regions", now + timedelta(days=1))
            assert S._governance_gap(s, "1.1+regions", now + timedelta(days=7, hours=1)) is None
            assert "no recorded change notice" in S._governance_gap(s, "2.0", now)
        assert S.verify_chain()["ok"]
    finally:
        indices.METHODOLOGY_VERSION = old


def test_retention_keeps_evidence():
    import storage

    storage.SessionLocal = W["Session"]
    ref = {r.snapshot_id for r in _q("SELECT DISTINCT snapshot_id FROM fixing_evidence")}
    total0 = _q("SELECT count(*) AS n FROM raw_snapshots")[0].n
    assert ref and total0 > len(ref)
    far = W["now"] + timedelta(days=400)
    for _ in range(50):
        r = storage.retention_batch(far, batch=1000)
        if not any(r.values()):
            break
    left = {x.id: x for x in _q("SELECT id, payload IS NOT NULL OR payload_hash IS NOT NULL AS body FROM raw_snapshots")}
    assert ref <= set(left), "every referenced snapshot survives"
    assert all(left[i].body for i in ref), "and keeps its body"
    unref = set(left) - ref
    assert len(left) < total0 and len(unref) <= 2 * len(fixtures.PROVIDERS) * 3, (len(left), total0, len(unref))
    row = _q("SELECT fixing_id FROM index_fixings f WHERE EXISTS (SELECT 1 FROM fixing_evidence e "
             "WHERE e.fixing_seq = f.seq) ORDER BY seq LIMIT 1")[0]
    v = S.verify_fixing(row.fixing_id)
    assert v["checks"]["evidence_retained"] and v["checks"]["evidence_index"], v


if __name__ == "__main__":
    pure = (test_frozen_math_matches_index_math, test_stat_weights_and_concentration, test_hourly_flags,
            test_daily_twap_and_waterfall, test_hashing_and_windows)
    db = (test_fixing_only_after_finality, test_hourly_equals_published_index, test_daily_twap_from_db,
          test_chain_and_trigger, test_verify_detects_tampering, test_restatement, test_api_shapes_and_scopes,
          test_more_hours_never_change_old_fixings, test_methodology_change_never_rewrites_fixings,
          test_retention_keeps_evidence)
    for t in pure:
        t(); print(t.__name__, "ok")
    setup_world()
    try:
        for t in db:
            t(); print(t.__name__, "ok")
    finally:
        normalize.SessionLocal.kw["bind"].dispose()
        scratchdb.drop(DB)
    print(f"{len(pure) + len(db)} tests ok")
