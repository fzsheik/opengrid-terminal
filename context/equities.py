"""INTERNAL research only: equity prices of GPU-relevant companies from the Alpaca Market Data API
(methodology/internal/equities.md). Never displayed, never served by a public read, never resold or republished.

    TICKERS                  the default list (EQUITY_TICKERS overrides it, comma-separated)
    refresh(full=False)      the context_equities job / `python -m context equities forward|backfill`:
                             daily bars (split-adjusted, raw, total-return "all") from 2016-01-01 into context_series
                             (visibility "internal"), hourly split-adjusted bars for the last 5 years into equity_bars;
                             every response page stored raw in context_snapshots first, failures too
    catalog()                the internal series, with points and latest
    points(...) / hourly(...) admin reads
    moves(start, end)        equity % moves beside GPU market median/lowest moves for a date window (internal context)

Market data only (data.alpaca.markets); the trading API is never called. SIP feed only: IEX volume is a fraction of
consolidated volume, so feeds are never mixed in one series. Alpaca's free plan refuses SIP for the most recent 15
minutes, so every request ends at now - 16 min, a daily bar is kept only once its session (incl. extended hours) is
over (20:15 ET), an hourly bar only once its hour has closed 16 minutes ago.
"""

from __future__ import annotations

import asyncio
import logging
import time as _time
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger(__name__)

SOURCE = "alpaca"
VISIBILITY = "internal"
BARS_URL = "https://data.alpaca.markets/v2/stocks/bars"
FEED = "sip"
ET = ZoneInfo("America/New_York")

TICKERS = ("NVDA", "AMD", "TSM", "AVGO", "MU", "INTC", "ARM", "SMCI", "DELL", "CRWV", "NBIS", "ORCL", "MSFT", "GOOGL",
           "AMZN", "META", "VRT", "ANET", "SMH", "SOXX",
           # Power: generators selling to datacenters, utilities in the datacenter regions (Virginia, Texas, the
           # Southeast, the Midwest), nuclear and fuel, grid and datacenter power equipment, and the utilities ETF.
           "CEG", "VST", "TLN", "NRG", "D", "AEP", "SO", "EXC", "NEE", "DUK", "ETR", "XEL", "OKLO", "SMR", "CCJ",
           "GEV", "ETN", "PWR", "BE", "XLU")
ETFS = frozenset({"SMH", "SOXX", "XLU"})

DAILY_START = date(2016, 1, 1)
HOURLY_YEARS = 5
FULL = ("open", "high", "low", "close", "volume", "vwap")
# (timeframe, adjustment) -> fields kept. "all" (splits + dividends) is the total-return close only.
DAILY_PLANS = {"split": FULL, "raw": FULL, "all": ("close",)}
HOURLY_PLANS = ("split",)
BAR_KEYS = {"open": "o", "high": "h", "low": "l", "close": "c", "volume": "v", "vwap": "vw"}

PAGE_LIMIT = 10000
PACE_SECONDS = 0.35           # <= ~170 requests a minute; Alpaca's free plan allows 200
RETRY_SECONDS = 60.0          # after a 429 without Retry-After
MAX_429 = 3
BACKOFF_SECONDS = (2.0, 10.0, 30.0)   # dropped connections, timeouts, 5xx
SIP_DELAY = timedelta(minutes=16)
SESSION_DONE = time(20, 15)   # ET: extended hours end at 20:00
OVERLAP_DAILY = timedelta(days=10)
OVERLAP_HOURLY = timedelta(days=3)
POLL_SECONDS = 3600
Q = Decimal("0.000001")
TIMEOUT = httpx.Timeout(60.0, connect=15.0)
RULE = ("INTERNAL: equity prices are OpenGrid research context, never displayed, served publicly or republished. "
        "Context, not cause.")


def tickers() -> tuple[str, ...]:
    from config import settings

    if settings.equity_tickers:
        return tuple(dict.fromkeys(t.strip().upper() for t in settings.equity_tickers.split(",") if t.strip()))
    return TICKERS


def enabled() -> bool:
    from config import settings

    return bool(settings.alpaca_data_key_id and settings.alpaca_data_secret_key)


def series_id(symbol: str, field: str, adjustment: str) -> str:
    return f"alpaca.{symbol.lower()}.{field}.{adjustment}"


def split_series_id(sid: str) -> tuple[str, str, str]:
    _, sym, field, adj = sid.split(".")
    return sym.upper(), field, adj


