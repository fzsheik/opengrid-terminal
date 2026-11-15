"""Lium feeds added 2026-11-15 (lium_data/): price rules per GPU model, fleet counts, the API version, and the two
per-provider feeds (statistics; every machine a provider runs with its rental state). All INTERNAL.

Fixtures in tests/fixtures_lium are REAL answers fetched 2026-11-15 without a key: machines, machines_ineligible,
executors_count, executors_total, version, provider_stats, provider_executors (one provider, 15 machines, 2 rented),
provider_stats_404 (an unknown key). No network: every call goes through MockTransport.

Covers: each parser by hand; what is kept and what is not (no coordinates, GPU serials, validator keys); that a
ticking counter alone stores nothing; that one provider's answer never marks another provider's machines as gone;
a machine leaving; a 404 stored once; a 429 ending the round; which providers are asked; the providers read and its
admin endpoint; fully rented machines reaching the operators read.

Run:  .venv/bin/python tests/test_lium_providers.py
"""

import asyncio
import base64
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

os.environ["OPENGRID_NO_JOBS"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402

import scratchdb  # noqa: E402  (isolates the process from real providers and the network)
import fixtures  # noqa: E402
import fetch as fetch_mod  # noqa: E402
import normalize  # noqa: E402
import operators as ops  # noqa: E402
import provider_meta  # noqa: E402
from config import settings  # noqa: E402
from lium_data import feeds as F  # noqa: E402
from lium_data import public, raw, reads  # noqa: E402
from models import RawResponse  # noqa: E402
from providers import lium  # noqa: E402
from sqlalchemy import func, select, text  # noqa: E402
from store.lium import LiumFeedSnapshot, LiumFeedValue  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures_lium"
DB = "og_test_lium_providers"
UTC = timezone.utc
HK = "5FbFNM1LWUTkxzj2maQ7PnBTXtSSuTzETVkBmgNCsYj9twkW"     # the fixture's provider
OTHER = "5OtherProviderHotkeyxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
GONE = "5UnknownProviderHotkeyxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"


def fx(name):
    return (FIX / name).read_text(encoding="utf-8")


def doc(name):
    return json.loads(fx(name))


def by(values):
    return {(v["subject"], v["metric"]): v for v in values}


class Server:
    """Routes by path; records calls. A route value is (status, body) or a callable(request) returning one."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def handler(self, request):
        self.calls.append(request.url.path)
        hit = self.routes.get(request.url.path)
        if callable(hit):
            hit = hit(request)
        status, body = hit if hit else (404, fx("provider_stats_404_2026-11-15.json"))
        return httpx.Response(status, text=body, headers={"content-type": "application/json"})

    def client(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


def provider_routes(executors=None, stats=None, hotkey=HK):
    return {f"/api/provider-stats/{hotkey}": (200, stats or fx("provider_stats_2026-11-15.json")),
            f"/api/provider-stats/{hotkey}/executors": (200, executors or fx("provider_executors_2026-11-15.json"))}


def run(coro):
    return asyncio.run(coro)


def wipe():
    with normalize.SessionLocal.begin() as s:
        s.execute(text("TRUNCATE lium_feed_values, lium_feed_snapshots, raw_snapshots, raw_payloads "
                       "RESTART IDENTITY CASCADE"))


def count(model, *where):
    with normalize.SessionLocal() as s:
        return s.execute(select(func.count()).select_from(model).where(*where)).scalar()


# ------------------------------------------------------------------------------------------------ parsing (pure)

def test_parse_market_feeds():
    _, v = public.parse_machines(doc("machines_2026-11-15.json"))
    got = by(v)
    src = {r["name"]: r for r in doc("machines_2026-11-15.json")}
    b200 = src["NVIDIA B200"]
    for m in ("price", "p_min", "p_max", "soft_price_limit", "median_price", "rental_rate", "avg_rental_rate_30d",
              "total_gpu_count", "usd_per_epoch_rented", "usd_per_epoch_unrented"):
        assert got[("NVIDIA B200", m)]["value"] == D(str(b200[m])), m
    assert got[("NVIDIA B200", "price")]["value"] == D("5.6") and got[("NVIDIA B200", "p_min")]["value"] == D("2.8")
    assert got[("NVIDIA B200", "supported")]["value"] == 1 and got[("NVIDIA A30", "median_price")]["value"] is None
    assert got[("NVIDIA B200", "gpu_class")]["value_text"] == "NVIDIA B200"
    assert len({s for s, _ in got}) == len(src) == 101
    _, v = public.parse_idle_ineligible(doc("machines_ineligible_2026-11-15.json"))
    assert [(x["subject"], x["metric"], x["value_text"]) for x in v] == \
        [("V100", "reason", "gpu_model_not_eligible_for_unrented_incentive")]
    assert public.PARSERS["executors_count"](doc("executors_count_2026-11-15.json"))[1][0]["value"] == \
        doc("executors_count_2026-11-15.json")["count"]
    assert public.PARSERS["executors_total"](doc("executors_total_2026-11-15.json"))[1][0]["value"] == \
        doc("executors_total_2026-11-15.json")["total_count"]
    got = by(public.parse_version(doc("version_2026-11-15.json"))[1])
    assert got[("*", "build_id")]["value_text"] == doc("version_2026-11-15.json")["build_id"]
    assert got[("*", "started_at")]["value_time"].tzinfo is not None and "rent_by_spec" in got[("*", "features")]["value_text"]
    for name, bad in (("machines", {"x": 1}), ("machines", []), ("idle_ineligible", {}), ("executors_count", {"count": "9"}),
                      ("executors_total", {"count": 9}), ("version", {"started_at": "x"})):
        try:
            public.PARSERS[name](bad)
        except ValueError:
            continue
        raise AssertionError(f"{name} accepted {bad!r}")


def test_parse_provider_feeds_and_what_is_kept():
    body = doc("provider_executors_2026-11-15.json")
    slim = public.slim_provider_executors(body)
    assert len(slim) == len(body) == 15 and [r["id"] for r in slim] == sorted(r["id"] for r in slim)
    dumped = json.dumps(slim)
    for secret in ("latitude", "longitude", "validator_hotkey", "rented_gpu_uuids", "uptime_in_minutes", "hard_disk",
                   "available_port_count", "5F7X5UpKSr26"):
        assert secret not in dumped, secret
    rented = [r for r in slim if r["rented"]]
    assert len(rented) == 2 and all(r["rented_gpu_count"] == 1 and r["revenue_per_hour"] == 0.6935 for r in rented)
    assert all(r["cpu_model"] and r["ram_total"] and r["country_code"] == "RO" for r in slim)
    _, v = public.parse_provider_executors(body, HK)
    got = by(v)
    mid = rented[0]["id"]
    assert got[(f"{HK}/{mid}", "rented")]["value"] == 1 and got[(f"{HK}/{mid}", "revenue_per_hour")]["value"] == D("0.6935")
    assert got[(f"{HK}/{mid}", "rented_since")]["value_time"] == datetime.fromisoformat(rented[0]["rented_since"]).replace(tzinfo=UTC)
    assert got[(f"{HK}/{mid}", "machine_name")]["value_text"] == "NVIDIA GeForce RTX 5090"
    assert all(s.startswith(HK + "/") for s, _ in got), "every machine is filed under its provider"

    stats = doc("provider_stats_2026-11-15.json")
    kept = public.slim_provider_stats(stats)
    assert "latitude" not in json.dumps(kept) and kept["locations"] == [("RO", "Ploieşti")]
    got = by(public.parse_provider_stats(stats, HK)[1])
    assert got[(HK, "rental_successes")]["value"] == 677 and got[(HK, "rental_failures")]["value"] == 12
    assert got[(HK, "success_rate_pct")]["value"] == D("98.26") and got[(HK, "unique_renters")]["value"] == 168
    assert got[(HK, "total_machines")]["value"] == 15 and got[(HK, "reclaimed_count")]["value"] == 0
    assert got[(HK, "countries")]["value_text"] == "RO" and got[(HK, "shared_pool")]["value"] == 0
    for bad in ({}, [], {"rental_successes": 3}):
        try:
            public.parse_provider_stats(bad, HK)
        except ValueError:
            continue
        raise AssertionError(f"provider_stats accepted {bad!r}")
    try:
        public.parse_provider_executors([{"id": "x"}], HK)
    except ValueError:
        pass
    else:
        raise AssertionError("a machine without executor_info must not parse")


def test_registry_internal():
    for f in (*F.PROVIDER_FEEDS, *(F.FEEDS[n] for n in ("machines", "idle_ineligible", "executors_count",
                                                        "executors_total", "version"))):
        assert provider_meta.visibility("lium", f.path, "signals") == "internal", f.name
        assert raw.visibility(f) == "internal", f.name
    assert [f.name for f in F.PROVIDER_FEEDS] == ["provider_stats", "provider_executors"]
    assert not any(f.per_provider for f in F.PUBLIC_FEEDS), "the 15-minute job never asks a per-provider path"
    assert F.url(F.FEEDS["provider_executors"], hotkey=HK) == f"https://lium.io/api/provider-stats/{HK}/executors"
    assert provider_meta.visibility("lium", lium.ENDPOINT) == "public", "Lium's listings stay public"


# ------------------------------------------------------------------------------------------------ storage

def test_provider_poll_change_only_and_scoped():
    wipe()
    srv = Server(provider_routes())
    out = run(public.poll_providers(srv.client(), gap=0, hotkeys=[HK]))
    assert out["states"] == {"ok": 2} and out["answers_changed"] == 2 and out["stopped"] is None
    assert srv.calls == [f"/api/provider-stats/{HK}", f"/api/provider-stats/{HK}/executors"]
    n_snap, n_val = count(LiumFeedSnapshot), count(LiumFeedValue)
    assert n_snap == 2 and n_val == 16 + 15 * 14
    with normalize.SessionLocal() as s:
        snaps = {r.feed: r for r in s.execute(select(LiumFeedSnapshot)).scalars()}
        vis = set(s.execute(select(LiumFeedValue.visibility)).scalars())
    assert vis == {"internal"} and {r.visibility for r in snaps.values()} == {"internal"}
    assert snaps["provider_executors"].params == {"hotkey": HK} and snaps["provider_executors"].rows == 15 * 14
    stored = json.loads(snaps["provider_executors"].body)
    assert stored == json.loads(json.dumps(public.slim_provider_executors(doc("provider_executors_2026-11-15.json"))))
    assert "latitude" not in snaps["provider_stats"].body and "latitude" not in snaps["provider_executors"].body

    # Only ticking counters move (jobs finished, average uptime, live disk and uptime): nothing new is stored.
    ex = doc("provider_executors_2026-11-15.json")
    for x in ex:
        x["executor_info"]["finished_jobs"] += 3
        x["executor_info"]["uptime_in_minutes"] += 60
        x["executor_info"]["specs"]["hard_disk"]["free"] -= 1000
    st = doc("provider_stats_2026-11-15.json")
    st["uptime_counts"]["average_uptime_hours"] += 1.0
    srv.routes.update(provider_routes(json.dumps(ex), json.dumps(st)))
    out = run(public.poll_providers(srv.client(), gap=0, hotkeys=[HK]))
    assert out["answers_changed"] == 0 and out["values_written"] == 0
    assert (count(LiumFeedSnapshot), count(LiumFeedValue)) == (n_snap, n_val)
    with normalize.SessionLocal() as s:
        assert set(s.execute(select(LiumFeedSnapshot.seen_count)).scalars()) == {2}

    # A rental starts on a free machine: one new answer, and only the values that moved are written (the jobs
    # counter, kept out of the comparison until now, comes along with the change).
    free = next(x for x in ex if not x["rented"])
    free.update(rented=True, rented_gpu_count=1, revenue_per_hour=0.7, rented_since="2026-11-15T09:00:00")
    free["executor_info"]["available_gpu_count"] = 0
    srv.routes.update(provider_routes(json.dumps(ex), json.dumps(st)))
    out = run(public.poll_providers(srv.client(), gap=0, hotkeys=[HK]))
    assert out["answers_changed"] == 1
    hist = reads.history("provider_executors", f"{HK}/{free['id']}", "rented")
    assert [h["value"] for h in hist] == [D(1), D(0)], "the rental start is in the machine's history"
    assert reads.history("provider_executors", f"{HK}/{free['id']}", "revenue_per_hour")[0]["value"] == D("0.7")

    # Another provider answers: none of the first provider's machines is marked as gone.
    two = json.dumps([dict(x, id="m-other", executor_info=dict(x["executor_info"], id="m-other", miner_hotkey=OTHER))
                      for x in ex[:1]])
    srv.routes.update(provider_routes(two, json.dumps(dict(st, miner_hotkey=OTHER)), hotkey=OTHER))
    before_absent = count(LiumFeedValue, LiumFeedValue.present.is_(False))
    run(public.poll_providers(srv.client(), gap=0, hotkeys=[OTHER]))
    assert count(LiumFeedValue, LiumFeedValue.present.is_(False)) == before_absent
    with normalize.SessionLocal() as s:
        mine = [v for v in public.latest_values(s, "provider_executors", scope=HK).values() if v.present]
        theirs = [v for v in public.latest_values(s, "provider_executors", scope=OTHER).values() if v.present]
    assert len({v.subject for v in mine}) == 15 and {v.subject for v in theirs} == {f"{OTHER}/m-other"}

    # A machine leaves the first provider: its values are written once as absent; the other 14 are untouched.
    left = ex.pop()
    srv.routes.update(provider_routes(json.dumps(ex), json.dumps(st)))
    run(public.poll_providers(srv.client(), gap=0, hotkeys=[HK]))
    with normalize.SessionLocal() as s:
        mine = [v for v in public.latest_values(s, "provider_executors", scope=HK).values() if v.present]
    assert len({v.subject for v in mine}) == 14 and f"{HK}/{left['id']}" not in {v.subject for v in mine}
    assert reads.history("provider_executors", f"{HK}/{left['id']}", "gpu_count")[0]["present"] is False


def test_unknown_provider_and_rate_limit():
    wipe()
    srv = Server({})
    out = run(public.poll_providers(srv.client(), gap=0, hotkeys=[GONE]))
    again = run(public.poll_providers(srv.client(), gap=0, hotkeys=[GONE]))
    assert out["states"] == {"not_found": 2} and again["answers_changed"] == 0
    assert count(LiumFeedSnapshot) == 2 and count(LiumFeedValue) == 0, "a 404 is one stored row, however often it repeats"
    # A 429 ends the round at once: the second provider is not asked.
    limited = Server({f"/api/provider-stats/{HK}": (429, json.dumps({"error": {"code": "rate_limited", "message": "slow"}}))})
    out = run(public.poll_providers(limited.client(), gap=0, hotkeys=[HK, OTHER]))
    assert out["stopped"] == "rate_limited" and limited.calls == [f"/api/provider-stats/{HK}"]
    # An unreadable body is kept and reported, never read as "no machines".
    odd = Server(provider_routes(executors=json.dumps({"detail": "reshaped"})))
    out = run(public.poll_providers(odd.client(), gap=0, hotkeys=[HK]))
    assert out["states"].get("error") == 1 and count(LiumFeedValue, LiumFeedValue.feed == "provider_executors") == 0


def test_known_hotkeys():
    wipe()
    assert public.known_hotkeys() == []
    now = datetime.now(UTC)
    fetch_mod.save([RawResponse(provider="lium", endpoint=lium.ENDPOINT, fetched_at=now, status_code=200, ok=True,
                                payload=[{"id": "a", "miner_hotkey": HK, "provider_key": "pool-1"},
                                         {"id": "b", "miner_hotkey": HK, "provider_key": "pool-2"},
                                         {"id": "c", "miner_hotkey": None}])])
    assert public.known_hotkeys() == [HK], "the miner hotkey, once, never the pool sub-key"
    # A provider that answered before stays asked when all its machines are rented (gone from the rentable list).
    run(public.poll_providers(Server(provider_routes(hotkey=OTHER)).client(), gap=0, hotkeys=[OTHER]))
    run(public.poll_providers(Server({}).client(), gap=0, hotkeys=[GONE]))
    assert public.known_hotkeys() == sorted([HK, OTHER]), "a key that only ever answered 404 is not asked again"
    with normalize.SessionLocal.begin() as s:
        s.execute(text("UPDATE lium_feed_snapshots SET last_seen_at = :t"), {"t": now - timedelta(days=31)})
    assert public.known_hotkeys() == [HK], "30 days without an answer: dropped"


# ------------------------------------------------------------------------------------------------ reads

def _basic(user, pw):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


def test_providers_read_operators_and_endpoint():
    from fastapi.testclient import TestClient

    wipe()
    run(public.poll_providers(Server(provider_routes()).client(), gap=0, hotkeys=[HK]))
    out = reads.providers()
    assert out["providers"] == 1
    r = out["rows"][0]
    assert (r["hotkey"], r["machines_listed"], r["machines_rented"], r["gpus_listed"], r["gpus_rented"]) == (HK, 15, 2, 15, 2)
    assert r["revenue_per_hour"] == D("1.387") and r["rental_successes"] == 677 and r["last_checked_at"] is not None
    assert out["totals"]["gpus_rented"] == 2

    # operators.py: the per-provider list adds the machines the rentable list does not show (the rented ones).
    body = doc("provider_executors_2026-11-15.json")
    rented_ids = {x["id"] for x in body if x["rented"]}
    listed = ops.lium_machines([{"id": x["id"], "provider_key": "pool-A", "miner_hotkey": HK, "gpu_count": 1,
                                 "gpu_class": "NVIDIA GeForce RTX 5090", "available_gpu_count": 1}
                                for x in body if not x["rented"]])
    fleet = ops.merge_lium(listed, [m for hk, b in reads.provider_machines() for m in ops.lium_provider_machines(hk, b)])
    assert len(fleet) == 15 and len(listed) == 13
    added = [m for m in fleet if m["machine_id"] in rented_ids]
    assert len(added) == 2 and all(m["operator"] == HK and m["free"] is False and m["cpu"] and m["ram_gb"] for m in added)
    assert all(m["operator"] == "pool-A" for m in fleet if m["machine_id"] not in rented_ids), \
        "a machine in both keeps the rentable list's row and its finer operator key"
    model = ops.load()
    assert len(model["machines"]["lium"]) == 15 and "fully rented" in model["coverage"]["lium"]

    import main
    from accounts import security

    client = TestClient(main.app, headers={"X-OpenGrid-Request": "1"})
    saved = (settings.app_password, settings.public_pages, settings.viewer_user, settings.viewer_password,
             settings.environment)
    try:
        security.reset()
        settings.app_password, settings.environment = "pw", "development"
        settings.viewer_user, settings.viewer_password = "vc", "look-only"
        settings.public_pages = True
        op, viewer = _basic("opengrid", "pw"), _basic("vc", "look-only")
        for p in ("/v1/ops/lium/providers", "/v1/ops/lium/feeds/machines", "/v1/ops/lium/feeds/provider_stats"):
            assert client.get(p).status_code == 401, p
            assert client.get(p, headers=viewer).status_code == 403, p
        got = client.get("/v1/ops/lium/providers?limit=5", headers=op)
        assert got.status_code == 200, got.text
        b = got.json()
        assert b["meta"]["visibility"] == "internal" and b["data"]["rows"][0]["hotkey"] == HK
        assert client.get("/v1/ops/lium/feeds/provider_stats", headers=op).status_code == 200
        st = client.get("/v1/ops/lium/status", headers=op).json()["data"]["feeds"]
        assert {"machines", "provider_stats", "provider_executors", "version"} <= {f["feed"] for f in st}
    finally:
        (settings.app_password, settings.public_pages, settings.viewer_user, settings.viewer_password,
         settings.environment) = saved


def main_():
    url = scratchdb.create(DB)
    normalize.SessionLocal = fixtures.session(url)
    fetch_mod.SessionLocal = normalize.SessionLocal
    try:
        for t in (test_parse_market_feeds, test_parse_provider_feeds_and_what_is_kept, test_registry_internal,
                  test_provider_poll_change_only_and_scoped, test_unknown_provider_and_rate_limit, test_known_hotkeys,
                  test_providers_read_operators_and_endpoint):
            t()
            print(t.__name__, "ok")
        print("all passed")
    finally:
        scratchdb.drop(DB)


if __name__ == "__main__":
    main_()
