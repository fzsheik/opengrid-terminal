"""Lium's public Grafana (lium_data/grafana.py): rental starts with the price paid, hourly spend and fleet.

Offline: a mock grafana.lium.io serves the dashboards and the panel answers captured on 2026-11-15
(tests/fixtures_lium/grafana_*). Checks that

    - only the query the dashboard holds is ever sent, with its own variables at values it lists; an unknown
      variable, an unlisted value, a renamed panel or a missing tier filter stops the read before any request;
    - the three tier reads of the rentals panel give one row per rental with its tier, and a repeat stores nothing;
    - hourly series are upserted in place and only whole hours are kept;
    - every failure is one stored row and one warning, however long it lasts;
    - the poll window follows the newest stored point; the backfill walks back in chunks and resumes;
    - every row is INTERNAL and the reads are admin-only; migration 0041 and the ORM agree.

    python tests/test_lium_grafana.py
"""

import asyncio
import base64
import copy
import json
import logging
import os
import statistics
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

os.environ["OPENGRID_NO_JOBS"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402

import scratchdb  # noqa: E402  (isolates the process from real providers and the network)
import fixtures  # noqa: E402
import normalize  # noqa: E402
import provider_meta  # noqa: E402
from config import settings  # noqa: E402
from lium_data import feeds as F  # noqa: E402
from lium_data import grafana as G  # noqa: E402
from lium_data import raw, reads  # noqa: E402
from sqlalchemy import func, select, text  # noqa: E402
from store.lium import LiumFeedSnapshot, LiumGrafanaHourly, LiumGrafanaRental  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures_lium"
DB = "og_test_lium_grafana"
UTC = timezone.utc
NOW = datetime(2026, 11, 15, 6, 40, tzinfo=UTC)          # just after the fixtures were captured
BOARDS = {uid: json.loads((FIX / f"grafana_dashboard_{uid}_2026-11-15.json").read_text())
          for uid in (F.MARKETPLACE_PRICES, F.NETWORK_STATUS, F.RENTAL_CHECK)}
ANSWERS = {name: json.loads((FIX / f"grafana_{name}_2026-11-15.json").read_text())
           for name in ("rentals_All", "rentals_spot", "rentals_secure", "spend", "listed", "rented", "empty",
                        # the panels read since migration 0043 (tests/test_lium_grafana_history.py checks them)
                        "launch_created", "launch_failures", "price_daily", "deploy_causes", "penalties")}


def setup():
    normalize.SessionLocal = fixtures.session(scratchdb.create(DB))


def _cells(name):
    """{(hour ms, GPU model): value} of a captured hourly answer."""
    frame = ANSWERS[name]["results"]["A"]["frames"][0]
    times, out = frame["data"]["values"][0], {}
    for fld, col in zip(frame["schema"]["fields"][1:], frame["data"]["values"][1:]):
        label = (fld.get("labels") or {}).get("gpu_type") or fld["name"]
        out.update({(t, label): v for t, v in zip(times, col) if v is not None})
    return out


FLEET = set(_cells("listed")) | set(_cells("rented"))
SPEND = _cells("spend")
# one row per hour and model the fleet panels name, plus the models that only have a spend above zero
HOURLY_ROWS = len(FLEET | {k for k, v in SPEND.items() if v != 0})


def wipe():
    with normalize.SessionLocal.begin() as s:
        s.execute(text("TRUNCATE lium_grafana_rentals, lium_grafana_hourly, lium_grafana_network_hourly, "
                       "lium_grafana_price_daily, lium_grafana_daily_counts, lium_feed_snapshots "
                       "RESTART IDENTITY CASCADE"))


def count(model, *where):
    with normalize.SessionLocal() as s:
        return s.execute(select(func.count()).select_from(model).where(*where)).scalar()


def run(coro):
    return asyncio.run(coro)


class Logs(logging.Handler):
    NAMES = ("lium_data.raw", "lium_data.grafana")

    def __init__(self):
        super().__init__(logging.INFO)
        self.records: list[logging.LogRecord] = []

    def __enter__(self):
        for n in self.NAMES:
            logging.getLogger(n).addHandler(self)
            logging.getLogger(n).setLevel(logging.DEBUG)
        return self

    def __exit__(self, *exc):
        for n in self.NAMES:
            logging.getLogger(n).removeHandler(self)

    def emit(self, record):
        self.records.append(record)

    def warnings(self):
        return [r.getMessage() for r in self.records if r.levelno >= logging.WARNING]


def expected_sql(panel: F.Panel, tier: str = "All") -> str:
    """What the dashboard itself would send: its stored query with its two variables filled in."""
    for p in G._walk(BOARDS[panel.dashboard]["dashboard"]["panels"]):
        if p["title"] == panel.title:
            target = next(t for t in p["targets"] if panel.ref in (None, t["refId"]))
            return (target["rawSql"].replace("${gpu_type:sqlstring}", "'All'")
                    .replace("${tier:sqlstring}", f"'{tier}'"))
    raise AssertionError(panel.title)


ALLOWED = {expected_sql(F.RENTALS_PANEL, t): f"rentals_{t}" for t in F.TIERS}
ALLOWED.update({expected_sql(p): p.name.removeprefix("grafana_")
                for p in (*F.SERIES_PANELS, *F.NETWORK_PANELS, *F.DAILY_PANELS)})
HISTORY = [p.name for p in (*F.NETWORK_PANELS, *F.DAILY_PANELS)]     # read after the six panels checked here


class Grafana:
    """A mock grafana.lium.io. Refuses (HTTP 400) any query that is not one of the dashboards' own."""

    def __init__(self, boards=None, answers=None, answer=None):
        self.boards = boards if boards is not None else BOARDS
        self.answers = dict(ANSWERS, **(answers or {}))
        self.answer = answer                      # optional: (name, request body) -> (status, doc) | None
        self.calls: list[httpx.Request] = []
        self.queries: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        assert request.url.host == "grafana.lium.io", request.url
        if request.method == "GET":
            uid = request.url.path.removeprefix("/api/dashboards/uid/")
            hit = self.boards.get(uid)
            if isinstance(hit, tuple):
                return httpx.Response(hit[0], json=hit[1])
            return httpx.Response(200, json=hit) if hit else httpx.Response(404, json={"message": "Dashboard not found"})
        assert request.method == "POST" and request.url.path == "/api/ds/query", request.url
        body = json.loads(request.content)
        self.queries.append(body)
        q = body["queries"][0]
        name = ALLOWED.get(q["rawSql"])
        if name is None:
            return httpx.Response(400, json={"message": "not a dashboard query"})
        if self.answer:
            hit = self.answer(name, body)
            if hit is not None:
                return httpx.Response(hit[0], json=hit[1])
        return httpx.Response(200, json=self.answers[name])

    def client(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))

    def posts(self):
        return [c for c in self.calls if c.method == "POST"]