def unit(field: str) -> str:
    return "shares" if field == "volume" else "usd"


def catalog() -> list[dict]:
    out = []
    for sym in tickers():
        for adj, fields in DAILY_PLANS.items():
            for f in fields:
                out.append({"series_id": series_id(sym, f, adj), "source": SOURCE, "entity": sym, "geo": None,
                            "name": f"{sym} daily {f} ({adj}-adjusted)" if adj != "raw" else f"{sym} daily {f} (unadjusted)",
                            "unit": unit(f), "frequency": "daily", "adjustment": adj, "field": f,
                            "kind": "etf" if sym in ETFS else "equity", "visibility": VISIBILITY})
    return out


# --------------------------------------------------------------------------- parsing

def parse_ts(v: str) -> datetime:
    """Alpaca RFC 3339; nanoseconds cut to microseconds."""
    s = v.replace("Z", "+00:00")
    if "." in s:
        head, rest = s.split(".", 1)
        i = next((k for k, ch in enumerate(rest) if ch in "+-"), len(rest))
        s = f"{head}.{rest[:i][:6]}{rest[i:]}"
    t = datetime.fromisoformat(s)
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def session_date(t: datetime) -> date:
    return t.astimezone(ET).date()


def session_complete(d: date, now: datetime) -> bool:
    return now >= datetime.combine(d, SESSION_DONE, tzinfo=ET)


def hour_complete(t: datetime, now: datetime) -> bool:
    return t + timedelta(hours=1) <= now - SIP_DELAY


def _dec(v) -> Decimal:
    return Decimal(str(v)).quantize(Q)


def _bars(payload: dict) -> dict:
    bars = payload.get("bars")
    if bars is None:
        return {}
    if not isinstance(bars, dict):
        raise ValueError("alpaca bars: expected an object keyed by symbol")
    return bars


def daily_points(payload: dict, adjustment: str, now: datetime) -> list[dict]:
    """context_series points (visibility internal) for complete sessions only."""
    fields = DAILY_PLANS[adjustment]
    out = []
    for sym, bars in _bars(payload).items():
        for b in bars:
            d = session_date(parse_ts(b["t"]))
            if not session_complete(d, now):
                continue
            for f in fields:
                v = b.get(BAR_KEYS[f])
                if v is None:
                    continue
                out.append({"series_id": series_id(sym, f, adjustment), "period_start": d, "period_end": d,
                            "value": _dec(v), "unit": unit(f), "source": SOURCE, "basis": "reported",
                            "published_at": None, "meta": None, "visibility": VISIBILITY})
    return out


def hourly_rows(payload: dict, timeframe: str, adjustment: str, now: datetime) -> list[dict]:
    out = []
    for sym, bars in _bars(payload).items():
        for b in bars:
            t = parse_ts(b["t"])
            if not hour_complete(t, now):
                continue
            out.append({"symbol": sym, "timeframe": timeframe, "adjustment": adjustment, "t": t,
                        "open": _dec(b["o"]), "high": _dec(b["h"]), "low": _dec(b["l"]), "close": _dec(b["c"]),
                        "volume": _dec(b["v"]), "vwap": None if b.get("vw") is None else _dec(b["vw"]),
                        "trades": b.get("n"), "feed": FEED})
    return out


# --------------------------------------------------------------------------- storage

def _session():
    import normalize

    return normalize.SessionLocal


def upsert_daily(points: list[dict], snapshot_id: int | None, now: datetime) -> dict:
    """INSERT .. ON CONFLICT: a new period is inserted; a different value for a known one is a revision (counted,
    previous value kept in meta). Returns counts and the revised (series_id, period_end) pairs."""
    from sqlalchemy import String, cast, func, literal_column
    from sqlalchemy.dialects.postgresql import JSONB, insert

    from store.context import ContextSeries as C

    new, revised = 0, []
    with _session().begin() as s:
        for i in range(0, len(points), 4000):
            rows = [{**p, "first_seen_at": now, "updated_at": now, "revisions": 0, "snapshot_id": snapshot_id}
                    for p in points[i:i + 4000]]
            stmt = insert(C).values(rows)
            ex = stmt.excluded
            stmt = stmt.on_conflict_do_update(
                index_elements=[C.series_id, C.period_start, C.period_end],
                set_={"value": ex.value, "updated_at": ex.updated_at, "revisions": C.revisions + 1,
                      "snapshot_id": ex.snapshot_id,
                      "meta": literal_column("CASE WHEN jsonb_typeof(context_series.meta) = 'object' "
                                     "THEN context_series.meta ELSE '{}'::jsonb END", JSONB).op("||")(
                          func.jsonb_build_object("previous_value", cast(C.value, String)))},
                where=C.value != ex.value,
            ).returning(C.series_id, C.period_end, literal_column("(xmax = 0)").label("inserted"))
            for r in s.execute(stmt):
                if r.inserted:
                    new += 1
                else:
                    revised.append((r.series_id, r.period_end))
    return {"new": new, "revised": len(revised), "revised_keys": revised}


