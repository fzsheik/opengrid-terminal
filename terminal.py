"""OpenGrid terminal: live GPU prices with a regex-registered command bar."""

import asyncio
import difflib
import importlib
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.widgets import DataTable, Header, Input, Static

import canonical
import mapping
import normalize
import raw_store
from db import init_db
from fetch import fetch_all, save
from poller import Ingest


@dataclass(frozen=True)
class Command:
    pattern: re.Pattern
    handler: Callable[["OpenGrid", re.Match], None]
    usage: str


COMMANDS: list[Command] = []


def command(pattern: str, usage: str):
    """Register a handler for input that fully matches `pattern` (case-insensitive).

    Commands are tried in registration order, first match wins.
    """

    def register(fn):
        COMMANDS.append(Command(re.compile(pattern, re.I), fn, usage))
        return fn

    return register


@command(r"help|\?", "help                this list")
def _help(app, m):
    app.show_panel(help_panel())


@command(r"(?:listings?|l)(?:\s+(?P<q>.+))?", "listings [filter]   normalized listings, cheapest first")
def _listings(app, m):
    app.show("listings", q=m["q"])


@command(r"(?:avail(?:able)?|a)(?:\s+(?P<q>.+))?", "avail [filter]      only listings with stock")
def _avail(app, m):
    app.show("listings", q=m["q"], only_available=True)


@command(r"cheapest(?:\s+(?P<n>\d+))?", "cheapest [n]        n cheapest in stock (default 10)")
def _cheapest(app, m):
    app.show("listings", only_available=True, limit=int(m["n"] or 10))


@command(r"(?:history|h)(?:\s+(?P<q>.+))?", "history [filter]    recorded changes, newest first")
def _history(app, m):
    app.show("history", q=m["q"])


@command(r"(?:refs?|reference)(?:\s+(?P<q>.+))?", "refs [filter]       priced entries that are not listings")
def _refs(app, m):
    app.show("refs", q=m["q"])


@command(r"(?:mapping|map)(?:\s+(?P<q>.+))?", "mapping [provider]  how each API fills ComputeListing")
def _mapping(app, m):
    app.show("mapping", q=m["q"])


@command(r"quirks?(?:\s+(?P<q>.+))?", "quirks [provider]   per-provider gotchas worth knowing")
def _quirks(app, m):
    app.show("quirks", q=m["q"])


@command(r"unmapped", "unmapped            raw GPU names with no canonical name")
def _unmapped(app, m):
    app.show("unmapped")


@command(r"(?:raw)(?:\s+(?P<q>.+))?", "raw [provider]      recent raw fetches")
def _raw(app, m):
    app.show("raw", q=m["q"])


@command(r"(?:summary|s)", "summary             per endpoint: fetches, distinct bodies, latency")
def _summary(app, m):
    app.show("summary")


@command(r"(?:fetch|f)", "fetch               pull every provider, then normalize")
def _fetch(app, m):
    app.run_fetch()


@command(r"(?:normalize|n)", "normalize           re-run the normalizer over stored raw")
def _normalize(app, m):
    app.run_normalize()


@command(r"(?:reset|clear)", "reset               clear filters")
def _reset(app, m):
    app.show("listings")


@command(r"(?:quit|exit|q)", "quit")
def _quit(app, m):
    app.exit()


# --------------------------------------------------------------------------
# Platform views. Each computes a Panel in a worker thread (they read the DB),
# so the UI never freezes; the 5 s redraw only repaints the computed panel.
# --------------------------------------------------------------------------

@command(r"indices(?:\s+(?P<q>.+))?", "indices [filter]    index board: level, 24h/7d change, published / reason")
def _indices(app, m):
    app.run_panel("indices", index_board, m["q"])


@command(r"idx(?:\s+(?P<q>.+))?", "idx [id|filter]     board; an exact id (or a single match) shows its detail")
def _idx(app, m):
    app.run_panel("idx", index_view, m["q"])


@command(r"ctx(?:\s+(?P<q>.+))?", "ctx <gpu>           historical context: percentiles vs 30d / 90d, in words")
def _ctx(app, m):
    app.run_panel("ctx", context_panel, m["q"])


@command(r"mkt(?:\s+(?P<q>.+))?", "mkt <gpu>           dispersion / efficiency, each provider's best + premium")
def _mkt(app, m):
    app.run_panel("mkt", market_panel, m["q"])


@command(r"best(?:\s+(?P<q>.+))?", "best <gpu> [count] [region] [mode]  best-execution ranking (never provisions)")
def _best(app, m):
    app.run_panel("best", best_panel, m["q"])


@command(r"preview(?:\s+(?P<q>.+))?", "preview <gpu> <count> [region] [max $]  route preview: QUOTE, nothing provisioned")
def _preview(app, m):
    app.run_panel("preview", preview_panel, m["q"])


@command(r"(?:events|ev)(?:\s+(?P<q>.+))?", "ev [filter]         market events (type, provider, GPU or text)")
def _events(app, m):
    app.run_panel("events", events_panel, m["q"])


@command(r"opps?|opportunities", "opp                 opportunities, and which are unavailable and why")
def _opp(app, m):
    app.run_panel("opp", opportunities_panel)


@command(r"news(?:\s+(?P<q>.+))?", "news [filter]       news, one row per story (GPU, provider or text)")
def _news(app, m):
    app.run_panel("news", news_panel, m["q"])


@command(r"prov(?:ider)?(?:\s+(?P<q>.+))?", "prov <name>         provider relative value, per-GPU premium, feed health")
def _prov(app, m):
    app.run_panel("prov", provider_panel, m["q"])


@command(r"ops", "ops                 quality: provider health, quarantine, open incidents")
def _ops(app, m):
    app.run_panel("ops", ops_panel)


@command(r"caps|capabilities", "caps                routing capability registry (supported vs implemented)")
def _caps(app, m):
    app.run_panel("caps", caps_panel)


@command(r"fam(?:ily)?(?:\s+(?P<q>.+))?", "fam <family>        a GPU family's variants with their lows")
def _fam(app, m):
    app.run_panel("fam", family_panel, m["q"])



@command(r"(?P<q>[\w .\-()+,/]+)", "<text>              anything else filters listings")
def _filter(app, m):
    app.show("listings", q=m["q"])


def age(ts: datetime | None) -> str:
    if ts is None:
        return "-"
    secs = max(0, int((datetime.now(timezone.utc) - ts).total_seconds()))
    if secs < 90:
        return f"{secs}s"
    if secs < 5400:
        return f"{secs // 60}m"
    if secs < 172800:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def num(v, places: int = 0) -> str:
    return "-" if v is None else f"{v:.{places}f}"


def price_arrow(current, previous) -> Text:
    """Which way the price moved since the previous observation.

    Up is red because it is worse for a buyer. Blank means no recorded change
    yet, which is not the same as a flat price.
    """
    if current is None or previous is None:
        return Text("", style="dim")
    if current > previous:
        return Text("^", style="red")
    if current < previous:
        return Text("v", style="green")
    return Text("=", style="dim")