def board_with(uid, change):
    doc = copy.deepcopy(BOARDS[uid])
    change(doc["dashboard"])
    return {**BOARDS, uid: doc}


def panel_of(dash, title):
    return next(p for p in G._walk(dash["panels"]) if p["title"] == title)


# ------------------------------------------------------------------------------------------------ the query


def test_resolve_takes_the_dashboards_own_query():
    board = BOARDS[F.MARKETPLACE_PRICES]
    for tier in F.TIERS:
        q = G.resolve(board, F.RENTALS_PANEL, {"gpu_type": "All", "tier": tier})
        assert q.sql == expected_sql(F.RENTALS_PANEL, tier), tier
        assert "$__timeFilter(rental_start_time)" in q.sql and "${" not in q.sql      # Grafana's macro is left to it
        assert q.fmt == "time_series" and q.datasource["type"] == "grafana-postgresql-datasource"
    # the hourly panels carry a one-hour minimum step, so any range up to 1,000 hours is read per hour
    for p in F.SERIES_PANELS:
        q = G.resolve(BOARDS[p.dashboard], p, {"gpu_type": "All", "tier": "All"})
        assert q.sql == expected_sql(p) and q.min_interval_ms == G.HOUR_MS, p.name
        assert q.interval_ms(NOW - timedelta(hours=4), NOW) == G.HOUR_MS
        assert q.interval_ms(NOW - timedelta(days=G.SERIES_CHUNK_DAYS), NOW) == G.HOUR_MS


def test_resolve_refuses_what_it_does_not_know():
    def code(boards, tier="All"):
        try:
            G.resolve(boards[F.MARKETPLACE_PRICES], F.RENTALS_PANEL, {"gpu_type": "All", "tier": tier})
        except G.PanelError as exc:
            return exc.code
        return None

    title = F.RENTALS_PANEL.title
    assert code(BOARDS) is None
    renamed = board_with(F.MARKETPLACE_PRICES, lambda d: panel_of(d, title).update(title="Rented-at price"))
    assert code(renamed) == "panel_not_found"
    extra = board_with(F.MARKETPLACE_PRICES, lambda d: panel_of(d, title)["targets"][0].update(
        rawSql=panel_of(d, title)["targets"][0]["rawSql"] + " AND region = ${region:sqlstring}"))
    assert code(extra) == "unknown_variable"
    plain = board_with(F.MARKETPLACE_PRICES, lambda d: panel_of(d, title)["targets"][0].update(
        rawSql=panel_of(d, title)["targets"][0]["rawSql"] + " AND country = $country"))
    assert code(plain) == "unknown_variable"

    def drop_spot(d):
        tier = next(v for v in d["templating"]["list"] if v["name"] == "tier")
        tier["options"] = [o for o in tier["options"] if o["value"] != "spot"]
    assert code(board_with(F.MARKETPLACE_PRICES, drop_spot), "spot") == "value_not_listed"
    assert code(board_with(F.MARKETPLACE_PRICES, drop_spot), "secure") is None

    # a query that stops filtering by tier must not be read as "spot": every rental would be labelled spot
    def unfilter(d):
        t = panel_of(d, title)["targets"][0]
        t["rawSql"] = t["rawSql"].replace(" AND (${tier:sqlstring} = 'All' OR tier = ${tier:sqlstring})", "")
    assert code(board_with(F.MARKETPLACE_PRICES, unfilter), "spot") == "filter_missing"
    assert code(board_with(F.MARKETPLACE_PRICES, unfilter), "All") is None

    two = board_with(F.MARKETPLACE_PRICES, lambda d: panel_of(d, title)["targets"].append(
        dict(panel_of(d, title)["targets"][0], refId="B")))
    assert code(two) == "panel_reshaped"
    assert code({F.MARKETPLACE_PRICES: {"message": "x"}}) == "no_dashboard"


