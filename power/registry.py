"""The power-source registry: what each source is, what its terms allow, and what OpenGrid reads from it.

A source is one module in power/sources/ (listed in power.sources.MODULES). It declares, at module level:

    SOURCE    "caiso"                          the source id (power_series.source, power_fetches.source)
    TERMS     Terms(...)                       the terms-of-use verdict: visibility, reason, terms URL, signup
    DATASETS  (Dataset(...), ...)              what it fetches; each Dataset carries its series, its time grid
                                               (tz + interval), its depth, and its fetch / parse functions
    DEFAULT_ON  True | False                   read when POWER_SOURCES is empty (no-signup sources: True)
    REQUIRES  ("pjm_api_key", ...)             config.settings fields that must be set, else the source is off

Visibility is decided HERE ONLY, per source, from TERMS.visibility: "public" only where redistribution is clearly
permitted (EIA: U.S.-government public domain), "internal" otherwise. Internal series are still pulled and used
(the founder's rule: data we cannot resell is used internally, never exposed). Every power_series row takes its
source's verdict on every run, and every public read re-checks the registry (registry.visibility), so tightening
a verdict here hides the data at once, before any re-run.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Awaitable, Callable

PUBLIC, INTERNAL = "public", "internal"
DAILY, WEEKLY, MONTHLY = 1440, 10080, 43200


@dataclass(frozen=True)
class Terms:
    """The source's terms-of-use verdict. `checked` is when the terms page was last read."""
    operator: str                 # "California ISO"
    visibility: str               # PUBLIC | INTERNAL
    reason: str                   # why, quoting the terms where they decide it
    terms_url: str
    checked: str                  # YYYY-MM-DD
    signup: str | None = None     # what the user must register for (None: no signup needed)
    attribution: str | None = None


@dataclass(frozen=True)
class Series:
    """One series. `key` is global and stable: "<source>.<market>.<kind>.<location>" lower-case."""
    key: str
    name: str
    kind: str                     # "lmp" | "demand" | "net_generation" | "interchange" | "demand_forecast" | ...
    market: str                   # "da" | "rt" | "actual" | "forecast"
    location: str                 # the source's own code (node, zone, BA), as the source spells it
    location_type: str            # "hub" | "zone" | "load_area" | "ba"
    unit: str                     # "usd_per_mwh" | "mwh"
    iso: str | None = None        # "CAISO" | "NYISO" | ... (the BA for EIA series)
    meta: dict | None = None
    commodity: str = "electricity"   # "electricity" | "natural_gas"


@dataclass
class Fetched:
    """One HTTP answer, as power/http.py returns it. body is the exact bytes received (None on a transport error)."""
    url: str                      # redacted
    status: int | None
    ms: int | None
    body: bytes | None
    content_type: str | None = None
    error: str | None = None
    empty: bool = False           # the source answered "no data for this selection" (set by the parser)
    start: date | None = None     # the market days the request covered (inclusive)
    end: date | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.status is not None and 200 <= self.status < 300 and self.body is not None


# parse(fetched) -> {series_key: {interval_start_utc: value}} (periodic datasets: {period date: value}). Points
# outside the requested days are ignored, a series key not in the dataset is ignored, a value of None (or NaN) is a
# gap. Raise ValueError on a malformed body; set fetched.empty = True for a "no data for this selection" answer.
Parse = Callable[[Fetched], dict[str, dict[datetime, float | None]]]
# fetch(get, start, end[, now=]) -> [Fetched]: one or more requests covering market days start..end (inclusive),
# made through `get` (power/http.Client.get: polite pacing, retries, redaction). Must not store anything. A fetch
# that declares a `now` parameter receives the run's clock. Set f.start / f.end on an answer that covers only some
# of the chunk's days (one file per day): its days are then marked stored, or retried, on their own.
Fetch = Callable[..., Awaitable[list[Fetched]]]


