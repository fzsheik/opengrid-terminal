"""HTML pages, SEO, methodology docs. The page router owns "/" and every page path.

Every page path serves the same shell (web/index.html) with, filled in server-side:
    - <title>, meta description, canonical URL, Open Graph tags (per path)
    - a boot JSON block (canonical GPU slugs, provider metadata, methodology names) so the
      client can resolve /gpu/<slug> without waiting on an API
    - an SEO summary block (#ssr) rendered from real data where a renderer is registered in
      SEO; the client replaces it when it mounts (methodology pages reuse it as the content)

SEO registry (wave-2 agents add theirs):
    SEO["/provider/{name}"] = fn(params: dict) -> (title, description, html_summary) | None
`html_summary` must be built with `esc()` around every dynamic value. Return None to fall back
to the generic title. Renderers must never invent numbers: real data or no number at all.
"""

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response

from accounts.auth import principal
from api import markdown
from api.common import envelope, gpu_slug, gpu_slugs
from cache import ttl_cache
from config import settings

log = logging.getLogger(__name__)
router = APIRouter()
ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
METHODOLOGY = ROOT / "methodology"
esc = markdown.esc

# (pattern, nav title, description, indexable). Patterns use FastAPI {param} syntax; the client
# registers the same paths with ":param" in web/pages/*.js.
PAGES: list[tuple[str, str, str, bool]] = [
    ("/", "GPU compute market", "Live GPU cloud prices, availability, indices and market events across providers.", True),
    ("/indices", "GPU price indices", "Transparent GPU compute price indices with published methodology and coverage.", True),
    ("/indices/{id}", "Index", "A GPU compute price index: level, history and constituents.", True),
    ("/gpus", "GPU markets", "Every GPU market: lowest on-demand price per GPU-hour, change and provider count.", True),
    ("/gpu/{slug}", "GPU", "Cloud price for one GPU across providers.", True),
    ("/providers", "GPU cloud providers", "GPU cloud providers tracked by OpenGrid: coverage, source and pricing.", True),
    ("/provider/{name}", "Provider", "One GPU cloud provider: listings, prices and coverage.", True),
    ("/compare", "Compare GPUs", "Compare two GPUs side by side: price, availability and history.", True),
    ("/compare/{pair}", "Compare", "Compare two GPUs side by side: price, availability and history.", True),
    ("/explorer", "Listings explorer", "Every current GPU cloud listing: filter, sort and export.", True),
    ("/opportunities", "Opportunities", "Unusually cheap or newly available GPU capacity.", True),
    ("/events", "Market events", "GPU market events: price moves, new lows and highs, sold-outs, outages.", True),
    ("/news", "GPU market news", "News about GPU compute, linked to the GPUs and providers it mentions.", True),
    ("/heatmaps", "Heatmaps", "GPU price heatmaps by provider and region.", True),
    ("/route", "Route", "Best-execution routing for GPU workloads.", False),
    ("/deployments", "Deployments", "Your deployments.", False),
    ("/deployments/{id}", "Deployment", "One deployment: state, costs, events and evidence.", False),
    ("/onboarding", "Onboarding", "Design-partner onboarding: key, credentials, first route.", False),
    ("/admin/execution", "Execution control", "Execution mode, kill switches, providers, orphans, reconciliation.", False),
    ("/admin/checklist", "First real route checklist", "The gate before supervised execution.", False),
    ("/admin/partners", "Design partners", "Design partners, their deployments and feedback.", False),
    ("/admin/value", "Value", "Economic value, routing quality, reliability and funnel.", False),
    ("/watchlists", "Watchlists", "Your watchlists and alerts.", False),
    ("/keys", "API keys", "Your OpenGrid API keys.", False),
    ("/api", "API", "The OpenGrid API: market data, routing and deployments with one key.", True),
    ("/methodology", "Methodology", "How OpenGrid computes every number it publishes.", True),
    ("/methodology/{name}", "Methodology", "OpenGrid methodology.", True),
    ("/ops", "Ops", "Feed health and data coverage.", False),
]

SEO: dict[str, Callable[[dict], tuple[str, str, str] | None]] = {}

_PATTERN_RES = [(p, re.compile("^" + re.sub(r"\{[^}]+\}", "[^/]+", p) + "/?$")) for p, *_ in PAGES]


def is_page_path(path: str) -> bool:
    return any(rx.match(path) for _, rx in _PATTERN_RES)


# /v1 areas readable without a key when PUBLIC_PAGES is on (GET/HEAD only).
PUBLIC_V1 = ("gpus", "markets", "indices", "history", "spreads", "events", "news", "providers", "heatmaps",
             "overview", "tape", "methodology", "timeline", "best", "capabilities", "opportunities", "trust",
             "families", "scopes")