# ------------------------------------------------------------------------------------------------ parsing


def _doc(name, panel):
    return G.compact(ANSWERS[name]["results"]["A"]["frames"], panel=panel, sha="x", start=NOW, end=NOW)


def test_compact_and_parse_rentals():
    frame = ANSWERS["rentals_All"]["results"]["A"]["frames"][0]
    points = sum(1 for col in frame["data"]["values"][1:] for v in col if v is not None)
    doc = _doc("rentals_All", F.RENTALS_PANEL)
    assert len(doc["cells"]) == points == 194 and doc["labels"] == sorted(doc["labels"])
    rows, rejected = G.rentals_from(doc)
    assert len(rows) == 194 and rejected == 0 and len({r["row_key"] for r in rows}) == 194
    # the first point by hand: 2026-11-15T00:40:14.918 plus 210,000 ns, as the frame's `nanos` says
    t0, ns0 = frame["data"]["values"][0][0], frame["data"]["nanos"][0][0]
    assert (t0, ns0) == (1794703214918, 210000)
    first = min(rows, key=lambda r: r["start_at"])
    assert first["start_at"] == datetime(2026, 11, 15, 0, 40, 14, 918210, tzinfo=UTC), first
    assert all(r["price_per_gpu_hour"] > 0 and r["gpu_model"].startswith("NVIDIA") for r in rows)
    assert min(r["price_per_gpu_hour"] for r in rows) == D("0.09") and max(r["price_per_gpu_hour"] for r in rows) == D("8.89")
    # the spot and secure reads are the same rentals, split: same keys, nothing left over
    keys = {t: {r["row_key"] for r in G.rentals_from(_doc(f"rentals_{t}", F.RENTALS_PANEL))[0]} for t in F.TIERS}
    assert (len(keys["spot"]), len(keys["secure"])) == (51, 143)
    assert keys["spot"] | keys["secure"] == keys["All"] and not keys["spot"] & keys["secure"]
    # the body does not depend on the order the columns arrive in
    shuffled = copy.deepcopy(frame)
    order = [0] + list(range(len(frame["schema"]["fields"]) - 1, 0, -1))
    shuffled["schema"]["fields"] = [frame["schema"]["fields"][i] for i in order]
    shuffled["data"]["values"] = [frame["data"]["values"][i] for i in order]
    shuffled["data"]["nanos"] = [frame["data"]["nanos"][i] for i in order]
    assert G.compact([shuffled], panel=F.RENTALS_PANEL, sha="x", start=NOW, end=NOW) == doc
    # identical to the nanosecond: two rentals, two keys
    twice = {"labels": ["NVIDIA H200"], "cells": [[1794703214918, 0, 2.5, 7], [1794703214918, 0, 2.5, 7]]}
    assert len({r["row_key"] for r in G.rentals_from(twice)[0]}) == 2
    # not a rental: no number, a negative price, an empty model, a label index out of range
    bad = {"labels": ["NVIDIA H200", ""], "cells": [[1, 0, "x"], [1, 0, -1.0], [1, 1, 2.0], [1, 5, 2.0], [1, 0, 3.0]]}
    rows, rejected = G.rentals_from(bad)
    assert len(rows) == 1 and rejected == 4
    assert G.compact(ANSWERS["empty"]["results"]["A"]["frames"], panel=F.RENTALS_PANEL, sha="x", start=NOW, end=NOW)["cells"] == []