def upsert_bars(rows: list[dict], snapshot_id: int | None, now: datetime) -> dict:
    from sqlalchemy import literal_column, or_
    from sqlalchemy.dialects.postgresql import insert

    from store.context import EquityBar as B

    new, revised = 0, []
    cols = ("open", "high", "low", "close", "volume", "vwap", "trades")
    with _session().begin() as s:
        for i in range(0, len(rows), 3000):
            chunk = [{**r, "first_seen_at": now, "updated_at": now, "revisions": 0, "snapshot_id": snapshot_id}
                     for r in rows[i:i + 3000]]
            stmt = insert(B).values(chunk)
            ex = stmt.excluded
            stmt = stmt.on_conflict_do_update(
                index_elements=[B.symbol, B.timeframe, B.adjustment, B.t],
                set_={**{c: getattr(ex, c) for c in cols}, "updated_at": ex.updated_at,
                      "revisions": B.revisions + 1, "snapshot_id": ex.snapshot_id},
                where=or_(*[getattr(B, c).is_distinct_from(getattr(ex, c)) for c in cols if c != "trades"]),
            ).returning(B.symbol, B.t.label("bar_start"), literal_column("(xmax = 0)").label("inserted"))
            for r in s.execute(stmt):
                if r.inserted:
                    new += 1
                else:
                    revised.append((r.symbol, r.bar_start))
    return {"new": new, "revised": len(revised), "revised_keys": revised}


def latest_daily(symbols, adjustment: str) -> dict[str, date]:
    from sqlalchemy import text

    ids = {series_id(s, "close", adjustment): s for s in symbols}
    with _session()() as s:
        rows = s.execute(text("""SELECT series_id, max(period_end) FROM context_series
                                 WHERE series_id = ANY(:ids) GROUP BY series_id"""), {"ids": list(ids)}).all()
    return {ids[r[0]]: r[1] for r in rows}


def latest_hourly(symbols, timeframe: str, adjustment: str) -> dict[str, datetime]:
    from sqlalchemy import text

    with _session()() as s:
        rows = s.execute(text("""SELECT symbol, max(t) FROM equity_bars WHERE timeframe = :tf AND adjustment = :a
                                 AND symbol = ANY(:s) GROUP BY symbol"""),
                         {"tf": timeframe, "a": adjustment, "s": list(symbols)}).all()
    return dict(rows)


# --------------------------------------------------------------------------- fetch

