"""Lium's public Grafana, the panels read since migration 0043 (lium_data/grafana.py): rentals created and
node-side deploy failures per hour, daily asking and paid medians per GPU model, failed deploys by cause and
penalties by reason per day.

Offline: the mock grafana.lium.io of tests/test_lium_grafana.py serves the real "Rental check" dashboard and real
panel answers captured on 2026-11-15 (tests/fixtures_lium/grafana_*). Each answer is REAL but TRIMMED, rows and
columns removed and no value changed:

    grafana_price_daily      the days 9 to 14 November and six GPU models, out of 46 days and 31 models
    grafana_launch_created   the 48 hours of 13 and 14 November, out of 14 days
    grafana_launch_failures  the same two days (23 hours had a failure)
    grafana_deploy_causes    12 to 14 November, out of 95 days
    grafana_penalties        1 to 14 November, out of 202 days

Checks that
    - a panel holding two queries is read one query at a time, by the dashboard's own refId, and an answer without
      the expected value column is a failed read;
    - the parsers give the values read by hand from the fixtures, and refuse what is not a whole hour / day / count;
    - rows are upserted in place, a repeat stores nothing, the daily panels are asked from a UTC midnight and only
      when due;
    - the backfill walks back in closed chunks that end before the closing midnight, and resumes;
    - every row is INTERNAL, the reads are admin-only, migration 0043 and the ORM agree and the head is single.

    python tests/test_lium_grafana_history.py
"""

import copy
import json
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_lium_grafana as T  # noqa: E402  (the mock Grafana; importing it isolates the process first)

import fixtures  # noqa: E402
import normalize  # noqa: E402
import provider_meta  # noqa: E402
import scratchdb  # noqa: E402
from config import settings  # noqa: E402
from lium_data import feeds as F  # noqa: E402
from lium_data import grafana as G  # noqa: E402
from lium_data import raw, reads  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
from store.lium import (LiumFeedSnapshot, LiumGrafanaDailyCount, LiumGrafanaNetworkHourly,  # noqa: E402
                        LiumGrafanaPriceDaily)

DB = "og_test_lium_grafana_history"
UTC = timezone.utc
NOW = T.NOW
P = F.GRAFANA_PANELS
CREATED, FAILURES = P["grafana_launch_created"], P["grafana_launch_failures"]
PRICE, CAUSES, PENALTIES = P["grafana_price_daily"], P["grafana_deploy_causes"], P["grafana_penalties"]
NEW = (CREATED, FAILURES, PRICE, CAUSES, PENALTIES)
Grafana, run, count, wipe = T.Grafana, T.run, T.count, T.wipe


def setup():
    normalize.SessionLocal = fixtures.session(scratchdb.create(DB))


def doc_of(panel):
    return G.compact(T.ANSWERS[panel.name.removeprefix("grafana_")]["results"]["A"]["frames"], panel=panel, sha="x",
                     start=NOW, end=NOW)


def one(model, *where):
    with normalize.SessionLocal() as s:
        return s.execute(select(model).where(*where)).scalar_one()


# ------------------------------------------------------------------------------------------------ the query


def test_two_queries_of_one_panel_are_read_by_ref():
    board = T.BOARDS[F.RENTAL_CHECK]
    panel = T.panel_of(board["dashboard"], CREATED.title)
    assert [t["refId"] for t in panel["targets"]] == ["A", "B"]          # the dashboard as captured
    values = {"gpu_type": "All", "tier": "All"}
    a, b = G.resolve(board, CREATED, values), G.resolve(board, FAILURES, values)
    assert a.sql == panel["targets"][0]["rawSql"] and b.sql == panel["targets"][1]["rawSql"] and a.sql != b.sql
    assert '"Rentals created"' in a.sql and '"Node-side deploy failures"' in b.sql
    assert a.fmt == b.fmt == "table" and a.min_interval_ms == G.HOUR_MS
    assert a.interval_ms(NOW - timedelta(days=G.SERIES_CHUNK_DAYS), NOW) == G.HOUR_MS

    def code(change, which=FAILURES):
        try:
            G.resolve(T.board_with(F.RENTAL_CHECK, change)[F.RENTAL_CHECK], which, values)
        except G.PanelError as exc:
            return exc.code
        return None
    # the refId is gone, or two queries now carry it: never a guess at which one is meant
    assert code(lambda d: T.panel_of(d, CREATED.title)["targets"][1].update(refId="C")) == "panel_reshaped"
    assert code(lambda d: T.panel_of(d, CREATED.title)["targets"][0].update(refId="B")) == "panel_reshaped"
    assert code(lambda d: T.panel_of(d, CREATED.title).update(title="Rentals")) == "panel_not_found"
    # the three daily panels hold one query each and no variable this module does not know
    for p in (PRICE, CAUSES, PENALTIES):
        q = G.resolve(T.BOARDS[p.dashboard], p, values)
        assert q.sql == T.expected_sql(p) and "$__timeFilter(" in q.sql and "${" not in q.sql, p.name