# --------------------------------------------------------------------------
# Panels: what a platform command shows. Built off the UI thread from the
# same Python functions the /v1 API uses; platform modules are imported lazily
# so one of them being mid-edit cannot stop the terminal from starting.
# --------------------------------------------------------------------------

@dataclass
class Panel:
    title: str
    columns: tuple = ()
    rows: list = field(default_factory=list)
    notes: list = field(default_factory=list)   # lines shown above the table
    status: str = ""                            # extra status-bar text


class Unavailable(Exception):
    """A command cannot answer; the message says why (shown, never swallowed)."""

    def __init__(self, message: str, rows: list | None = None, columns: tuple = ()):
        super().__init__(message)
        self.rows, self.columns = rows or [], columns


def na(reason: str | None) -> Text:
    """An unavailable statistic: never blank, never 0, always why."""
    return Text(f"n/a: {reason or 'no reason recorded'}", style="yellow")


def gap(value, style: str = ""):
    """A value whose row carries the reason in its own column: 'n/a' in yellow when missing."""
    if value is None:
        return Text("n/a", style="yellow", justify="right")
    return Text(str(value), style=style, justify="right")


def usd(v, places: int = 3):
    return None if v is None else f"${float(v):,.{places}f}"


def pct(v, places: int = 1):
    return None if v is None else f"{float(v) * 100:+.{places}f}%"


def cell(value, reason: str | None = None, style: str = ""):
    """A formatted value, or n/a with its reason."""
    if value is None:
        return na(reason)
    return Text(str(value), style=style, justify="right")


def when(ts) -> str:
    """ISO string or datetime -> 'MM-DD HH:MM (age)'."""
    if ts is None:
        return "-"
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts)
    return f"{ts:%m-%d %H:%M} ({age(ts)})"


def short(s, n: int = 140) -> str:
    s = "" if s is None else str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def help_panel() -> Panel:
    rows = []
    for c in COMMANDS:
        if not c.usage:
            continue
        parts = re.split(r"\s{2,}", c.usage, maxsplit=1)
        rows.append([Text(parts[0], style="bold"), parts[1] if len(parts) > 1 else ""])
    return Panel("help", ("command", "what it shows"), rows,
                 notes=["Platform views read the database directly; prices are observed market prices unless "
                        "marked QUOTE. Nothing here provisions anything."])


# ---- argument resolution ---------------------------------------------------

def _words(s: str) -> list[str]:
    return re.findall(r"[a-z0-9.]+", s.lower())


def resolve_gpu_arg(q: str | None) -> tuple[str, str | None]:
    """A typed GPU -> (canonical name, note). Exact slug / name first (api.common.resolve_gpu), then a
    word-prefix match over canonical names. Ambiguous or unknown raises Unavailable listing the
    candidates: a GPU is never guessed silently, and variants are never merged."""
    from api.common import gpu_slug, resolve_gpu

    if not q or not q.strip():
        raise Unavailable("a GPU is required, e.g. h100-80gb-sxm5 or 'h100 sxm'")
    q = q.strip()
    hit = resolve_gpu(q, strict=False)
    if hit:
        return hit, None
    names = canonical.all_canonical_names()
    toks = _words(q)
    words = {n: _words(n) for n in names}
    cands = [n for n in names if toks and all(any(w.startswith(t) for w in words[n]) for t in toks)]
    exact = [n for n in cands if all(t in words[n] for t in toks)]
    if len(cands) == 1:
        return cands[0], f"resolved {q!r} -> {cands[0]}"
    if len(exact) == 1:
        return exact[0], f"resolved {q!r} -> {exact[0]} (exact word match; {len(cands)} names contain it)"
    if cands:
        hint = ""
        try:
            if importlib.import_module("families").resolve_family(q):
                hint = f" ({q!r} is a GPU family: 'fam {q}' compares its variants)"
        except Exception:
            pass
        raise Unavailable(f"{q!r} matches {len(cands)} GPUs; be more specific (e.g. a slug below){hint}",
                          [[n, gpu_slug(n)] for n in cands], ("candidate gpu", "slug"))
    close = difflib.get_close_matches(gpu_slug(q), [gpu_slug(n) for n in names], n=6, cutoff=0.5)
    hint = f"; did you mean: {', '.join(close)}" if close else ""
    raise Unavailable(f"unknown GPU {q!r}{hint} (type 'l' to see listed GPUs)")


def known_providers() -> list[str]:
    from providers import PROVIDERS
    from quality import trust

    try:
        seen = trust.known_providers()
    except Exception:
        seen = []
    return sorted(set(PROVIDERS) | set(seen))


def resolve_provider_arg(q: str | None) -> str:
    import provider_meta

    if not q or not q.strip():
        raise Unavailable("a provider is required, e.g. vast")
    low = q.strip().lower()
    names = known_providers()
    for p in names:
        if low in (p.lower(), provider_meta.meta(p).display_name.lower()) or re.sub(r"[^a-z0-9]", "", low) == \
                re.sub(r"[^a-z0-9]", "", p.lower()):
            return p
    cands = [p for p in names if p.lower().startswith(low) or provider_meta.meta(p).display_name.lower().startswith(low)]
    if len(cands) == 1:
        return cands[0]
    if cands:
        raise Unavailable(f"{q!r} matches several providers: {', '.join(cands)}")
    raise Unavailable(f"unknown provider {q!r}; known: {', '.join(names)}")


def _region_group(tok: str) -> str | None:
    try:
        from regions import REGION_GROUPS
    except ImportError:
        return None
    for g in REGION_GROUPS:
        if g.lower() == tok.lower():
            return g
    return None


def parse_route_args(q: str | None, *, need_count: bool, with_mode: bool) -> dict:
    """'<gpu words> [count] [region] [mode | max$]' -> parts, peeled from the end.

    count is a bare integer 1..64 (so 'rtx 4090' stays a GPU); a max price is written with '$' or a
    decimal point; region is a region group (US, Europe, 'Middle East', ...)."""
    from routing import scoring

    toks = (q or "").split()
    out = {"count": 1, "region_group": None, "mode": "BALANCED", "max_price": None}
    if with_mode and toks and toks[-1].upper() in scoring.MODES:
        out["mode"] = toks.pop().upper()
    if not with_mode and toks and re.fullmatch(r"\$\d+(\.\d+)?|\d*\.\d+|\d+\.", toks[-1]):
        out["max_price"] = float(toks.pop().lstrip("$"))
    if len(toks) >= 2 and _region_group(" ".join(toks[-2:])):
        out["region_group"] = _region_group(" ".join(toks[-2:]))
        del toks[-2:]
    elif toks and _region_group(toks[-1]):
        out["region_group"] = _region_group(toks.pop())
    if len(toks) >= 2 and re.fullmatch(r"\d+", toks[-1]) and 1 <= int(toks[-1]) <= 64:
        out["count"] = int(toks.pop())
    elif need_count:
        raise Unavailable("usage: preview <gpu> <count> [region] [max $], e.g. preview h100-80gb-sxm5 8 US $3.50 "
                          "(count = GPUs per instance, 1..64)")
    out["gpu_text"] = " ".join(toks)
    return out


