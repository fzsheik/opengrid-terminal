"""The site-dataset registry: what each physical-grid source is, what its terms allow, and what OpenGrid reads.

power/registry.py is built for time series on a market-day grid (Series, Dataset, power_days); plant inventories
and interconnection-queue editions are snapshots, not series, so they get this sibling registry. It reuses the
same Terms verdict and the same rule: visibility is decided HERE ONLY, "public" only where redistribution is
clearly permitted, "internal" otherwise; internal data is still pulled and used, never exposed. A source whose
terms forbid automated access is not pulled at all.

    SOURCES                      every source with its verdict and datasets
    visibility(source, edition)  the tier of a source (or of one edition, where editions differ)
    selected()                   the sources the job runs: every source whose REQUIRES settings are set
"""

from __future__ import annotations

from dataclasses import dataclass, field

from power.registry import INTERNAL, PUBLIC, Terms

EIA_TERMS = Terms(
    operator="U.S. Energy Information Administration",
    visibility=PUBLIC,
    reason=("U.S.-government work: 'U.S. government publications are in the public domain and are not subject to "
            "copyright protection. You may use and/or distribute any of our data, files, databases, reports, "
            "graphs, charts, and other information products' (EIA copyright and reuse). EIA asks for attribution."),
    terms_url="https://www.eia.gov/about/copyrights_reuse.php",
    checked="2026-11-15",
    signup="free EIA API key: https://www.eia.gov/opendata/register.php (EIA_API_KEY; already set in production)",
    attribution="Source: U.S. Energy Information Administration, Form EIA-860M and Form EIA-923",
)

LBNL_TERMS = Terms(
    operator="Lawrence Berkeley National Laboratory (Queued Up)",
    visibility=PUBLIC,
    reason=("The 2026 edition's data page (eta-publications.lbl.gov/publications/us-interconnection-queue-data-0) "
            "says: 'The Queued Up data file is licensed CC BY 4.0. You may use, share, or adapt the dataset as long "
            "as you attribute it to Lawrence Berkeley National Laboratory and GridTracker.' The 2025 edition's page "
            "states no licence, so that edition is internal (EDITIONS below). The file is read from "
            "eta-publications.lbl.gov; emp.lbl.gov answers automated clients with a Cloudflare challenge, which is "
            "not bypassed."),
    terms_url="https://eta-publications.lbl.gov/publications/us-interconnection-queue-data-0",
    checked="2026-11-15",
    signup=None,
    attribution="Source: Lawrence Berkeley National Laboratory and GridTracker, Queued Up (CC BY 4.0)",
)

ERCOT_TERMS = Terms(
    operator="Electric Reliability Council of Texas",
    visibility=PUBLIC,
    reason=("ERCOT Terms of Use, section 5: 'raw data provided in public portions of this website may be used, "
            "reproduced, and redistributed in compilations, charts, and analyses' (as for power/sources/ercot.py). "
            "The GIS report (PG7-200-ER) is a public MIS report, keyless. Each monthly document is downloaded once "
            "(the API terms cap a report at three downloads a year). ERCOT publishes no project-level large-load "
            "queue: large loads appear only as zone and total summaries in PDF slides (Monthly Operational "
            "Overview, LLWG / TAC updates), so none is read."),
    terms_url="https://www.ercot.com/help/terms",
    checked="2026-11-15",
    signup=None,
    attribution="Source: ERCOT, Generator Interconnection Status report",
)


@dataclass(frozen=True)
class Source:
    key: str                       # power_fetches.source
    terms: Terms
    datasets: tuple[str, ...]      # power_fetches.dataset values it writes
    description: str
    cadence: str                   # how often the source publishes
    depth: str
    requires: tuple[str, ...] = ()
    editions: dict = field(default_factory=dict)   # edition -> visibility, where editions differ


SOURCES: tuple[Source, ...] = (
    Source("eia860m", EIA_TERMS, ("sites.eia860m_api", "sites.eia860m_file"),
           "Generator inventory: every operable generator per month from API v2 operating-generator-capacity "
           "(coordinates, county, BA, technology, nameplate / summer MW, status, planned retirement), plus the "
           "planned, retired and canceled generators of the latest monthly EIA-860M file",
           cadence="monthly", depth="API 2008-01 onward; the file's Retired sheet lists retirements back to the 1950s",
           requires=("eia_api_key",)),
    Source("eia923", EIA_TERMS, ("sites.eia923_annual",),
           "Annual net generation per plant (API v2 facility-fuel, fuel ALL, prime mover ALL), for capacity factors",
           cadence="monthly (the latest year is preliminary and revised)", depth="2008 onward (the API has 2001)",
           requires=("eia_api_key",)),
    Source("lbnl_queues", LBNL_TERMS, ("sites.lbnl_queued_up",),
           "Every interconnection request in the US queues (all ISOs and ~50 utilities): state, county, type, MW, "
           "queue / proposed / online / withdrawn dates, status. Generation and storage only (no loads)",
           cadence="yearly edition", depth="every edition listed in power/sites/lbnl.py, kept side by side",
           editions={"2024-12-31": INTERNAL, "2025-12-31": PUBLIC}),
    Source("ercot_gis", ERCOT_TERMS, ("sites.ercot_gis",),
           "ERCOT Generator Interconnection Status report: every large (and public small) generation project with a "
           "full interconnection study requested: county, fuel, technology, MW, projected COD, milestones",
           cadence="monthly (first business days)",
           depth="every report ERCOT still lists (94 months from 2018-12, measured 2026-11-15)"),
)


def source(key: str) -> Source:
    for s in SOURCES:
        if s.key == key:
            return s
    raise KeyError(f"unknown site source {key!r}")


def visibility(key: str, edition: str | None = None) -> str:
    """Unknown source or edition: internal. An edition's own verdict can only tighten the source's."""
    try:
        s = source(key)
    except KeyError:
        return INTERNAL
    v = s.terms.visibility
    if edition is not None and s.editions:
        v = s.editions.get(str(edition), INTERNAL) if v == PUBLIC else v
    return PUBLIC if v == PUBLIC else INTERNAL


def configured(key: str) -> bool:
    from config import settings

    return all(getattr(settings, r, None) for r in source(key).requires)


def selected() -> list[str]:
    return [s.key for s in SOURCES if configured(s.key)]


def as_list() -> list[dict]:
    return [{"source": s.key, "operator": s.terms.operator, "visibility": visibility(s.key),
             "editions": dict(s.editions) or None, "reason": s.terms.reason, "terms_url": s.terms.terms_url,
             "terms_checked": s.terms.checked, "signup": s.terms.signup, "attribution": s.terms.attribution,
             "requires": list(s.requires), "configured": configured(s.key), "datasets": list(s.datasets),
             "description": s.description, "cadence": s.cadence, "depth": s.depth} for s in SOURCES]