# ------------------------------------------------------------------------------------------------ parsing


def test_parse_network_hourly():
    rows, rejected = G.network_from(doc_of(CREATED), CREATED.column)
    assert (len(rows), rejected) == (48, 0)
    by_hour = {r["hour_utc"]: r["value"] for r in rows}
    # by hand from the fixture: the first three hours of 13 November and the last three of 14 November
    assert [by_hour[datetime(2026, 11, 13, h, tzinfo=UTC)] for h in (0, 1, 2)] == [121, 82, 95]
    assert [by_hour[datetime(2026, 11, 14, h, tzinfo=UTC)] for h in (21, 22, 23)] == [60, 56, 58]
    assert sum(by_hour.values()) == 3330
    rows, rejected = G.network_from(doc_of(FAILURES), FAILURES.column)
    assert (len(rows), rejected, sum(r["value"] for r in rows)) == (23, 0, 63)      # an hour with none has no row
    assert {r["hour_utc"]: r["value"] for r in rows}[datetime(2026, 11, 13, 0, tzinfo=UTC)] == 7
    # the other query's answer under this panel's name is a reshaped panel, never stored as this count
    for wrong, column in ((FAILURES, CREATED.column), (CREATED, FAILURES.column)):
        try:
            G.network_from(doc_of(wrong), column)
            raise AssertionError("read the wrong column")
        except ValueError as exc:
            assert column in str(exc)
    # off the hour, a fraction, a negative: not a count of an hour
    hour = 1794528000000
    assert hour % G.HOUR_MS == 0
    bad = {"labels": ["Rentals created"], "cells": [[hour, 0, 5], [hour + 60_000, 0, 6], [hour + G.HOUR_MS, 0, 1.5],
                                                    [hour + 2 * G.HOUR_MS, 0, -1]]}
    rows, rejected = G.network_from(bad, "Rentals created")
    assert [r["value"] for r in rows] == [5] and rejected == 3
    assert G.network_from({"labels": [], "cells": []}, "Rentals created") == ([], 0)       # an empty window


def test_parse_price_daily():
    doc = doc_of(PRICE)
    assert len(doc["cells"]) == 53 and len(doc["labels"]) == 10
    rows, rejected = G.price_daily_from(doc)
    assert (len(rows), rejected) == (53, 0)
    got = {(r["day"], r["gpu_model"], r["column"]): r["value"] for r in rows}
    day = lambda d: date(2026, 11, d)  # noqa: E731
    # by hand from the fixture
    assert [got[(day(d), "NVIDIA H200", "median_asking")] for d in range(9, 15)] == \
        [D("4.15"), D("4.15"), D("4.06"), D("4.08"), D("4.2"), D("4.2")]
    assert [got[(day(d), "NVIDIA H200", "median_paid")] for d in range(9, 15)] == \
        [D("4"), D("4.08"), D("4.08"), D("3.85"), D("4.04"), D("4.08")]
    assert got[(day(9), "NVIDIA GeForce RTX 4090", "median_paid")] == D("0.425")        # 0.42500000000000004
    assert got[(day(14), "NVIDIA H100 80GB HBM3", "median_asking")] == D("2.65")
    assert got[(day(14), "NVIDIA H100 80GB HBM3", "median_paid")] == D("1.7")
    # a model with fewer than three unrented nodes has no asking median that day, and the other way round
    assert {k[0] for k in got if k[1] == "NVIDIA GeForce RTX 4070 Ti SUPER"} == {day(d) for d in range(11, 15)}
    assert not any(k[1] == "NVIDIA GeForce RTX 4070 Ti SUPER" and k[2] == "median_asking" for k in got)
    assert [k for k in got if k[1] == "NVIDIA RTX A4000"] == [(day(10), "NVIDIA RTX A4000", "median_asking")]
    assert len({(k[0], k[1]) for k in got}) == 29                                       # table rows
    # not a daily median: off midnight, a series named another way, a negative price
    midnight = 1794528000000
    assert midnight % G.DAY_MS == 0
    bad = {"labels": ["NVIDIA H200 - asking, unrented", "NVIDIA H200", " - paid, rented"],
           "cells": [[midnight, 0, 4.0], [midnight + G.HOUR_MS, 0, 4.1], [midnight, 1, 4.2], [midnight, 2, 4.3],
                     [midnight + G.DAY_MS, 0, -1.0]]}
    rows, rejected = G.price_daily_from(bad)
    assert [(r["gpu_model"], r["column"], r["value"]) for r in rows] == [("NVIDIA H200", "median_asking", D("4"))]
    assert rejected == 4
    # every series renamed: a reshaped panel, not an empty day
    try:
        G.price_daily_from({"labels": ["NVIDIA H200 asking"], "cells": [[midnight, 0, 4.0]]})
        raise AssertionError("read a renamed series")
    except ValueError:
        pass


