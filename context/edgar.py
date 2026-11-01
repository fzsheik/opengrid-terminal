"""SEC EDGAR XBRL: GPU supply and AI capex as quarterly numeric series (methodology/market-context.md).

Numbers only. Filings as news items (8-K, Form D) belong to news/; this module reads XBRL facts.

    companyfacts  https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json
                  total revenue (NVIDIA, AMD) and capital expenditure (the cash paid for property and equipment)
    submissions   https://data.sec.gov/submissions/CIK##########.json      -> the newest 10-Q / 10-K filings
    instance      https://www.sec.gov/Archives/edgar/data/<cik>/<accession>/<doc>_htm.xml
                  each filing's XBRL instance, for SEGMENT revenue, which companyfacts does not carry (it has no
                  dimensions): NVIDIA Data Center (srt:ProductOrServiceAxis = nvda:DataCenterMember) and AMD Data
                  Center (us-gaap:StatementBusinessSegmentsAxis = amd:DataCenterMember)

Fair access (sec.gov "Accessing EDGAR Data"): a User-Agent naming the company and a contact email (SEC_USER_AGENT;
without it this source is off), at most 10 requests a second (this client paces itself well under that). EDGAR
data is public. Every value carries the accession number of the filing it came from.

Quarters: an income-statement value usually comes as a three-month figure; cash-flow values (capex) come
year-to-date. A quarter is taken as REPORTED when a filing gives that three-month period, else DERIVED as
year-to-date minus the previous year-to-date with the same start (Q4 = full year - nine months), or as six months
minus its reported last quarter (the first quarter, when only that filing is read). published_at is the
filing date of the first filing that made the value knowable (the later of the two for a derived quarter). A period
reported again in a later filing (as a prior-year comparative) keeps its first filing's value and date; a different
later value is a revision, stored in meta.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation

SOURCE = "sec_edgar"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
INSTANCE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accn}/{doc}"
UNIT = "usd"
ATTRIBUTION = "Source: U.S. Securities and Exchange Commission, EDGAR (XBRL financial data from company filings)"
SINCE = date(2019, 1, 1)
MAX_FILINGS = 8          # 10-Q / 10-K instances read per company on the first run (each also gives prior-year figures)
REVENUE = ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax")
CAPEX = ("PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets")

# ticker -> (CIK, name, capex concepts to choose from, reads total revenue). Companies switch concepts
# (NVIDIA: PaymentsToAcquirePropertyPlantAndEquipment until 2020, then PaymentsToAcquireProductiveAssets).
COMPANIES: dict[str, tuple[int, str, tuple[str, ...], bool]] = {
    "NVDA": (1045810, "NVIDIA", CAPEX, True),
    "AMD": (2488, "AMD", CAPEX, True),
    "MSFT": (789019, "Microsoft", CAPEX, False),
    "GOOGL": (1652044, "Alphabet", CAPEX, False),
    "AMZN": (1018724, "Amazon", CAPEX, False),
    "META": (1326801, "Meta Platforms", CAPEX, False),
    "ORCL": (1341439, "Oracle", CAPEX, False),
    "CRWV": (1769628, "CoreWeave", CAPEX, False),
}
# ticker -> the exact dimensions of the data-center segment revenue facts
SEGMENTS: dict[str, dict[str, str]] = {
    "NVDA": {"srt:ProductOrServiceAxis": "nvda:DataCenterMember"},
    "AMD": {"srt:ConsolidationItemsAxis": "us-gaap:OperatingSegmentsMember",
            "us-gaap:StatementBusinessSegmentsAxis": "amd:DataCenterMember"},
}


def series_id(ticker: str, what: str) -> str:
    return f"sec.{ticker.lower()}.{what}"


def catalog() -> list[dict]:
    out = []
    for t, (cik, name, capex, rev) in COMPANIES.items():
        rows = [("capex", f"{name} capital expenditure (cash paid for property and equipment), quarterly")]
        if rev:
            rows.append(("revenue_total", f"{name} total revenue, quarterly"))
        if t in SEGMENTS:
            rows.append(("revenue_data_center", f"{name} Data Center segment revenue, quarterly"))
        for what, label in rows:
            out.append({"series_id": series_id(t, what), "source": SOURCE, "unit": UNIT, "frequency": "quarterly",
                        "name": label, "entity": t, "geo": None, "market_area": None, "attribution": ATTRIBUTION,
                        "cik": cik})
    return out


# --------------------------------------------------------------------------- reshape (pure)

def reshape_facts(body: dict, concepts: tuple[str, ...]) -> dict:
    facts = (body or {}).get("facts", {}).get("us-gaap")
    if not isinstance(facts, dict):
        raise ValueError("edgar companyfacts: no us-gaap facts - reshaped")
    kept = {c: {"USD": facts[c]["units"]["USD"]} for c in concepts if c in facts and "USD" in facts[c].get("units", {})}
    if not kept:
        raise ValueError(f"edgar companyfacts: none of {concepts}")
    return {"cik": body.get("cik"), "entityName": body.get("entityName"), "facts": kept}


def reshape_submissions(body: dict) -> dict:
    r = (body or {}).get("filings", {}).get("recent")
    if not isinstance(r, dict) or "accessionNumber" not in r:
        raise ValueError("edgar submissions: no filings.recent - reshaped")
    rows = [{"accn": r["accessionNumber"][i], "form": f, "filed": r["filingDate"][i], "report": r["reportDate"][i],
             "doc": r["primaryDocument"][i]} for i, f in enumerate(r["form"]) if f in ("10-Q", "10-K")]
    return {"cik": body.get("cik"), "name": body.get("name"), "filings": rows[:MAX_FILINGS]}


def instance_doc(primary_doc: str) -> str:
    """EDGAR's extracted XBRL instance for an inline-XBRL filing: nvda-20260726.htm -> nvda-20260726_htm.xml."""
    if not primary_doc.endswith(".htm"):
        raise ValueError(f"edgar: primary document {primary_doc!r} is not .htm")
    return primary_doc[:-4] + "_htm.xml"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_instance(xml: bytes | str, concepts: tuple[str, ...], dims: dict[str, str]) -> list[dict]:
    """Facts of `concepts` in USD whose context has EXACTLY these explicit dimensions."""
    root = ET.fromstring(xml)
    ctx = {}
    for c in root.iter():
        if _local(c.tag) != "context":
            continue
        d = {m.get("dimension"): (m.text or "").strip() for m in c.iter() if _local(m.tag) == "explicitMember"}
        start = end = None
        for p in c.iter():
            if _local(p.tag) == "startDate":
                start = p.text.strip()
            elif _local(p.tag) == "endDate":
                end = p.text.strip()
        ctx[c.get("id")] = (d, start, end)
    out = []
    for el in root:
        name = _local(el.tag)
        if name not in concepts or not el.get("contextRef") or (el.get("unitRef") or "").lower() != "usd":
            continue
        d, start, end = ctx.get(el.get("contextRef"), ({}, None, None))
        if d != dims or not start or not end:
            continue
        try:
            val = Decimal((el.text or "").strip())
        except InvalidOperation:
            continue
        out.append({"concept": name, "start": start, "end": end, "val": str(val)})
    return out