def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Fetcher:
    """Paged GETs of /v2/stocks/bars, paced under the free-plan rate limit, every page stored raw first."""

    def __init__(self, client: httpx.AsyncClient, key_id: str, secret: str):
        self.client, self._secrets = client, (key_id, secret)
        self.headers = {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret, "Accept": "application/json"}
        self.requests = self.failed = 0

    def _redact(self, msg: str) -> str:
        for s in self._secrets:
            msg = msg.replace(s, "***")
        return msg[:500]

    async def _get(self, params: dict):
        """429: wait (Retry-After) and retry; a dropped connection, timeout or 5xx: back off and retry. The last
        attempt's response (or exception) is what the caller sees."""
        transient = 0
        throttled = 0
        while True:
            await asyncio.sleep(PACE_SECONDS)
            self.requests += 1
            started = _time.perf_counter()
            try:
                resp = await self.client.get(BARS_URL, params=params, headers=self.headers)
            except httpx.TransportError:
                if transient >= len(BACKOFF_SECONDS):
                    raise
                await asyncio.sleep(BACKOFF_SECONDS[transient])
                transient += 1
                continue
            ms = int((_time.perf_counter() - started) * 1000)
            if resp.status_code == 429 and throttled < MAX_429:
                throttled += 1
                wait = float(resp.headers.get("retry-after") or RETRY_SECONDS)
                log.warning("alpaca 429; waiting %.0fs", wait)
                await asyncio.sleep(min(wait, 120.0))
                continue
            if resp.status_code >= 500 and transient < len(BACKOFF_SECONDS):
                await asyncio.sleep(BACKOFF_SECONDS[transient])
                transient += 1
                continue
            return resp, ms

    async def bars(self, symbols: list[str], timeframe: str, adjustment: str, start: datetime, end: datetime,
                   on_page) -> int:
        """Every page of one request; on_page(payload, snapshot_id) parses and stores. Returns pages stored."""
        from context.series import save_snapshot

        pages, token, shifted = 0, None, False
        key = f"bars:{timeframe}:{adjustment}:{','.join(symbols)}"[:200]
        while True:
            params = {"symbols": ",".join(symbols), "timeframe": timeframe, "start": _iso(start), "end": _iso(end),
                      "adjustment": adjustment, "feed": FEED, "limit": PAGE_LIMIT, "sort": "asc"}
            if token:
                params["page_token"] = token
            url = f"{BARS_URL}?{urlencode(params)}"
            now = datetime.now(timezone.utc)
            status = ms = None
            try:
                resp, ms = await self._get(params)
                status = resp.status_code
                if status == 403 and not shifted and token is None and "recent SIP" in resp.text:
                    shifted, end = True, end - timedelta(hours=1)   # the 15-minute rule: step back, never IEX
                    continue
                resp.raise_for_status()
                body = resp.json()
                payload = {"request": {k: v for k, v in params.items()}, "bars": _bars(body),
                           "next_page_token": body.get("next_page_token")}
            except Exception as exc:  # noqa: BLE001 - stored as a failed fetch
                self.failed += 1
                await asyncio.to_thread(save_snapshot, SOURCE, key, url, now, status, ms, False,
                                        self._redact(f"{type(exc).__name__}: {exc}"), None)
                return pages
            sid = await asyncio.to_thread(save_snapshot, SOURCE, key, url, now, status, ms, True, None, payload)
            pages += 1
            await on_page(payload, sid)
            token = payload["next_page_token"]
            if not token:
                return pages


async def refresh(client: httpx.AsyncClient | None = None, full: bool = False, now: datetime | None = None,
                  symbols: tuple[str, ...] | None = None, hourly: bool = True) -> dict:
    """Forward (default): from each symbol's latest stored bar minus an overlap; a symbol with nothing stored is
    backfilled. full=True re-reads everything (idempotent). A revised close on an adjusted series before the latest
    stored bar means a new split or dividend: that symbol's adjusted history is re-read in full."""
    from config import settings

    if not enabled():
        return {"source": SOURCE, "enabled": False}
    own = client is None
    client = client or httpx.AsyncClient(timeout=TIMEOUT)
    now = now or datetime.now(timezone.utc)
    end = now - SIP_DELAY
    symbols = tuple(symbols or tickers())
    f = Fetcher(client, settings.alpaca_data_key_id, settings.alpaca_data_secret_key)
    out = {"source": SOURCE, "enabled": True, "daily": {}, "hourly": {}, "refetched": []}
    daily_floor = datetime.combine(DAILY_START, time(0), tzinfo=timezone.utc)
    hourly_floor = now - timedelta(days=365 * HOURLY_YEARS + 1)

    async def run(timeframe, adj, starts: dict[str, datetime], prior: dict, stats: dict) -> set[str]:
        """Fetch grouped by start; returns the symbols whose adjusted history moved (corporate action)."""
        moved: set[str] = set()
        groups: dict[datetime, list[str]] = {}
        for sym, st in starts.items():
            groups.setdefault(st, []).append(sym)
        for st, syms in sorted(groups.items()):
            async def on_page(payload, sid):
                if timeframe == "1Day":
                    res = await asyncio.to_thread(upsert_daily, daily_points(payload, adj, now), sid, now)
                    keys = [(split_series_id(k)[0], split_series_id(k)[1], d) for k, d in res["revised_keys"]]
                    hit = {s for s, fld, d in keys if fld == "close" and prior.get(s) and d < prior[s]}
                else:
                    res = await asyncio.to_thread(upsert_bars, hourly_rows(payload, timeframe, adj, now), sid, now)
                    hit = {s for s, t in res["revised_keys"] if prior.get(s) and t < prior[s]}
                for k in ("new", "revised"):
                    stats[k] += res[k]
                moved.update(hit if adj != "raw" else set())
            await f.bars(sorted(syms), timeframe, adj, st, end, on_page)
        return moved

    try:
        for adj in DAILY_PLANS:
            prior = {} if full else await asyncio.to_thread(latest_daily, symbols, adj)
            starts = {s: (datetime.combine(prior[s] - OVERLAP_DAILY, time(0), tzinfo=timezone.utc) if s in prior
                          else daily_floor) for s in symbols}
            stats = {"new": 0, "revised": 0}
            moved = await run("1Day", adj, starts, prior, stats)
            if moved and not full:
                out["refetched"] += [f"1Day:{adj}:{s}" for s in sorted(moved)]
                await run("1Day", adj, {s: daily_floor for s in moved}, {}, stats)
            out["daily"][adj] = stats
        if hourly:
            for adj in HOURLY_PLANS:
                prior = {} if full else await asyncio.to_thread(latest_hourly, symbols, "1Hour", adj)
                starts = {s: max(prior[s] - OVERLAP_HOURLY, hourly_floor) if s in prior else hourly_floor
                          for s in symbols}
                # one group for the "nothing stored" symbols; forward starts rounded to the hour so they group
                starts = {s: v.replace(minute=0, second=0, microsecond=0) for s, v in starts.items()}
                stats = {"new": 0, "revised": 0}
                moved = await run("1Hour", adj, starts, prior, stats)
                if moved and not full:
                    out["refetched"] += [f"1Hour:{adj}:{s}" for s in sorted(moved)]
                    await run("1Hour", adj, {s: hourly_floor.replace(minute=0, second=0, microsecond=0) for s in moved},
                              {}, stats)
                out["hourly"][adj] = stats
    finally:
        if own:
            await client.aclose()
    out["requests"], out["failed"] = f.requests, f.failed
    return out