# Never public, whatever else matches: money, private data, operator functions.
PRIVATE_V1 = ("route", "deployments", "keys", "admin", "ops", "me", "usage", "billing", "watchlists", "alerts", "credentials")
# The first UI's JSON reads, which the interim pages still call. Read-only current market data.
PUBLIC_LEGACY = ("/market", "/market/detail", "/listings", "/polling", "/changes")


def is_public(request: Request) -> bool:
    """With PUBLIC_PAGES on: which requests skip the site password. GETs of pages and data only."""
    if request.method == "POST" and request.url.path == "/v1/events/track":
        return True  # anonymous product analytics: rate-limited, no PII, writes only product_events
    if request.method not in ("GET", "HEAD"):
        return False
    path = request.url.path
    if path.startswith("/v1/"):
        area = path[4:].split("/", 1)[0]
        if area in PRIVATE_V1:
            return False
        return area in PUBLIC_V1
    if path in ("/robots.txt", "/sitemap.xml") or path in PUBLIC_LEGACY:
        return True
    if path.startswith("/static/"):
        return True
    return is_page_path(path)


# ---------------------------------------------------------------- shell

_template_cache: dict = {}


def _template() -> str:
    f = WEB / "index.html"
    mtime = f.stat().st_mtime
    if _template_cache.get("mtime") != mtime:
        _template_cache.update(mtime=mtime, text=f.read_text(encoding="utf-8"))
    return _template_cache["text"]


def _boot() -> dict:
    import provider_meta
    from providers import PROVIDERS

    names = sorted(set(PROVIDERS) | set(provider_meta.PROVIDER_META))
    return {
        "base_url": settings.public_base_url.rstrip("/"),
        "gpus": sorted([s, n] for s, n in gpu_slugs().items()),
        "providers": [provider_meta.as_dict(n) for n in names],
        "methodology": methodology_names(),
    }


def shell(path: str, title: str, description: str, *, ssr: str = "", indexable: bool = True, status: int = 200) -> HTMLResponse:
    base = settings.public_base_url.rstrip("/")
    canonical = base + (path if path == "/" else path.rstrip("/"))
    full_title = title if title.endswith("OpenGrid") else f"{title} | OpenGrid"
    head = "\n".join([
        f"<title>{esc(full_title)}</title>",
        f'<meta name="description" content="{esc(description)}">',
        f'<link rel="canonical" href="{esc(canonical)}">',
        '<meta name="robots" content="noindex">' if not indexable or status != 200 else "",
        '<meta property="og:site_name" content="OpenGrid">',
        '<meta property="og:type" content="website">',
        f'<meta property="og:title" content="{esc(full_title)}">',
        f'<meta property="og:description" content="{esc(description)}">',
        f'<meta property="og:url" content="{esc(canonical)}">',
        '<meta name="twitter:card" content="summary">',
    ])
    boot = json.dumps(_boot(), separators=(",", ":")).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    page = re.sub(r"<!--OG:HEAD-->.*?<!--/OG:HEAD-->", lambda _: head, _template(), count=1, flags=re.S)
    page = page.replace('<div id="ssr" data-path="">', f'<div id="ssr" class="ssr" data-path="{esc(path)}">', 1)
    page = page.replace("<!--OG:SSR-->", ssr, 1).replace("<!--OG:BOOT-->", boot, 1)
    return HTMLResponse(page, status_code=status)


def _render(pattern: str, path: str, params: dict) -> HTMLResponse:
    _, nav_title, desc, indexable = next(p for p in PAGES if p[0] == pattern)
    fn = SEO.get(pattern)
    out = None
    if fn:
        try:
            out = fn(params)
        except HTTPException:
            raise
        except Exception:  # SEO must never take a page down
            log.exception("SEO renderer failed for %s", path)
    if out:
        title, desc2, html_summary = out
        return shell(path, title, desc2, ssr=html_summary, indexable=indexable)
    return shell(path, nav_title, desc, ssr=f"<h1>{esc(nav_title)}</h1><p>{esc(desc)}</p>", indexable=indexable)


def not_found(path: str, what: str) -> HTMLResponse:
    return shell(path, "Not found", what, ssr=f"<h1>Not found</h1><p>{esc(what)}</p>", status=404)


# ---------------------------------------------------------------- SEO renderers

@ttl_cache(120)
def market_snapshot(gpu: str) -> dict:
    """Each provider's cheapest live on-demand price for one GPU now, by the market's own rules.

    Reads current listings only (compute_listings), with market.py's eligibility and staleness
    rules, so it is cheap enough for crawlers. Same numbers as /market/detail's "now" column.
    """
    import market
    import normalize

    now = datetime.now(timezone.utc)
    with normalize.SessionLocal() as s:
        rows = [dict(r._mapping) for r in s.execute(market._LISTINGS, {"gpu": gpu})]
    best: dict[str, dict] = {}
    for r in rows:
        if r["price"] is None or r["price"] <= 0 or r["available"] is False:
            continue
        if now - r["last_seen"] > market.stale_after(r["provider"]):
            continue
        cur = best.get(r["provider"])
        if cur is None or r["price"] < cur["price"]:
            best[r["provider"]] = r
    providers = sorted(({"provider": p, "price": float(r["price"]), "gpu_count": r["gpu_count"], "region": r["region"],
                         "available": r["available"]} for p, r in best.items()), key=lambda x: (x["price"], x["provider"]))
    return {"gpu": gpu, "as_of": now, "providers": providers}


