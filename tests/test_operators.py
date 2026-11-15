"""Marketplace operators and cross-marketplace hardware matches (operators.py; api/operators.py;
methodology/internal/operators.md). INTERNAL data.

Covers: the operator key kept in Lium's stored executors and the host fields kept in Vast's census; that neither
ever reaches a listing or a capacity row; operators and concentration per marketplace by hand; fingerprint matches
(strong / build) and price gaps by hand; the database read; the admin-only endpoints; import isolation.

Lium fixture: tests/fixtures_providers/lium_executors_with_nodes.json (a real /api/executors body). Vast offers are
built in the stored shape (providers/vast.py KEPT_FIELDS / CENSUS_FIELDS). No network.

Run:  .venv/bin/python tests/test_operators.py
"""

import asyncio
import base64
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
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
from config import settings  # noqa: E402
from models import RawResponse  # noqa: E402
from providers import lium, vast  # noqa: E402
from providers.lium import LiumProvider  # noqa: E402
from providers.vast import VastProvider  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures_providers"
DB = "og_test_operators"


def fx(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def lium_poll():
    def handler(request):
        if request.url.path == lium.ENDPOINT:
            return httpx.Response(200, json=fx("lium_executors_with_nodes.json"))
        if request.url.path == lium.NODES:
            return httpx.Response(200, json=fx("lium_nodes_include_rented.json"))
        if request.url.path == lium.STATS:
            return httpx.Response(200, json=fx("lium_public_stats.json"))
        return httpx.Response(404)
    return asyncio.run(LiumProvider(httpx.AsyncClient(base_url=lium.BASE_URL, transport=httpx.MockTransport(handler))).fetch())


def lm(id_, op, gpu="NVIDIA GeForce RTX 4090", n=4, cpu="AMD EPYC 7443P 24-Core Processor", ram_gb=252.0, cc="SK",
       price=0.5, free=4):
    """A stored Lium executor."""
    return {"id": id_, "provider_key": op, "miner_hotkey": op, "gpu_class": gpu, "machine_name": gpu, "gpu_count": n,
            "available_gpu_count": free, "cpu_model": cpu, "cpu_count": 48, "ram_total": ram_gb * ops.KIB_PER_GIB,
            "country_code": cc, "price_per_gpu": price}


def vo(machine, host, gpu="RTX 4090", n=4, frac=1.0, cpu="AMD EPYC 7443P 24-Core Processor", ram_gb=252.0,
       geo="Bratislava, SK", dph=2.14):
    """A stored Vast price-search offer (whole machine unless frac < 1)."""
    return {"id": machine * 10, "machine_id": machine, "host_id": host, "gpu_name": gpu, "num_gpus": n,
            "gpu_frac": frac, "cpu_name": cpu, "cpu_cores": 48, "cpu_cores_effective": 48 * frac,
            "cpu_ram": ram_gb * 1024 * frac, "geolocation": geo, "dph_total": dph}


# --------------------------------------------------------------------------- what is stored

def test_lium_keeps_the_operator_key_and_nothing_public_carries_it():
    out = lium_poll()
    stored = out[0].payload
    body = fx("lium_executors_with_nodes.json")
    keys = {e["id"]: e["provider_key"] for e in body}
    assert all(k for k in keys.values()), "the fixture carries a provider key on every machine"
    assert {e["id"]: e["provider_key"] for e in stored} == keys
    assert all(e["miner_hotkey"] == next(b["miner_hotkey"] for b in body if b["id"] == e["id"]) for e in stored)
    assert not any("validator_hotkey" in e or "location" in e for e in stored), "nothing else about identity is added"

    # The normalized rows (listings and capacity) are what public reads serve: no operator key in any of them.
    by_endpoint = {r.endpoint: [type("S", (), {"payload": r.payload, "request": r.request, "fetched_at": r.fetched_at,
                                               "endpoint": r.endpoint})()] for r in out if r.ok}
    listings = LiumProvider.normalize(by_endpoint)
    capacity = LiumProvider.normalize_capacity(by_endpoint)
    assert listings and capacity
    public = json.dumps([x.model_dump(mode="json") for x in listings + capacity])
    for key in set(keys.values()) | {e["miner_hotkey"] for e in body}:
        assert key not in public, "an operator key never reaches a listing, a listing id or a capacity row"
    from models import CapacityObservation, ComputeListing
    for model in (ComputeListing, CapacityObservation):
        assert not [f for f in model.model_fields if re.search(r"operator|host_id|hotkey|provider_key", f)]


def test_vast_census_keeps_host_and_cpu_not_ip():
    assert {"host_id", "cpu_name", "cpu_cores"} <= set(vast.CENSUS_FIELDS)
    for banned in ("public_ipaddr", "hostname", "ssh_host"):
        assert banned not in vast.CENSUS_FIELDS and banned not in vast.KEPT_FIELDS, banned
    assert "host_id" in vast.KEPT_FIELDS and "cpu_name" in vast.KEPT_FIELDS

    def handler(request):
        return httpx.Response(200, json={"offers": [{
            "machine_id": 7, "host_id": 99, "cpu_name": "AMD EPYC 7763 64-Core Processor", "cpu_cores": 128,
            "num_gpus": 2, "gpu_frac": 0.25, "gpu_ids": [1, 2], "geolocation": "Texas, US", "rentable": False,
            "public_ipaddr": "203.0.113.9", "hostname": "box-1", "dph_total": 1.0}]})
    prov = VastProvider(httpx.AsyncClient(base_url="https://x", transport=httpx.MockTransport(handler)), api_key="k")
    raw = asyncio.run(prov._census("H100 SXM", False))
    assert raw.payload == [{"machine_id": 7, "num_gpus": 2, "gpu_frac": 0.25, "gpu_ids": [1, 2],
                            "geolocation": "Texas, US", "rentable": False, "host_id": 99,
                            "cpu_name": "AMD EPYC 7763 64-Core Processor", "cpu_cores": 128}]
    assert "203.0.113.9" not in json.dumps(raw.model_dump(mode="json")) and "box-1" not in json.dumps(raw.payload)


# --------------------------------------------------------------------------- machines, operators (pure)

def test_machines_from_stored_payloads():
    m = ops.lium_machines([lm("a", "opA"), {"no": "id"}, lm("b", None, ram_gb=0)])
    assert [(x["machine_id"], x["operator"], x["gpu"], x["gpus"], x["ram_gb"], x["country"], x["free"]) for x in m] == \
        [("a", "opA", "RTX4090", 4, 252.0, "SK", True), ("b", None, "RTX4090", 4, None, "SK", True)]
    assert ops.gpu_key("NVIDIA GeForce RTX 5090") == ops.gpu_key("RTX 5090") == "RTX5090"
    assert ops.cpu_key("Intel(R) Xeon(R)  Gold 6338") == "INTEL XEON GOLD 6338"

    # Vast: a quarter-machine slice scales back to the whole machine (GPUs and RAM); two offers of one machine merge.
    v = ops.vast_machines([[vo(1, 10, n=1, frac=0.25, dph=0.6), vo(1, 10, n=2, frac=0.5, dph=1.0), vo(2, 11)]],
                          [("RTX 4090", False, [{"machine_id": 3, "host_id": 10, "num_gpus": 8, "gpu_frac": 1,
                                                  "cpu_name": "AMD EPYC 7443P 24-Core Processor", "cpu_cores": 48,
                                                  "geolocation": "Bratislava, SK", "rentable": False},
                                                 {"machine_id": 1, "host_id": 10, "num_gpus": 4, "gpu_frac": 1}])])
    by = {x["machine_id"]: x for x in v}
    assert set(by) == {"1", "2", "3"}
    assert (by["1"]["gpus"], by["1"]["ram_gb"], by["1"]["price_per_gpu_hour"], by["1"]["operator"]) == (4, 252.0, 0.5, "10")
    assert (by["3"]["gpus"], by["3"]["ram_gb"], by["3"]["price_per_gpu_hour"], by["3"]["free"]) == (8, None, None, False), \
        "a census-only machine: no RAM, no price, rented"
    assert by["2"]["country"] == "SK" and ops.vast_machines([[vo(5, 1, geo="Somewhere")]])[0]["country"] is None
    # Payloads are oldest first: a newer round's price replaces an older one's, even when it is higher.
    assert ops.vast_machines([[vo(9, 1, dph=2.0)], [vo(9, 1, dph=4.0)]])[0]["price_per_gpu_hour"] == 1.0


def test_operators_and_concentration_by_hand():
    machines = ops.lium_machines([
        lm("a1", "opA", n=8, price=0.50), lm("a2", "opA", n=8, price=0.70, cc="US"),
        lm("b1", "opB", gpu="NVIDIA H200", n=4, price=3.65), lm("x", None, n=2)])
    r = ops.operators(machines)
    assert (r["machines"], r["identified_machines"], r["unidentified_machines"], r["operators"], r["gpus"]) == (4, 3, 1, 2, 20)
    a, b = r["rows"]
    assert (a["operator"], a["machines"], a["gpus"], a["gpu_share"], a["countries"]) == ("opA", 2, 16, 0.8, ["SK", "US"])
    assert (a["min_price_per_gpu_hour"], a["max_price_per_gpu_hour"]) == (0.5, 0.7)
    assert a["gpu_models"] == {"NVIDIA GeForce RTX 4090": 16} and b["gpu_models"] == {"NVIDIA H200": 4}
    # Shares 0.8 and 0.2: HHI = (0.64 + 0.04) x 10,000 = 6,800. The unidentified machine is in neither share.
    assert (r["top1_gpu_share"], r["top5_gpu_share"], r["hhi"]) == (0.8, 1.0, 6800)
    empty = ops.operators([])
    assert (empty["operators"], empty["hhi"], empty["top1_gpu_share"], empty["rows"]) == (0, None, None, [])


def test_overlap_tiers_and_price_gap_by_hand():
    a = ops.lium_machines([
        lm("L1", "opA", price=0.50),                                   # strong match with V1
        lm("L2", "opA", n=8, price=0.55),                              # same build, other GPU count: build tier
        lm("L3", "opB", ram_gb=128.0),                                 # same build and count, RAM far off: build
        lm("L4", "opB", cc="US"),                                      # another country: no match
        lm("L5", "opC", cpu="Intel(R) Xeon(R) Gold 6338"),             # another CPU: no match
    ])
    b = ops.vast_machines([[vo(1, 10, dph=2.14, ram_gb=255.0), vo(2, 11, gpu="RTX 3090")]])
    r = ops.overlap(a, b)
    assert (r["strong_matches"], r["build_matches"]) == (1, 2)
    assert (r["machines_a_matched"], r["machines_b_matched"], r["machines_a_strong"], r["machines_b_strong"]) == (3, 1, 1, 1)
    first = r["matches"][0]
    assert first["tier"] == "strong" and first["a"]["machine_id"] == "L1" and first["b"]["machine_id"] == "1"
    assert first["a"]["operator"] == "opA" and first["b"]["operator"] == "10"
    # Vast asks 2.14 / 4 = 0.535 per GPU against Lium's 0.50: +7.0%.
    assert first["price_gap_pct"] == 7.0 and first["b"]["price_per_gpu_hour"] == 0.535
    assert {(m["a"]["machine_id"], m["tier"]) for m in r["matches"]} == {("L1", "strong"), ("L2", "build"), ("L3", "build")}
    assert r["operator_pairs"] == [{"operator_a": "opA", "operator_b": "10", "strong": 1, "build": 1},
                                   {"operator_a": "opB", "operator_b": "10", "strong": 0, "build": 1}]
    # RAM tolerance is 3%: 255 vs 252 is inside (1.2%), 262 vs 252 is outside (3.8%).
    wide = ops.overlap(ops.lium_machines([lm("L1", "opA")]), ops.vast_machines([[vo(1, 10, ram_gb=262.0)]]))
    assert (wide["strong_matches"], wide["build_matches"]) == (0, 1)
    # A fingerprint shared inside one marketplace is reported as not unique.
    twins = ops.lium_machines([lm("t1", "o"), lm("t2", "o"), lm("t3", "o", n=8)])
    assert ops.fingerprint_unique(twins) == {"comparable_machines": 3, "unique": 1}
    # No operator key on one side: the match stands, but no operator pair is invented.
    anon = ops.overlap(ops.lium_machines([lm("L1", None)]), b)
    assert anon["strong_matches"] == 1 and anon["operator_pairs"] == []
    assert ops.overlap([], b)["matches"] == [] and ops.overlap(a, [])["matches"] == []


# --------------------------------------------------------------------------- isolation, database, API

def test_never_imported_by_public_code():
    pat = re.compile(r"^\s*(import operators\b|from operators import)", re.M)
    users = sorted(str(p.relative_to(ROOT)) for p in ROOT.rglob("*.py")
                   if not any(part.startswith(".") for part in p.relative_to(ROOT).parts) and "tests" not in p.parts and pat.search(p.read_text(encoding="utf-8", errors="ignore")))
    assert users == ["api/operators.py"], f"only the admin API imports the operators model: {users}"
    src = (ROOT / "api" / "operators.py").read_text(encoding="utf-8")
    assert src.count("Depends(admin)") == src.count("@router.get") == 3 and src.count("include_in_schema=False") == 3
    for name in ("normalize.py", "market.py", "capacity.py"):
        assert "provider_key" not in (ROOT / name).read_text(encoding="utf-8"), name
    assert not (ROOT / "methodology" / "operators.md").exists(), "the note is internal: never a served methodology page"


def _basic(user, pw):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


def test_database_read_and_admin_endpoints():
    from fastapi.testclient import TestClient

    url = scratchdb.create(DB)
    try:
        Session = fixtures.session(url)
        fetch_mod.SessionLocal = Session
        normalize.SessionLocal = Session
        now = datetime.now(timezone.utc).replace(microsecond=0)
        body = fx("lium_executors_with_nodes.json")
        first = lium._keep(body[0])
        cpu, cc = first["cpu_model"], first["country_code"]
        gpus, ram_gb = first["gpu_count"], first["ram_total"] / ops.KIB_PER_GIB
        vast_gpu = re.sub(r"^NVIDIA (GeForce )?", "", first["gpu_class"] or first["machine_name"])
        twin = vo(501, 77, gpu=vast_gpu, n=gpus, cpu=cpu, ram_gb=ram_gb, geo=f"Somewhere, {cc}", dph=1.0 * gpus)

        def vast_raw(payload, request, at):
            return RawResponse(provider="vast", endpoint=vast.ENDPOINT, method="POST" if request.get("census") else "GET",
                               fetched_at=at, status_code=200, ok=True, request=request, payload=payload)
        fetch_mod.save([r.model_copy(update={"fetched_at": now - timedelta(minutes=3)}) for r in lium_poll()])
        fetch_mod.save([
            vast_raw([vo(500, 70, dph=9.0), dict(twin, dph_total=5.0 * gpus)], {"gpu_name": vast_gpu},
                     now - timedelta(minutes=20)),                                                  # an older round
            vast_raw([twin, vo(502, 77, gpu="RTX 3090")], {"gpu_name": vast_gpu}, now - timedelta(minutes=4)),
            vast_raw([{"machine_id": 503, "host_id": 78, "num_gpus": 8, "gpu_frac": 1, "geolocation": "Texas, US",
                       "cpu_name": "AMD EPYC 7763 64-Core Processor", "cpu_cores": 128, "rentable": False}],
                     {"census": True, "gpu_name": "H100 SXM", "rentable": False}, now - timedelta(hours=5)),
            vast_raw([vo(599, 79)], {"gpu_name": "RTX 4090"}, now - timedelta(hours=4)),             # too old to read
        ])
        model = ops.load(now)
        assert len(model["machines"]["lium"]) == len(body)
        ids = {m["machine_id"] for m in model["machines"]["vast"]}
        assert ids == {"500", "501", "502", "503"}, f"every price round of 3 hours, plus the census inside 26 h: {ids}"
        twin_row = next(m for m in model["machines"]["vast"] if m["machine_id"] == "501")
        assert twin_row["price_per_gpu_hour"] == 1.0, "a machine seen in two rounds keeps the newest round's price"
        assert model["as_of"]["vast"] == now - timedelta(minutes=4) and model["as_of"]["vast_census"] == now - timedelta(hours=5)
        r = ops.overlap(model["machines"]["lium"], model["machines"]["vast"])
        assert r["strong_matches"] >= 1 and r["matches"][0]["b"]["machine_id"] == "501"
        assert r["matches"][0]["a"]["operator"] == first["provider_key"] and r["matches"][0]["b"]["operator"] == "77"

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
            for p in ("/v1/ops/operators", "/v1/ops/operators/overlap", "/v1/ops/operators/history"):
                assert client.get(p).status_code == 401, p
                assert client.get(p, headers=viewer).status_code == 403, p
            got = client.get("/v1/ops/operators", headers=op)
            assert got.status_code == 200, got.text
            b = got.json()
            assert b["meta"]["visibility"] == "internal" and set(b["data"]) == {"lium", "vast"}
            assert b["data"]["lium"]["operators"] == len({e["provider_key"] for e in body})
            assert b["data"]["vast"]["rows"][0]["operator"] in {"77", "78"}
            one = client.get("/v1/ops/operators?marketplace=vast&limit=1", headers=op).json()["data"]
            assert set(one) == {"vast"} and len(one["vast"]["rows"]) == 1
            assert client.get("/v1/ops/operators?marketplace=clore", headers=op).status_code == 404
            got = client.get("/v1/ops/operators/overlap?tier=strong", headers=op)
            assert got.status_code == 200, got.text
            o = got.json()
            assert o["meta"]["visibility"] == "internal" and "candidate" in o["meta"]["caveat"]
            assert o["data"]["strong_matches"] >= 1 and all(m["tier"] == "strong" for m in o["data"]["matches"])
            assert client.get("/v1/ops/operators/overlap?tier=maybe", headers=op).status_code == 422
            paths = main.app.openapi()["paths"]
            assert not [p for p in paths if "operators" in p], "admin reads stay out of the public schema"
            from api import pages
            assert "ops" not in pages.PUBLIC_V1
        finally:
            (settings.app_password, settings.public_pages, settings.viewer_user, settings.viewer_password,
             settings.environment) = saved
    finally:
        scratchdb.drop(DB)


def test_daily_series_and_backfill():
    """Two past days rebuilt from stored raw copies: one in the old shape (64 cheapest, no CPU, no Lium keys), one
    in today's shape. Each row says what its day covered; a rebuild changes nothing; the job writes only missing days."""
    from datetime import date

    from sqlalchemy import func, select, text

    from store.operators import OperatorDaily, OperatorOverlapDaily

    db = DB + "_daily"
    url = scratchdb.create(db)
    try:
        Session = fixtures.session(url)
        fetch_mod.SessionLocal = Session
        normalize.SessionLocal = Session
        today = datetime.now(timezone.utc).date()
        old_day, new_day = today - timedelta(days=3), today - timedelta(days=2)
        body = fx("lium_executors_with_nodes.json")
        kept = [lium._keep(e) for e in body]
        first = kept[0]
        vast_gpu = re.sub(r"^NVIDIA (GeForce )?", "", first["gpu_class"] or first["machine_name"])
        ram_gb = first["ram_total"] / ops.KIB_PER_GIB

        def raw(provider, endpoint, payload, at, request=None):
            return RawResponse(provider=provider, endpoint=endpoint, fetched_at=at, status_code=200, ok=True,
                               request=request, payload=payload)

        # The old day: Lium without operator keys; Vast's 64 cheapest in the shape stored then (host id, no CPU).
        m_old = ops.day_moment(old_day)
        old_offers = [{k: v for k, v in vo(700 + i, 40 + i % 3, gpu=vast_gpu, dph=1.0 + i).items()
                       if k not in ("cpu_name", "cpu_cores", "gpu_frac")} for i in range(4)]
        fetch_mod.save([
            raw("lium", lium.ENDPOINT, [{k: v for k, v in e.items() if k not in lium.OPERATOR_FIELDS} for e in kept],
                m_old - timedelta(minutes=5)),
            raw("vast", vast.ENDPOINT, old_offers, m_old - timedelta(minutes=4), {"gpu_name": vast_gpu}),
        ])
        # The new day: keys on Lium, Vast's whole book with a twin of a Lium machine, and a census with host ids.
        m_new = ops.day_moment(new_day)
        twin = vo(501, 77, gpu=vast_gpu, n=first["gpu_count"], cpu=first["cpu_model"], ram_gb=ram_gb,
                  geo=f"Somewhere, {first['country_code']}", dph=1.0 * first["gpu_count"])
        fetch_mod.save([
            raw("lium", lium.ENDPOINT, kept, m_new - timedelta(minutes=5)),
            raw("vast", vast.ENDPOINT, [twin, vo(502, 77, gpu="RTX 3090"), vo(503, 78, gpu="RTX 3090")],
                m_new - timedelta(minutes=4), {"gpu_name": vast_gpu, "book": vast.FULL_BOOK}),
            raw("vast", vast.ENDPOINT, [{"machine_id": 504, "host_id": 79, "num_gpus": 8, "gpu_frac": 1,
                                         "geolocation": "Texas, US", "cpu_name": "AMD EPYC 7763 64-Core Processor",
                                         "cpu_cores": 128, "rentable": False}],
                m_new - timedelta(hours=5), {"census": True, "gpu_name": "H100 SXM", "rentable": False}),
            # Stored after the day's 12:00: no part of that day's row.
            raw("vast", vast.ENDPOINT, [vo(599, 99, gpu=vast_gpu)], m_new + timedelta(minutes=30), {"gpu_name": vast_gpu}),
        ])
        out = ops.backfill(old_day, new_day)
        assert [r["day"] for r in out] == [str(old_day), str(new_day)]
        with Session() as s:
            rows = {(r.day, r.marketplace): r for r in s.execute(select(OperatorDaily)).scalars()}
            over = {r.day: r for r in s.execute(select(OperatorOverlapDaily)).scalars()}
            vis = set(s.execute(text("SELECT visibility FROM operator_daily UNION SELECT visibility FROM operator_overlap_daily")).scalars())
        assert vis == {"internal"} and set(rows) == {(old_day, "lium"), (old_day, "vast"), (new_day, "lium"), (new_day, "vast")}
        o_l, o_v, n_l, n_v = rows[(old_day, "lium")], rows[(old_day, "vast")], rows[(new_day, "lium")], rows[(new_day, "vast")]
        # Old day: Lium has machines but no key, so no operator and no share, and the row says why.
        assert (o_l.machines, o_l.identified_machines, o_l.operators, o_l.hhi, o_l.coverage) == \
            (len(body), 0, 0, None, ops.COVERAGE_NO_KEYS)
        assert (o_v.machines, o_v.operators, o_v.coverage) == (4, 3, ops.COVERAGE_VAST_CAPPED)
        assert over[old_day].comparable_b == 0 and over[old_day].strong_matches == 0 and "not comparable" in over[old_day].note
        # New day.
        assert (n_l.identified_machines, n_l.operators, n_l.coverage) == \
            (len(body), len({e["provider_key"] for e in body}), ops.COVERAGE_LIUM_FREE)
        assert (n_v.machines, n_v.operators, n_v.coverage) == (4, 3, ops.COVERAGE_VAST_FLEET), "599 came after 12:00"
        # Hosts 77 (the twin + a 4-GPU box), 78 (4 GPUs) and 79 (8 GPUs, census only): shares and HHI by hand.
        per_host = sorted([first["gpu_count"] + 4, 4, 8], reverse=True)
        total = sum(per_host)
        assert n_v.gpus == total and float(n_v.top1_gpu_share) == round(per_host[0] / total, 4)
        assert n_v.hhi == round(sum((g / total) ** 2 for g in per_host) * 10000)
        assert n_v.as_of == m_new - timedelta(minutes=4)
        ov = over[new_day]
        assert ov.strong_matches >= 1 and ov.note is None and ov.comparable_a > 0 and ov.comparable_b >= 1
        assert ov.operator_pairs >= 1 and ov.median_strong_gap_pct is not None

        # A rebuild replaces the day's rows: same content, no extra rows.
        before = {k: (r.machines, r.operators, r.gpus, r.hhi, r.coverage) for k, r in rows.items()}
        ops.backfill(old_day, new_day)
        with Session() as s:
            again = {(r.day, r.marketplace): (r.machines, r.operators, r.gpus, r.hhi, r.coverage)
                     for r in s.execute(select(OperatorDaily)).scalars()}
            n_over = s.execute(select(func.count()).select_from(OperatorOverlapDaily)).scalar()
        assert again == before and n_over == 2
        # A day with nothing stored writes nothing; a day whose 12:00 has not come is skipped.
        assert ops.snapshot_day(date(2020, 1, 1))["marketplaces"] == {}
        assert "skipped" in ops.snapshot_day(today + timedelta(days=1))
        # The job writes a missing day once its 12:00 has passed, and leaves a written day alone.
        with Session.begin() as s:
            s.execute(text("DELETE FROM operator_daily WHERE day = :d"), {"d": new_day})
        wrote = ops.run_daily(ops.day_moment(new_day) + timedelta(hours=1))["written"]
        assert [w["day"] for w in wrote] == [str(new_day)], "yesterday (old_day) was already written; only the missing day"
        assert ops.run_daily(ops.day_moment(new_day) + timedelta(hours=2))["written"] == []
        assert ops.run_daily(ops.day_moment(new_day) - timedelta(hours=2))["written"] == [], "before 12:00: not yet"

        hist = ops.history(30)
        assert [r["day"] for r in hist["daily"]][:2] == [new_day, new_day] and len(hist["overlap"]) == 2
        assert ops.main(["backfill", "--since", str(new_day), "--until", str(new_day)]) == 0
    finally:
        scratchdb.drop(db)


if __name__ == "__main__":
    for t in (test_lium_keeps_the_operator_key_and_nothing_public_carries_it, test_vast_census_keeps_host_and_cpu_not_ip,
              test_machines_from_stored_payloads, test_operators_and_concentration_by_hand,
              test_overlap_tiers_and_price_gap_by_hand, test_never_imported_by_public_code,
              test_database_read_and_admin_endpoints, test_daily_series_and_backfill):
        t()
        print(t.__name__, "ok")
    print("all passed")