# ---- indices ---------------------------------------------------------------

def _level_text(lv: dict):
    if lv.get("level") is None:
        return gap(None)
    places = 2 if lv.get("unit") == "points" else 4
    return Text(f"{lv['level']:.{places}f}", justify="right")


def index_board(q: str | None = None) -> Panel:
    from analytics import indices

    items = indices.index_list()
    if q:
        ql = q.strip().lower()
        items = [i for i in items if ql in i["id"].lower() or ql in (i["name"] or "").lower()
                 or ql in (i.get("gpu") or "").lower()]
    rows = []
    for i in items:
        ch, why = i.get("changes") or {}, i.get("change_reasons") or {}
        if i["published"]:
            state = Text("published", style="green")
            missing = "; ".join(f"{w} n/a: {why[w]}" for w in ("24h", "7d") if ch.get(w) is None and why.get(w))
            if missing:
                state.append(f"  ({missing})", style="dim")
        else:
            state = Text(i.get("reason") or "not published", style="yellow")
        rows.append([
            i["id"], i["kind"], _level_text(i), i.get("unit") or "-",
            gap(pct(ch.get("24h"), 2)), gap(pct(ch.get("7d"), 2)),
            str(i.get("constituents") or 0), state,
        ])
    notes = [] if items else [("no indices have data in the last 90 days" if not q else f"no index matches {q!r}")
                              + " (indices are computed after each market_hourly rollup)"]
    return Panel("indices", ("id", "kind", "level", "unit", "24h", "7d", "n", "published / reason"), rows, notes,
                 status=f"filter: {q}" if q else "")


def index_view(q: str | None = None) -> Panel:
    """`idx`: the board; an exact id, a GPU, or a filter matching one index shows the detail."""
    from analytics import indices

    if not q:
        return index_board()
    q = q.strip()
    if q in indices.REGISTRY:
        return index_detail(q)
    try:
        gpu, note = resolve_gpu_arg(q)
        iid = indices.gpu_index_id(gpu)
        if iid in indices.REGISTRY:
            p = index_detail(iid)
            if note:
                p.notes.insert(0, note)
            return p
    except Unavailable:
        pass
    board = index_board(q)
    if len(board.rows) == 1:
        return index_detail(board.rows[0][0])
    return board


def index_detail(iid: str) -> Panel:
    from analytics import indices

    lv = indices.index_level(iid)
    if lv is None:
        raise Unavailable(f"unknown index {iid!r}; type 'indices' for the board")
    notes = [f"{lv['name']}  [{lv['kind']}, {lv['segment']}{', interruptible' if lv['interruptible'] else ''}, "
             f"{lv['unit']}]  methodology {lv['methodology_version']}",
             (f"level {lv['level']:.4f} at {lv['hour']} from {lv['constituents']} constituents"
              if lv["level"] is not None else
              f"NOT PUBLISHED: {lv.get('reason') or 'no reason recorded'}"
              + (f" (raw level {lv['raw_level']:.4f})" if lv.get("raw_level") is not None else ""))]
    if lv.get("note"):
        notes.append(f"note: {lv['note']}")
    if lv.get("last_published"):
        notes.append(f"last published {lv['last_published']['level']:.4f} at {lv['last_published']['hour']}")
    rows = []
    for w, c in (lv.get("changes") or {}).items():
        rows.append(["change", w, cell(pct(c["pct"], 2), c.get("reason")),
                     f"from {c['from_level']:.4f} at {c['from_hour']}" if c.get("from_level") is not None
                     else "-", c.get("note") or ""])
    for k in ("high", "low"):
        x = lv.get(k)
        rows.append(["extreme", k, cell(None if x is None else f"{x['level']:.4f}", "index never published"),
                     f"at {x['hour']} (since {x['since']})" if x else "-", ""])
    for w, v in (lv.get("volatility") or {}).items():
        rows.append(["volatility", w, cell(None if v.get("annualized") is None else f"{v['annualized']:.1%}",
                                           v.get("reason")),
                     f"{v.get('returns', 0)} {v.get('basis', '')}, annualized", ""])
    cov = lv.get("coverage") or {}
    rows.append(["coverage", "published hours 30d",
                 Text(f"{cov.get('published_hours_30d', 0)}/{cov.get('hours_30d', 720)}", justify="right"),
                 f"first published {cov.get('first_published') or 'never'}", ""])
    try:
        cons = indices.constituents_now(iid)
    except Exception as exc:  # the detail still stands without them
        cons = {"constituents": [], "error": str(exc)}
    for c in cons.get("constituents", []):
        who = c.get("provider") or c.get("name") or c.get("index_id")
        val = c.get("price", c.get("level"))
        st = c.get("status") or "-"
        rows.append(["constituent", who, cell(None if val is None else f"{val:.4f}", st),
                     Text(st, style="green" if st == "included" else "yellow"),
                     f"recorded since {c['recorded_since']}" if c.get("recorded_since") else ""])
    if not cons.get("constituents"):
        rows.append(["constituent", "-", na(cons.get("error") or "no stored constituents"), "-", ""])
    return Panel(f"idx {iid}", ("section", "item", "value", "detail", "note"), rows, notes)


# ---- market ----------------------------------------------------------------

def context_panel(q: str | None) -> Panel:
    from analytics import stats

    gpu, note = resolve_gpu_arg(q)
    c = stats.gpu_context(gpu)
    notes = ([note] if note else []) + list(c.get("summaries") or [])
    if c.get("reason"):
        notes.append(f"unavailable: {c['reason']}")
    cur = c.get("current") or {}
    notes.append(f"now ({c.get('hour') or 'no hour'}): lowest {usd(cur.get('lowest')) or 'n/a'}, median "
                 f"{usd(cur.get('median')) or 'n/a'} across {cur.get('providers', 0)} providers "
                 f"[observed market price]; label: {c.get('label') or 'n/a: ' + str(c.get('label_reason') or c.get('reason'))}")
    rows = []
    for metric, mt in (c.get("metrics") or {}).items():
        for w, st in mt["windows"].items():
            why = st.get("reason")
            rows.append([metric, w, gap(usd(st.get("current"))),
                         gap(None if st.get("percentile") is None else f"{st['percentile']:.0f}"),
                         gap(st.get("label")), gap(usd(st.get("median"))),
                         gap(pct(st.get("distance_from_median_pct"))),
                         f"{st['samples']}/{st['window_hours']}h ({st['coverage']:.0%})",
                         Text(why, style="yellow") if why else Text("ok", style="green")])
        h = mt.get("historical") or {}
        lo, hi = h.get("low"), h.get("high")
        rows.append([metric, "all recorded", gap(usd(mt.get("current"))), "-", "-",
                     gap(f"low {usd(lo['value'])} / high {usd(hi['value'])}" if lo else None),
                     gap(pct(h.get("from_low_pct"))),
                     f"{h.get('samples', 0)} samples since {(h.get('since') or '-')[:16]}",
                     Text(h["reason"], style="yellow") if h.get("reason") else
                     Text("ok (vs median column: distance from the recorded low)", style="green")])
    return Panel(f"ctx {gpu}", ("metric", "window", "current", "pctile", "label", "median / range",
                                "vs median / low", "samples", "reason"), rows, notes)