# --------------------------------------------------------------------------- quarters (pure)

def _d(s: str) -> date:
    return date.fromisoformat(s)


def quarterize(facts: list[dict], since: date = SINCE) -> list[dict]:
    """facts: [{start, end, val, filed, accn, form?}] (one concept, USD) -> one row per fiscal quarter.

    Same period in several filings: the first filing's value and date (later different values -> meta.revised).
    A three-month fact is a REPORTED quarter; otherwise a quarter is DERIVED from two facts one quarter apart that share
    a start (YTD minus the previous YTD: the last quarter) or an end (six months minus its last quarter: the first)."""
    by_period: dict[tuple[date, date], dict] = {}
    for f in sorted(facts, key=lambda f: (f["filed"], f.get("accn") or "")):
        k = (_d(f["start"]), _d(f["end"]))
        v = Decimal(str(f["val"]))
        cur = by_period.get(k)
        if cur is None:
            by_period[k] = {"start": k[0], "end": k[1], "val": v, "filed": f["filed"], "accn": f.get("accn"),
                            "form": f.get("form"), "revised": None}
        elif v != cur["val"]:
            cur["revised"] = {"val": str(v), "filed": f["filed"], "accn": f.get("accn")}

    def quarter(s, e):
        return 80 <= (e - s).days <= 100

    q = {k: dict(p, basis="reported", how=None) for k, p in by_period.items() if quarter(*k)}
    changed = True
    while changed:
        changed = False
        for big in [p for p in by_period.values() if (p["end"] - p["start"]).days > 100]:
            for small in by_period.values():
                if small["start"] == big["start"] and small["end"] < big["end"]:
                    k = (small["end"] + timedelta(days=1), big["end"])      # YTD minus the earlier YTD: the last quarter
                elif small["end"] == big["end"] and small["start"] > big["start"]:
                    k = (big["start"], small["start"] - timedelta(days=1))  # YTD minus its last quarter: the first part
                else:
                    continue
                if not quarter(*k) or k in q:
                    continue
                q[k] = {"start": k[0], "end": k[1], "val": big["val"] - small["val"], "basis": "derived",
                        "filed": max(big["filed"], small["filed"]), "accn": big["accn"], "form": big.get("form"),
                        "revised": None,
                        "how": f"{big['start']}..{big['end']} minus {small['start']}..{small['end']}"}
                changed = True
    return [q[k] for k in sorted(q) if k[1] >= since]


