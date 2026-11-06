"""Provider region -> the wholesale price series for that place. Never guessed; unknown means unmapped.

    locate(provider, region, country) -> {"state", "iso", "area", "match", "price_series", "ba"} | None

Built on context/geo.py (unchanged): a region maps to a US state only where the cloud documents it or the region
string names one ("US, Culpeper, VA", "Arizona, US"). This module adds one step, in two tiers:

    "zone"             the place itself is documented (a cloud region table below, or a "US, <city>, <ST>" region
                       whose city is in CITY_AREA) and lies in one ISO pricing area: that area's day-ahead series
                       (a CAISO trading hub, an ERCOT load-zone hub, a NYISO zone, a PJM zone)
    "state_hub"        only the state is known and the ISO prices a hub for that state (MISO: MINN.HUB, MS.HUB,
                       ARKANSAS.HUB, LOUISIANA.HUB): that hub
    "market_area_hub"  only the state is known and the state's load sits in one ISO (context/geo.MARKET_AREA):
                       the ISO's reference hub (REFERENCE_HUB), labelled as such. It is the market's price level,
                       not the local price. CAISO and NYISO have no single reference hub, so a state-only
                       California or New York region stays unmapped

A city alone ("Dallas", which is also in Georgia), a multi-region listing, or a state split between markets is
None. A series is used only if a registered dataset defines it (power/registry.py), matched on the series' own
`iso` and `location` code (normalised: upper-case, letters and digits only), so a source added later is picked up
without editing this file. `ba` is the EIA-930 balancing authority of the ISO (demand / generation context).
"""

from __future__ import annotations

import re

# context/geo.py's documented region -> state tables, read as DATA (no context series, nothing statistical).
from context import geo  # tests/test_context.py lists this file as an allowed importer

STATES = geo.STATES

# Documented cloud region locations, refined below the state (each cloud's region list names the place)
CLOUD_AREA: dict[str, dict[str, tuple[str, str]]] = {
    # AWS "US East (N. Virginia)": Northern Virginia is PJM's Dominion zone; "US West (N. California)": north of
    # Path 15 (CAISO NP15)
    "aws": {"us-east-1": ("PJM", "DOM"), "us-west-1": ("CAISO", "TH_NP15_GEN-APND")},
    # Google Cloud: us-east4 Ashburn, Northern Virginia; us-west2 Los Angeles; us-south1 Dallas
    "gcp": {"us-east4": ("PJM", "DOM"), "us-west2": ("CAISO", "TH_SP15_GEN-APND"), "us-south1": ("ERCOT", "HB_NORTH")},
    # Oracle: us-ashburn-1 Ashburn VA, us-sanjose-1 San Jose CA, us-chicago-1 Chicago IL (PJM ComEd)
    "oracle": {"us-ashburn-1": ("PJM", "DOM"), "us-sanjose-1": ("CAISO", "TH_NP15_GEN-APND"),
               "us-chicago-1": ("PJM", "COMED")},
}

# (city, state) named in a "US, <city>, <ST>" region -> (ISO, pricing area). Only cities wholly inside one area.
CITY_AREA: dict[tuple[str, str], tuple[str, str]] = {
    **{(c, "TX"): ("ERCOT", "HB_NORTH") for c in ("dallas", "irving", "plano", "fort worth", "richardson")},
    ("houston", "TX"): ("ERCOT", "HB_HOUSTON"),
    **{(c, "TX"): ("ERCOT", "HB_SOUTH") for c in ("austin", "san antonio")},
    ("abilene", "TX"): ("ERCOT", "HB_WEST"),
    **{(c, "CA"): ("CAISO", "TH_NP15_GEN-APND") for c in ("san jose", "santa clara", "fremont", "san francisco",
                                                         "sacramento", "hayward", "sunnyvale", "milpitas")},
    **{(c, "CA"): ("CAISO", "TH_SP15_GEN-APND") for c in ("los angeles", "el segundo", "irvine", "san diego")},
    ("new york", "NY"): ("NYISO", "N.Y.C."),
    ("buffalo", "NY"): ("NYISO", "WEST"),
    **{(c, "MN"): ("MISO", "MINN.HUB") for c in ("minneapolis", "saint paul", "st. paul", "chaska", "eagan")},
    **{(c, "VA"): ("PJM", "DOM") for c in ("ashburn", "sterling", "culpeper", "manassas", "reston", "leesburg")},
    ("chicago", "IL"): ("PJM", "COMED"),
}

# A state's own hub where the ISO prices one per state (MISO's hub names, as MISO spells them): a state-only
# match uses it before the ISO's reference hub
STATE_HUB = {"MN": ("MISO", "MINN.HUB"), "MS": ("MISO", "MS.HUB"), "AR": ("MISO", "ARKANSAS.HUB"),
             "LA": ("MISO", "LOUISIANA.HUB")}

# The ISO's reference hub, for a state-only match (None: the ISO has no single reference price)
REFERENCE_HUB = {"PJM": "WESTERN HUB", "ERCOT": "HB_HUBAVG", "MISO": "INDIANA.HUB", "SPP": "SPPSOUTH_HUB",
                 "ISO-NE": ".H.INTERNAL_HUB", "CAISO": None, "NYISO": None}
BA = {"CAISO": "CISO", "ERCOT": "ERCO", "NYISO": "NYIS", "ISO-NE": "ISNE", "PJM": "PJM", "MISO": "MISO", "SPP": "SWPP"}

_US_CITY = re.compile(r"^\s*US\s*,\s*([^,;]+?)\s*,\s*([A-Z]{2})\s*$")


def norm(code: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (code or "").upper())


def _series_for(iso: str, area: str | None) -> str | None:
    """The day-ahead LMP series of (iso, area) among the registered datasets, else None."""
    if not area:
        return None
    from power import registry

    want = norm(area)
    for ds in registry.datasets():
        for s in ds.series:
            if s.iso == iso and s.market == "da" and s.kind == "lmp" and norm(s.location) == want:
                return s.key
    return None


def area_of(provider: str | None, region: str | None) -> tuple[str, str] | None:
    """(ISO, pricing area) where the place is documented below the state, else None."""
    if not region:
        return None
    hit = CLOUD_AREA.get((provider or "").lower(), {}).get(region.strip().lower())
    if hit:
        return hit
    m = _US_CITY.match(region)
    if m:
        return CITY_AREA.get((m.group(1).strip().lower(), m.group(2)))
    return None


def locate(provider: str | None, region: str | None, country: str | None = None) -> dict | None:
    state = geo.us_state(provider, region, country)
    if not state:
        return None
    area = area_of(provider, region)
    if area:
        iso, code = area
        if geo.market_area(state) not in (None, iso):
            return None    # the documented area and the state disagree: refuse rather than pick
        key = _series_for(iso, code)
        if key:
            return {"state": state, "iso": iso, "area": code, "match": "zone", "price_series": key, "ba": BA.get(iso)}
    iso = geo.market_area(state)
    if not iso:
        return None
    sh = STATE_HUB.get(state)
    if sh and sh[0] == iso:
        key = _series_for(iso, sh[1])
        if key:
            return {"state": state, "iso": iso, "area": sh[1], "match": "state_hub", "price_series": key,
                    "ba": BA.get(iso)}
    key = _series_for(iso, REFERENCE_HUB.get(iso))
    if not key:
        return None
    return {"state": state, "iso": iso, "area": REFERENCE_HUB[iso], "match": "market_area_hub", "price_series": key,
            "ba": BA.get(iso)}