def market_panel(q: str | None) -> Panel:
    """The same function GET /v1/markets/{gpu} returns (analytics.dispersion.market_now)."""
    from analytics import dispersion

    gpu, note = resolve_gpu_arg(q)
    m = dispersion.market_now(gpu)
    st, sc, li = m["stats"], m["score"], m["listings"]
    reasons = st.get("reasons") or {}
    notes = [note] if note else []
    if sc.get("efficiency") is not None:
        notes.append(f"efficiency {sc['efficiency']:.1f} / fragmentation {sc['fragmentation']:.1f}: {sc['label']} "
                     f"(confidence {sc['confidence']}; methodology /methodology/dispersion)")
    else:
        notes.append(f"efficiency n/a: {sc.get('reason')}")
    if m["providers"]:
        notes.append(f"{m['providers']} providers: low {usd(m['low'])}  median {usd(m['median'])}  high "
                     f"{usd(m['high'])}  spread {usd(st['spread_abs'])} ({pct(st['spread_pct_of_low'], 0)} of low)  "
                     f"[observed market price, one vote per provider]")
        notes.append("  ".join(f"{k} {v:.3f}" if st.get(k) is not None else f"{k} n/a: {reasons.get(k, 'n/a')}"
                               for k, v in (("cv", st.get("cv")), ("iqr_rel", st.get("iqr_rel")))))
    else:
        notes.append(f"no provider prices {gpu} right now: {reasons.get('all', 'no live eligible listing')}")
    notes.append(f"listings: {li['live']} live, {li['priced']} priced, {li['available']} in stock, "
                 f"{li['availability_unknown']} stock unknown, {li['sold_out']} sold out")
    rows = []
    for p in m["by_provider"]:
        prem = p["premium_vs_others_median"]
        rows.append([str(p["rank"]), p["provider"], Text(usd(p["price"]), justify="right"),
                     cell(pct(prem), "no other provider prices it",
                          style="red" if prem and prem > 0 else "green"),
                     str(p["listings"]), p["sku"], p["region"] or "-",
                     Text("*", style="green") if p["available"] else
                     Text("?", style="yellow") if p["available"] is None else Text("o", style="red")])
    return Panel(f"mkt {gpu}", ("rank", "provider", "best $/gpu-hr", "premium vs others' median", "listings", "sku",
                                "region", "stock"), rows, notes)


# ---- routing ---------------------------------------------------------------

def _exclusion_summary(by_code: dict, total: int) -> str:
    if not total:
        return "exclusions: none"
    return f"exclusions ({total}): " + ", ".join(f"{k} {v}" for k, v in sorted(by_code.items(), key=lambda x: -x[1]))


def _cand_row(label, c, extra: str = "") -> list:
    return [label, c["provider"], short(c["sku"], 40), c["region"] or "-", str(c["gpu_count"]),
            Text(usd(c["price_per_gpu_hour"]), justify="right"), Text(f"{c['score']:.3f}", justify="right"),
            Text("*", style="green") if c["available"] else Text("?", style="yellow"),
            Text(f"L{c['integration_level']}", style="green" if c["provisionable"] else "dim"),
            short((extra + " | " if extra else "") + c["explanation"], 400)]


_CAND_COLS = ("rank", "provider", "sku", "region", "x", "observed $/gpu-hr", "score", "stock", "lvl", "why")


def best_panel(q: str | None) -> Panel:
    """routing.scoring.rank_listings, as GET /v1/best/{gpu}. Ranking only: nothing is provisioned."""
    from routing import scoring

    a = parse_route_args(q, need_count=False, with_mode=True)
    gpu, note = resolve_gpu_arg(a["gpu_text"])
    if a["mode"] == "USER_DEFINED":
        raise Unavailable("USER_DEFINED needs explicit weights: use POST /v1/route/preview")
    r = scoring.rank_listings(gpu, count=a["count"], region_group=a["region_group"], mode=a["mode"], limit=25)
    mk = r["market"]
    notes = ([note] if note else []) + [
        f"RANKING ONLY - nothing provisioned. {gpu} x{a['count']} ({r['count_semantics']}), mode {r['mode']}"
        + (f", region {a['region_group']}" if a["region_group"] else "")
        + "; weights " + ", ".join(f"{k} {v:g}" for k, v in r["weights"].items() if v),
        (f"market median {usd(mk['median'])}, low {usd(mk['low'])} ({mk.get('low_provider')}) across "
         f"{mk['providers']} providers [observed market price; candidate prices are observed, not quotes]")
        if mk.get("median") is not None else "market median n/a: no priced live listing",
        f"{r['candidates_total']} candidates; " + _exclusion_summary(r["exclusions_by_code"], r["exclusions_total"]),
        "not used: " + "; ".join(f"{k} ({v})" for k, v in r["not_used"].items()),
    ]
    rows = [_cand_row(str(c["rank"]), c, c.get("vs_selected", "")) for c in r["candidates"]]
    rows += [_cand_row(f"m{c['rank']}", c, c["note"]) for c in r["multi_instance_alternatives"]]
    if not r["candidates"] and r["multi_instance_alternatives"]:
        notes.append(f"no single instance has {a['count']} GPUs; rows m1.. are multi-instance alternatives "
                     f"(never auto-provisioned)")
    if not rows:
        notes.append("no eligible listing satisfies the request; exclusions below")
        rows = [[e["code"], e["provider"], short(e["listing_id"], 40), e["region"] or "-", str(e["gpu_count"]),
                 cell(usd(e["price_per_gpu_hour"]), "no price"), "-", "-", "-", e["reason"]]
                for e in r["exclusions"][:50]]
    return Panel(f"best {gpu}", _CAND_COLS, rows, notes, status=f"mode {r['mode']}")


