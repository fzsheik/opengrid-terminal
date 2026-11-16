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


@dataclass(frozen=True)
class Declined:
    """A dataset considered and NOT pulled: its terms forbid commercial use or automated access, it needs an
    account, or no honest route exists. Kept here so the verdict is recorded once, beside the pulled sources."""
    dataset: str
    terms: Terms
    why: str


DECLINED = (
    Declined("eog_vnf_annual", Terms(
        operator="Earth Observation Group, Payne Institute, Colorado School of Mines",
        visibility=INTERNAL,
        reason=("VIIRS Nightfire annual flare site records (Zenodo doi:10.5281/zenodo.23091822, 2012-2025, keyless). "
                "README: 'License: CC BY-NC 4.0 ... Commercial use requires permission from the Earth Observation "
                "Group, Payne Institute for Public Policy, Colorado School of Mines.' Ranking sites for a commercial "
                "product is commercial use, so not pulled, not even internally."),
        terms_url="https://eogdata.mines.edu/products/vnf/global_gas_flare.html", checked="2026-11-15",
        signup="Commercial licence by request: eog@mines.edu"),
        "non-commercial licence"),
    Declined("worldbank_gfmr_2012_2025", Terms(
        operator="World Bank, Global Flaring and Methane Reduction Partnership",
        visibility=INTERNAL,
        reason=("Flare-Volume-Estimates-by-individual-Flare-Location-2012-2025.xlsx and the country file "
                "(thedocs.worldbank.org) carry no open licence and are not in the Data Catalog, so the site terms "
                "apply: 'For the remainder of the Materials, you may make non-commercial uses thereof, but you may "
                "not make any derivative work or commercial use ... without the prior written consent.' The volumes "
                "are EOG's CC BY-NC release. Not pulled; the CC BY 4.0 catalog files (2017-2021) are used instead."),
        terms_url="https://www.worldbank.org/ext/en/legal/terms-conditions", checked="2026-11-15"),
        "non-commercial terms"),
    Declined("rrc_pdq_dump", Terms(
        operator="Railroad Commission of Texas",
        visibility=INTERNAL,
        reason=("Texas lease-level 'vented or flared' gas (disposition code 04, flared and vented combined) is in the "
                "Production Data Query dump (PDQ_DSV.zip, 3.45 GB, ~25 GB unzipped, monthly) served through a "
                "GoAnywhere file-transfer web app (mft.rrc.texas.gov) that needs a browser session, not a plain "
                "file URL. Site policies: 'RRC grants permission to copy and distribute the information on its "
                "website for noncommercial use'; the PDQ query pages end sessions that use 'an automated tool'. Not "
                "pulled in this change: an internal-only, county-level extract is possible but needs a streaming "
                "reader for a multi-GB zip and a check that the portal allows scripted download."),
        terms_url="https://www.rrc.texas.gov/site-policies/", checked="2026-11-15"),
        "multi-GB dump behind a session portal; redistribution noncommercial only"),
    Declined("ercot_wind_solar_archive", Terms(
        operator="Electric Reliability Council of Texas",
        visibility=PUBLIC,
        reason=("History of NP4-732-CD / NP4-737-CD beyond the ~7 days the MIS keeps is only behind api.ercot.com "
                "(HTTP 401 without an Ocp-Apim-Subscription-Key, which needs an ERCOT account). No account is "
                "created, so the ERCOT curtailment proxy starts when the forward job first runs."),
        terms_url="https://www.ercot.com/help/terms", checked="2026-11-15",
        signup="ERCOT Public API registration (apiexplorer.ercot.com)"),
        "needs an account"),
    Declined("caiso_oasis_curtailment", Terms(
        operator="California ISO", visibility=INTERNAL,
        reason=("Not needed: CAISO's production and curtailment workbooks and daily renewable reports "
                "(power/sources/caiso_curtailment.py) carry curtailment by fuel and reason; OASIS has no "
                "curtailment report of its own."),
        terms_url="https://www.caiso.com/privacy-terms-of-use", checked="2026-11-15"),
        "covered by caiso_curtailment"),
)


def declined_list() -> list[dict]:
    return [{"dataset": d.dataset, "operator": d.terms.operator, "why": d.why, "reason": d.terms.reason,
             "terms_url": d.terms.terms_url, "terms_checked": d.terms.checked, "signup": d.terms.signup,
             "pulled": False} for d in DECLINED]


