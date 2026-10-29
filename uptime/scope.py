"""Regional vs global, two separate questions kept apart:

  REPORTED  what a provider's status page says an incident covered (classify_incident). Inferred from the text and
            the affected components it names; Google Cloud lists affected locations outright.
  MEASURED  what the probes saw during a dip (classify_dip). Inferred from how many probes failed and, for providers
            with several measured regions, how many of those regions dipped in the same window.

Both are labelled inferred wherever they are shown, and each carries a plain-words `basis`. Pure functions: no I/O.

Dip scope (measured)
    partial   only SOME probes failed (degraded minutes). That is a path or vantage-point problem as much as a provider
              one, so it is never called regional or global.
    regional  EVERY probe failed for one measured region while at least one sibling region of the same provider did not.
    global    every probe failed, and either every measured region of the provider dipped in that window, or the
              provider has a single measured endpoint (so "global" means all five probe locations, region unknown).
"""

from __future__ import annotations

import re

_CLOUD = (r"(?:us|eu|ap|sa|ca|me|af|il|mx|europe|asia|australia|northamerica|southamerica)"
          r"(?:-[a-z]+)+-?\d[a-z]?")
_GEO = r"(?:north|south|east|west|central|midwest|southcentral|northeast|southeast|northwest|southwest)[a-z]*\d?"
_REGION_RES = [
    re.compile(r"\b" + _CLOUD + r"\b", re.I),                                        # us-east-1, eu-north1, europe-west2
    re.compile(r"\b(?:us|eu|ap|sa|ca|me|af|europe|asia|australia)-" + _GEO + r"\b(?!-\d)", re.I),  # us-southcentral (no digit)
    re.compile(r"\b(nyc|sfo|ams|sgp|lon|fra|tor|blr|syd|atl)\s?-?(\d)\b", re.I),     # DigitalOcean datacentres
    re.compile(r"\b(norway|canada|us)\s?-?\s?(\d)\b(?=\s*(?:data\s?cent(?:re|er)|dc|region)|\W|$)", re.I),  # Hyperstack
]
_SHORT = re.compile(r"\b(CA|NO|US)-?(\d)\b")   # Hyperstack's own shorthand: CA1, NO1, US1 (upper case only)
_SHORT_NAME = {"CA": "canada", "NO": "norway", "US": "us"}
_GLOBAL_WORD = re.compile(r"\bglobal(?:ly)?\b|\ball regions\b|\bmultiple regions\b|\bworldwide\b", re.I)
GLOBAL_FRACTION = 0.6   # an incident touching this share of a page's components is called global
MIN_COMPONENTS = 5      # ...but only on pages that have at least this many
MANY_REGIONS = 5        # an incident naming this many distinct regions is called global: it is not one region's problem


def regions_in(*texts: str | None) -> list[str]:
    """Region tokens named in the given texts, lowercased and normalised ('Canada 1' -> 'canada-1'), sorted."""
    found: set[str] = set()
    for text in texts:
        if not text:
            continue
        for rx in _REGION_RES:
            for m in rx.finditer(text):
                tok = m.group(0) if m.lastindex is None else "-".join(g for g in m.groups() if g)
                found.add(re.sub(r"[\s_]+", "-", tok.lower()).replace("--", "-"))
        for m in _SHORT.finditer(text):
            found.add(f"{_SHORT_NAME[m.group(1)]}-{m.group(2)}")
    return sorted(found)


def classify_incident(title: str, component_names: list[str], total_components: int | None) -> tuple[str, list[str], str]:
    """(scope, regions, basis) for a Statuspage-style incident."""
    names = sorted({n for n in component_names if n})
    regions = regions_in(title, *names)
    if _GLOBAL_WORD.search(title or "") or any(n.strip().lower() == "global" for n in names):
        return "global", regions, "the title or an affected component says global / all regions"
    if total_components and total_components >= MIN_COMPONENTS and len(names) / total_components >= GLOBAL_FRACTION:
        return "global", regions, f"it touched {len(names)} of the page's {total_components} components"
    if len(regions) >= MANY_REGIONS:
        return "global", regions, f"it names {len(regions)} regions"
    if regions:
        return "regional", regions, "it names " + ", ".join(regions)
    return "unspecified", [], "it names no region and does not say global"


def classify_gcp_incident(locations: list[str]) -> tuple[str, list[str], str]:
    """Google Cloud lists affected locations itself; 'global' is one of them when it is worldwide."""
    locs = sorted({x for x in locations if x})
    if "global" in locs:
        return "global", [x for x in locs if x != "global"], "Google Cloud lists 'global' among the affected locations"
    if locs:
        return "regional", locs, "Google Cloud lists " + ", ".join(locs)
    return "unspecified", [], "Google Cloud lists no affected location"


def dip_kind(down: int | None, degraded: int | None) -> str:
    d, g = down or 0, degraded or 0
    return "down" if d and not g else "degraded" if g and not d else "mixed"


def classify_dip(service: dict, dipped: dict[str, dict], measured_regions: list[dict]) -> tuple[str, str]:
    """(scope, basis) for one service in one bucket.

    service           {id, region, down_minutes, degraded_minutes}
    dipped            {service_id: row} every service of the provider with a dip in this same bucket
    measured_regions  the provider's compute services that carry a region (>= 2 makes a regional call possible)
    """
    down = service.get("down_minutes") or 0
    if not down:
        return "partial", "only some probes failed (degraded minutes, no full-failure minutes): a path or vantage point, not provably the provider"
    sibs = [s for s in measured_regions if s.get("region")]
    if service.get("region") and len(sibs) >= 2:
        hit = [s for s in sibs if (dipped.get(s["id"]) or {}).get("down_minutes")]
        if len(hit) >= len(sibs):
            return "global", f"every probe failed, and all {len(sibs)} measured regions dipped in the same window"
        names = ", ".join(sorted(s["region"] for s in hit))
        return "regional", f"every probe failed for {names}, but {len(sibs) - len(hit)} of {len(sibs)} measured regions did not"
    return "global", "every probe failed; the provider has one measured endpoint, so all probe locations saw it and the region is not known"