def test_parse_daily_counts():
    # a table answer (day, cause, n): each row's cause is the label of its count, so the pairs stay together
    frame = T.ANSWERS["deploy_causes"]["results"]["A"]["frames"][0]
    assert [f["type"] for f in frame["schema"]["fields"]] == ["time", "string", "number"]
    doc = doc_of(CAUSES)
    assert len(doc["cells"]) == 38 and "node: rental_disk_short" in doc["labels"] and "n" not in doc["labels"]
    rows, rejected = G.counts_from(doc)
    assert (len(rows), rejected) == (38, 0)
    got = {(r["day"], r["label"]): r["value"] for r in rows}
    day = lambda d: date(2026, 11, d)  # noqa: E731
    # by hand from the fixture
    assert got[(day(12), "node: rental_disk_short")] == 54 and got[(day(12), "node: unreachable")] == 43
    assert got[(day(13), "renter: deleted while starting")] == 278 and got[(day(13), "node: docker_pull")] == 9
    assert got[(day(14), "node: docker_run")] == 8
    assert [sum(v for (d, _), v in got.items() if d == day(n)) for n in (12, 13, 14)] == [180, 329, 21]
    shuffled = copy.deepcopy(frame)                 # the stored body does not depend on the order of the rows
    for col in shuffled["data"]["values"]:
        col.reverse()
    assert G.compact([shuffled], panel=CAUSES, sha="x", start=NOW, end=NOW) == doc

    rows, rejected = G.counts_from(doc_of(PENALTIES))
    assert (len(rows), rejected) == (36, 0)         # MANUAL has no value in these 14 days: no row
    got = {(r["day"], r["label"]): r["value"] for r in rows}
    assert [got[(day(d), "EXECUTOR_INACTIVE_MID_RENTAL")] for d in (1, 2, 11, 14)] == [6, 32, 45, 18]
    assert got[(day(3), "BROKEN_BY_PROVIDER")] == 22 and got[(day(7), "PROVIDER_KILL")] == 1
    assert (day(6), "BROKEN_BY_PROVIDER") not in got and not any(k[1] == "MANUAL" for k in got)


# ------------------------------------------------------------------------------------------------ the hourly job