def filed_at(day: str) -> datetime:
    """A filing date as an instant: end of that day in UTC (EDGAR gives the date; acceptance time is not read)."""
    return datetime.combine(_d(day), time(23, 59, 59), timezone.utc)


def to_points(ticker: str, what: str, quarters: list[dict], concept: str) -> list[dict]:
    return [{"series_id": series_id(ticker, what), "period_start": r["start"], "period_end": r["end"], "value": r["val"],
             "unit": UNIT, "source": SOURCE, "basis": r["basis"], "published_at": filed_at(r["filed"]),
             "meta": {"concept": concept, "accession": r["accn"], "form": r.get("form"), "filed": r["filed"],
                      **({"derived_from": r["how"]} if r.get("how") else {}),
                      **({"revised": r["revised"]} if r.get("revised") else {})}} for r in quarters]


def facts_points(ticker: str, payload: dict) -> list[dict]:
    """A reshaped companyfacts payload -> capex (and total revenue) points for one company."""
    cik, name, capex, rev = COMPANIES[ticker]
    out = []
    groups = [("capex", capex)] + ([("revenue_total", REVENUE)] if rev else [])
    for what, concepts in groups:
        best = None   # the concept whose facts reach the latest period (companies switch concepts over time)
        for c in concepts:
            rows = (payload["facts"].get(c) or {}).get("USD") or []
            rows = [r for r in rows if r.get("start") and r.get("end") and r.get("form") in ("10-Q", "10-K", "10-Q/A", "10-K/A")]
            if rows and (best is None or max(r["end"] for r in rows) > best[1]):
                best = (c, max(r["end"] for r in rows), rows)
        if best:
            out += to_points(ticker, what, quarterize(best[2]), best[0])
    return out


def segment_points(ticker: str, instances: list[dict]) -> list[dict]:
    """Stored instance payloads [{accn, filed, form, facts}] -> data-center revenue points."""
    rows, concept = [], None
    for inst in instances:
        for f in inst.get("facts") or []:
            concept = concept or f["concept"]
            rows.append({**f, "filed": inst["filed"], "accn": inst["accn"], "form": inst.get("form")})
    by_concept: dict[str, list] = {}
    for r in rows:
        by_concept.setdefault(r["concept"], []).append(r)
    if not by_concept:
        return []
    c = max(by_concept, key=lambda k: (len(by_concept[k]), k))
    return to_points(ticker, "revenue_data_center", quarterize(by_concept[c]), c)