def test_parse_series():
    doc = _doc("spend", F.GRAFANA_PANELS["grafana_spend"])
    rows, rejected = G.series_from(doc)
    assert rejected == 0 and len(rows) == 500 and len({r["gpu_model"] for r in rows}) == 100
    assert {r["hour_utc"] for r in rows} == {datetime(2026, 11, 15, h, tzinfo=UTC) for h in range(2, 7)}
    h100 = [r for r in rows if r["gpu_model"] == "NVIDIA H100 80GB HBM3"]
    frame = ANSWERS["spend"]["results"]["A"]["frames"][0]
    col = next(i for i, f in enumerate(frame["schema"]["fields"]) if (f.get("labels") or {}).get("gpu_type") == "NVIDIA H100 80GB HBM3")
    assert [float(r["value"]) for r in sorted(h100, key=lambda r: r["hour_utc"])] == \
        [round(v, 6) for v in frame["data"]["values"][col]]
    # a point off the hour is a different quantity (another step): never stored as an hour
    off = {"labels": ["NVIDIA H200"], "cells": [[1794708000000, 0, 5.0], [1794708000000 + 60_000, 0, 6.0]]}
    rows, rejected = G.series_from(off)
    assert len(rows) == 1 and rejected == 1 and rows[0]["hour_utc"] == datetime(2026, 11, 15, 2, tzinfo=UTC)


# ------------------------------------------------------------------------------------------------ the hourly job


def test_poll_stores_rentals_with_their_tier():
    wipe()
    g = Grafana()
    out = run(G.poll(g.client(), now=NOW, gap=0))
    assert [(r["feed"], r.get("tier"), r["state"]) for r in out["panels"]] == [
        ("grafana_rentals", "spot", "ok"), ("grafana_rentals", "secure", "ok"), ("grafana_rentals", "All", "ok"),
        ("grafana_listed", None, "ok"), ("grafana_rented", None, "ok"), ("grafana_spend", None, "ok"),
        *((name, None, "ok") for name in HISTORY)], out
    # three dashboards read once each, eleven queries, each one of the dashboards' own
    assert sorted(c.url.path for c in g.calls if c.method == "GET") == sorted(
        f"/api/dashboards/uid/{u}" for u in BOARDS)
    assert len(g.posts()) == 11 and all(q["queries"][0]["rawSql"] in ALLOWED for q in g.queries)
    # nothing stored yet: the first window is the last six hours; the hourly panels start on the hour
    first = g.queries[0]
    assert (int(first["from"]), int(first["to"])) == (int((NOW - timedelta(hours=6)).timestamp() * 1000), int(NOW.timestamp() * 1000))
    assert int(g.queries[3]["from"]) % G.HOUR_MS == 0 and g.queries[3]["queries"][0]["intervalMs"] == G.HOUR_MS

    assert count(LiumGrafanaRental) == 194
    assert count(LiumGrafanaRental, LiumGrafanaRental.tier == "spot") == 51
    assert count(LiumGrafanaRental, LiumGrafanaRental.tier == "secure") == 143
    assert count(LiumGrafanaRental, LiumGrafanaRental.tier.is_(None)) == 0
    # each rental is stored by its tier read; the unfiltered read then finds nothing left to add
    assert [(r["new"], r["tier_named"]) for r in out["panels"][:3]] == [(51, 0), (143, 0), (0, 0)], out["panels"][:3]
    H = LiumGrafanaHourly
    assert count(H) == HOURLY_ROWS and 157 <= HOURLY_ROWS < 500
    assert count(H, H.gpus_listed.is_not(None)) == 157 and count(H, H.gpus_rented.is_not(None)) == 157
    # spend: every value above zero is stored; a zero only on a row the fleet made; a model with neither has no row
    assert count(H, H.spend_usd_per_hour > 0) == sum(1 for v in SPEND.values() if v != 0)
    assert count(H, H.spend_usd_per_hour == 0) == sum(1 for k, v in SPEND.items() if v == 0 and k in FLEET) > 0
    assert sum(1 for k, v in SPEND.items() if v == 0 and k not in FLEET) > 0
    assert count(LiumFeedSnapshot) == 11
    for model in (LiumGrafanaRental, LiumGrafanaHourly, LiumFeedSnapshot):
        assert count(model, model.visibility != "internal") == 0, model.__tablename__

    # the same answers again: nothing new anywhere; the eight hourly answers are seen twice (the three daily
    # panels were read less than six hours ago and are not asked again)
    again = run(G.poll(Grafana().client(), now=NOW + timedelta(hours=1), gap=0))
    assert not any(r["changed"] for r in again["panels"]) and count(LiumGrafanaRental) == 194
    assert len(again["panels"]) == 8
    assert count(LiumFeedSnapshot) == 11 and count(LiumFeedSnapshot, LiumFeedSnapshot.seen_count == 2) == 8

    # one more rental arrives (a secure H200 at 02:00): one new row, tier named by the secure read
    def plus_one(name):
        doc = copy.deepcopy(ANSWERS[name])
        frame = doc["results"]["A"]["frames"][0]
        col = next(i for i, f in enumerate(frame["schema"]["fields"]) if f["name"] == "NVIDIA H200")
        for i, values in enumerate(frame["data"]["values"]):
            values.append(1794708000000 if i == 0 else (3.33 if i == col else None))
        frame["data"]["nanos"][0].append(0)
        return doc
    more = Grafana(answers={"rentals_All": plus_one("rentals_All"), "rentals_secure": plus_one("rentals_secure")})
    third = run(G.poll(more.client(), now=NOW + timedelta(hours=2), gap=0))
    spot, secure, everything = third["panels"][:3]
    assert (spot["changed"], secure["new"], everything["changed"], everything["new"]) == (False, 1, True, 0)
    with normalize.SessionLocal() as s:
        row = s.execute(select(LiumGrafanaRental).where(LiumGrafanaRental.price_per_gpu_hour == D("3.33"))).scalar_one()
    assert (row.gpu_model, row.tier, row.start_at) == ("NVIDIA H200", "secure", datetime(2026, 11, 15, 2, tzinfo=UTC))
    assert count(LiumGrafanaRental) == 195
    # the newest stored rental started at 06:27:40; two hours on, the next read starts one hour before it
    newest = max(ANSWERS["rentals_All"]["results"]["A"]["frames"][0]["data"]["values"][0])
    assert int(more.queries[0]["from"]) == newest - G.HOUR_MS
    assert int(more.queries[0]["to"]) == int((NOW + timedelta(hours=2)).timestamp() * 1000)


