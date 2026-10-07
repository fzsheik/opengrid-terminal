"""Where a listing runs, as one of a few broad region groups.

    region_group(provider, region, country) -> "US" | "Canada" | "Europe" | "UK" | "APAC"
                                               | "Middle East" | "LATAM" | "Africa" | None

Deterministic and never guessed. In order:
    1. an ISO 3166 country code the provider gave us (compute_listings.country)
    2. the region string, parsed by the shapes providers actually send:
         "us-east-1"                      cloud region codes (AWS's full table, Lambda's,
                                          and the unambiguous prefixes us-/ca-/sa-/ap-/me-/af-)
         "US, Culpeper, VA; DE, Frankfurt" Shadeform (Crusoe, Denvr, Latitude): "CC, city[, state]"
         "Arizona, US"                    Vast: "<place>, CC"
         "nyc2,tor1,ams3"                 DigitalOcean slugs, by city prefix
         "CANADA-1"                       Hyperstack: country word + index
    3. otherwise None.

A listing that spans several groups ("NL, Amsterdam; US, Ashburn, VA") is None
from `region_group` (it is not in one place); `region_groups` returns them all.

Country -> group follows the UN M49 geoscheme, with the groups this product
uses: UK and Canada stand alone; Western Asia is "Middle East" (Cyprus, an EU
member, is Europe); Russia is Europe (M49: Eastern Europe); Central, South,
East and South-East Asia and Oceania are "APAC"; Mexico, Central and South
America and the Caribbean are "LATAM". Generic "eu-"/"europe-" codes are NOT
mapped for an unknown provider, because the UK sits inside them (AWS eu-west-2
is London) and UK is a separate group here.
"""

from __future__ import annotations

import re

REGION_GROUPS = ("US", "Canada", "Europe", "UK", "APAC", "Middle East", "LATAM", "Africa")

_EUROPE = """AD AL AT BA BE BG BY CH CY CZ DE DK EE ES FI FO FR GI GR HR HU IE IM IS IT JE GG LI LT LU LV
             MC MD ME MK MT NL NO PL PT RO RS RU SE SI SK SM UA VA XK AX""".split()
_APAC = """AF AU BD BN BT CN FJ HK ID IN IR JP KG KH KR KZ LA LK MM MN MO MV MY NP NZ PG PH PK SG TH TJ TL TM
           TW UZ VN""".split()
_MIDDLE_EAST = "AE AM AZ BH GE IL IQ JO KW LB OM PS QA SA SY TR YE".split()
_LATAM = """AR BO BR BS BZ CL CO CR CU DO EC GT HN HT JM MX NI PA PE PR PY SV TT UY VE BB""".split()
_AFRICA = """DZ AO BJ BW BF BI CM CV CF TD KM CD CG CI DJ EG GQ ER SZ ET GA GM GH GN GW KE LS LR LY MG MW ML
             MR MU MA MZ NA NE NG RW ST SN SC SL SO ZA SS SD TZ TG TN UG ZM ZW""".split()

COUNTRY_GROUP: dict[str, str] = {"US": "US", "CA": "Canada", "GB": "UK", "UK": "UK"}
for _codes, _g in ((_EUROPE, "Europe"), (_APAC, "APAC"), (_MIDDLE_EAST, "Middle East"),
                   (_LATAM, "LATAM"), (_AFRICA, "Africa")):
    for _c in _codes:
        COUNTRY_GROUP[_c] = _g

# English country names some APIs return instead of codes (Hyperstack's region country, Vast's place).
COUNTRY_NAMES = {
    "united states": "US", "usa": "US", "canada": "CA", "united kingdom": "GB", "norway": "NO",
    "germany": "DE", "france": "FR", "netherlands": "NL", "finland": "FI", "sweden": "SE", "iceland": "IS",
    "ireland": "IE", "spain": "ES", "portugal": "PT", "italy": "IT", "poland": "PL", "belgium": "BE",
    "czechia": "CZ", "bulgaria": "BG", "romania": "RO", "japan": "JP", "australia": "AU", "india": "IN",
    "singapore": "SG", "israel": "IL",
}