# --------------------------------------------------------------------------- reads (admin only)

def coverage() -> list[dict]:
    """Per symbol: daily points and earliest/latest session per adjustment, hourly bars and range."""
    from sqlalchemy import text

    syms = tickers()
    with _session()() as s:
        d = s.execute(text("""SELECT series_id, count(*), min(period_end), max(period_end) FROM context_series
                              WHERE source = 'alpaca' AND series_id LIKE 'alpaca.%.close.%' GROUP BY series_id""")).all()
        h = s.execute(text("""SELECT symbol, adjustment, count(*), min(t), max(t) FROM equity_bars
                              GROUP BY symbol, adjustment""")).all()
    daily = {split_series_id(r[0])[0::2]: r for r in d}
    hourly = {(r[0], r[1]): r for r in h}
    out = []
    for sym in syms:
        row = {"symbol": sym, "kind": "etf" if sym in ETFS else "equity", "visibility": VISIBILITY, "daily": {},
               "hourly": {}}
        for adj in DAILY_PLANS:
            r = daily.get((sym, adj))
            row["daily"][adj] = {"sessions": r[1], "earliest": r[2], "latest": r[3]} if r else {"sessions": 0}
        for adj in HOURLY_PLANS:
            r = hourly.get((sym, adj))
            row["hourly"][adj] = {"bars": r[2], "earliest": r[3], "latest": r[4]} if r else {"bars": 0}
        out.append(row)
    return out


def points(symbol: str, field: str = "close", adjustment: str = "split", since: date | None = None,
           until: date | None = None) -> list[dict]:
    from sqlalchemy import text

    with _session()() as s:
        rows = s.execute(text("""SELECT period_end AS date, value FROM context_series
                                 WHERE series_id = :id AND visibility = 'internal'
                                 AND (CAST(:since AS date) IS NULL OR period_end >= :since)
                                 AND (CAST(:until AS date) IS NULL OR period_end <= :until)
                                 ORDER BY period_end"""),
                         {"id": series_id(symbol, field, adjustment), "since": since, "until": until}).all()
    return [{"date": r.date, "value": float(r.value)} for r in rows]


def hourly(symbol: str, since: datetime | None = None, until: datetime | None = None,
           adjustment: str = "split") -> list[dict]:
    from sqlalchemy import text

    with _session()() as s:
        rows = s.execute(text("""SELECT t AS bar_start, open, high, low, close, volume, vwap, trades FROM equity_bars
                                 WHERE symbol = :s AND timeframe = '1Hour' AND adjustment = :a
                                 AND (CAST(:since AS timestamptz) IS NULL OR t >= :since)
                                 AND (CAST(:until AS timestamptz) IS NULL OR t <= :until) ORDER BY t"""),
                         {"s": symbol, "a": adjustment, "since": since, "until": until}).all()
    return [{"t": r.bar_start, **{k: None if getattr(r, k) is None else float(getattr(r, k))
                          for k in ("open", "high", "low", "close", "volume", "vwap")}, "trades": r.trades} for r in rows]