def _price(v: float) -> str:
    return f"${v:.2f}" if v >= 0.1 else f"${v:.3f}"


def _short(name: str) -> str:
    return re.sub(r"^(NVIDIA|AMD Instinct|AMD|Intel)\s+", "", name)


def _median(xs: list[float]) -> float:
    xs = sorted(xs)
    k = len(xs) // 2
    return xs[k] if len(xs) % 2 else (xs[k - 1] + xs[k]) / 2


def _jsonld(obj: dict) -> str:
    raw = json.dumps(obj, separators=(",", ":")).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return f'<script type="application/ld+json">{raw}</script>'


def _gpu_index(slug: str) -> dict | None:
    """The GPU's published OpenGrid index level, or None (never a stand-in number)."""
    try:
        from analytics import indices
        lv = indices.index_level(slug)
    except Exception:
        return None
    return lv if lv and lv.get("published") and lv.get("level") is not None else None


def _hw_rows(name: str) -> str:
    try:
        import hardware
        hw = hardware.spec(name)
    except Exception:
        hw = None
    if not hw:
        return ""
    tf = lambda v: None if v is None else f"{v:,.0f} TFLOPS" if v >= 100 else f"{v:,.1f} TFLOPS"  # noqa: E731
    items = [("Architecture", hw.get("architecture")),
             ("VRAM", f"{hw['vram_gb']} GB {hw.get('memory_type') or ''}".strip() if hw.get("vram_gb") else None),
             ("Memory bandwidth", f"{hw['memory_bandwidth_tbps']} TB/s" if hw.get("memory_bandwidth_tbps") else None),
             ("BF16 (dense)", tf(hw.get("bf16_tflops_dense"))), ("FP8 (dense)", tf(hw.get("fp8_tflops_dense"))),
             ("Form factor", hw.get("form_factor")), ("GPU-GPU link", hw.get("nvlink")),
             ("TDP", f"{hw['tdp_w']} W" if hw.get("tdp_w") else None)]
    rows = "".join(f"<tr><th>{esc(k)}</th><td>{esc(str(v))}</td></tr>" for k, v in items if v)
    return (f"<h2>{esc(_short(name))} hardware</h2><table><tbody>{rows}</tbody></table>"
            "<p>Vendor peak figures, dense (no sparsity), theoretical; not benchmarks.</p>")


def _family(slug: str) -> dict | None:
    """The GPU family this slug names (families.py), or None: unknown, or no families module on this server."""
    try:
        import families
    except ImportError:
        return None
    try:
        d = families.family(slug)
    except Exception:  # a broken families module must not take GPU pages down
        log.exception("families.family(%r) failed", slug)
        return None
    return d if d and d.get("variants") else None


