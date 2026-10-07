"""The Textual terminal: every platform command driven through the real app (App.run_test pilot)
against a scratch database seeded with synthetic history.

Run:  .venv/Scripts/python tests/test_terminal.py
"""

import asyncio
import os
import sys
from datetime import timedelta
from pathlib import Path

os.environ.setdefault("OPENGRID_NO_JOBS", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fixtures  # noqa: E402
import normalize  # noqa: E402
import scratchdb  # noqa: E402
from sqlalchemy import text  # noqa: E402

import terminal  # noqa: E402
from textual.widgets import DataTable, Input  # noqa: E402

DB = "og_test_tui"
H100 = "NVIDIA H100 80GB SXM5"


class FakeIngest:
    """No polling in tests: the terminal must not fetch real providers."""

    def start(self):
        pass

    async def stop(self):
        pass

    def describe(self):
        return "disabled (test)"


def test_usage_lines():
    pats = [c.pattern.pattern for c in terminal.COMMANDS]
    assert all(c.usage.strip() for c in terminal.COMMANDS), [p for p, c in zip(pats, terminal.COMMANDS) if not c.usage]
    for word in ("idx", "indices", "ctx", "mkt", "best", "preview", "ev", "opp", "news", "prov", "ops", "caps", "fam"):
        assert any(c.usage.startswith(word) for c in terminal.COMMANDS), f"no usage line for {word}"
    # The catch-all listings filter stays last, so it never shadows a command.
    assert terminal.COMMANDS[-1].handler is terminal._filter
    assert not any("provision" == c.usage.split()[0] for c in terminal.COMMANDS), "no provisioning command"


def test_parsing():
    p = terminal.parse_route_args("rtx 4090 2 middle east $1.50", need_count=True, with_mode=False)
    assert p == {"count": 2, "region_group": "Middle East", "mode": "BALANCED", "max_price": 1.5,
                 "gpu_text": "rtx 4090"}, p
    p = terminal.parse_route_args("h100 sxm 8 europe cheapest", need_count=False, with_mode=True)
    assert (p["count"], p["region_group"], p["mode"], p["gpu_text"]) == (8, "Europe", "CHEAPEST", "h100 sxm"), p
    assert terminal.parse_route_args("rtx 4090", need_count=False, with_mode=True)["count"] == 1, "4090 is not a count"
    try:
        terminal.parse_route_args("h100-80gb-sxm5", need_count=True, with_mode=False)
        raise AssertionError("preview needs a count")
    except terminal.Unavailable as e:
        assert "usage: preview" in str(e)
    assert terminal.resolve_gpu_arg("h100-80gb-sxm5") == (H100, None)
    g, note = terminal.resolve_gpu_arg("h100 sxm")
    assert g == H100 and "resolved" in note, "fuzzy resolution is announced, not silent"
    try:
        terminal.resolve_gpu_arg("h100")
        raise AssertionError("h100 is ambiguous")
    except terminal.Unavailable as e:
        cands = [r[0] for r in e.rows]
        assert H100 in cands and "NVIDIA H100 80GB PCIe" in cands and len(cands) >= 3, cands


def _plain(c) -> str:
    return getattr(c, "plain", None) or str(c)


def _table(app):
    t = app.query_one(DataTable)
    cols = [_plain(c.label) for c in t.columns.values()]
    rows = [[_plain(x) for x in t.get_row(k)] for k in t.rows]
    return cols, rows


def _notes(app) -> str:
    return "\n".join(str(n) for n in app.panel.notes)


def _seed():
    url = scratchdb.create(DB)
    Session = fixtures.session(url)
    meta = fixtures.seed(Session, days=45)
    with Session.begin() as s:  # keep everything in stock so indices publish (as test_indices does)
        s.execute(text("UPDATE listing_observations SET available = true"))
        s.execute(text("UPDATE compute_listings SET available = true"))
    normalize.SessionLocal = Session
    # Import every module whose rollup hook should run, then build the rollup (+ indices, events, daily).
    from analytics import events, indices, providers, rollups  # noqa: F401
    rollups.refresh()
    # Two sources carrying the same story, so news is story-clustered with source_count 2.
    from news import parse, store
    from news import sources as registry
    now = meta["now"]
    rfc = (now - timedelta(hours=3)).strftime("%a, %d %b %Y %H:%M:%S +0000")
    title = "CoreWeave signs GPU compute deal for H100 capacity"

    def feed(t, link):
        return (f'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title><item><title>{t}</title>'
                f'<link>{link}</link><description>H100 GPUs.</description><pubDate>{rfc}</pubDate></item>'
                f'</channel></rss>').encode()

    store.sync_sources()
    store.ingest(registry.get("dcd"), parse.parse(feed(title + " - DCD", "https://www.datacenterdynamics.com/x/"),
                                                 "rss"), now)
    store.ingest(registry.get("the_register"), parse.parse(feed(title, "https://www.theregister.com/x/"), "rss"), now)
    return Session, meta


async def _drive(Session):
    terminal.init_db = lambda: None   # the scratch schema comes from create_all, not alembic
    terminal.Ingest = FakeIngest
    app = terminal.OpenGrid()
    async with app.run_test(size=(240, 70)) as pilot:
        async def run(cmd):
            app.query_one(Input).value = cmd
            await pilot.press("enter")
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            return _table(app)

        def flat(rows):
            return "\n".join(" | ".join(r) for r in rows)

        # Existing behaviour is intact: the default listings view and a plain-text filter.
        cols, rows = await run("l")
        assert app.view == "listings" and "canonical gpu" in cols and rows, cols
        cols, rows = await run("H100 80GB SXM5")
        assert app.view == "listings" and rows and all(H100 in r for r in rows), rows[:2]

        cols, rows = await run("help")
        assert cols == ["command", "what it shows"] and any(r[0].startswith("preview") for r in rows)
        assert len(rows) == len(terminal.COMMANDS)

        # Index board and detail.
        cols, rows = await run("indices")
        assert cols[:3] == ["id", "kind", "level"] and "published / reason" in cols, cols
        ids = [r[0] for r in rows]
        assert "h100-80gb-sxm5" in ids, ids
        h = next(r for r in rows if r[0] == "h100-80gb-sxm5")
        assert h[-1].startswith("published") and h[2] != "n/a" and h[4].endswith("%"), h
        assert all(r[-1] for r in rows), "every index shows published or its reason"
        unpub = [r for r in rows if not r[-1].startswith("published")]
        assert all(r[2] == "n/a" and len(r[-1]) > 5 for r in unpub), unpub[:2]
        cols, rows = await run("indices h200")
        assert rows and all("h200" in r[0] for r in rows), rows
        cols, rows = await run("idx h100-80gb-sxm5")
        assert app.panel.title == "idx h100-80gb-sxm5" and "level" in _notes(app)
        t = flat(rows)
        for want in ("change | 24h", "change | 90d", "extreme | high", "volatility | 7d", "constituent | syn_"):
            assert want in t, (want, t[:800])
        r90 = next(r for r in rows if r[:2] == ["change", "90d"])
        assert r90[2].startswith("n/a: history does not cover"), r90   # 45 days of history: says why
        await run("idx h100 sxm")
        assert app.panel.title == "idx h100-80gb-sxm5" and "resolved" in _notes(app)
        await run("idx no-such-index-zz")
        assert app.panel.title == "indices" and "no index matches" in _notes(app)

        # Historical context.
        cols, rows = await run("ctx h100-80gb-sxm5")
        n = _notes(app)
        assert "percentile" in n and "observed market price" in n, n
        r30 = next(r for r in rows if r[:2] == ["median", "30d"])
        r90 = next(r for r in rows if r[:2] == ["median", "90d"])
        assert r30[3] != "n/a" and r30[-1] == "ok", r30
        assert r90[3] == "n/a" and "history" in r90[-1], r90

        # Market dispersion.
        cols, rows = await run("mkt h100 sxm")
        n = _notes(app)
        assert "premium vs others' median" in cols and rows and "efficiency" in n and "resolved" in n, (cols, n)
        assert rows[0][0] == "1" and rows[0][3].startswith("-"), "cheapest provider is below the others' median"
        cols, rows = await run("mkt h100")
        assert app.panel.status == "unavailable" and "matches" in _notes(app)
        assert cols == ["candidate gpu", "slug"] and any(r[0] == H100 for r in rows), (cols, rows)
        cols, rows = await run("mkt zzz-gpu")
        assert app.panel.status == "unavailable" and "unknown GPU" in _notes(app)
        msgs = [str(x.message) for x in app._notifications]
        assert any("unknown GPU" in m for m in msgs), msgs

        # Best execution: ranked, explained, nothing provisioned.
        cols, rows = await run("best h100-80gb-sxm5")
        n = _notes(app)
        assert "RANKING ONLY - nothing provisioned" in n and "exclusions" in n, n
        assert rows and rows[0][0] == "1" and "market median" in rows[0][-1], rows[:1]
        assert float(rows[0][6]) >= float(rows[-1][6]) or rows[-1][0].startswith("m")
        cols, rows = await run("best h100-80gb-sxm5 8 cheapest")
        assert "mode CHEAPEST" in _notes(app) and "multi-instance alternatives" in _notes(app), _notes(app)
        assert rows and all(r[0].startswith("m") and "instances" in r[-1] for r in rows), rows[:2]

        # Preview: QUOTE vs observed price, preview only, as the operator, audited.
        cols, rows = await run("preview h100-80gb-sxm5 1 $9.99")
        n = _notes(app)
        assert "PREVIEW ONLY - nothing provisioned" in n and "QUOTE [quote" in n and "OBSERVED" in n, n
        assert "max $9.99" in n and rows[0][0] == "selected", rows[:1]
        with Session() as s:
            assert s.execute(text("SELECT count(*) FROM routing_decisions")).scalar() >= 1, "preview is audited"
            assert s.execute(text("SELECT count(*) FROM deployments")).scalar() == 0, "nothing provisioned"
        await run("preview h100-80gb-sxm5 1 $0.01")
        assert "no route" in _notes(app) and "over_max_price" in _notes(app), _notes(app)
        await run("preview h100-80gb-sxm5")
        assert app.panel.status == "unavailable" and "usage: preview" in _notes(app)

        # Events, opportunities, news.
        cols, rows = await run("ev")
        assert cols[:3] == ["when", "sev", "type"], cols
        with Session() as s:
            n_ev = s.execute(text("SELECT count(*) FROM market_events")).scalar()
        assert (len(rows) == min(n_ev, 300)) if n_ev else "no market events" in _notes(app)
        cols, rows = await run("events syn_alpha")
        assert "provider syn_alpha" in app.panel.status and all(r[4] in ("syn_alpha", "-") for r in rows), rows[:3]
        cols, rows = await run("opp")
        assert cols[0] == "score" and "unavailable" in _notes(app)
        assert all(r[0] != "n/a: unavailable" or r[-1] for r in rows), "every unavailable type says why"
        cols, rows = await run("news")
        assert rows and "CoreWeave" in rows[0][3] and rows[0][1] == "2", rows
        assert "never that news caused" in _notes(app)
        cols, rows = await run("news h100 sxm")
        assert "gpu NVIDIA H100 80GB SXM5" in app.panel.status, app.panel.status
        cols, rows = await run("news nothing-matches-this")
        assert not rows and "no news stored for this filter" in _notes(app)

        # Provider, ops, capabilities, families.
        cols, rows = await run("prov syn_alpha")
        n = _notes(app)
        assert "health:" in n and "relative value 30d" in n and rows and rows[0][2].count("/") == 1, (n, rows[:1])
        await run("prov nobody")
        assert app.panel.status == "unavailable" and "unknown provider" in _notes(app)
        cols, rows = await run("ops")
        assert "status" in cols and any(r[0] == "syn_alpha" for r in rows) and "quarantine pending" in _notes(app)
        cols, rows = await run("caps")
        assert "verified live" in cols and rows and all(r[3] == "no" for r in rows), rows[:2]
        assert "verified_live is false everywhere" in _notes(app)
        try:
            import families
        except ImportError:
            families = None
        if families is None:
            await run("fam h100")
            assert app.panel.status == "unavailable" and "families module is not installed" in _notes(app)
        else:
            cols, rows = await run("fam h100")
            assert app.panel.title == "fam " + families.resolve_family("h100"), app.panel.title
            names = [r[0] for r in rows]
            assert H100 in names and "NVIDIA H100 80GB PCIe" in names, names   # variants side by side
            h = next(r for r in rows if r[0] == H100)
            assert h[1].startswith("$") and h[2].startswith("syn_"), h
            unpriced = [r for r in rows if r[0] not in fixtures.GPUS]
            assert unpriced and all(r[1].startswith("n/a: no live priced") for r in unpriced), unpriced
            assert "never merged" in _notes(app)
            cols, rows = await run("fam")
            assert cols[0] == "family" and rows, cols
            await run("fam not-a-family")
            assert app.panel.status == "unavailable" and "unknown GPU family" in _notes(app)
            await run("mkt h100")
            assert "is a GPU family" in _notes(app), _notes(app)

        # Required arguments missing: a reason, never an empty table.
        for cmd in ("ctx", "mkt", "prov"):
            await run(cmd)
            assert app.panel.status == "unavailable" and "required" in _notes(app), (cmd, _notes(app))

        # The UI stays responsive: a periodic redraw does not recompute or wipe a panel.
        before = app.panel
        app._tick()
        assert app.panel is before


def test_tui():
    Session, _ = _seed()
    try:
        asyncio.run(_drive(Session))
    finally:
        Session.kw["bind"].dispose()
        scratchdb.drop(DB)


if __name__ == "__main__":
    for t in (test_usage_lines, test_parsing, test_tui):
        t(); print(t.__name__, "ok")