def test_series_update_in_place():
    wipe()
    run(G.poll(Grafana().client(), now=NOW, gap=0))
    H = LiumGrafanaHourly

    def value(hour):
        with normalize.SessionLocal() as s:
            return s.execute(select(H.gpus_rented, H.gpus_listed, H.updated_at).where(
                H.gpu_model == "NVIDIA H200", H.hour_utc == hour)).one()
    running, closed = datetime(2026, 11, 15, 6, tzinfo=UTC), datetime(2026, 11, 15, 5, tzinfo=UTC)
    before, before_closed = value(running), value(closed)
    moved = copy.deepcopy(ANSWERS["rented"])
    frame = moved["results"]["A"]["frames"][0]
    col = next(i for i, f in enumerate(frame["schema"]["fields"]) if f["name"] == "NVIDIA H200")
    frame["data"]["values"][col][-1] += 7                       # the running hour (06:00) moves
    out = run(G.poll(Grafana(answers={"rented": moved}).client(), now=NOW + timedelta(minutes=30), gap=0))
    assert [r["written"] for r in out["panels"][3:6]] == [0, 1, 0], out["panels"][3:6]
    after = value(running)
    assert after[0] == before[0] + 7 and after[1] == before[1] and after[2] > before[2]
    assert value(closed) == before_closed                       # an hour that did not move is not rewritten
    assert count(H) == HOURLY_ROWS

    # a zero spend never makes a row of its own, and a spend that falls to zero is written on the row it has
    wipe()
    at, hour = NOW, datetime(2026, 11, 15, 5, tzinfo=UTC)
    row = lambda model, v: {"hour_utc": hour, "gpu_model": model, "value": D(v)}  # noqa: E731
    with normalize.SessionLocal.begin() as s:
        assert G.write_series(s, "gpus_listed", [row("NVIDIA H200", "40")], "internal", at) == 1
        assert G.write_series(s, "spend_usd_per_hour", [row("NVIDIA H200", "0"), row("NVIDIA T4", "0"),
                                                         row("NVIDIA L4", "1.5")], "internal", at) == 2
        assert G.write_series(s, "spend_usd_per_hour", [row("NVIDIA L4", "0"), row("NVIDIA H200", "0")], "internal", at) == 1
    with normalize.SessionLocal() as s:
        got = {r[0]: (r[1], r[2]) for r in s.execute(select(H.gpu_model, H.spend_usd_per_hour, H.gpus_listed))}
    assert got == {"NVIDIA H200": (D("0"), D("40")), "NVIDIA L4": (D("0"), None)}, got