def test_poll_stores_and_updates_in_place():
    wipe()
    g = Grafana()
    out = run(G.poll(g.client(), now=NOW, gap=0))
    mine = {r["feed"]: r for r in out["panels"] if r["feed"] in {p.name for p in NEW}}
    assert {k: (v["state"], v["rows"], v["written"]) for k, v in mine.items()} == {
        "grafana_launch_created": ("ok", 48, 48), "grafana_launch_failures": ("ok", 23, 23),
        "grafana_price_daily": ("ok", 53, 53), "grafana_deploy_causes": ("ok", 38, 38),
        "grafana_penalties": ("ok", 36, 36)}, mine
    N, PD, C = LiumGrafanaNetworkHourly, LiumGrafanaPriceDaily, LiumGrafanaDailyCount
    assert count(N) == 48 and count(N, N.node_deploy_failures.is_not(None)) == 23
    assert count(PD) == 29 and count(PD, PD.median_asking.is_not(None)) == 25 and count(PD, PD.median_paid.is_not(None)) == 28
    assert count(C, C.series == "deploy_failures") == 38 and count(C, C.series == "penalties") == 36
    row = one(N, N.hour_utc == datetime(2026, 11, 13, 0, tzinfo=UTC))
    assert (row.rentals_created, row.node_deploy_failures) == (121, 7)
    quiet = one(N, N.hour_utc == datetime(2026, 11, 14, 5, tzinfo=UTC))            # rentals, and no failure row
    assert quiet.rentals_created == 49 and quiet.node_deploy_failures is None
    h200 = one(PD, PD.day == date(2026, 11, 12), PD.gpu_model == "NVIDIA H200")
    assert (h200.median_asking, h200.median_paid) == (D("4.08"), D("3.85"))
    for model in (N, PD, C):
        assert count(model, model.visibility != "internal") == 0, model.__tablename__
    assert count(LiumFeedSnapshot, LiumFeedSnapshot.feed.in_([p.name for p in NEW]),
                 LiumFeedSnapshot.visibility == "internal") == 5

    # what was asked: the hourly counts per whole hour; each daily panel from a UTC midnight (a window that began
    # mid-day would return a partial first day and overwrite the full one)
    asked = {T.ALLOWED[q["queries"][0]["rawSql"]]: q for q in g.queries}
    assert int(asked["launch_created"]["from"]) % G.HOUR_MS == 0
    assert asked["launch_created"]["queries"][0]["intervalMs"] == G.HOUR_MS
    assert asked["launch_failures"]["queries"][0]["format"] == "table"
    yesterday = int(datetime(2026, 11, 14, tzinfo=UTC).timestamp() * 1000)
    for name in ("price_daily", "deploy_causes", "penalties"):
        assert int(asked[name]["from"]) == yesterday, name
        assert int(asked[name]["to"]) == int(NOW.timestamp() * 1000)

    # an hour later: the hourly counts are asked again, the daily panels are not due, nothing is rewritten
    again = Grafana()
    out = run(G.poll(again.client(), now=NOW + timedelta(hours=1), gap=0))
    assert [r["feed"] for r in out["panels"][6:]] == ["grafana_launch_created", "grafana_launch_failures"]
    assert not any(r["changed"] or r["written"] for r in out["panels"][6:])
    assert {T.ALLOWED[q["queries"][0]["rawSql"]] for q in again.queries} & {"price_daily", "deploy_causes", "penalties"} == set()
    # the next read of the hourly counts starts one hour before the newest stored hour (14 November 23:00)
    w = G.windows(NOW)
    assert w["grafana_launch_created"] == (datetime(2026, 11, 14, 22, tzinfo=UTC), NOW)
    assert w["grafana_launch_failures"] == (datetime(2026, 11, 14, 22, tzinfo=UTC), NOW)
    assert w["grafana_price_daily"] == (datetime(2026, 11, 13, tzinfo=UTC), NOW)     # newest stored day: 14 November

    # the running hour moves: one row rewritten, the others left alone
    moved = copy.deepcopy(T.ANSWERS["launch_created"])
    moved["results"]["A"]["frames"][0]["data"]["values"][1][-1] += 4
    before = one(N, N.hour_utc == datetime(2026, 11, 14, 22, tzinfo=UTC)).updated_at
    out = run(G.poll(Grafana(answers={"launch_created": moved}).client(), now=NOW + timedelta(hours=2), gap=0))
    assert [r["written"] for r in out["panels"][6:]] == [1, 0]
    last = one(N, N.hour_utc == datetime(2026, 11, 14, 23, tzinfo=UTC))
    assert (last.rentals_created, last.node_deploy_failures) == (62, 2)             # 58 + 4; the failures stay
    assert one(N, N.hour_utc == datetime(2026, 11, 14, 22, tzinfo=UTC)).updated_at == before

    # six hours on the daily panels are due again; a median that moved is updated in place, a count too
    wipe()
    run(G.poll(Grafana().client(), now=NOW, gap=0))
    with normalize.SessionLocal.begin() as s:       # as if the stored polls were six hours old
        s.execute(text("UPDATE lium_feed_snapshots SET last_seen_at = last_seen_at - interval '6 hours'"))
    price, causes = copy.deepcopy(T.ANSWERS["price_daily"]), copy.deepcopy(T.ANSWERS["deploy_causes"])
    frame = price["results"]["A"]["frames"][0]
    col = next(i for i, f in enumerate(frame["schema"]["fields"]) if f["name"] == "NVIDIA H200 - paid, rented")
    frame["data"]["values"][col][-1] = 4.11
    causes["results"]["A"]["frames"][0]["data"]["values"][2][-1] += 1               # 14 November, node: docker_run
    out = run(G.poll(Grafana(answers={"price_daily": price, "deploy_causes": causes}).client(),
                     now=datetime.now(UTC), gap=0))
    daily = {r["feed"]: r["written"] for r in out["panels"][8:]}
    assert daily == {"grafana_price_daily": 1, "grafana_deploy_causes": 1, "grafana_penalties": 0}, out["panels"][8:]
    h200 = one(PD, PD.day == date(2026, 11, 14), PD.gpu_model == "NVIDIA H200")
    assert (h200.median_asking, h200.median_paid) == (D("4.2"), D("4.11"))
    assert one(C, C.day == date(2026, 11, 14), C.series == "deploy_failures", C.label == "node: docker_run").count == 9
    assert count(PD) == 29 and count(C) == 74