@dataclass(frozen=True)
class Dataset:
    """A set of series fetched together on one time grid."""
    key: str                      # "caiso.da_lmp" (power_series.dataset)
    source: str
    series: tuple[Series, ...]
    interval_minutes: int         # 60, 15, 5; PERIODIC (>= 1440: daily 1440, weekly 10080, monthly 43200) = one
                                  # value per row, dated by the source's own period label
    tz: str                       # the market day's zone ("America/Los_Angeles"; "UTC" for EIA's UTC hours)
    chunk_days: int               # market days per fetch() call
    earliest: Callable[[datetime], date]   # the first market day the source still serves (now -> date)
    latest: Callable[[datetime], date]     # the last COMPLETE market day it serves now (DA: tomorrow after the run)
    fetch: Fetch
    parse: Parse
    pace_seconds: float = 5.0     # between requests of this source (one at a time per source)
    overlap_days: int = 2         # forward re-reads the last N stored days (late corrections)
    keep_raw: bool = True         # forward fetches keep the bytes (measured small per day); False: url + sha256
    description: str = ""
    # HTTP statuses that mean "not published (yet)", not a failure: e.g. (404,) for per-day files posted late.
    # Such an answer is recorded as empty and its days re-asked while recent (fetch / parse may also set f.empty)
    empty_statuses: tuple[int, ...] = ()
    align: str | None = None      # "month": chunks never cross a calendar month (sources with monthly archives)
    meta: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- discovery

def modules() -> list:
    from power import sources

    return [importlib.import_module(f"power.sources.{m}") for m in sources.MODULES]


def module(source: str):
    for m in modules():
        if m.SOURCE == source:
            return m
    raise KeyError(f"unknown power source {source!r}")


def terms(source: str) -> Terms:
    return module(source).TERMS


def visibility(source: str) -> str:
    """The tier of a source; an unknown source is internal (never public by default)."""
    try:
        v = module(source).TERMS.visibility
    except KeyError:
        return INTERNAL
    return PUBLIC if v == PUBLIC else INTERNAL


def public_sources() -> set[str]:
    return {m.SOURCE for m in modules() if m.TERMS.visibility == PUBLIC}


def configured(source: str) -> bool:
    """Every setting the source REQUIRES is set."""
    from config import settings

    return all(getattr(settings, r, None) for r in getattr(module(source), "REQUIRES", ()))


def selected() -> list[str]:
    """POWER_SOURCES (comma-separated) or each module's DEFAULT_ON; then only the configured ones."""
    from config import settings

    names = [m.SOURCE for m in modules()]
    if settings.power_sources:
        want = [s.strip().lower() for s in settings.power_sources.split(",") if s.strip()]
        names = [n for n in names if n in want]
    else:
        names = [m.SOURCE for m in modules() if getattr(m, "DEFAULT_ON", False)]
    return [n for n in names if configured(n)]


def datasets(source: str | None = None) -> list[Dataset]:
    return [d for m in modules() if source in (None, m.SOURCE) for d in m.DATASETS]


def series_index() -> dict[str, tuple[Dataset, Series]]:
    return {s.key: (d, s) for d in datasets() for s in d.series}


def as_list() -> list[dict]:
    """Every source with its verdict, for the methodology page and /v1/ops/power/sources."""
    out = []
    for m in modules():
        t = m.TERMS
        out.append({"source": m.SOURCE, "operator": t.operator, "visibility": visibility(m.SOURCE),
                    "reason": t.reason, "terms_url": t.terms_url, "terms_checked": t.checked, "signup": t.signup,
                    "attribution": t.attribution, "default_on": bool(getattr(m, "DEFAULT_ON", False)),
                    "requires": list(getattr(m, "REQUIRES", ())), "configured": configured(m.SOURCE),
                    "datasets": [{"key": d.key, "interval_minutes": d.interval_minutes, "tz": d.tz,
                                  "series": len(d.series), "keep_raw": d.keep_raw, "description": d.description}
                                 for d in m.DATASETS]})
    return out