def test_failures_are_one_row_and_one_warning():
    wipe()
    # a renamed panel: nothing is sent for it, and two polls leave one row
    renamed = board_with(F.MARKETPLACE_PRICES, lambda d: panel_of(d, F.RENTALS_PANEL.title).update(title="Paid"))
    with Logs() as logs:
        g = Grafana(boards=renamed)
        a = run(G.poll(g.client(), now=NOW, gap=0))
        b = run(G.poll(Grafana(boards=renamed).client(), now=NOW + timedelta(hours=1), gap=0))
    assert [r["state"] for r in a["panels"][:3]] == ["error"] * 3 and "panel_not_found" in a["panels"][0]["error"]
    assert [r["state"] for r in a["panels"][3:]] == ["ok"] * 8            # every other panel is unaffected
    assert len(g.posts()) == 8 and count(LiumGrafanaRental) == 0
    assert count(LiumFeedSnapshot, LiumFeedSnapshot.feed == "grafana_rentals") == 3
    assert count(LiumFeedSnapshot, LiumFeedSnapshot.feed == "grafana_rentals", LiumFeedSnapshot.seen_count == 2) == 3
    assert not any(r["changed"] for r in b["panels"][:3])
    assert len([w for w in logs.warnings() if "grafana_rentals" in w]) == 3, logs.warnings()   # once per tier read

    wipe()
    # the dashboard is gone (404), then a query Grafana refuses, then an answer that carries an error
    gone = run(G.poll(Grafana(boards={}).client(), now=NOW, gap=0))
    assert {r["state"] for r in gone["panels"]} == {"not_found"} and count(LiumGrafanaRental) == 0
    refused = run(G.poll(Grafana(answer=lambda name, body: (500, {"results": {"A": {"error": "db query error", "status": 500}}})
                                 if name == "spend" else None).client(), now=NOW, gap=0))
    spend = refused["panels"][5]
    assert spend["state"] == "error" and spend["status_code"] == 500 and "db query error" in spend["error"], spend
    broken = run(G.poll(Grafana(answer=lambda name, body: (200, {"results": {"A": {"error": "timeout", "status": 200}}})
                                if name == "listed" else None).client(), now=NOW, gap=0))
    assert broken["panels"][3]["state"] == "error" and "timeout" in broken["panels"][3]["error"]
    assert count(LiumGrafanaHourly, LiumGrafanaHourly.gpus_listed.is_not(None)) == 157    # the earlier good read stays
    # an empty window is `empty`, not a failure
    empty = run(G.poll(Grafana(answers={"rentals_spot": ANSWERS["empty"]}).client(), now=NOW, gap=0))
    assert empty["panels"][0]["state"] == "empty" and empty["panels"][0]["error"] is None


def test_poll_window_follows_the_newest_point():
    hours = lambda n: timedelta(hours=n)  # noqa: E731
    assert G._window(None, NOW) == (NOW - hours(6), NOW)
    assert G._window(NOW - timedelta(minutes=30), NOW) == (NOW - hours(3), NOW)       # never under three hours
    assert G._window(NOW - hours(10), NOW) == (NOW - hours(11), NOW)                  # an hour before the newest
    assert G._window(NOW - timedelta(days=10), NOW) == (NOW - hours(72), NOW)         # capped; the backfill does the rest
    wipe()
    w = G.windows(NOW)
    assert w["grafana_rentals"] == (NOW - hours(6), NOW)
    assert w["grafana_spend"] == (datetime(2026, 11, 15, 0, tzinfo=UTC), NOW)         # whole hours only


# ------------------------------------------------------------------------------------------------ backfill


def test_backfill_walks_back_and_resumes():
    # Chunk dates are in the past on purpose: a chunk counts as done only when it was read after it closed.
    wipe()
    day = lambda d: datetime(2026, 6, d, tzinfo=UTC)  # noqa: E731
    data_from = int(day(10).timestamp() * 1000)

    def answer(name, body):                       # the venue has data from 10 June on; older windows are empty
        return None if int(body["to"]) > data_from else (200, ANSWERS["empty"])
    g = Grafana(answer=answer)
    out = run(G.backfill(until=date(2026, 6, 15), only="rentals", http=g.client(), gap=0))
    t = out["grafana_rentals"]
    # 3-day chunks back from 16 June: 13-16 and 10-13 have data, 7-10 and 4-7 are empty, then it stops
    assert (t["chunks"], t["oldest"], t["stopped"]) == (4, "2026-06-10", "2 empty chunks in a row"), t
    assert len(g.posts()) == 12 and count(LiumGrafanaRental) == 194       # the same 194 rentals in both chunks: once
    assert count(LiumGrafanaRental, LiumGrafanaRental.tier.is_(None)) == 0
    froms = sorted({datetime.fromtimestamp(int(q["from"]) / 1000, UTC).date().isoformat() for q in g.queries})
    assert froms == ["2026-06-04", "2026-06-07", "2026-06-10", "2026-06-13"], froms
    assert count(LiumFeedSnapshot, LiumFeedSnapshot.state == "empty") == 6

    # run again: every chunk closed and stored, so nothing is asked
    again = Grafana(answer=answer)
    out = run(G.backfill(until=date(2026, 6, 15), only="rentals", http=again.client(), gap=0))
    assert not again.posts() and out["grafana_rentals"]["skipped"] == 12 and out["grafana_rentals"]["stopped"], out
    # --force asks again; --since stops exactly there
    forced = Grafana(answer=answer)
    out = run(G.backfill(since=date(2026, 6, 12), until=date(2026, 6, 15), only="rentals", force=True,
                         http=forced.client(), gap=0))
    assert len(forced.posts()) == 6 and out["grafana_rentals"]["stopped"] == "reached --since"
    assert sorted({q["from"] for q in forced.queries}) == [str(int(day(d).timestamp() * 1000)) for d in (12, 13)]
    # the chunk that holds today is still open: it is read again on every run
    wipe()
    today = datetime.now(UTC).date()
    for _ in range(2):
        open_ = Grafana()
        run(G.backfill(since=today - timedelta(days=1), only="rentals", http=open_.client(), gap=0))
        assert len(open_.posts()) == 3

    # the hourly series: 14-day chunks, read per hour
    wipe()
    s = Grafana(answer=answer)
    out = run(G.backfill(since=date(2026, 6, 2), until=date(2026, 6, 15), only="series", http=s.client(), gap=0))
    assert set(out) == {p.name for p in F.SERIES_PANELS} and len(s.posts()) == 3
    assert all(q["queries"][0]["intervalMs"] == G.HOUR_MS for q in s.queries)
    assert count(LiumGrafanaHourly) == HOURLY_ROWS
    # three failed chunks in a row stop a panel
    wipe()
    down = Grafana(answer=lambda name, body: (502, {"message": "bad gateway"}))
    out = run(G.backfill(until=date(2026, 6, 15), only="rentals", http=down.client(), gap=0))
    assert out["grafana_rentals"]["stopped"] == "3 failed chunks in a row" and out["grafana_rentals"]["errors"] == 3