# --------------------------------------------------------------------------- nodal daily summaries

@dataclass(frozen=True)
class NodalDataset:
    """Every priced node of one market, read one market day at a time and kept only as one summary row per node
    and day (power/nodal.py; power_node_days). The interval prices themselves are never stored.

    fetch(get, day, day[, now=]) -> [Fetched] as for Dataset; parse(fetched) -> NodalParsed. A fetch that returns
    no answer leaves the day unread (asked again next run)."""
    key: str                      # "ercot.rt_nodal" (power_fetches.dataset)
    source: str
    iso: str                      # "ERCOT" | "SPP" (power_nodes.iso)
    market: str                   # "rt" | "da" (power_node_days.market)
    interval_minutes: int
    tz: str
    earliest: Callable[[datetime], date]   # the first market day the source still holds now
    latest: Callable[[datetime], date]     # the last complete market day it serves now
    fetch: Fetch
    parse: Callable[[Fetched], "NodalParsed"]
    pace_seconds: float = 2.5
    overlap_days: int = 0         # forward re-reads the last N days (0: the source's prices are final when posted)
    empty_statuses: tuple[int, ...] = ()
    description: str = ""


@dataclass
class NodalParsed:
    """prices: {node: {interval_start_utc: price}}; nodes: {node: {"node_type": .., "zone": .., "meta": {..}}},
    what the answer itself says about each node (the catalogue keeps the first non-null value of each field)."""
    prices: dict[str, dict[datetime, float]] = field(default_factory=dict)
    nodes: dict[str, dict] = field(default_factory=dict)


@dataclass(frozen=True)
class NodalVerdict:
    """The terms verdict for a source's nodal datasets, recorded beside (and checked against) its hub verdict."""
    visibility: str
    same_terms_as_hub: bool
    reason: str
    checked: str


# Nodal data comes from the same operators, the same sites and the same terms as each source's hub data, so the
# verdicts match the hub verdicts (a test pins that). Every nodal read is admin-only anyway (/v1/ops/power/nodes).
NODAL_VERDICTS = {
    "ercot": NodalVerdict(
        visibility=PUBLIC, same_terms_as_hub=True, checked="2026-11-15",
        reason=("NP6-905-CD (real-time settlement point prices at resource nodes, hubs and load zones), NP4-190-CD "
                "(DAM settlement point prices) and NP4-160-SG (Settlement Points List and Electrical Buses Mapping) "
                "are public MIS reports on www.ercot.com, read keyless like the hub data, under the same Terms of "
                "Use section 5 ('raw data ... may be used, reproduced, and redistributed in compilations, charts, "
                "and analyses'). The per-document limit of the API terms (three downloads a year) is kept: a "
                "document the hub dataset already read in this process is reused, not downloaded again. ERCOT's "
                "deeper nodal archive is only on the Public Data API (api.ercot.com), which needs a registration: "
                "not used.")),
    "spp": NodalVerdict(
        visibility=INTERNAL, same_terms_as_hub=True, checked="2026-11-15",
        reason=("The same marketplace files as the hub data (RTBM and DA LMP by settlement location, every "
                "location in each file) under the same SPP Terms & Conditions: commercial use needs SPP's prior "
                "written authorization, so internal.")),
}


def nodal_visibility(source: str) -> str:
    """A nodal verdict never looser than the hub verdict; an unknown source is internal."""
    v = NODAL_VERDICTS.get(source)
    if v is None or v.visibility != PUBLIC:
        return INTERNAL
    return visibility(source)


def nodal_datasets(source: str | None = None) -> list[NodalDataset]:
    return [d for m in modules() if source in (None, m.SOURCE) for d in getattr(m, "NODAL_DATASETS", ())]


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
        nv = NODAL_VERDICTS.get(m.SOURCE)
        if getattr(m, "NODAL_DATASETS", ()):
            out[-1]["nodal"] = {"visibility": nodal_visibility(m.SOURCE),
                                "same_terms_as_hub": bool(nv and nv.same_terms_as_hub),
                                "reason": nv.reason if nv else None, "terms_checked": nv.checked if nv else None,
                                "datasets": [{"key": d.key, "market": d.market, "interval_minutes": d.interval_minutes,
                                              "description": d.description} for d in m.NODAL_DATASETS]}
    return out