def test_day_window_starts_on_a_midnight():
    midnight = lambda d: datetime(2026, 11, d, tzinfo=UTC)  # noqa: E731
    assert G._day_window(None, NOW) == (midnight(14), NOW)                           # nothing stored: yesterday on
    assert G._day_window(date(2026, 11, 15), NOW) == (midnight(14), NOW)             # yesterday closes on this read
    assert G._day_window(date(2026, 11, 10), NOW) == (midnight(9), NOW)              # after an outage
    assert G._day_window(date(2026, 8, 1), NOW) == (datetime(2026, 10, 16, tzinfo=UTC), NOW)   # capped at 30 days
    assert G._day_window(date(2026, 11, 20), NOW) == (midnight(14), NOW)             # a day ahead of the clock


def test_a_reshaped_answer_is_one_failed_row():
    wipe()
    # the dashboard swaps the two queries of the panel: each answer now lacks the column its read expects
    swapped = Grafana(answers={"launch_created": T.ANSWERS["launch_failures"],
                               "launch_failures": T.ANSWERS["launch_created"]})
    with T.Logs() as logs:
        a = run(G.poll(swapped.client(), now=NOW, gap=0))
        b = run(G.poll(Grafana(answers=swapped.answers).client(), now=NOW + timedelta(hours=1), gap=0))
    for out in (a, b):
        got = {r["feed"]: r for r in out["panels"]}
        for p in (CREATED, FAILURES):
            assert got[p.name]["state"] == "error" and p.column in got[p.name]["error"], got[p.name]
    assert count(LiumGrafanaNetworkHourly) == 0
    for p in (CREATED, FAILURES):
        assert count(LiumFeedSnapshot, LiumFeedSnapshot.feed == p.name) == 1
        assert count(LiumFeedSnapshot, LiumFeedSnapshot.feed == p.name, LiumFeedSnapshot.seen_count == 2) == 1
        assert len([w for w in logs.warnings() if p.name in w]) == 1, logs.warnings()
    # the dashboard is gone: stored once per panel, nothing parsed
    wipe()
    gone = run(G.poll(Grafana(boards={k: v for k, v in T.BOARDS.items() if k != F.RENTAL_CHECK}).client(), now=NOW, gap=0))
    got = {r["feed"]: r["state"] for r in gone["panels"]}
    assert (got["grafana_launch_created"], got["grafana_deploy_causes"]) == ("not_found", "not_found")
    assert got["grafana_price_daily"] == got["grafana_penalties"] == got["grafana_spend"] == "ok"
    # an empty window is `empty`, not a failure
    wipe()
    empty = run(G.poll(Grafana(answers={"penalties": T.ANSWERS["empty"]}).client(), now=NOW, gap=0))
    assert {r["feed"]: r["state"] for r in empty["panels"]}["grafana_penalties"] == "empty"


# ------------------------------------------------------------------------------------------------ backfill