def seo_family(slug: str, fam: dict):
    """A family page: each variant side by side with its own price. Never a merged family price or offer."""
    import provider_meta

    label = lambda p: provider_meta.meta(p).display_name  # noqa: E731
    fid = fam.get("id") or slug
    rows = []
    as_of = None
    for gpu in fam["variants"]:
        snap = market_snapshot(gpu)
        as_of = as_of or snap["as_of"]
        ps = snap["providers"]
        rows.append({"gpu": gpu, "slug": gpu_slug(gpu), "n": len(ps),
                     "low": ps[0] if ps else None, "high": ps[-1] if ps else None,
                     "median": _median([p["price"] for p in ps]) if ps else None})
    when = (as_of or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M UTC")
    priced = sorted((r for r in rows if r["low"]), key=lambda r: r["low"]["price"])
    kind = "architecture" if fam.get("kind") == "architecture" else "family"
    note = ("Variants are different products (memory, form factor, interconnect); OpenGrid never merges their "
            "prices, so there is no single family price.")
    if priced:
        title = (f"{fid} price: variants from {_price(priced[0]['low']['price'])}/hr — "
                 + ", ".join(f"{_short(r['gpu'])} {_price(r['low']['price'])}" for r in priced[:4]))
        desc = (f"{fid} GPU cloud prices by variant, as of {when}: "
                + "; ".join(f"{_short(r['gpu'])} from {_price(r['low']['price'])}/GPU-hour at "
                            f"{label(r['low']['provider'])} ({r['n']} provider{'s' if r['n'] != 1 else ''})" for r in priced[:6])
                + ". Observed on-demand list prices, per variant.")
    else:
        title = f"{fid} cloud price by variant"
        desc = (f"{fid} {kind}: {len(rows)} variant{'s' if len(rows) != 1 else ''}; none is sold in stock on demand "
                f"right now (as of {when}). {note}")
    trs = "".join(
        f'<tr><td><a href="/gpu/{esc(r["slug"])}">{esc(r["gpu"])}</a></td>'
        + (f"<td>{esc(_price(r['low']['price']))}</td><td>{esc(label(r['low']['provider']))}</td>"
           f"<td>{esc(_price(r['median']))}</td><td>{esc(_price(r['high']['price']))}</td><td>{r['n']}</td>"
           if r["low"] else '<td colspan="5">not sold in stock on demand right now</td>')
        + "</tr>" for r in sorted(rows, key=lambda r: (r["low"] is None, r["low"]["price"] if r["low"] else 0, r["gpu"])))
    # JSON-LD: one Product + AggregateOffer PER VARIANT. Never a merged family offer.
    graph = [{"@type": "Product", "name": f"{r['gpu']} cloud GPU (per GPU-hour)", "category": "Cloud GPU compute",
              "brand": {"@type": "Brand", "name": r["gpu"].split()[0]}, "url": settings.public_base_url.rstrip("/") + f"/gpu/{r['slug']}",
              "offers": {"@type": "AggregateOffer", "priceCurrency": "USD", "lowPrice": round(r["low"]["price"], 4),
                         "highPrice": round(r["high"]["price"], 4), "offerCount": r["n"]}} for r in priced]
    ld = _jsonld({"@context": "https://schema.org", "@graph": graph}) if graph else ""
    body = (f"<h1>{esc(fid)} cloud price by variant</h1><p>{esc(note)} As of {esc(when)}; observed on-demand list prices, "
            f"in stock or stock unknown, one price per provider (its lowest).</p>"
            "<table><thead><tr><th>Variant</th><th>From $/GPU-hour</th><th>at</th><th>Median</th><th>High</th><th>Providers</th>"
            f"</tr></thead><tbody>{trs}</tbody></table>"
            '<p><a href="/methodology/families">How GPU families are defined</a> · '
            '<a href="/methodology/data-kinds">How these prices are observed</a></p>' + ld)
    return title, desc[:300], body


def seo_gpu(params: dict):
    slug = params["slug"]
    name = gpu_slugs().get(slug)
    if name is None:
        fam = _family(slug)
        if fam is None:
            raise HTTPException(404)
        return seo_family(slug, fam)
    import provider_meta

    snap = market_snapshot(name)
    when = snap["as_of"].strftime("%Y-%m-%d %H:%M UTC")
    ps = snap["providers"]
    short = _short(name)
    if not ps:
        title = f"{name} cloud price"
        desc = f"{name}: no provider is selling it in stock on demand right now (as of {when}). Live GPU cloud prices on OpenGrid."
        body = (f"<h1>{esc(name)} cloud price</h1><p>No provider is selling the {esc(name)} in stock on demand right now "
                f"(as of {esc(when)}).</p>" + _hw_rows(name))
        return title, desc, body
    low, high = ps[0], ps[-1]
    mid = _median([p["price"] for p in ps])
    n = len(ps)
    label = lambda p: provider_meta.meta(p).display_name  # noqa: E731
    plural = "s" if n != 1 else ""
    title = f"{name} cloud price: from {_price(low['price'])}/hr across {n} provider{plural}"
    desc = (f"Live on-demand {name} prices: from {_price(low['price'])} per GPU-hour ({label(low['provider'])}) across {n} "
            f"provider{plural}, median {_price(mid)}, as of {when}. Observed list prices.")
    rows = "".join(
        f"<tr><td>{esc(label(p['provider']))}</td><td>{esc(_price(p['price']))}</td>"
        f"<td>{esc(str(p['gpu_count'] or ''))}</td><td>{esc(p['region'] or '')}</td></tr>" for p in ps)
    idx = _gpu_index(slug)
    idx_p = (f"<p>OpenGrid {esc(short)} index: <strong>{esc(_price(idx['level']))}</strong> per GPU-hour "
             f"({esc(str(idx.get('constituents') or ''))} providers, hour {esc(str(idx.get('hour') or '')[:16].replace('T', ' '))} UTC). "
             f'<a href="/indices/{esc(slug)}">Index methodology and history</a>.</p>') if idx else ""
    spread = (f", a spread of {(high['price'] / low['price'] - 1) * 100:.0f}% between the cheapest and the most expensive provider"
              if n > 1 and low["price"] > 0 else "")
    where = f" ({low['region']})" if low.get("region") else ""
    faq = [
        (f"What is the {short} price per hour?",
         f"As of {when}, on-demand {short} cloud prices observed by OpenGrid range from {_price(low['price'])} to "
         f"{_price(high['price'])} per GPU-hour across {n} provider{plural}, with a median of {_price(mid)}{spread}."),
        (f"What is the cheapest {short} cloud?",
         f"The cheapest in-stock (or stock-unknown) on-demand {short} listing is {_price(low['price'])} per GPU-hour at "
         f"{label(low['provider'])}{where}, as of {when}."),
        (f"Where can I rent {short} GPUs?",
         f"{n} provider{plural} list the {short} on demand right now: "
         + ", ".join(f"{label(p['provider'])} ({_price(p['price'])})" for p in ps[:12]) + "."),
        (f"How is {short} cloud pricing measured?",
         "These are observed list prices per GPU-hour, read from each provider's public pricing data, on demand and not "
         "interruptible, one price per provider (its lowest). They are not quotes or execution prices."),
    ]
    faq_html = "".join(f"<h3>{esc(q)}</h3><p>{esc(a)}</p>" for q, a in faq)
    ld = _jsonld({"@context": "https://schema.org", "@graph": [
        {"@type": "Product", "name": f"{name} cloud GPU (per GPU-hour)", "category": "Cloud GPU compute",
         "brand": {"@type": "Brand", "name": name.split()[0]},
         "offers": {"@type": "AggregateOffer", "priceCurrency": "USD", "lowPrice": round(low["price"], 4),
                    "highPrice": round(high["price"], 4), "offerCount": n}},
        {"@type": "FAQPage", "mainEntity": [{"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}}
                                            for q, a in faq]},
    ]})
    body = (f"<h1>{esc(name)} cloud price</h1>"
            f"<p>From <strong>{esc(_price(low['price']))}</strong> per GPU-hour on demand across {n} provider{plural} "
            f"(median {esc(_price(mid))}, high {esc(_price(high['price']))}), as of {esc(when)}. Observed list prices, "
            f"in stock or stock unknown.</p>" + idx_p +
            f"<table><thead><tr><th>Provider</th><th>$/GPU-hour</th><th>GPUs</th><th>Region</th></tr></thead><tbody>{rows}</tbody></table>"
            + _hw_rows(name) + f"<h2>{esc(short)} price FAQ</h2>" + faq_html +
            f'<p><a href="/compare?a={esc(slug)}">Compare the {esc(short)}</a> · <a href="/route?gpu={esc(slug)}">Route a workload</a> · '
            f'<a href="/methodology/data-kinds">How these prices are observed</a></p>' + ld)
    return title, desc, body


def seo_provider(params: dict):
    import provider_meta
    from providers import PROVIDERS

    name = params["name"]
    if name not in PROVIDERS and name not in provider_meta.PROVIDER_META:
        raise HTTPException(404)
    m = provider_meta.meta(name)
    dn = m.display_name
    base_desc = (f"{dn} GPU cloud listings and prices as observed by OpenGrid "
                 f"({m.provider_class.replace('_', ' ')}, read from {m.source or 'its public data'}).")
    try:
        rows, n_listings, as_of, window = _provider_seo_data(name)
    except Exception:  # no database (tests, outage): metadata only, never invented numbers
        log.exception("provider SEO data unavailable for %s", name)
        return f"{dn} GPU cloud prices", base_desc, f"<h1>{esc(dn)} GPU cloud prices</h1><p>{esc(base_desc)}</p>"
    others = sorted(p for p in set(PROVIDERS) | set(provider_meta.PROVIDER_META) if p != name)[:6]
    vs = " · ".join(f'<a href="/compare?a={esc(name)}&amp;b={esc(o)}">{esc(dn)} vs {esc(provider_meta.meta(o).display_name)}</a>'
                    for o in others)
    when = as_of.strftime("%Y-%m-%d %H:%M UTC")
    if not rows:
        title = f"{dn} GPU cloud prices"
        desc = f"{dn}: no live on-demand GPU listings observed by OpenGrid right now (as of {when}). {base_desc}"
        body = (f"<h1>{esc(dn)} GPU cloud prices</h1><p>{esc(desc)}</p>"
                f'<p><a href="/providers">GPU cloud comparison: every provider</a> · {vs}</p>')
        return title, desc, body
    short = lambda g: re.sub(r"^(NVIDIA|AMD Instinct|AMD|Intel)\s+", "", g)  # noqa: E731
    priced = [r for r in rows if r["price"] is not None]
    head = max(priced, key=lambda r: (r["providers"], -r["price"])) if priced else None
    cheapest = sorted(priced, key=lambda r: r["price"])[:4]
    prem = sorted(r["premium"] for r in rows if r["premium"] is not None)
    med_prem = (prem[len(prem) // 2] if len(prem) % 2 else (prem[len(prem) // 2 - 1] + prem[len(prem) // 2]) / 2) if prem else None
    n = len(rows)
    title = (f"{dn} {short(head['gpu'])} price: {_price(head['price'])}/hr · {n} GPU{'s' if n != 1 else ''} compared"
             if head else f"{dn} GPU cloud prices: {n} GPU{'s' if n != 1 else ''}")
    prem_s = ""
    if med_prem is not None:
        prem_s = (f" Typically {abs(med_prem) * 100:.0f}% {'above' if med_prem > 0 else 'below'} the median of other providers "
                  f"right now (median over {len(prem)} GPU{'s' if len(prem) != 1 else ''} sold elsewhere too).")
    if window is not None:
        prem_s += (f" Over the last 30 days it averaged {abs(window) * 100:.1f}% "
                   f"{'above' if window > 0 else 'below'} other providers in the same hour.")
    desc = (f"{dn} on-demand GPU prices: " + ", ".join(f"{short(r['gpu'])} {_price(r['price'])}" for r in cheapest)
            + f" per GPU-hour; {n} GPU model{'s' if n != 1 else ''}, {n_listings} live listings, as of {when}.{prem_s}")
    def tr(r):
        price = _price(r["price"]) if r["price"] is not None else "not priced"
        median = _price(r["median"]) if r["median"] is not None else ""
        vs_other = (f"{r['premium'] * 100:+.0f}%" if r["premium"] is not None
                    else "only provider" if r["providers"] <= 1 else "")
        rank = f"{r['rank']} of {r['providers']}" if r["rank"] else ""
        return (f'<tr><td><a href="/gpu/{esc(gpu_slug(r["gpu"]))}">{esc(r["gpu"])}</a></td><td>{esc(price)}</td>'
                f"<td>{esc(median)}</td><td>{esc(vs_other)}</td><td>{esc(rank)}</td></tr>")

    trs = "".join(tr(r) for r in sorted(rows, key=lambda r: (r["price"] is None, r["price"] or 0)))
    body = (f"<h1>{esc(dn)} GPU cloud prices</h1>"
            f"<p>{esc(dn)} ({esc(m.provider_class.replace('_', ' '))}) lists {n} GPU model{'s' if n != 1 else ''} across "
            f"{n_listings} live listings, as of {esc(when)}.{esc(prem_s)} Observed list prices, read from {esc(m.source or 'its public data')}.</p>"
            "<table><thead><tr><th>GPU</th><th>Lowest $/GPU-hour</th><th>Market median</th><th>vs other providers</th>"
            f"<th>Rank</th></tr></thead><tbody>{trs}</tbody></table>"
            f'<p><a href="/providers">GPU cloud comparison: every provider</a> · {vs}</p>'
            '<p><a href="/methodology/provider-value">How premium and rank are computed</a></p>')
    return title, desc, body


@ttl_cache(120)
def _provider_seo_data(name: str):
    """Per GPU this provider lists now: its lowest price, the market median, premium vs others, rank.

    Same numbers as /v1/providers/{p} (dispersion.all_markets: market.py's eligibility and
    staleness rules). The 30-day average premium only when the rollup has enough hours."""
    from analytics import dispersion, providers as pv

    mine = [r for r in dispersion.current_listings() if r["provider"] == name]
    markets = dispersion.all_markets()
    rows = []
    for g in sorted({r["gpu"] for r in mine}):
        mk = markets.get(g, {})
        me = next((x for x in mk.get("by_provider", []) if x["provider"] == name), None)
        rows.append({"gpu": g, "price": me and me["price"], "median": mk.get("median"), "providers": mk.get("providers", 0),
                     "premium": me and me["premium_vs_others_median"], "rank": me and me["rank"]})
    window = ((pv.provider_value_all(30).get(name) or {}).get(pv.ALL) or {}).get("premium_avg")
    as_of = max((r["last_seen"] for r in mine if r.get("last_seen")), default=None) or datetime.now(timezone.utc)
    return rows, len(mine), as_of, window


def _compare_provider(value: str) -> str | None:
    import provider_meta
    from providers import PROVIDERS

    v = value.lower()
    for p in sorted(set(PROVIDERS) | set(provider_meta.PROVIDER_META)):
        if v in (p.lower(), gpu_slug(provider_meta.meta(p).display_name)):
            return p
    return None


_COMPARE_REGIONS = ("US", "Canada", "Europe", "UK", "APAC", "Middle East", "LATAM", "Africa")


def _compare_side(name: str, snap: dict) -> dict:
    try:
        import hardware
        hw = hardware.spec(name) or {}
    except Exception:
        hw = {}
    ps = snap["providers"]
    med = _median([p["price"] for p in ps]) if ps else None
    tf = hw.get("bf16_tflops_dense")
    return {"low": ps[0]["price"] if ps else None, "median": med, "n": len(ps), "vram": hw.get("vram_gb"), "bf16": tf,
            "per_tf": med / tf if med is not None and tf else None}


def seo_compare(params: dict):
    m = re.match(r"^(.+?)-vs-(.+)$", params["pair"])
    if not m:
        raise HTTPException(404)
    slugs = gpu_slugs()
    x, y = m.group(1), m.group(2)
    if x in slugs and y in slugs:
        a, b = slugs[x], slugs[y]
        title = f"{a} vs {b}: cloud price comparison"
        sa = market_snapshot(a)
        when = sa["as_of"].strftime("%Y-%m-%d %H:%M UTC")
        A, B = _compare_side(a, sa), _compare_side(b, market_snapshot(b))
        f = lambda v, fn: "n/a" if v is None else fn(v)  # noqa: E731
        rows = [("Lowest $/GPU-hour", f(A["low"], _price), f(B["low"], _price)),
                ("Median $/GPU-hour", f(A["median"], _price), f(B["median"], _price)),
                ("Providers pricing it", str(A["n"]), str(B["n"])),
                ("VRAM", f(A["vram"], lambda v: f"{v} GB"), f(B["vram"], lambda v: f"{v} GB")),
                ("BF16 TFLOPS (dense, vendor peak)", f(A["bf16"], lambda v: f"{v:,.0f}"), f(B["bf16"], lambda v: f"{v:,.0f}")),
                ("$ per BF16 TFLOP-hour at median (theoretical)", f(A["per_tf"], lambda v: f"${v:.5f}"), f(B["per_tf"], lambda v: f"${v:.5f}"))]
        table = (f"<table><thead><tr><th></th><th>{esc(_short(a))}</th><th>{esc(_short(b))}</th></tr></thead><tbody>"
                 + "".join(f"<tr><th>{esc(k)}</th><td>{esc(u)}</td><td>{esc(v)}</td></tr>" for k, u, v in rows) + "</tbody></table>")
        lines = []
        if A["low"] is not None and B["low"] is not None and A["low"] != B["low"]:
            c = a if A["low"] < B["low"] else b
            lines.append(f"Cheapest listing now: {_short(c)} ({_price(min(A['low'], B['low']))} vs "
                         f"{_price(max(A['low'], B['low']))} per GPU-hour).")
        if A["per_tf"] and B["per_tf"] and A["per_tf"] != B["per_tf"]:
            c = a if A["per_tf"] < B["per_tf"] else b
            lines.append(f"Cheaper per dense BF16 TFLOP at the median price: {_short(c)} (theoretical, from vendor peak specs).")
        desc = (f"{a} vs {b} cloud prices as of {when}: " + " ".join(lines)) if lines else \
            f"Compare {a} and {b} GPU cloud prices, availability and history across providers."
        body = (f"<h1>{esc(title)}</h1><p>As of {esc(when)}. Different GPUs are different products: figures are side by side, "
                f"not equivalences.</p>" + table + "".join(f"<p>{esc(t)}</p>" for t in lines) +
                f'<p><a href="/gpu/{esc(x)}">{esc(_short(a))} market</a> · <a href="/gpu/{esc(y)}">{esc(_short(b))} market</a></p>')
        return title, desc[:300], body
    pa, pb = _compare_provider(x), _compare_provider(y)
    if pa and pb:
        import provider_meta

        na, nb = provider_meta.meta(pa).display_name, provider_meta.meta(pb).display_name
        title = f"{na} vs {nb}: GPU cloud price comparison"
        common = []
        try:
            from analytics import dispersion
            for g, mk in sorted(dispersion.all_markets().items()):
                by = {r["provider"]: r["price"] for r in mk["by_provider"]}
                if pa in by and pb in by:
                    common.append((g, by[pa], by[pb]))
        except Exception:
            common = []
        rows = "".join(f"<tr><td>{esc(_short(g))}</td><td>{esc(_price(u))}</td><td>{esc(_price(v))}</td></tr>" for g, u, v in common)
        wins_a, wins_b = sum(1 for _, u, v in common if u < v), sum(1 for _, u, v in common if v < u)
        desc = (f"{na} vs {nb}: {len(common)} GPUs priced by both now; {na} is cheaper on {wins_a}, {nb} on {wins_b}. "
                f"Observed list prices." if common else f"Compare {na} and {nb} GPU cloud coverage, prices and feed health.")
        body = f"<h1>{esc(title)}</h1><p>{esc(desc)}</p>" + (
            f"<table><thead><tr><th>GPU</th><th>{esc(na)} $/GPU-hour</th><th>{esc(nb)} $/GPU-hour</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>" if common else "")
        return title, desc, body
    ra = next((g for g in _COMPARE_REGIONS if gpu_slug(g) == x), None)
    rb = next((g for g in _COMPARE_REGIONS if gpu_slug(g) == y), None)
    if ra and rb:
        title = f"GPU cloud prices: {ra} vs {rb}"
        desc = f"The cheapest on-demand price of each GPU in {ra} and in {rb}, from each listing's stated location."
        return title, desc, f"<h1>{esc(title)}</h1><p>{esc(desc)}</p>"
    raise HTTPException(404)


def seo_methodology_index(params: dict):
    readme = METHODOLOGY / "README.md"
    intro = markdown.render(readme.read_text(encoding="utf-8")) if readme.exists() else "<h1>Methodology</h1>"
    items = "".join(f'<li><a href="/methodology/{esc(n)}">{esc(t)}</a> <span class="dim">{esc(n)}</span></li>'
                    for n, t in methodology_titles())
    return "Methodology", "How OpenGrid computes every number it publishes.", intro + f'<ul class="md-list">{items}</ul>'


def seo_methodology_doc(params: dict):
    name = params["name"]
    src = methodology_source(name)
    t = markdown.title(src, name)
    first_p = re.search(r"<p>(.*?)</p>", markdown.render(src), re.S)
    desc = re.sub(r"<[^>]+>", "", first_p.group(1))[:300] if first_p else f"OpenGrid methodology: {t}"
    body = markdown.render(src) + '<p class="note"><a href="/methodology">All methodology</a> · <a href="/v1/methodology/' + esc(name) + '">raw markdown</a></p>'
    return f"{t} — methodology", desc, body


SEO.update({
    "/gpu/{slug}": seo_gpu,
    "/provider/{name}": seo_provider,
    "/compare/{pair}": seo_compare,
    "/methodology": seo_methodology_index,
    "/methodology/{name}": seo_methodology_doc,
})


# ---------------------------------------------------------------- methodology docs

_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")


def methodology_names() -> list[str]:
    return sorted(p.stem for p in METHODOLOGY.glob("*.md") if p.stem.lower() != "readme" and _NAME.match(p.stem))


def methodology_titles() -> list[tuple[str, str]]:
    return [(n, markdown.title(methodology_source(n), n)) for n in methodology_names()]


def methodology_source(name: str) -> str:
    if not _NAME.match(name or "") or name not in methodology_names():
        raise HTTPException(404, f"no methodology document {name!r}")
    return (METHODOLOGY / f"{name}.md").read_text(encoding="utf-8")


@router.get("/v1/methodology", summary="Methodology documents: name, title, links")
def v1_methodology(who=Depends(principal)):
    summaries = _methodology_summaries()
    docs = [{"name": n, "title": t, "summary": summaries.get(n) or None, "html": f"/methodology/{n}", "markdown": f"/v1/methodology/{n}"}
            for n, t in methodology_titles()]
    return envelope(docs)


def _methodology_summaries() -> dict:
    """{name: one-line summary} from methodology_index (lazy; {} if that module is absent or fails)."""
    try:
        import methodology_index
        return methodology_index.methodology_summaries() or {}
    except Exception:
        log.exception("methodology summaries unavailable")
        return {}


@router.get("/v1/methodology/{name}", summary="One methodology document as raw markdown")
def v1_methodology_doc(name: str, who=Depends(principal)):
    return PlainTextResponse(methodology_source(name), media_type="text/markdown; charset=utf-8")


# ---------------------------------------------------------------- robots, sitemap

@router.get("/robots.txt", include_in_schema=False)
def robots():
    base = settings.public_base_url.rstrip("/")
    private = [p for p, *_, idx in PAGES if not idx]
    lines = ["User-agent: *", "Allow: /", "Disallow: /v1/", "Disallow: /raw", "Disallow: /docs", *[f"Disallow: {p}" for p in private],
             f"Sitemap: {base}/sitemap.xml"]
    return PlainTextResponse("\n".join(lines) + "\n")


def sitemap_paths() -> list[str]:
    import provider_meta
    from providers import PROVIDERS

    static = [p for p, *_, idx in PAGES if idx and "{" not in p]
    gpus = [f"/gpu/{s}" for s in sorted(gpu_slugs())]
    provs = [f"/provider/{n}" for n in sorted(set(PROVIDERS) | set(provider_meta.PROVIDER_META))]
    docs = [f"/methodology/{n}" for n in methodology_names()]
    try:
        import families
        fams = [f"/gpu/{d['slug']}" for d in families.all_families() if d.get("variants")]
    except Exception:  # no families module on this server: GPU pages only
        fams = []
    fams = [p for p in fams if p not in gpus]
    return static + gpus + fams + provs + docs


@router.get("/sitemap.xml", include_in_schema=False)
def sitemap():
    base = settings.public_base_url.rstrip("/")
    urls = "".join(f"<url><loc>{esc(base + p)}</loc></url>" for p in sitemap_paths())
    xml = f'<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>\n'
    return Response(xml, media_type="application/xml")


# ---------------------------------------------------------------- page routes (registered last)

def _page_route(pattern: str):
    def endpoint(request: Request):
        try:
            return _render(pattern, request.url.path, dict(request.path_params))
        except HTTPException as e:
            if e.status_code == 404:
                return not_found(request.url.path, "No page at this address.")
            raise

    endpoint.__name__ = "page_" + (re.sub(r"[^a-z0-9]+", "_", pattern).strip("_") or "index")
    return endpoint


for _pattern, *_ in PAGES:
    router.add_api_route(_pattern, _page_route(_pattern), methods=["GET", "HEAD"], include_in_schema=False,
                         response_class=HTMLResponse)

__all__ = ["router", "is_public", "SEO", "PAGES", "shell", "esc", "gpu_slug"]