def preview_panel(q: str | None) -> Panel:
    """routing.engine.preview, the function POST /v1/route/preview calls, as the operator principal.

    A preview never calls a provider and never provisions; it writes the same audit record the API does.
    There is deliberately no provisioning command in the terminal."""
    from accounts.auth import OPERATOR
    from routing import engine, scoring

    a = parse_route_args(q, need_count=True, with_mode=False)
    gpu, note = resolve_gpu_arg(a["gpu_text"])
    spec = {"gpu": gpu, "count": a["count"], "region_group": a["region_group"],
            "max_price_per_gpu_hour": a["max_price"], "duration_hours": None, "deadline_hours": None,
            "mode": "BALANCED", "weights": None, "preferences": {}, "launch": None}
    scoring.effective_weights(spec["mode"], spec["weights"], bool(spec["region_group"]))
    out = engine.preview(spec, OPERATOR)
    sel, qt, mk = out["selected"], out["quote"], out["market"]
    notes = ([note] if note else []) + [
        f"PREVIEW ONLY - nothing provisioned. route_request_id {out['route_request_id']}; "
        f"live provisioning {'ENABLED' if out['live_provisioning_enabled'] else 'disabled'} in this environment",
        f"request: {gpu} x{a['count']} ({out['count_semantics']})"
        + (f", region {a['region_group']}" if a["region_group"] else "")
        + (f", max ${a['max_price']:.2f}/GPU-hr" if a["max_price"] is not None else "") + ", mode BALANCED",
    ]
    if sel is None:
        notes.append(f"no route: {out.get('reason')}")
    else:
        notes.append(f"QUOTE [{qt['kind']}, basis {qt['basis']}]: ${qt['price_per_gpu_hour']:.4f}/GPU-hr x "
                     f"{qt['gpu_count']} = ${qt['price_per_hour']:.4f}/hr  ({qt['note']})")
        notes.append(f"selected: {sel['provider']} {sel['listing_id']} at OBSERVED {usd(sel['price_per_gpu_hour'], 4)}"
                     f"/GPU-hr [observed_market_price, seen {when(sel['observed_at'])}]; market median "
                     f"{usd(mk.get('median'), 4) or 'n/a'} [observed_market_price]")
        sv = qt.get("savings_vs_median")
        if sv:
            notes.append(f"vs market median: {sv['pct']:+.1%} ({usd(sv['per_gpu_hour'], 4)}/GPU-hr); {sv['basis']}")
        notes.append("can provision selected: " + ("yes (level >= 2)" if out["can_provision_selected"] else
                     "no" + (f"; best provisionable: {out['best_provisionable']['provider']}"
                             if out.get("best_provisionable") else "")))
    for k in ("provisioning_note", "quote_note"):
        if out.get(k):
            notes.append(out[k])
    notes.append(_exclusion_summary(out["exclusions_by_code"], out["exclusions_total"]))
    rows = []
    if sel:
        rows.append(_cand_row("selected", sel))
    rows += [_cand_row(f"alt {c['rank']}", c, c.get("vs_selected", "")) for c in out["alternatives"]]
    rows += [_cand_row(f"multi {c['rank']}", c, c.get("note", "")) for c in out["multi_instance_alternatives"]]
    if not rows:
        rows = [[e["code"], e["provider"], short(e["listing_id"], 40), e["region"] or "-", str(e["gpu_count"]),
                 cell(usd(e["price_per_gpu_hour"]), "no price"), "-", "-", "-", e["reason"]]
                for e in out["exclusions"]]
    return Panel(f"preview {gpu}", _CAND_COLS, rows, notes, status="PREVIEW ONLY - nothing provisioned")


def caps_panel() -> Panel:
    from config import settings
    from routing import capabilities

    rows = []
    for c in capabilities.all_capabilities():
        sup = c["level_supported_by_provider_api"]
        rows.append([c["provider"],
                     na("not established") if sup is None else f"{sup} {c.get('level_supported_label') or ''}",
                     Text(f"{c['level_implemented']} {c['level_implemented_label']}",
                          style="green" if c["level_implemented"] >= 2 else ""),
                     Text("yes", style="green") if c["verified_live"] else Text("no", style="yellow"),
                     "yes" if c["availability_check_verified_live"] else "no",
                     c.get("via") or "-", c.get("credential_requirement") or "-",
                     "yes" if c["credentials_configured"] else "no", short(c.get("notes"), 160)])
    notes = ["levels: " + ", ".join(f"{k} {v}" for k, v in capabilities.LEVELS.items()),
             "verified_live is false everywhere: no adapter has been run against a real provider account yet",
             f"live provisioning {'ENABLED' if settings.routing_live_provisioning else 'disabled'} in this environment"]
    return Panel("caps", ("provider", "api supports", "OpenGrid implements", "verified live", "avail check live",
                          "via", "credential", "configured", "notes"), rows, notes)


# ---- events, opportunities, news -------------------------------------------

def events_panel(q: str | None = None) -> Panel:
    from analytics import events

    kw, how = {}, None
    if q:
        q = q.strip()
        if q.lower() in events.TYPES:
            kw["types"], how = [q.lower()], f"type {q.lower()}"
        else:
            try:
                kw["provider"] = resolve_provider_arg(q)
                how = f"provider {kw['provider']}"
            except Unavailable:
                try:
                    kw["gpu"], _ = resolve_gpu_arg(q)
                    how = f"gpu {kw['gpu']}"
                except Unavailable:
                    how = f"text {q!r}"
    rows_in = events.recent(limit=300, **kw)
    if how and how.startswith("text"):
        ql = q.lower()
        rows_in = [e for e in rows_in if ql in " ".join(str(e.get(k) or "") for k in
                                                       ("title", "type", "gpu", "provider")).lower()]
    sev = {"major": "bold red", "notable": "yellow", "info": "dim"}
    rows = [[when(e["occurred_at"]), Text(e["severity"], style=sev.get(e["severity"], "")), e["type"],
             e.get("gpu") or "-", e.get("provider") or "-", e.get("region_group") or "-", e.get("kind") or "-",
             short(e["title"], 160)] for e in rows_in]
    notes = [] if rows else ["no market events match" + (f" ({how})" if how else "")
                             + "; events are detected hourly from the market_hourly rollup, so thin history "
                               "yields few"]
    return Panel("events", ("when", "sev", "type", "gpu", "provider", "region", "kind", "title"), rows, notes,
                 status=f"filter: {how}" if how else "")


def opportunities_panel() -> Panel:
    from analytics import opportunities

    o = opportunities.opportunities()
    rows = [[Text(f"{x['score']:.1f}", justify="right"), x["type"], x.get("gpu") or "-",
             x.get("provider") or "-", x.get("kind") or "-", short(x["explanation"], 300)] for x in o["items"]]
    rows += [[na("unavailable"), u["type"], "-", "-", "-", Text(u["reason"], style="yellow")]
             for u in o.get("unavailable", [])]
    notes = [f"as of hour {o.get('as_of_hour') or 'n/a: no market history yet'}; {o.get('total', len(o['items']))} "
             f"opportunities; {len(o.get('unavailable', []))} types unavailable (reasons below)"]
    return Panel("opp", ("score", "type", "gpu", "provider", "kind", "explanation / reason"), rows, notes)