def test_backfill_walks_back_and_resumes():
    # Chunk dates are in the past on purpose: a chunk counts as done only when it was read after it closed.
    wipe()
    ms = lambda y, m, d: int(datetime(y, m, d, tzinfo=UTC).timestamp() * 1000)  # noqa: E731
    data_from = ms(2026, 4, 20)

    def answer(name, body):                       # the venue has data from 20 April on; older windows are empty
        return None if int(body["to"]) > data_from else (200, T.ANSWERS["empty"])
    g = Grafana(answer=answer)
    out = run(G.backfill(until=date(2026, 6, 15), only="daily", http=g.client(), gap=0))
    assert set(out) == {PRICE.name, CAUSES.name, PENALTIES.name}
    for name, t in out.items():
        # 30-day chunks back from 16 June: 17 May-16 June and 17 April-17 May have data, the next two are empty
        assert (t["chunks"], t["oldest"], t["stopped"]) == (4, "2026-04-17", "2 empty chunks in a row"), (name, t)
    assert len(g.posts()) == 12
    spans = sorted({(int(q["from"]), int(q["to"])) for q in g.queries})
    # every chunk starts on a midnight and ends one millisecond before the next chunk's first midnight: the
    # dashboards filter BETWEEN both ends, and the closing midnight belongs to the newer chunk
    assert spans == [(ms(2026, 2, 16), ms(2026, 3, 18) - 1), (ms(2026, 3, 18), ms(2026, 4, 17) - 1),
                     (ms(2026, 4, 17), ms(2026, 5, 17) - 1), (ms(2026, 5, 17), ms(2026, 6, 16) - 1)], spans
    assert count(LiumGrafanaPriceDaily) == 29 and count(LiumGrafanaDailyCount) == 74       # the same answer twice: once
    assert count(LiumFeedSnapshot, LiumFeedSnapshot.state == "empty") == 6

    # run again: every chunk closed and stored, so nothing is asked
    again = Grafana(answer=answer)
    out = run(G.backfill(until=date(2026, 6, 15), only="daily", http=again.client(), gap=0))
    assert not again.posts() and all(t["skipped"] == 4 and t["stopped"] for t in out.values()), out
    # --force asks again; --since stops exactly there
    forced = Grafana(answer=answer)
    out = run(G.backfill(since=date(2026, 6, 1), until=date(2026, 6, 15), only="daily", force=True,
                         http=forced.client(), gap=0))
    assert len(forced.posts()) == 3 and all(t["stopped"] == "reached --since" for t in out.values())
    assert {(int(q["from"]), int(q["to"])) for q in forced.queries} == {(ms(2026, 6, 1), ms(2026, 6, 16) - 1)}

    # the hourly counts: 14-day chunks read per hour, both queries of the panel; two groups in one run
    wipe()
    both = Grafana(answer=answer)
    out = run(G.backfill(since=date(2026, 6, 2), until=date(2026, 6, 15), only="launches,daily",
                         http=both.client(), gap=0))
    assert set(out) == {p.name for p in NEW} and len(both.posts()) == 5
    hourly = [q for q in both.queries if T.ALLOWED[q["queries"][0]["rawSql"]].startswith("launch_")]
    assert len(hourly) == 2 and all(q["queries"][0]["intervalMs"] == G.HOUR_MS for q in hourly)
    assert all((int(q["from"]), int(q["to"])) == (ms(2026, 6, 2), ms(2026, 6, 16) - 1) for q in both.queries)
    assert count(LiumGrafanaNetworkHourly) == 48 and count(LiumGrafanaPriceDaily) == 29
    # the groups this change did not add are untouched, and their chunks still end on the midnight itself
    wipe()
    old = Grafana(answer=answer)
    run(G.backfill(since=date(2026, 6, 2), until=date(2026, 6, 15), only="series", http=old.client(), gap=0))
    assert len(old.posts()) == 3 and all(int(q["to"]) == ms(2026, 6, 16) for q in old.queries)
    # an unknown group is refused before anything is asked
    nothing = Grafana()
    try:
        run(G.backfill(only="daily,everything", http=nothing.client(), gap=0))
        raise AssertionError("an unknown group was accepted")
    except ValueError:
        assert not nothing.calls
    # three failed chunks in a row stop a panel
    wipe()
    down = Grafana(answer=lambda name, body: (502, {"message": "bad gateway"}))
    out = run(G.backfill(until=date(2026, 6, 15), only="launches", http=down.client(), gap=0))
    assert all(t["stopped"] == "3 failed chunks in a row" and t["errors"] == 3 for t in out.values()), out


def test_reparse_rebuilds_from_stored_bodies():
    wipe()
    run(G.poll(Grafana().client(), now=NOW, gap=0))
    with normalize.SessionLocal.begin() as s:
        s.execute(text("TRUNCATE lium_grafana_network_hourly, lium_grafana_price_daily, lium_grafana_daily_counts"))
    out = G.reparse()
    assert all(out["snapshots"][p.name] == 1 for p in NEW), out
    assert count(LiumGrafanaNetworkHourly) == 48 and count(LiumGrafanaPriceDaily) == 29
    assert count(LiumGrafanaDailyCount) == 74
    row = one(LiumGrafanaNetworkHourly, LiumGrafanaNetworkHourly.hour_utc == datetime(2026, 11, 13, 0, tzinfo=UTC))
    assert (row.rentals_created, row.node_deploy_failures) == (121, 7)


# ------------------------------------------------------------------------------------------------ reads


