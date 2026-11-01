"""Where a listing's power comes from: provider region -> US state -> wholesale market area. Never guessed.

    us_state(provider, region, country) -> "VA" | None
    market_area(state)                  -> "PJM" | "ERCOT" | ... | None

US only: the power price series OpenGrid can republish are EIA's, which are US states. A listing maps to a state only
when its region names one unambiguously:
    * a cloud region code whose location the cloud documents (AWS, Google Cloud, Azure, Oracle tables below);
    * Shadeform-style "US, Culpeper, VA" (the state code after the city);
    * Vast-style "Arizona, US" (a state name, then the country).
Everything else (a city alone, a multi-region listing, a non-US country) is None.

market_area is the organised wholesale market most of the state's load sits in. It is context only: OpenGrid reads
no wholesale (LMP) prices, because each market operator's terms decide whether they may be republished. States split
between markets (IL, IN, MI, ...) and states with no organised market are None.
"""

from __future__ import annotations

import re

# Documented region locations (each cloud's region list).
CLOUD_STATE: dict[str, dict[str, str]] = {
    "aws": {"us-east-1": "VA", "us-east-2": "OH", "us-west-1": "CA", "us-west-2": "OR"},
    "gcp": {"us-central1": "IA", "us-east1": "SC", "us-east4": "VA", "us-east5": "OH", "us-south1": "TX",
            "us-west1": "OR", "us-west2": "CA", "us-west3": "UT", "us-west4": "NV"},
    "azure": {"eastus": "VA", "eastus2": "VA", "centralus": "IA", "northcentralus": "IL", "southcentralus": "TX",
              "westus": "CA", "westus2": "WA", "westus3": "AZ", "westcentralus": "WY"},
    "oracle": {"us-ashburn-1": "VA", "us-phoenix-1": "AZ", "us-chicago-1": "IL", "us-sanjose-1": "CA"},
}

STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan",
    "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
    "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island",
    "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}
_BY_NAME = {v.lower(): k for k, v in STATES.items()}

# States whose load is (almost) entirely in one organised market.
MARKET_AREA = {
    **{s: "PJM" for s in ("VA", "OH", "PA", "NJ", "MD", "DE", "DC", "WV")},
    "TX": "ERCOT", "CA": "CAISO", "NY": "NYISO",
    **{s: "ISO-NE" for s in ("MA", "CT", "RI", "NH", "VT", "ME")},
    **{s: "MISO" for s in ("IA", "MN", "WI", "ND", "LA", "MS", "AR")},
    **{s: "SPP" for s in ("KS", "OK", "NE")},
}

_SHADEFORM = re.compile(r"^\s*US\s*,\s*[^,;]+,\s*([A-Z]{2})\s*$")
_VAST = re.compile(r"^\s*([A-Za-z .]+?)\s*,\s*US\s*$")


def us_state(provider: str | None, region: str | None, country: str | None = None) -> str | None:
    if not region:
        return None
    if ";" in region or ("," in region and region.count(",") > 2):
        return None                      # several places: not one state
    if country and country.upper() not in ("US", "USA"):
        return None
    r = region.strip()
    table = CLOUD_STATE.get((provider or "").lower())
    if table and r.lower() in table:
        return table[r.lower()]
    m = _SHADEFORM.match(r)
    if m and m.group(1) in STATES:
        return m.group(1)
    m = _VAST.match(r)
    if m and m.group(1).lower() in _BY_NAME:
        return _BY_NAME[m.group(1).lower()]
    return None


def market_area(state: str | None) -> str | None:
    return MARKET_AREA.get(state or "")
