"""EIA-923 annual net generation per plant (PUBLIC: U.S.-government public domain).

    GET https://api.eia.gov/v2/electricity/facility-fuel/data/?frequency=annual&start=YYYY&end=YYYY
        &data[]=generation&facets[fuel2002][]=ALL&facets[primeMover][]=ALL&sort=plantCode&offset=N&length=5000

The route carries one row per plant, fuel and prime mover; the fuel ALL / prime mover ALL row is the plant's total
net generation in MWh (measured 2026-11-15: ~14.1k plants for 2025, 3 pages; Limestone, plant 298, 7,523,395 MWh).
Net generation is after the plant's own use, so storage-heavy plants are negative.

Depth: the route starts in 2001; the sync reads from 2008 (the generator inventory's first month). The latest
year or two are preliminary and revised, so the two newest complete years are re-read on every run (revisions
counted); older stored years are not asked again. A year is "complete" once the route's monthly endPeriod has
reached its December.
"""

from __future__ import annotations

import json
from datetime import date

from power.registry import Fetched

SOURCE = "eia923"
DS = "sites.eia923_annual"
ROUTE = "electricity/facility-fuel/"
EARLIEST_YEAR = 2008
REREAD_YEARS = 2


def params(year: int) -> list[tuple[str, str]]:
    return [("frequency", "annual"), ("start", str(year)), ("end", str(year)), ("data[]", "generation"),
            ("facets[fuel2002][]", "ALL"), ("facets[primeMover][]", "ALL")]


async def latest_year(get) -> tuple[int | None, Fetched]:
    from power.sources import eia

    f = await get.get(eia.BASE + ROUTE, params=[("api_key", eia.api_key())])
    try:
        end = json.loads(f.body)["response"]["endPeriod"] if f.ok else None   # "2026-08"
    except (ValueError, KeyError, TypeError):
        end = None
    if not end:
        f.error = f.error or "eia923: no endPeriod in the route metadata"
        return None, f
    y, m = int(end[:4]), int(end[5:7]) if len(end) >= 7 else 12
    return (y if m == 12 else y - 1), f


async def fetch_year(get, year: int) -> list[Fetched]:
    from power.sources import eia

    return await eia.paged(get, ROUTE + "data/", params(year), ("plantCode",))


def parse(f: Fetched) -> dict[int, tuple[float | None, str | None]]:
    from power.sources import eia

    out: dict[int, tuple[float | None, str | None]] = {}
    for r in eia.rows(f):
        if r.get("fuel2002") != "ALL" or r.get("primeMover") != "ALL":
            continue
        u = (r.get("generation-units") or "").lower()
        if u and u != "megawatthours":
            raise ValueError(f"eia923: unexpected unit {u!r}")
        try:
            pid = int(r["plantCode"])
        except (KeyError, TypeError, ValueError):
            continue
        v = r.get("generation")
        try:
            v = None if v in (None, "") else float(v)
        except (TypeError, ValueError):
            v = None
        out[pid] = (v, (r.get("state") or None) and str(r["state"])[:2])
    return out


async def sync(get, since: date | None = None, dry_run: bool = False) -> dict:
    import asyncio

    from power.sites import store

    out = {"dataset": DS, "requests": 0, "years": 0, "failed": 0, "new": 0, "revised": 0, "unchanged": 0}
    last, meta = await latest_year(get)
    out["requests"] += 1
    if last is None:
        await asyncio.to_thread(store.record, SOURCE, DS, meta)
        out.update(failed=1, error=meta.error or f"HTTP {meta.status}")
        return out
    done = {p.year for p in await asyncio.to_thread(store.applied_periods, DS)}
    # like the API months: once something is stored, a run without --since does not reach further back. A
    # --since inside an incomplete year still reads the newest complete one (a capacity factor needs a year)
    first = since.year if since else (min(done) if done else EARLIEST_YEAR)
    first = min(max(first, 2001), last)
    todo = [y for y in range(first, last + 1) if y not in done or y > last - REREAD_YEARS]
    out.update(first=first, latest=last, years_to_fetch=todo)
    if dry_run:
        return out
    for y in todo:
        answers = await fetch_year(get, y)
        out["requests"] += len(answers)
        plants, bad = {}, None
        for f in answers:
            if not f.ok:
                bad = f.error or f"HTTP {f.status}"
                continue
            try:
                plants.update(parse(f))
            except ValueError as exc:
                bad = f.error = f"parse: {exc}"[:500]
        ids = [await asyncio.to_thread(store.record, SOURCE, DS, f, date(y, 1, 1), date(y, 12, 31))
               for f in answers]
        if bad:
            out["failed"] += 1
            out["error"] = bad
            continue
        res = await asyncio.to_thread(store.upsert_generation, y, plants, ids[-1])
        for i in ids:
            await asyncio.to_thread(store.mark_applied, i)
        out["years"] += 1
        for k in ("new", "revised", "unchanged"):
            out[k] += res[k]
    return out