def test_reads_by_hand():
    wipe()
    run(G.poll(Grafana().client(), now=NOW, gap=0))
    saved = reads.datetime
    try:
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return NOW
        reads.datetime = Clock
        prices = reads.grafana_price_daily(days=3, gpu="NVIDIA H100 80GB HBM3")
        assert [r["day"] for r in prices] == [date(2026, 11, d) for d in (14, 13, 12)]
        newest = prices[0]
        assert (newest["median_asking"], newest["median_paid"]) == (D("2.65"), D("1.7"))
        assert newest["asking_over_paid_pct"] == round((2.65 / 1.7 - 1) * 100, 2) == 55.88
        assert newest["canonical_gpu_name"] == "NVIDIA H100 80GB SXM5"
        only_paid = reads.grafana_price_daily(days=3, gpu="NVIDIA GeForce RTX 4070 Ti SUPER")[0]
        assert only_paid["median_asking"] is None and only_paid["asking_over_paid_pct"] is None
        assert len(reads.grafana_price_daily(days=30)) == 29

        hours = reads.grafana_launches(hours=24 * 3)
        assert len(hours) == 48 and hours[0]["hour_utc"] == datetime(2026, 11, 14, 23, tzinfo=UTC)
        assert (hours[0]["rentals_created"], hours[0]["node_deploy_failures"]) == (58, 2)
        assert hours[0]["failures_per_100_rentals"] == round(100 * 2 / 58, 2)
        assert [h for h in hours if h["hour_utc"].hour == 5 and h["hour_utc"].day == 14][0]["failures_per_100_rentals"] is None
        days = reads.grafana_launches(hours=24 * 3, by="day")
        assert [(d["day"], d["rentals_created"], d["node_deploy_failures"]) for d in days] == [
            (date(2026, 11, 14), 1225, 15), (date(2026, 11, 13), 2105, 48)], days
        assert days[1]["failures_per_100_rentals"] == round(100 * 48 / 2105, 2)

        causes = reads.grafana_daily_counts("deploy_failures", days=2)
        assert {r["day"] for r in causes} == {date(2026, 11, 13), date(2026, 11, 14)}
        assert (causes[0]["label"], causes[0]["count"]) == ("node: docker_run", 8)        # the day's largest first
        one_reason = reads.grafana_daily_counts("penalties", days=30, label="POD_UNDEPLOY_FAILED")
        assert [r["count"] for r in one_reason] == [1, 3, 2, 1, 1, 1, 1, 1, 2]            # 14 November back to the 2nd
    finally:
        reads.datetime = saved
    st = reads.status()
    feeds = {f["feed"]: f for f in st["feeds"]}
    for p in NEW:
        assert feeds[p.name]["state"] == "ok" and feeds[p.name]["visibility"] == "internal", p.name
        assert p.title in feeds[p.name]["path"] and p.dashboard in feeds[p.name]["path"]
    assert (st["rows"]["lium_grafana_network_hourly"], st["rows"]["lium_grafana_price_daily"],
            st["rows"]["lium_grafana_daily_counts"]) == (48, 29, 74)


def test_registry_internal():
    for p in NEW:
        assert provider_meta.visibility("lium", p.path, "signals") == "internal", p.name
        assert raw.visibility(p) == "internal"
    assert CREATED.path == "grafana.lium.io/d/fejrp6bf9f5s0e"
    assert {e.source for e in provider_meta.internal_entries("lium") if e.source.startswith("grafana.lium.io")} == {
        "grafana.lium.io/d/marketplace-prices-v1", "grafana.lium.io/d/bejrh6ldw5lhcc", "grafana.lium.io/d/fejrp6bf9f5s0e"}
    # a dashboard the registry does not name would be stored public: every panel's dashboard must be named
    assert provider_meta.visibility("lium", "grafana.lium.io/d/some-other-board", "signals") == "public"
    assert provider_meta.is_public_provider("lium")               # Lium's prices and capacity stay public
    # the feed names fit the column that holds them, and no panel is in two groups
    assert all(len(p.name) <= 48 for p in P.values())
    groups = [F.RENTALS_PANEL, *F.SERIES_PANELS, *F.NETWORK_PANELS, *F.DAILY_PANELS]
    assert len(groups) == len(P) == 9 and {p.name for p in groups} == set(P)
    assert set(F.COUNT_SERIES) == {"deploy_failures", "penalties"}


def test_admin_endpoints():
    wipe()
    run(G.poll(Grafana().client(), now=datetime.now(UTC), gap=0))
    from fastapi.testclient import TestClient

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
        op, viewer = T._basic("opengrid", "pw"), T._basic("vc", "look-only")
        paths = ("/v1/ops/lium/grafana/prices-daily", "/v1/ops/lium/grafana/launches",
                 "/v1/ops/lium/grafana/daily-counts")
        for p in paths:
            assert client.get(p).status_code == 401, p
            assert client.get(p, headers=viewer).status_code == 403, p
            r = client.get(p, headers=op)
            assert r.status_code == 200 and r.json()["meta"]["visibility"] == "internal", (p, r.text)
            assert r.json()["meta"]["kind"] == "observed" and "never republish" in r.json()["meta"]["note"]
        r = client.get("/v1/ops/lium/grafana/prices-daily?days=730&gpu=NVIDIA%20H200", headers=op)
        assert r.json()["meta"]["count"] == 6 and set(r.json()["meta"]["columns"]) == {"median_asking", "median_paid"}
        assert float(r.json()["data"][0]["median_paid"]) == 4.08
        r = client.get("/v1/ops/lium/grafana/launches?hours=17520&by=day", headers=op)
        assert [d["rentals_created"] for d in r.json()["data"]] == [1225, 2105], r.text
        assert client.get("/v1/ops/lium/grafana/launches?by=week", headers=op).status_code == 422
        r = client.get("/v1/ops/lium/grafana/daily-counts?series=penalties&days=730", headers=op)
        assert r.json()["meta"]["count"] == 36 and "Penalty events per day" in r.json()["meta"]["source"]
        assert client.get("/v1/ops/lium/grafana/daily-counts?series=nope", headers=op).status_code == 404
        assert client.get("/v1/ops/lium/grafana/prices-daily?days=0", headers=op).status_code == 422
        # nothing of it on a public surface
        spec = main.app.openapi()["paths"]
        assert all(p in spec for p in paths)
    finally:
        (settings.app_password, settings.public_pages, settings.viewer_user, settings.viewer_password,
         settings.environment) = saved