# AWS's published region codes (docs.aws.amazon.com/global-infrastructure/latest/regions/aws-regions.html).
AWS_REGIONS = {
    "us-east-1": "US", "us-east-2": "US", "us-west-1": "US", "us-west-2": "US",
    "us-gov-east-1": "US", "us-gov-west-1": "US",
    "ca-central-1": "Canada", "ca-west-1": "Canada",
    "eu-west-1": "Europe", "eu-west-2": "UK", "eu-west-3": "Europe", "eu-central-1": "Europe",
    "eu-central-2": "Europe", "eu-north-1": "Europe", "eu-south-1": "Europe", "eu-south-2": "Europe",
    "ap-east-1": "APAC", "ap-east-2": "APAC", "ap-south-1": "APAC", "ap-south-2": "APAC",
    "ap-northeast-1": "APAC", "ap-northeast-2": "APAC", "ap-northeast-3": "APAC",
    "ap-southeast-1": "APAC", "ap-southeast-2": "APAC", "ap-southeast-3": "APAC", "ap-southeast-4": "APAC",
    "ap-southeast-5": "APAC", "ap-southeast-7": "APAC",
    "me-south-1": "Middle East", "me-central-1": "Middle East", "il-central-1": "Middle East",
    "af-south-1": "Africa", "sa-east-1": "LATAM", "mx-central-1": "LATAM",
}

# Lambda: its region prefix carries the country (see providers/lambda_labs.py).
LAMBDA_PREFIX = {"us": "US", "europe": "Europe", "eu": "Europe", "asia": "APAC", "me": "Middle East",
                 "australia": "APAC"}

# Unambiguous cloud-code prefixes, for any provider. "eu"/"europe" deliberately absent (UK is inside them).
_CODE_PREFIX = {"us": "US", "ca": "Canada", "sa": "LATAM", "ap": "APAC", "asia": "APAC",
                "australia": "APAC", "me": "Middle East", "il": "Middle East", "af": "Africa"}
_CLOUD_CODE = re.compile(r"^([a-z]+)-[a-z]+-?\d+[a-z]?$")


def _country_of_word(word: str) -> str | None:
    w = word.strip().upper()
    if len(w) == 2 and w in COUNTRY_GROUP:
        return w
    return COUNTRY_NAMES.get(word.strip().lower())


def _digitalocean(region: str) -> set[str]:
    from providers.digitalocean import COUNTRY_BY_REGION_PREFIX

    out = set()
    for slug in region.split(","):
        cc = COUNTRY_BY_REGION_PREFIX.get(slug.strip()[:3].lower())
        out.add(COUNTRY_GROUP.get(cc) if cc else None)
    return out


def _one(provider: str, part: str) -> str | None:
    """Group of a single location string, or None."""
    p = part.strip().rstrip("…").strip()
    if not p:
        return None
    low = p.lower()
    if provider == "aws":
        return AWS_REGIONS.get(low)
    m = _CLOUD_CODE.match(low)
    if m:
        if provider == "lambda":
            return LAMBDA_PREFIX.get(m.group(1))
        return _CODE_PREFIX.get(m.group(1))
    if "," in p:
        bits = [b.strip() for b in p.split(",")]
        # Shadeform "CC, city[, ST]" puts the code first; Vast "<place>, CC" puts it last.
        first, last = bits[0], bits[-1]
        if len(first) == 2 and first.isupper():
            cc = _country_of_word(first)
        elif len(last) == 2 and last.isupper():
            cc = _country_of_word(last)
        else:
            cc = None
        return COUNTRY_GROUP.get(cc) if cc else None
    if provider == "hyperstack":
        cc = _country_of_word(re.split(r"[-_ ]", p)[0])
        return COUNTRY_GROUP.get(cc) if cc else None
    cc = _country_of_word(p)
    return COUNTRY_GROUP.get(cc) if cc else None


def region_groups(provider: str | None, region: str | None, country: str | None) -> set[str]:
    """Every group a listing's location names; empty when unknown."""
    provider = (provider or "").lower()
    if country:
        g = COUNTRY_GROUP.get(country.strip().upper()) or COUNTRY_GROUP.get(COUNTRY_NAMES.get(country.strip().lower(), ""))
        if g:
            return {g}
    if not region:
        return set()
    if provider == "digitalocean":
        found = _digitalocean(region)
    else:
        found = {_one(provider, part) for part in region.split(";")}
    if None in found:
        return set()  # one unrecognised part: we cannot vouch for where the listing runs
    return found


def region_group(provider: str | None, region: str | None, country: str | None) -> str | None:
    """The single group a listing is in, or None when unknown or spread over several groups."""
    g = region_groups(provider, region, country)
    return next(iter(g)) if len(g) == 1 else None


def region_label(provider: str | None, region: str | None, country: str | None) -> str:
    """A short human label: the provider's own region text, its group in brackets when known."""
    g = region_groups(provider, region, country)
    base = region or country or "unspecified"
    if not g:
        return base
    return f"{base} [{', '.join(sorted(g))}]"