def test_reparse_rebuilds_from_stored_bodies():
    wipe()
    run(G.poll(Grafana().client(), now=NOW, gap=0))
    with normalize.SessionLocal.begin() as s:
        s.execute(text("TRUNCATE lium_grafana_rentals, lium_grafana_hourly"))
    out = G.reparse()
    assert sum(out["snapshots"].values()) == 11
    assert count(LiumGrafanaRental) == 194 and count(LiumGrafanaRental, LiumGrafanaRental.tier == "spot") == 51
    assert count(LiumGrafanaHourly) == HOURLY_ROWS


# ------------------------------------------------------------------------------------------------ reads


def test_stats_by_hand():
    wipe()
    run(G.poll(Grafana().client(), now=NOW, gap=0))
    rows, _ = G.rentals_from(_doc("rentals_secure", F.RENTALS_PANEL))
    mine = sorted(float(r["price_per_gpu_hour"]) for r in rows if r["gpu_model"] == "NVIDIA GeForce RTX 4090")
    assert len(mine) >= 3
    saved = reads.datetime
    try:
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return NOW
        reads.datetime = Clock
        stats = reads.grafana_rental_stats(days=2, gpu="NVIDIA GeForce RTX 4090", tier="secure")
        assert len(stats) == 1, stats
        got = stats[0]
        assert got["day"] == date(2026, 11, 15) and got["tier"] == "secure" and got["rentals"] == len(mine)
        assert got["median"] == round(statistics.median(mine), 4) and got["mean"] == round(statistics.fmean(mine), 4)
        assert (float(got["low"]), float(got["high"])) == (mine[0], mine[-1])
        assert got["canonical_gpu_name"] == "NVIDIA RTX 4090 24GB"
        by_hour = reads.grafana_rental_stats(days=2, gpu="NVIDIA GeForce RTX 4090", tier="secure", by="hour")
        assert sum(r["rentals"] for r in by_hour) == len(mine) and all("hour_utc" in r for r in by_hour)
        both = reads.grafana_rental_stats(days=2, gpu="NVIDIA GeForce RTX 4090")
        assert {r["tier"] for r in both} <= {"spot", "secure"} and sum(r["rentals"] for r in both) >= len(mine)
        spend = reads.grafana_hourly("spend_usd_per_hour", hours=24, gpu="NVIDIA H200")
        assert [r["hour_utc"].hour for r in spend] == [6, 5, 4, 3, 2] and all(r["value"] >= 0 for r in spend)
    finally:
        reads.datetime = saved
    st = reads.status()
    assert {f["feed"] for f in st["feeds"]} >= set(F.GRAFANA_PANELS)
    mine_feed = next(f for f in st["feeds"] if f["feed"] == "grafana_rentals")
    assert mine_feed["state"] == "ok" and mine_feed["visibility"] == "internal" and "Rented-at price" in mine_feed["path"]
    assert st["rows"]["lium_grafana_rentals"] == 194 and st["grafana_rentals_start_range"]["first"] is not None


def test_registry_internal():
    for p in F.GRAFANA_PANELS.values():
        assert provider_meta.visibility("lium", p.path, "signals") == "internal", p.name
        assert raw.visibility(p) == "internal"
    assert {p.path for p in F.GRAFANA_PANELS.values()} == {
        e.source for e in provider_meta.internal_entries("lium") if e.source.startswith("grafana.lium.io")}
    assert provider_meta.is_public_provider("lium")               # Lium's prices and capacity stay public


def _basic(user, pw):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