def news_panel(q: str | None = None) -> Panel:
    from news import store

    kw, how = {}, None
    if q:
        q = q.strip()
        try:
            kw["provider"] = resolve_provider_arg(q)
            how = f"provider {kw['provider']}"
        except Unavailable:
            try:
                kw["gpu"], _ = resolve_gpu_arg(q)
                how = f"gpu {kw['gpu']}"
            except Unavailable:
                kw["q"], how = q, f"text {q!r}"
    items, total = store.list_stories(limit=100, **kw)
    rows = []
    for d in items:
        pub = d.get("last_published_at") or d["published_at"]
        t = when(pub) + (" ~" if d.get("published_at_inferred") else "")
        rows.append([t, Text(str(d.get("source_count", 1)), justify="right"),
                     Text(f"{d['relevance']:.2f}" if d.get("relevance") is not None else "-", justify="right"),
                     short(d["title"], 140), d.get("source_name") or "-", ",".join(d.get("topics") or []) or "-"])
    notes = [f"{total} stories" + (f" ({how})" if how else "")
             + "; related means mentions the same entities, never that news caused a price move"
             + ("; '~' = publish time inferred" if any(d.get("published_at_inferred") for d in items) else "")]
    if not items:
        notes.append("no news stored" + (" for this filter" if how else "")
                     + " (news is ingested by the server's background job)")
    return Panel("news", ("published", "sources", "relevance", "title", "source", "topics"), rows, notes,
                 status=f"filter: {how}" if how else "")


# ---- providers and ops -----------------------------------------------------

def provider_panel(q: str | None) -> Panel:
    import provider_meta
    from analytics import dispersion, providers
    from quality import trust

    p = resolve_provider_arg(q)
    meta = provider_meta.meta(p)
    h = trust.provider_health(p)
    value = providers.provider_value_all(30).get(p, {})
    allv = value.get(providers.ALL)
    st_style = {"healthy": "green", "degraded": "yellow", "down": "red"}
    notes = [f"{meta.display_name} [{meta.provider_class}, {meta.source_type}{', ' + meta.source if meta.source else ''}]",
             f"health: {h['status'].upper()}" + (f" - {'; '.join(h['status_reasons'])}" if h["status_reasons"] else "")
             + f"; last ok fetch {when(h['last_ok_fetch'])}; {h['consecutive_failures']} consecutive failures; "
               f"24h: {h['fetches_24h']} fetches, fail rate "
             + (f"{h['failure_rate_24h']:.1%}" if h["failure_rate_24h"] is not None else "n/a: no fetches in 24h")
             + f", latency p50/p95 {num(h['latency_ms_p50_24h'])}/{num(h['latency_ms_p95_24h'])} ms",
             f"listings now {h['listings_now']} (24h ago: "
             + (str(h["listings_24h_ago"]) if h["listings_24h_ago"] is not None else f"n/a: {h['listings_24h_ago_note']}")
             + f"); quarantined {h['quarantined']}; schema changes 7d {h['schema_changes_7d']}; open incidents "
             + (", ".join(f"{k} {v}" for k, v in h["open_incidents"].items()) or "none")]
    if allv is None:
        notes.append("relative value (30d) n/a: no daily structure rows for this provider yet")
    else:
        why = allv.get("reasons") or {}
        parts = []
        for k, label, f in (("premium_avg", "avg premium", pct), ("cheapest_share", "cheapest", lambda x: f"{x:.0%}"),
                            ("top3_share", "top-3", lambda x: f"{x:.0%}"), ("availability", "availability",
                                                                           lambda x: f"{x:.0%}"),
                            ("volatility_daily", "daily vol", lambda x: f"{x:.2%}")):
            v = allv.get(k)
            parts.append(f"{label} {f(v)}" if v is not None else f"{label} n/a ({why.get(k, 'no data')})")
        notes.append("relative value 30d: " + "; ".join(parts))
    for fact in providers.facts(p, value, 30):
        notes.append(fact)
    rows = []
    for g, m in sorted(dispersion.all_markets().items()):
        mine = next((x for x in m["by_provider"] if x["provider"] == p), None)
        if mine is None:
            continue
        w = value.get(g)
        wprem = None if w is None else w.get("premium_avg")
        wwhy = "no 30d history" if w is None else (w.get("reasons") or {}).get("premium_avg")
        rows.append([g, Text(usd(mine["price"]), justify="right"), f"{mine['rank']}/{m['providers']}",
                     cell(usd(m["median"]), "no market"),
                     cell(pct(mine["premium_vs_others_median"]), "no other provider prices it"),
                     cell(pct(wprem), wwhy), str(mine["listings"])])
    if not rows:
        notes.append(f"no priced live listing from {p} right now")
    return Panel(f"prov {p}", ("gpu", "best now", "rank", "market median", "premium now", "avg premium 30d",
                               "listings"), rows, notes,
                 status=Text(h["status"], style=st_style.get(h["status"], "")).plain)


def ops_panel() -> Panel:
    from quality import incidents, ops

    s = ops.summary()
    pv = s["providers"]
    style = {"healthy": "green", "degraded": "yellow", "down": "red"}
    rows = []
    for h in pv["items"]:
        rows.append([h["provider"], Text(h["status"], style=style.get(h["status"], "")),
                     short("; ".join(h["status_reasons"]) or "-", 120), when(h["last_ok_fetch"]),
                     str(h["consecutive_failures"]),
                     cell(None if h["failure_rate_24h"] is None else f"{h['failure_rate_24h']:.1%}", "no fetches in 24h"),
                     cell(num(h["latency_ms_p50_24h"]) if h["latency_ms_p50_24h"] is not None else None, "no fetches"),
                     cell(num(h["latency_ms_p95_24h"]) if h["latency_ms_p95_24h"] is not None else None, "no fetches"),
                     str(h["listings_now"]),
                     cell(h["listings_24h_ago"], "insufficient history"),
                     str(h["quarantined"]), str(h["schema_changes_7d"])])
    q = s["quarantine"]
    notes = [f"providers {pv['count']}: " + ", ".join(f"{k} {v}" for k, v in pv["by_status"].items()),
             f"quarantine pending {q['pending']}" + (" (" + ", ".join(f"{k} {v}" for k, v in q["by_rule"].items()) + ")"
                                                     if q["by_rule"] else ""),
             "open incidents: " + (", ".join(f"{k} {v}" for k, v in s["incidents_open"].items()) or "none"),
             f"stale listings {s['stale_listings']['count']}; unmapped GPUs {s['unmapped_gpus']['count']}; "
             f"schema changes 7d {s['schema_changes_7d']['count']}",
             "jobs: " + (", ".join(f"{j['name']} {j['status']}" for j in s["jobs"]) or "none registered")
             + " (background jobs run in the server process, not in this terminal)"]
    for i in incidents.recent(status="open", limit=8):
        notes.append(f"  incident {i['kind']} [{i['severity']}] {i.get('provider') or '-'} "
                     f"x{i['count']} last {when(i['last_seen'])}")
    return Panel("ops", ("provider", "status", "reasons", "last ok", "consec fail", "fail 24h", "p50 ms", "p95 ms",
                         "listings", "24h ago", "quarantined", "schema 7d"), rows, notes)


# ---- families --------------------------------------------------------------

def _families():
    try:
        return importlib.import_module("families")
    except ImportError:
        raise Unavailable("GPU families unavailable: the families module is not installed yet")