def _pct(a, b):
    return None if a in (None, 0) or b is None else round((float(b) / float(a) - 1) * 100, 4)


def moves(start: date, end: date, symbols: tuple[str, ...] | None = None, segment: str = "on_demand") -> dict:
    """For [start, end]: each ticker's % move (split-adjusted close; total return from the "all" close) from the
    last session on or before `start` to the last on or before `end`, beside each GPU's market median / lowest move
    (market_gpu_hourly, the last hour on or before each date's end, at most 48h stale). INTERNAL context only."""
    from sqlalchemy import text

    if end < start:
        raise ValueError("end before start")
    symbols = tuple(symbols or tickers())
    ids = [series_id(s, "close", a) for s in symbols for a in ("split", "all")]
    with _session()() as s:
        rows = s.execute(text("""SELECT series_id, period_end, value FROM context_series
                                 WHERE series_id = ANY(:ids) AND visibility = 'internal'
                                 AND period_end BETWEEN :lo AND :hi ORDER BY series_id, period_end"""),
                         {"ids": ids, "lo": start - timedelta(days=10), "hi": end}).all()
        t0 = datetime.combine(start, time(23, 59, 59), tzinfo=timezone.utc)
        t1 = datetime.combine(end, time(23, 59, 59), tzinfo=timezone.utc)
        g = s.execute(text("""
            SELECT DISTINCT ON (gpu, which) gpu, which, hour, median, lowest, providers_priced FROM (
              SELECT gpu, 0 AS which, hour, median, lowest, providers_priced FROM market_gpu_hourly
              WHERE segment = :seg AND hour <= :t0 AND hour > :t0 - interval '48 hours'
              UNION ALL
              SELECT gpu, 1, hour, median, lowest, providers_priced FROM market_gpu_hourly
              WHERE segment = :seg AND hour <= :t1 AND hour > :t1 - interval '48 hours') x
            ORDER BY gpu, which, hour DESC"""), {"seg": segment, "t0": t0, "t1": t1}).all()
    by: dict[str, list] = {}
    for r in rows:
        by.setdefault(r.series_id, []).append(r)

    def at(sid, d):
        pts = [r for r in by.get(sid, []) if r.period_end <= d]
        return pts[-1] if pts else None

    eq = []
    for sym in symbols:
        a, b = at(series_id(sym, "close", "split"), start), at(series_id(sym, "close", "split"), end)
        ta, tb = at(series_id(sym, "close", "all"), start), at(series_id(sym, "close", "all"), end)
        eq.append({"symbol": sym, "kind": "etf" if sym in ETFS else "equity",
                   "from": a and a.period_end, "to": b and b.period_end,
                   "close_from": a and float(a.value), "close_to": b and float(b.value),
                   "pct": _pct(a and a.value, b and b.value) if a and b and a.period_end < b.period_end else None,
                   "total_return_pct": _pct(ta and ta.value, tb and tb.value)
                   if ta and tb and ta.period_end < tb.period_end else None})
    gpus: dict[str, dict] = {}
    for r in g:
        x = gpus.setdefault(r.gpu, {"gpu": r.gpu})
        x["from" if r.which == 0 else "to"] = {"hour": r.hour, "median": r.median and float(r.median),
                                               "lowest": r.lowest and float(r.lowest), "providers": r.providers_priced}
    gl = []
    for x in gpus.values():
        a, b = x.get("from"), x.get("to")
        x["median_pct"] = _pct(a and a["median"], b and b["median"]) if a and b and a["hour"] < b["hour"] else None
        x["lowest_pct"] = _pct(a and a["lowest"], b and b["lowest"]) if a and b and a["hour"] < b["hour"] else None
        gl.append(x)
    gl.sort(key=lambda x: -abs(x["median_pct"] or 0))
    return {"window": {"start": start, "end": end}, "visibility": VISIBILITY, "rule": RULE, "segment": segment,
            "equities": eq, "gpus": gl}


_registered = False


def register() -> None:
    """The context_equities job, hourly, only when both Alpaca market-data keys are set."""
    global _registered
    from config import settings
    from jobs import job

    if _registered or not settings.poller_enabled or not enabled():
        return
    _registered = True
    job("context_equities", every_seconds=POLL_SECONDS, initial_delay_seconds=300)(refresh)