def test_admin_endpoints_and_job():
    wipe()
    run(G.poll(Grafana().client(), now=datetime.now(UTC), gap=0))
    from fastapi.testclient import TestClient

    import jobs
    import main
    from accounts import security
    from lium_data import schedule

    client = TestClient(main.app, headers={"X-OpenGrid-Request": "1"})
    saved = (settings.app_password, settings.public_pages, settings.viewer_user, settings.viewer_password,
             settings.environment, settings.poller_enabled, settings.lium_grafana_enabled)
    try:
        security.reset()
        settings.app_password, settings.environment = "pw", "development"
        settings.viewer_user, settings.viewer_password = "vc", "look-only"
        settings.public_pages = True
        op, viewer = _basic("opengrid", "pw"), _basic("vc", "look-only")
        for p in ("/v1/ops/lium/grafana/rentals", "/v1/ops/lium/grafana/hourly"):
            assert client.get(p).status_code == 401, p
            assert client.get(p, headers=viewer).status_code == 403, p
        r = client.get("/v1/ops/lium/grafana/rentals?days=730", headers=op)
        assert r.status_code == 200, r.text
        assert r.json()["meta"]["visibility"] == "internal" and r.json()["meta"]["kind"] == "transaction"
        assert client.get("/v1/ops/lium/grafana/rentals?tier=cheap", headers=op).status_code == 422
        assert client.get("/v1/ops/lium/grafana/rentals?by=week", headers=op).status_code == 422
        r = client.get("/v1/ops/lium/grafana/hourly?metric=gpus_listed&hours=9600", headers=op)
        assert r.status_code == 200 and r.json()["meta"]["kind"] == "observed", r.text
        assert client.get("/v1/ops/lium/grafana/hourly?metric=nope", headers=op).status_code == 404

        # the job: on with the Lium feeds, off by its own switch
        settings.poller_enabled = True
        for on, want in ((True, ["lium_public", "lium_providers", "lium_grafana"]), (False, ["lium_public", "lium_providers"])):
            schedule._registered, settings.lium_grafana_enabled = False, on
            for name in ("lium_public", "lium_providers", "lium_grafana", "lium_partner"):
                jobs.JOBS.pop(name, None)
            names = schedule.register()
            assert [n for n in names if n != "lium_partner"] == want and ("lium_grafana" in jobs.JOBS) is on
        for name in ("lium_public", "lium_providers", "lium_grafana", "lium_partner"):
            jobs.JOBS.pop(name, None)
    finally:
        (settings.app_password, settings.public_pages, settings.viewer_user, settings.viewer_password,
         settings.environment, settings.poller_enabled, settings.lium_grafana_enabled) = saved


def test_migration_0041():
    from alembic import command
    from sqlalchemy import create_engine

    import db as dbmod
    name = DB + "_mig"
    url = scratchdb.create(name)
    old = settings.database_url
    settings.database_url = url
    try:
        cfg = dbmod._alembic_config()
        command.upgrade(cfg, "0041_lium_grafana")
        eng = create_engine(url)
        with eng.connect() as c:
            from store import lium as L
            for model in (L.LiumGrafanaRental, L.LiumGrafanaHourly):
                t = model.__tablename__
                cols = {r[0]: r[1] for r in c.execute(text(
                    "SELECT column_name, is_nullable FROM information_schema.columns WHERE table_name = :t"), {"t": t})}
                assert set(cols) == {col.name for col in model.__table__.columns}, t
                assert {k for k, v in cols.items() if v == "YES"} == {col.name for col in model.__table__.columns if col.nullable}, t
                d = c.execute(text("SELECT column_default FROM information_schema.columns WHERE table_name = :t "
                                   "AND column_name = 'visibility'"), {"t": t}).scalar()
                assert "internal" in d, (t, d)
            try:
                c.execute(text("INSERT INTO lium_grafana_hourly (hour_utc, gpu_model, updated_at, visibility) "
                               "VALUES (now(), 'g', now(), 'secret')"))
                raise AssertionError("CHECK did not fire")
            except Exception as exc:  # noqa: BLE001
                assert "ck_lium_grafana_hourly_visibility" in str(exc), exc
                c.rollback()
        command.downgrade(cfg, "0040_lium_data")
        with eng.connect() as c:
            assert c.execute(text("SELECT to_regclass('public.lium_grafana_rentals')")).scalar() is None
            assert c.execute(text("SELECT to_regclass('public.lium_feed_snapshots')")).scalar() is not None
        eng.dispose()
    finally:
        settings.database_url = old
        scratchdb.drop(name)


TESTS = [test_resolve_takes_the_dashboards_own_query, test_resolve_refuses_what_it_does_not_know,
         test_compact_and_parse_rentals, test_parse_series, test_poll_stores_rentals_with_their_tier,
         test_series_update_in_place, test_failures_are_one_row_and_one_warning,
         test_poll_window_follows_the_newest_point, test_backfill_walks_back_and_resumes,
         test_reparse_rebuilds_from_stored_bodies, test_stats_by_hand, test_registry_internal,
         test_admin_endpoints_and_job, test_migration_0041]


def main():
    setup()
    failed = 0
    try:
        for fn in TESTS:
            try:
                fn()
                print(f"ok   {fn.__name__}")
            except Exception:  # noqa: BLE001
                failed += 1
                import traceback
                print(f"FAIL {fn.__name__}")
                traceback.print_exc()
    finally:
        scratchdb.drop(DB)
    print(f"{len(TESTS) - failed}/{len(TESTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