def family_panel(q: str | None) -> Panel:
    """A family's canonical variants side by side, each with its own live low (dispersion.market_now,
    the numbers /v1/families uses). families.py is imported lazily; its absence is said, not hidden."""
    from analytics import dispersion

    fam = _families()
    markets = dispersion.all_markets()
    if not q or not q.strip():
        rows = []
        for d in fam.all_families():
            lows = [(markets[g]["low"], g) for g in d["variants"] if g in markets and markets[g]["low"] is not None]
            lo = min(lows) if lows else None
            rows.append([d["slug"], d["kind"], str(len(d["variants"])), str(len(lows)),
                         cell(usd(lo[0]) if lo else None, "no variant has a live priced listing now"),
                         lo[1] if lo else "-"])
        return Panel("fam", ("family", "kind", "variants", "priced", "cheapest low", "cheapest variant"), rows,
                     [fam.NOTE, "type fam <family> for its variants"])
    d = fam.family(q.strip())
    if d is None:
        known = ", ".join(x["slug"] for x in fam.all_families())
        raise Unavailable(f"unknown GPU family {q.strip()!r}; known: {known}")
    rows = []
    for g in d["variants"]:
        m = markets.get(g)
        priced = bool(m and m["providers"])
        rows.append([g, cell(usd(m["low"]) if priced else None, "no live priced listing now"),
                     m["by_provider"][0]["provider"] if priced else "-",
                     cell(usd(m["median"]) if priced else None, "no live priced listing now"),
                     str(m["providers"] if m else 0), str(m["listings"]["available"] if m else 0)])
    rows.sort(key=lambda r: (r[1].plain.startswith("n/a"), r[1].plain))
    notes = [f"{d['name']} ({d['kind']}{', ' + d['architecture'] if d.get('architecture') else ''}): "
             f"{len(d['variants'])} variants. {fam.NOTE}",
             "lows are observed market prices: each provider's lowest eligible live price, lowest across providers"]
    if d.get("member_families"):
        notes.append("member families: " + ", ".join(d["member_families"]))
    return Panel(f"fam {d['id']}", ("variant", "low $/gpu-hr", "low provider", "median", "providers", "in stock"),
                 rows, notes)


