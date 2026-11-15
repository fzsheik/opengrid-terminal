"""Lium's keyless market feeds (job `lium_public`, every 15 minutes) and its published throughput.

    poll()          fetches every public feed (feeds.PUBLIC_FEEDS), stores each answer raw and change-only
                    (raw.store), and for a CHANGED answer writes its numbers to lium_feed_values in long form. A 404
                    from /public/v1/requests ("while the feature is off") is stored once and logged once at INFO.
                    Then parse_stats().
    parse_stats()   Lium's public stats feed is already fetched and stored raw by providers/lium.py (raw_snapshots,
                    endpoint STATS). The newest stored answer is parsed into lium_throughput_daily (per UTC day:
                    GMV billed, GPU-hours, rentals started, fleet utilisation) and lium_model_stats_daily (per GPU
                    model, for the day it was read: the 30-day GPU-hour-weighted median PAID price, utilisation,
                    cheapest ask, GPUs listed / rented). Kind transaction, basis published_aggregate: Lium computed
                    them from its own rentals; OpenGrid cannot check them against trade records without the partner
                    feed. Every snapshot carries 30 days, so one missed run loses nothing.
                    reparse_stats() walks every stored STATS snapshot (oldest first) for the history.

Every row is stamped with the registry's tier (provider_meta: internal) and never reaches a public reader.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

import httpx
from sqlalchemy import select

from lium_data import feeds as F
from lium_data import raw
from providers.lium import STATS as STATS_ENDPOINT
from store.lium import LiumFeedValue, LiumModelStatsDaily, LiumThroughputDaily

log = logging.getLogger(__name__)

POLL_SECONDS = 900


def _session():
    import normalize

    return normalize.SessionLocal


def _dec(v) -> Decimal | None:
    if v is None or isinstance(v, (dict, list)):
        return None
    if isinstance(v, bool):
        return Decimal(int(v))
    try:
        d = Decimal(str(v))
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def _time(v) -> datetime | None:
    if not isinstance(v, str) or not v:
        return None
    try:
        t = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t


def _v(subject, metric, value=None, *, gpu_count=0, text=None, at=None) -> dict:
    return {"subject": str(subject)[:160], "gpu_count": int(gpu_count or 0), "metric": metric, "value": _dec(value),
            "value_text": text, "value_time": at}


# --------------------------------------------------------------------------
# Parsers: JSON document -> (generated_at, [values])
# --------------------------------------------------------------------------


def parse_launch_stats(doc) -> tuple[datetime | None, list[dict]]:
    if not isinstance(doc, dict) or not isinstance(doc.get("gpus"), list):
        raise ValueError("launch-stats: no `gpus` list")
    out = [_v("*", k, doc.get(k)) for k in ("window_days", "min_launches", "min_providers") if doc.get(k) is not None]
    for g in doc["gpus"]:
        if not isinstance(g, dict) or not g.get("gpu_type"):
            continue
        for m in ("success_rate", "time_to_ssh_p50_seconds", "time_to_ssh_p90_seconds"):
            out.append(_v(g["gpu_type"], m, g.get(m), gpu_count=g.get("gpu_count")))
    return _time(doc.get("generated_at")), out


def parse_requests(doc) -> tuple[datetime | None, list[dict]]:
    if not isinstance(doc, dict) or not isinstance(doc.get("requests"), list):
        raise ValueError("requests: no `requests` list")
    out = [_v("*", "classes", text=json.dumps(doc.get("classes") or []))]
    for r in doc["requests"]:
        if not isinstance(r, dict) or not r.get("class"):
            continue
        out += [_v(r["class"], "requests", r.get("requests")), _v(r["class"], "gpus_asked", r.get("gpus_asked")),
                _v(r["class"], "oldest_at", at=_time(r.get("oldest_at")), text=r.get("oldest_at"))]
    return _time(doc.get("generated_at")), out


def parse_executor_stats(doc) -> tuple[None, list[dict]]:
    if not isinstance(doc, list):
        raise ValueError("executors/stats: not a list")
    out = []
    for r in doc:
        if isinstance(r, dict) and r.get("gpu_type"):
            out += [_v(r["gpu_type"], m, r.get(m)) for m in ("rented_count", "all_count", "percentage")]
    return None, out


def parse_spot_idle_rates(doc) -> tuple[None, list[dict]]:
    if not isinstance(doc, list):
        raise ValueError("spot-idle-rates: not a list")
    return None, [_v(r["gpu_model"], "spot_usd_per_gpu_hour", r.get("spot_usd_per_gpu_hour"), gpu_count=r.get("gpu_count"))
                  for r in doc if isinstance(r, dict) and r.get("gpu_model")]


def parse_idle_capacity(doc) -> tuple[None, list[dict]]:
    if not isinstance(doc, list):
        raise ValueError("machines/capacity: not a list")
    out = []
    for r in doc:
        if not isinstance(r, dict) or not (r.get("gpu_model") or r.get("base_model")):
            continue
        subject = r.get("gpu_model") or r.get("base_model")
        out.append(_v(subject, "base_model", text=r.get("base_model")))
        for bucket, b in (r.get("buckets") or {}).items():
            if not isinstance(b, dict) or not str(bucket).isdigit():
                continue
            out += [_v(subject, m, b.get(m), gpu_count=int(bucket))
                    for m in ("max_cap", "unrented_count", "hourly_rate", "next_gpu_idle_rate", "emission_capped")]
        for bucket, why in (r.get("ineligible_buckets") or {}).items():
            if str(bucket).isdigit():
                out.append(_v(subject, "ineligible", gpu_count=int(bucket),
                              text=why if isinstance(why, str) else json.dumps(why, sort_keys=True)))
    return None, out


def parse_shared_config(doc) -> tuple[None, list[dict]]:
    if not isinstance(doc, dict) or not isinstance(doc.get("machine_prices"), dict):
        raise ValueError("shared-config: no `machine_prices`")
    out = [_v(gpu, "reference_price", p) for gpu, p in doc["machine_prices"].items()]
    out += [_v(gpu, "reference_price_p90", p) for gpu, p in (doc.get("machine_prices_p90") or {}).items()]
    out += [_v("*", k, doc.get(k)) for k in F.FEEDS["shared_config"].keep
            if k not in ("machine_prices", "machine_prices_p90") and doc.get(k) is not None]
    return None, out


def _flag(v):
    return None if v is None else int(bool(v))


_MACHINE_NUMBERS = ("price", "base_price", "p_min", "p_max", "soft_price_limit", "median_price", "rental_rate",
                    "avg_rental_rate_30d", "total_gpu_count", "k", "usd_per_epoch_rented", "usd_per_epoch_unrented")


def parse_machines(doc) -> tuple[None, list[dict]]:
    if not isinstance(doc, list) or not any(isinstance(r, dict) and r.get("name") for r in doc):
        raise ValueError("machines: not a list of GPU models")
    out = []
    for r in doc:
        if not isinstance(r, dict) or not r.get("name"):
            continue
        out += [_v(r["name"], m, r.get(m)) for m in _MACHINE_NUMBERS]
        out += [_v(r["name"], "supported", _flag(r.get("supported"))),
                _v(r["name"], "estimates_available", _flag(r.get("estimates_available"))),
                _v(r["name"], "gpu_class", text=r.get("gpu_class"))]
    return None, out


def parse_idle_ineligible(doc) -> tuple[None, list[dict]]:
    if not isinstance(doc, list):
        raise ValueError("machines/capacity/ineligible: not a list")
    return None, [_v(r["model"], "reason", text=r.get("reason")) for r in doc if isinstance(r, dict) and r.get("model")]


def _parse_count(key: str):
    def parse(doc) -> tuple[None, list[dict]]:
        if not isinstance(doc, dict) or isinstance(doc.get(key), bool) or not isinstance(doc.get(key), int):
            raise ValueError(f"no integer `{key}`")
        return None, [_v("*", "count", doc[key])]
    return parse


def parse_version(doc) -> tuple[None, list[dict]]:
    if not isinstance(doc, dict) or not doc.get("build_id"):
        raise ValueError("version: no `build_id`")
    return None, [_v("*", "build_id", text=str(doc["build_id"])),
                  _v("*", "started_at", at=_time(doc.get("started_at")), text=doc.get("started_at")),
                  _v("*", "features", text=json.dumps(sorted(doc.get("features") or [])))]


# ---- per provider. Stored slimmed (raw.SLIMMERS): coordinates, GPU serials, validator keys and live counters
# (uptime minutes, free disk, open ports) are not kept, so an unchanged provider is one stored answer.

def slim_provider_stats(doc) -> dict:
    if not isinstance(doc, dict) or not isinstance(doc.get("rental_successes"), dict):
        raise ValueError("provider-stats: no `rental_successes`")
    out = {k: doc.get(k) for k in ("miner_hotkey", "shared_pool", "average_network_speed", "feedback_rates",
                                   "rental_failures", "rental_successes", "uptime_counts", "max_rental_time",
                                   "unique_renters", "unique_positive_renters")}
    out["locations"] = sorted({(str(x.get("country_code") or ""), str(x.get("city") or ""))
                               for x in doc.get("locations") or [] if isinstance(x, dict)})
    load = doc.get("host_load") if isinstance(doc.get("host_load"), dict) else {}
    out["host_load"] = {"enabled": load.get("enabled"), "marked_node_count": load.get("marked_node_count")}
    return out


_EXECUTOR_INFO = ("machine_name", "gpu_count", "available_gpu_count", "price_per_gpu", "pending_price_per_hour",
                  "price_change_effective_date", "active", "verified", "finished_jobs", "reclaimed", "reclaimed_at",
                  "created_at")
_EXECUTOR_TOP = ("rented", "rented_gpu_count", "revenue_per_hour", "rented_since", "listing_hidden",
                 "listing_hidden_reasons")


def slim_provider_executors(doc) -> list[dict]:
    if not isinstance(doc, list):
        raise ValueError("provider executors: not a list")
    out = []
    for x in doc:
        info = x.get("executor_info") if isinstance(x, dict) else None
        if not isinstance(info, dict) or not (x.get("id") or info.get("id")):
            raise ValueError("provider executors: a machine without executor_info or id")
        specs = info.get("specs") if isinstance(info.get("specs"), dict) else {}
        loc = x.get("location") if isinstance(x.get("location"), dict) else {}
        out.append({"id": str(x.get("id") or info.get("id")),
                    **{k: info.get(k) for k in _EXECUTOR_INFO}, **{k: x.get(k) for k in _EXECUTOR_TOP},
                    "cpu_model": (specs.get("cpu") or {}).get("model"), "cpu_count": (specs.get("cpu") or {}).get("count"),
                    "ram_total": (specs.get("ram") or {}).get("total"),
                    "country_code": loc.get("country_code"), "city": loc.get("city")})
    return sorted(out, key=lambda r: r["id"])


def parse_provider_stats(doc, hotkey: str) -> tuple[None, list[dict]]:
    d = slim_provider_stats(doc)
    g = lambda k, m: (d.get(k) or {}).get(m) if isinstance(d.get(k), dict) else None  # noqa: E731
    pairs = [("rental_successes", g("rental_successes", "rental_successes")),
             ("success_rate_pct", g("rental_successes", "success_rate_percentage")),
             ("rental_failures", g("rental_failures", "rental_failures")),
             ("unique_renters", g("unique_renters", "unique_renters")),
             ("unique_positive_renters", g("unique_positive_renters", "unique_positive_renters")),
             ("good_feedbacks", g("feedback_rates", "good_feedbacks")),
             ("bad_feedbacks", g("feedback_rates", "bad_feedbacks")),
             ("average_uptime_hours", g("uptime_counts", "average_uptime_hours")),
             ("reclaimed_count", g("uptime_counts", "reclaimed_count")),
             ("total_machines", g("uptime_counts", "total_machines")), ("total_gpus", g("uptime_counts", "total_gpus")),
             ("max_rental_duration_hours", g("max_rental_time", "max_rental_duration_hours")),
             ("average_upload_mbps", g("average_network_speed", "average_upload_speed_mbps")),
             ("average_download_mbps", g("average_network_speed", "average_download_speed_mbps")),
             ("shared_pool", _flag(d.get("shared_pool")))]
    out = [_v(hotkey, m, v) for m, v in pairs]
    out.append(_v(hotkey, "countries", text=",".join(sorted({c for c, _ in d["locations"] if c}))))
    return None, out


def parse_provider_executors(doc, hotkey: str) -> tuple[None, list[dict]]:
    out = []
    for r in slim_provider_executors(doc):
        subject = f"{hotkey}/{r['id']}"
        out += [_v(subject, m, r.get(m)) for m in ("gpu_count", "available_gpu_count", "price_per_gpu",
                                                   "rented_gpu_count", "revenue_per_hour", "finished_jobs")]
        out += [_v(subject, m, _flag(r.get(m))) for m in ("rented", "active", "verified", "reclaimed", "listing_hidden")]
        out += [_v(subject, "rented_since", at=_time(r.get("rented_since")), text=r.get("rented_since")),
                _v(subject, "created_at", at=_time(r.get("created_at")), text=r.get("created_at")),
                _v(subject, "machine_name", text=r.get("machine_name"))]
    return None, out


def _stable_provider_stats(d: dict) -> dict:
    up = {k: v for k, v in (d.get("uptime_counts") or {}).items() if k != "average_uptime_hours"}
    return {**d, "uptime_counts": up}


def _stable_provider_executors(rows: list[dict]) -> list[dict]:
    return [{k: v for k, v in r.items() if k != "finished_jobs"} for r in rows]


raw.SLIMMERS.update(provider_stats=slim_provider_stats, provider_executors=slim_provider_executors)
# Average uptime grows with every read and a busy machine finishes jobs every hour: both are kept in the stored
# body and written as values whenever anything else about the provider changes, but neither makes a new row alone.
raw.STABLE.update(provider_stats=_stable_provider_stats, provider_executors=_stable_provider_executors)

PARSERS = {"machines": parse_machines, "idle_ineligible": parse_idle_ineligible,
           "executors_count": _parse_count("count"), "executors_total": _parse_count("total_count"),
           "version": parse_version, "provider_stats": parse_provider_stats,
           "provider_executors": parse_provider_executors,
           "launch_stats": parse_launch_stats, "requests": parse_requests, "executor_stats": parse_executor_stats,
           "spot_idle_rates": parse_spot_idle_rates, "idle_capacity": parse_idle_capacity,
           "shared_config": parse_shared_config}


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------


def latest_values(s, feed: str, at: datetime | None = None, scope: str | None = None) -> dict[tuple, LiumFeedValue]:
    """The value in force for every (subject, gpu_count, metric) of `feed` at `at` (default: now), absent ones
    included (present False). lium_feed_values is change-only, so this is the newest row per key.
    `scope` (a provider's hotkey) limits it to that provider's subjects: the hotkey itself and "hotkey/machine"."""
    stmt = (select(LiumFeedValue).where(LiumFeedValue.feed == feed)
            .order_by(LiumFeedValue.subject, LiumFeedValue.gpu_count, LiumFeedValue.metric,
                      LiumFeedValue.observed_at.desc(), LiumFeedValue.id.desc())
            .distinct(LiumFeedValue.subject, LiumFeedValue.gpu_count, LiumFeedValue.metric))
    if at is not None:
        stmt = stmt.where(LiumFeedValue.observed_at <= at)
    if scope is not None:
        stmt = stmt.where((LiumFeedValue.subject == scope) | LiumFeedValue.subject.startswith(scope + "/", autoescape=True))
    return {(v.subject, v.gpu_count, v.metric): v for v in s.execute(stmt).scalars()}


def _same(old: LiumFeedValue, new: dict) -> bool:
    if not old.present:
        return False
    ov, nv = old.value, new["value"]
    return ((ov is None and nv is None) or (ov is not None and nv is not None and Decimal(ov) == nv)) \
        and old.value_text == new["value_text"] and old.value_time == new["value_time"]


def save(f: raw.Fetched) -> dict:
    """Store the answer raw (change-only), then its values change-only too: for a changed answer, only the values
    that differ from the ones in force are written, and a key the answer no longer carries is written once with
    present = false (a GPU that dropped out of launch-stats, a request class that closed)."""
    gen, values = None, []
    # A per-provider answer speaks only for its own provider: its keys are compared with that provider's alone.
    scope = str(f.params.get("hotkey")) if f.feed.per_provider else None
    if f.status_code == 200 and f.body is not None:
        try:
            parse = PARSERS[f.feed.name]
            gen, values = parse(json.loads(f.body), scope) if f.feed.per_provider else parse(json.loads(f.body))
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            f.error = f"parse: {exc}"[:500]
            values = []
    written = 0
    with _session().begin() as s:
        st = raw.store(s, f, rows=len(values))
        if st.new and st.state in ("ok", "empty"):
            new = {(v["subject"], v["gpu_count"], v["metric"]): v for v in values}
            old = latest_values(s, f.feed.name, scope=scope)
            common = {"snapshot_id": st.snapshot.id, "feed": f.feed.name, "observed_at": f.fetched_at,
                      "generated_at": gen, "visibility": st.snapshot.visibility}
            rows = [LiumFeedValue(**common, **v, present=True) for k, v in new.items()
                    if k not in old or not _same(old[k], v)]
            rows += [LiumFeedValue(**common, subject=k[0], gpu_count=k[1], metric=k[2], present=False)
                     for k, o in old.items() if k not in new and o.present]
            s.add_all(rows)
            written = len(rows)
    raw.log_outcome(st, f)
    return {"feed": f.feed.name, "state": st.state, "status_code": f.status_code, "changed": st.new,
            "values": len(values), "values_written": written,
            "error": f.error if st.state not in ("ok", "empty") else None}


async def poll(http: httpx.AsyncClient | None = None, gap: float = 0.2) -> dict:
    """The `lium_public` job: every public feed once, then the published throughput."""
    own = http is None
    http = http or raw.client()
    results = []
    try:
        for feed in F.PUBLIC_FEEDS:
            results.append(save(await raw.fetch(feed, http)))
            await asyncio.sleep(gap)
    finally:
        if own:
            await http.aclose()
    try:
        stats = parse_stats()
    except Exception as exc:  # noqa: BLE001  (the feeds above are stored; a stats parse failure is reported)
        log.warning("lium stats parse failed: %s", exc)
        stats = {"error": f"{type(exc).__name__}: {exc}"[:300]}
    return {"feeds": results, "stats": stats}


# --------------------------------------------------------------------------
# Per provider (job `lium_providers`, hourly)
# --------------------------------------------------------------------------

PROVIDERS_POLL_SECONDS = 3600
KNOWN_PROVIDER_DAYS = 30


def known_hotkeys() -> list[str]:
    """Every provider to ask about: the miner hotkeys in the newest stored /api/executors copy (machines with a free
    GPU), plus every provider that answered within the last 30 days, so one whose machines are all rented now is
    still read."""
    from datetime import timedelta

    from providers.lium import ENDPOINT as EXECUTORS
    from store.lium import LiumFeedSnapshot
    from tables import RawSnapshot

    keys: set[str] = set()
    with _session()() as s:
        newest = s.execute(select(RawSnapshot).where(RawSnapshot.provider == "lium", RawSnapshot.endpoint == EXECUTORS,
                                                     RawSnapshot.ok.is_(True))
                           .order_by(RawSnapshot.fetched_at.desc(), RawSnapshot.id.desc()).limit(1)).scalar()
        if newest is not None and isinstance(newest.payload, list):
            keys |= {e["miner_hotkey"] for e in newest.payload if isinstance(e, dict) and e.get("miner_hotkey")}
        since = datetime.now(timezone.utc) - timedelta(days=KNOWN_PROVIDER_DAYS)
        seen = s.execute(select(LiumFeedSnapshot.params).where(
            LiumFeedSnapshot.feed == "provider_stats", LiumFeedSnapshot.state == "ok",
            LiumFeedSnapshot.last_seen_at >= since)).scalars()
        keys |= {p["hotkey"] for p in seen if isinstance(p, dict) and p.get("hotkey")}
    return sorted(str(k) for k in keys)


async def poll_providers(http: httpx.AsyncClient | None = None, gap: float = F.POLITE_GAP_SECONDS,
                         hotkeys: list[str] | None = None) -> dict:
    """The `lium_providers` job: both per-provider feeds for every known provider, one request at a time. A 429
    ends the round (the next one starts over); every other answer, a 404 included, is stored change-only."""
    hotkeys = known_hotkeys() if hotkeys is None else hotkeys
    own = http is None
    http = http or raw.client()
    states: dict[str, int] = {}
    changed = written = 0
    stopped = None
    try:
        for hk in hotkeys:
            for feed in F.PROVIDER_FEEDS:
                r = save(await raw.fetch(feed, http, path={"hotkey": hk}))
                states[r["state"]] = states.get(r["state"], 0) + 1
                changed += bool(r["changed"])
                written += r["values_written"]
                if r["state"] == "rate_limited":
                    stopped = "rate_limited"
                    break
                await asyncio.sleep(gap)
            if stopped:
                break
    finally:
        if own:
            await http.aclose()
    if stopped:
        log.warning("lium providers: rate limited; round ended early")
    return {"providers": len(hotkeys), "states": states, "answers_changed": changed, "values_written": written,
            "stopped": stopped}


# --------------------------------------------------------------------------
# Published throughput and per-model paid prices (from providers/lium.py's raw STATS snapshots)
# --------------------------------------------------------------------------


def _stats_snapshots(newest_only: bool):
    from tables import RawSnapshot

    stmt = (select(RawSnapshot).where(RawSnapshot.provider == "lium", RawSnapshot.endpoint == STATS_ENDPOINT,
                                      RawSnapshot.ok.is_(True)))
    stmt = stmt.order_by(RawSnapshot.fetched_at.desc(), RawSnapshot.id.desc()).limit(1) if newest_only else \
        stmt.order_by(RawSnapshot.fetched_at, RawSnapshot.id)
    with _session()() as s:
        return [(r.id, r.fetched_at, r.payload) for r in s.execute(stmt).scalars()]


def _store_stats(raw_id: int, fetched_at: datetime, doc: dict) -> dict:
    import provider_meta

    vis = provider_meta.enforce("lium", provider_meta.visibility("lium", STATS_ENDPOINT, "signals"))
    now = datetime.now(timezone.utc)
    read_day = fetched_at.astimezone(timezone.utc).date()
    days: dict[date, dict] = {}
    tp = doc.get("throughput") if isinstance(doc.get("throughput"), dict) else {}
    for d in tp.get("daily") or []:
        try:
            day = date.fromisoformat(str(d.get("day")))
        except (ValueError, TypeError, AttributeError):
            continue
        days.setdefault(day, {}).update(gmv_usd=_dec(d.get("gmv_usd")), gpu_hours=_dec(d.get("gpu_hours")),
                                        rentals=d.get("rentals") if isinstance(d.get("rentals"), int) else None)
    for d in doc.get("utilisation_daily") or []:
        try:
            day = date.fromisoformat(str(d.get("day")))
        except (ValueError, TypeError, AttributeError):
            continue
        days.setdefault(day, {}).update(utilisation=_dec(d.get("utilisation")), utilisation_partial=day >= read_day)
    revised = written = 0
    with _session().begin() as s:
        old = {r.day: r for r in s.execute(select(LiumThroughputDaily).where(
            LiumThroughputDaily.day.in_(list(days)))).scalars()} if days else {}
        for day, vals in sorted(days.items()):
            row = old.get(day)
            if row is None:
                s.add(LiumThroughputDaily(day=day, raw_snapshot_id=raw_id, first_seen_at=now, updated_at=now,
                                          revisions=0, visibility=vis, **vals))
                written += 1
                continue
            changed = {k: v for k, v in vals.items() if v is not None and getattr(row, k) != v
                       and not (isinstance(v, Decimal) and getattr(row, k) is not None
                                and Decimal(getattr(row, k)) == v)}
            if changed:
                # a figure that moves after its day closed is a revision by Lium (today's running row is not)
                if any(k in ("gmv_usd", "gpu_hours", "rentals") and getattr(row, k) is not None for k in changed) or \
                        ("utilisation" in changed and row.utilisation is not None and not row.utilisation_partial):
                    row.revisions = (row.revisions or 0) + 1
                    revised += 1
                for k, v in changed.items():
                    setattr(row, k, v)
                row.raw_snapshot_id, row.updated_at = raw_id, now
                written += 1
        models = 0
        for m in doc.get("models") or []:
            if not isinstance(m, dict) or not m.get("gpu_model"):
                continue
            vals = {"median_price_30d": _dec(m.get("median_price_30d")), "utilisation_30d": _dec(m.get("utilisation_30d")),
                    "from_price": _dec(m.get("from_price")),
                    "gpus_listed": m.get("gpus_listed") if isinstance(m.get("gpus_listed"), int) else None,
                    "gpus_rented": m.get("gpus_rented") if isinstance(m.get("gpus_rented"), int) else None}
            row = s.get(LiumModelStatsDaily, (read_day, str(m["gpu_model"])[:128]))
            if row is None:
                s.add(LiumModelStatsDaily(day=read_day, gpu_model=str(m["gpu_model"])[:128], raw_snapshot_id=raw_id,
                                          observed_at=fetched_at, visibility=vis, **vals))
            elif fetched_at >= row.observed_at:          # the newest read of the day wins
                for k, v in vals.items():
                    setattr(row, k, v)
                row.raw_snapshot_id, row.observed_at = raw_id, fetched_at
            models += 1
    if revised:
        log.info("lium stats: Lium revised %d closed day(s) of throughput", revised)
    return {"raw_snapshot_id": raw_id, "days": len(days), "written": written, "revised": revised, "models": models}


def parse_stats() -> dict | None:
    snaps = _stats_snapshots(newest_only=True)
    if not snaps or not isinstance(snaps[0][2], dict):
        return None
    return _store_stats(*snaps[0])


def reparse_stats() -> dict:
    n = 0
    for raw_id, fetched_at, doc in _stats_snapshots(newest_only=False):
        if isinstance(doc, dict):
            _store_stats(raw_id, fetched_at, doc)
            n += 1
    return {"snapshots": n}