def test_cli_only_groups():
    from lium_data import __main__ as cli
    assert cli.main(["grafana-backfill", "--only", "everything"]) == 2          # refused before any request
    assert cli.main(["grafana-backfill", "--only", "daily,nope"]) == 2
    assert "launches" in cli.__doc__ and "daily" in cli.__doc__


def test_migration_0043():
    from alembic import command
    from alembic.script import ScriptDirectory
    from sqlalchemy import create_engine

    import db as dbmod
    name = DB + "_mig"
    url = scratchdb.create(name)
    old = settings.database_url
    settings.database_url = url
    try:
        cfg = dbmod._alembic_config()
        script = ScriptDirectory.from_config(cfg)
        assert script.get_heads() == ["0043_lium_grafana_history"]                  # one head
        assert script.get_revision("0043_lium_grafana_history").down_revision == "0042_operator_daily"
        command.upgrade(cfg, "0043_lium_grafana_history")
        eng = create_engine(url)
        from store import lium as L
        models = (L.LiumGrafanaNetworkHourly, L.LiumGrafanaPriceDaily, L.LiumGrafanaDailyCount)
        with eng.connect() as c:
            for model in models:
                t = model.__tablename__
                cols = {r[0]: r[1] for r in c.execute(text(
                    "SELECT column_name, is_nullable FROM information_schema.columns WHERE table_name = :t"), {"t": t})}
                assert set(cols) == {col.name for col in model.__table__.columns}, t
                assert {k for k, v in cols.items() if v == "YES"} == {col.name for col in model.__table__.columns if col.nullable}, t
                pk = [r[0] for r in c.execute(text(
                    "SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid AND "
                    "a.attnum = ANY(i.indkey) WHERE i.indrelid = CAST(:t AS regclass) AND i.indisprimary "
                    "ORDER BY array_position(i.indkey, a.attnum)"), {"t": t})]
                assert pk == [col.name for col in model.__table__.primary_key.columns], (t, pk)
                d = c.execute(text("SELECT column_default FROM information_schema.columns WHERE table_name = :t "
                                   "AND column_name = 'visibility'"), {"t": t}).scalar()
                assert "internal" in d, (t, d)
            try:
                c.execute(text("INSERT INTO lium_grafana_daily_counts (day, series, label, count, updated_at, visibility) "
                               "VALUES ('2026-11-14', 'penalties', 'X', 1, now(), 'secret')"))
                raise AssertionError("CHECK did not fire")
            except Exception as exc:  # noqa: BLE001
                assert "ck_lium_grafana_daily_counts_visibility" in str(exc), exc
                c.rollback()
        command.downgrade(cfg, "0042_operator_daily")
        with eng.connect() as c:
            for model in models:
                assert c.execute(text("SELECT to_regclass(:t)"), {"t": "public." + model.__tablename__}).scalar() is None
            assert c.execute(text("SELECT to_regclass('public.lium_grafana_hourly')")).scalar() is not None
            assert c.execute(text("SELECT to_regclass('public.operator_daily')")).scalar() is not None
        command.upgrade(cfg, "head")                                                # and up again
        with eng.connect() as c:
            assert c.execute(text("SELECT to_regclass('public.lium_grafana_price_daily')")).scalar() is not None
        eng.dispose()
    finally:
        settings.database_url = old
        scratchdb.drop(name)


TESTS = [test_two_queries_of_one_panel_are_read_by_ref, test_parse_network_hourly, test_parse_price_daily,
         test_parse_daily_counts, test_poll_stores_and_updates_in_place, test_day_window_starts_on_a_midnight,
         test_a_reshaped_answer_is_one_failed_row, test_backfill_walks_back_and_resumes,
         test_reparse_rebuilds_from_stored_bodies, test_reads_by_hand, test_registry_internal, test_admin_endpoints,
         test_cli_only_groups, test_migration_0043]


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