class OpenGrid(App):
    TITLE = "OpenGrid"
    CSS = """
    #status { height: 1; padding: 0 1; background: $boost; color: $text-muted; }
    #note { height: auto; max-height: 14; padding: 0 1; color: $text; display: none; }
    DataTable { height: 1fr; }
    Input { dock: bottom; }
    """

    def __init__(self) -> None:
        super().__init__()
        self.ingest = Ingest()
        self.view = "listings"
        self.q: str | None = None
        self.only_available = False
        self.limit: int | None = None
        self.busy = False
        self.panel: Panel | None = None
        self._panel_seq = 0

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="status")
        yield Static(id="note")
        yield DataTable(zebra_stripes=True, cursor_type="row")
        yield Input(placeholder="type a GPU, or: help", id="cmd")

    async def on_mount(self) -> None:
        init_db()
        self.ingest.start()
        # Redraw periodically so polled data appears without a keystroke.
        self.set_interval(5, self._tick)
        self.query_one(Input).focus()
        self.render_view()

    async def on_unmount(self) -> None:
        await self.ingest.stop()

    def show(self, view: str, q: str | None = None, only_available: bool = False, limit: int | None = None):
        self.view, self.q, self.only_available, self.limit = view, q, only_available, limit
        self.render_view()

    def _tick(self) -> None:
        # A computed panel is a snapshot: re-querying it every 5 s would reset the cursor for nothing.
        if self.view != "panel":
            self.render_view()

    def show_panel(self, panel: Panel) -> None:
        self.view, self.panel = "panel", panel
        self.render_view()

    def run_panel(self, title: str, fn: Callable[..., Panel], *args) -> None:
        """Compute a panel in a thread; only the latest request's result is shown."""
        self._panel_seq += 1
        self.show_panel(Panel(title, ("",), [], [f"{title}: loading..."], status="loading"))
        self.run_worker(self._panel_worker(self._panel_seq, title, fn, args), group="panel", exclusive=True)

    async def _panel_worker(self, seq: int, title: str, fn, args) -> None:
        try:
            panel = await asyncio.to_thread(fn, *args)
        except Unavailable as exc:
            panel = Panel(title, exc.columns or ("unavailable",), exc.rows or [[Text(str(exc), style="yellow")]],
                          [f"unavailable: {exc}"], status="unavailable")
            self.notify(str(exc), title=title, severity="warning", timeout=8)
        except Exception as exc:
            logging.getLogger(__name__).exception("%s failed", title)
            panel = Panel(title, ("error",), [[Text(f"{type(exc).__name__}: {exc}", style="red")]],
                          [f"{title} failed: {type(exc).__name__}: {exc}"], status="error")
            self.notify(f"{title} failed: {exc}", severity="error", timeout=10)
        if seq == self._panel_seq:
            self.show_panel(panel)

    def run_fetch(self) -> None:
        if self.busy:
            self.notify("already running")
            return
        self.busy = True
        self.notify("fetching...")
        self.run_worker(self._fetch_worker(), exclusive=True)

    async def _fetch_worker(self) -> None:
        try:
            responses = await fetch_all()
            await asyncio.to_thread(save, responses)
            failed = sum(1 for r in responses if not r.ok)
            result = await asyncio.to_thread(self._normalize)
            self.notify(
                f"{len(responses)} responses ({failed} failed) - "
                f"{result['listings']} listings, {result['observations']} changes recorded"
            )
        except Exception as exc:
            self.notify(f"fetch failed: {exc}", severity="error")
        finally:
            self.busy = False
            self.render_view()

    def run_normalize(self) -> None:
        try:
            result = self._normalize()
            self.notify(
                f"{result['listings']} listings, {result['observations']} changes, "
                f"{result['reference_prices']} reference prices"
            )
        except Exception as exc:
            self.notify(f"normalize failed: {exc}", severity="error")
        self.render_view()

    @staticmethod
    def _normalize() -> dict:
        return normalize.refresh()

    def render_view(self) -> None:
        table = self.query_one(DataTable)
        table.clear(columns=True)
        note = self.query_one("#note", Static)
        if self.view == "panel":
            p = self.panel
            table.add_columns(*p.columns)
            for r in p.rows:
                table.add_row(*r)
            note.update(Text("\n".join(str(n) for n in p.notes)))
            note.display = bool(p.notes)
            parts = [p.title, f"{len(p.rows)} rows"] + ([p.status] if p.status else [])
            if self.busy:
                parts.append("fetching...")
            self.query_one("#status", Static).update(" · ".join(parts))
            return
        note.display = False
        count = {
            "listings": self._render_listings,
            "history": self._render_history,
            "mapping": self._render_mapping,
            "quirks": self._render_quirks,
            "refs": self._render_refs,
            "unmapped": self._render_unmapped,
            "raw": self._render_raw,
            "summary": self._render_summary,
        }[self.view](table)
        parts = [self.view]
        if self.q:
            parts.append(f"filter: {self.q}")
        if self.only_available:
            parts.append("in stock only")
        parts.append(f"{count} rows")
        if self.busy:
            parts.append("fetching...")
        parts.append(f"polling: {self.ingest.describe()}")
        self.query_one("#status", Static).update(" \u00b7 ".join(parts))

    def _render_listings(self, table: DataTable) -> int:
        rows = normalize.listings(self.q, self.only_available, self.limit)
        table.add_columns(
            "provider", "canonical gpu", "x", "region", "market", "tier", "sku",
            "vcpu", "ram", "disk", "$/gpu-hr", "d", "prev", "$/inst-hr",
            "stock", "cap", "unit", "age"
        )
        for r in rows:
            table.add_row(
                r["provider"],
                r["canonical_gpu_name"] or Text(f"? {r['raw_gpu_name']}", style="yellow"),
                str(r["gpu_count"]),
                r["region"] or "-",
                r["market_type"] or "-",
                r["provider_tier"] or "-",
                r["sku"],
                num(r["vcpu"]),
                num(r["ram_gb"]),
                num(r["storage_gb"]),
                Text(num(r["price_per_gpu_hour"], 3), justify="right"),
                price_arrow(r["price_per_gpu_hour"], r["previous_price_per_gpu_hour"]),
                Text(num(r["previous_price_per_gpu_hour"], 3), style="dim", justify="right"),
                Text(num(r["price_per_instance_hour"], 2), justify="right"),
                Text("*", style="green") if r["available"] else Text("o", style="red"),
                num(r["capacity"]),
                r["capacity_unit"] or "-",
                age(r["observed_at"]),
            )
        return len(rows)

    def _render_history(self, table: DataTable) -> int:
        rows = normalize.history(self.q)
        table.add_columns(
            "when", "provider", "canonical gpu", "x", "region", "market", "tier",
            "sku", "$/gpu-hr", "$/inst-hr", "stock", "cap", "unit"
        )
        for r in rows:
            table.add_row(
                f"{r['observed_at']:%m-%d %H:%M:%S}",
                r["provider"],
                r["canonical_gpu_name"] or Text(f"? {r['raw_gpu_name']}", style="yellow"),
                str(r["gpu_count"]),
                r["region"] or "-",
                r["market_type"] or "-",
                r["provider_tier"] or "-",
                r["sku"],
                Text(num(r["price_per_gpu_hour"], 3), justify="right"),
                Text(num(r["price_per_instance_hour"], 2), justify="right"),
                Text("*", style="green") if r["available"] else Text("o", style="red"),
                num(r["capacity"]),
                r["capacity_unit"] or "-",
            )
        return len(rows)

    def _render_refs(self, table: DataTable) -> int:
        rows = normalize.reference_prices(self.q)
        table.add_columns("provider", "name", "kind", "listed", "value", "was", "discount", "age")
        for r in rows:
            table.add_row(
                r["provider"],
                r["name"],
                r["kind"] or Text("?", style="yellow"),
                Text("listing", style="green") if r["is_listed"] else Text("-", style="dim"),
                Text(num(r["value"], 6), justify="right"),
                Text(num(r["original_value"], 6), justify="right"),
                "yes" if r["discount_applied"] else "-",
                age(r["observed_at"]),
            )
        return len(rows)

    KIND_STYLES = {
        mapping.RAW: "green",
        mapping.DERIVED: "cyan",
        mapping.CONSTANT: "yellow",
        mapping.LOOKUP: "magenta",
        mapping.ABSENT: "red",
    }

    def _wanted_providers(self) -> list[str]:
        if not self.q:
            return list(mapping.PROVIDER_MAPPINGS)
        hit = [p for p in mapping.PROVIDER_MAPPINGS if self.q.lower() in p.lower()]
        return hit or list(mapping.PROVIDER_MAPPINGS)

    def _render_mapping(self, table: DataTable) -> int:
        """Side by side, one row per ComputeListing field.

        All providers: compact, so three columns stay readable. One provider:
        the notes as well. A field the providers disagree on is marked.
        """
        wanted = self._wanted_providers()
        detail = len(wanted) == 1
        table.add_columns(
            "", "field", *(wanted if not detail else [wanted[0], "note"])
        )
        for row in mapping.comparison_rows(wanted):
            marker = Text("!", style="bold yellow") if row["differs"] else Text("")
            cells = [marker, Text(row["field"], style="bold" if row["differs"] else "")]
            for provider in wanted:
                fm = row.get(provider)
                if fm is None:
                    cells.append(Text("(undocumented)", style="red"))
                else:
                    cells.append(
                        Text(f"{fm.kind}: {fm.source}", style=self.KIND_STYLES.get(fm.kind, ""))
                    )
                    if detail:
                        cells.append(Text(fm.note, style="dim"))
            table.add_row(*cells)
        return len(mapping.MODEL_FIELDS)

    def _render_quirks(self, table: DataTable) -> int:
        rows = mapping.quirk_rows(self._wanted_providers())
        table.add_columns("provider", "gotcha")
        for r in rows:
            table.add_row(r["provider"], r["quirk"])
        return len(rows)

    def _render_unmapped(self, table: DataTable) -> int:
        rows = normalize.unmapped_gpus()
        table.add_columns("provider", "raw gpu name", "reason")
        for r in rows:
            table.add_row(r["provider"], r["raw_gpu_name"], r["reason"])
        return len(rows)

    def _render_raw(self, table: DataTable) -> int:
        rows = raw_store.snapshots(q=self.q)
        table.add_columns("provider", "endpoint", "method", "status", "ms", "bytes", "age")
        for r in rows:
            table.add_row(
                r["provider"], r["endpoint"], r["method"],
                Text(str(r["status_code"] or "-"), style="green" if r["ok"] else "red"),
                str(r["duration_ms"] or "-"), f"{(r['bytes'] or 0):,}", age(r["fetched_at"]),
            )
        return len(rows)

    def _render_summary(self, table: DataTable) -> int:
        rows = raw_store.endpoint_summary()
        table.add_columns("provider", "endpoint", "fetches", "distinct", "avg ms", "fails", "last")
        for r in rows:
            table.add_row(
                r["provider"], r["endpoint"], str(r["fetches"]), str(r["distinct_bodies"]),
                str(int(r["avg_ms"] or 0)),
                Text(str(r["failures"]), style="red" if r["failures"] else "dim"),
                age(r["last_fetch"]),
            )
        return len(rows)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return
        for cmd in COMMANDS:
            if m := cmd.pattern.fullmatch(text):
                try:
                    cmd.handler(self, m)
                except Exception as exc:
                    self.notify(f"{text}: {exc}", severity="error")
                return
        self.notify(f"unknown command: {text}", severity="warning")


def main() -> None:
    log_dir = Path.home() / ".opengrid"
    log_dir.mkdir(exist_ok=True)
    logging.basicConfig(filename=log_dir / "opengrid.log", level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    OpenGrid().run()


if __name__ == "__main__":
    main()
