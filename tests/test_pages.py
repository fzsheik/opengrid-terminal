"""Page shell, SEO, sitemap, methodology rendering, and the public-surface gate.

Run:  .venv/Scripts/python tests/test_pages.py
No database needed: the GPU SEO renderer's data source is replaced with a fixture.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

os.environ["OPENGRID_NO_JOBS"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402
from starlette.requests import Request  # noqa: E402

import main  # noqa: E402
from api import markdown, pages  # noqa: E402
from api.common import gpu_slugs  # noqa: E402
from config import settings  # noqa: E402

client = TestClient(main.app, headers={"X-OpenGrid-Request": "1"})  # CSRF header, as web/core.js sends  # no `with`: lifespan (DB init, poller) does not run
BASE = settings.public_base_url.rstrip("/")

FAKE = {
    "NVIDIA H100 80GB SXM5": [
        {"provider": "lium", "price": 1.3, "gpu_count": 1, "region": "Secaucus", "available": True},
        {"provider": "aws", "price": 6.88, "gpu_count": 8, "region": "us-east-1<script>alert(1)</script>", "available": True},
    ],
}


def fake_snapshot(gpu):
    return {"gpu": gpu, "as_of": datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc), "providers": FAKE.get(gpu, [])}


pages.market_snapshot = fake_snapshot


def title_of(html):
    return re.search(r"<title>(.*?)</title>", html, re.S).group(1)


def test_every_page_path_serves_the_shell():
    samples = {"/indices/{id}": "/indices/gpu-h100", "/gpu/{slug}": "/gpu/h100-80gb-sxm5", "/provider/{name}": "/provider/lambda",
               "/compare/{pair}": "/compare/h100-80gb-sxm5-vs-h200-141gb-sxm5", "/methodology/{name}": "/methodology/data-kinds"}
    for pattern, nav_title, _, indexable in pages.PAGES:
        path = samples.get(pattern, pattern)
        r = client.get(path)
        assert r.status_code == 200, f"{path} -> {r.status_code}"
        html = r.text
        assert 'id="page"' in html and "/static/core.js" in html, f"{path}: shell"
        canon = re.search(r'<link rel="canonical" href="([^"]+)"', html).group(1)
        assert canon == BASE + path, f"{path}: canonical {canon}"
        assert f'og:url" content="{BASE + path}"' in html and 'og:title' in html
        assert ('name="robots" content="noindex"' in html) == (not indexable), f"{path}: robots meta"
        assert "<!--OG:" not in html, f"{path}: every placeholder filled"
        boot = json.loads(re.search(r'<script id="og-boot" type="application/json">(.*?)</script>', html, re.S).group(1))
        assert ["h100-80gb-sxm5", "NVIDIA H100 80GB SXM5"] in boot["gpus"] and any(p["name"] == "lambda" for p in boot["providers"])
        assert f'<div id="ssr" class="ssr" data-path="{path}">' in html, f"{path}: ssr block tagged with its path"
        if "{" not in pattern:
            assert nav_title in title_of(html), f"{path}: title {title_of(html)}"
    assert client.head("/gpus").status_code == 200, "HEAD works for crawlers"


def test_every_client_page_has_a_server_path():
    """Each OG.page() pattern in web/pages/*.js must be served by the server (else a reload 404s)."""
    web = Path(__file__).resolve().parent.parent / "web" / "pages"
    pats = set()
    for f in web.glob("*.js"):
        pats |= set(re.findall(r'OG\.page\("([^"]+)"', f.read_text(encoding="utf-8")))
    assert len(pats) >= 22, pats
    for p in pats:
        sample = re.sub(r":a-vs-:b", "h100-80gb-sxm5-vs-a100-80gb-sxm4", p)
        sample = re.sub(r":slug", "h100-80gb-sxm5", sample)
        sample = re.sub(r":name", "lambda" if "provider" in p else "data-kinds", sample)
        sample = re.sub(r":\w+", "x", sample)
        assert pages.is_page_path(sample), f"client page {p} ({sample}) has no server route"
        assert client.get(sample).status_code == 200, sample


def test_gpu_seo_uses_real_data_and_escapes():
    r = client.get("/gpu/h100-80gb-sxm5")
    html = r.text
    t = title_of(html)
    assert t == "NVIDIA H100 80GB SXM5 cloud price: from $1.30/hr across 2 providers | OpenGrid", t
    assert "median $4.09" in html, "median of the two fixture prices"
    assert "as of 2026-10-06 12:00 UTC" in html
    assert "<td>Lium</td><td>$1.30</td>" in html and "<td>AWS</td><td>$6.88</td>" in html, "provider price table"
    assert "<script>alert(1)" not in html and "us-east-1&lt;script&gt;" in html, "dynamic values are escaped"


def test_gpu_seo_without_sellers_states_it_and_invents_nothing():
    html = client.get("/gpu/a100-80gb-sxm4").text
    assert "no provider is selling it in stock" in title_of(html) + html
    assert not re.search(r"from \$\d", html), "no price claimed when nobody sells"


def test_unknown_pages_404_but_still_render():
    for path in ["/gpu/not-a-gpu", "/provider/nobody", "/compare/h100-80gb-sxm5-vs-nope", "/compare/garbage", "/methodology/nope", "/methodology/..%2Fconfig"]:
        if "%2F" in path:
            assert client.get(path).status_code == 404
            continue
        r = client.get(path)
        assert r.status_code == 404, f"{path} -> {r.status_code}"
        assert 'name="robots" content="noindex"' in r.text, f"{path}: not indexed"


def test_seo_failure_falls_back():
    def boom(params):
        raise RuntimeError("db down")
    saved = pages.SEO["/provider/{name}"]
    pages.SEO["/provider/{name}"] = boom
    try:
        r = client.get("/provider/lambda")
        assert r.status_code == 200 and "Provider | OpenGrid" in title_of(r.text), "a failing SEO renderer never takes the page down"
    finally:
        pages.SEO["/provider/{name}"] = saved


def test_sitemap_and_robots():
    xml = client.get("/sitemap.xml").text
    locs = re.findall(r"<loc>([^<]+)</loc>", xml)
    for slug in gpu_slugs():
        assert f"{BASE}/gpu/{slug}" in locs, slug
    assert f"{BASE}/provider/lambda" in locs and f"{BASE}/methodology/data-kinds" in locs and f"{BASE}/" in locs
    assert not any(p in loc for loc in locs for p in ("/keys", "/deployments", "/route", "/ops", "/watchlists", "{")), "private pages and patterns are not listed"
    assert len(locs) == len(set(locs)), "no duplicates"
    robots = client.get("/robots.txt").text
    assert "Disallow: /v1/" in robots and "Disallow: /keys" in robots and f"Sitemap: {BASE}/sitemap.xml" in robots


def test_family_page_lists_variants_never_a_merged_offer():
    import families

    fam = families.family("h100")
    assert fam and len(fam["variants"]) > 1, "fixture assumption: H100 is a family with several variants"
    FAKE.setdefault("NVIDIA H100 80GB PCIe", [{"provider": "vast", "price": 1.9, "gpu_count": 1, "region": None, "available": None}])
    try:
        r = client.get("/gpu/h100")
        assert r.status_code == 200, r.status_code
        t = title_of(r.text)
        assert t.startswith("H100 price: variants from $1.30/hr") and "H100 80GB SXM5 $1.30" in t and "H100 80GB PCIe $1.90" in t, t
        ld = [json.loads(x) for x in re.findall(r'<script type="application/ld\+json">(.*?)</script>', r.text, re.S)]
        offers = [n for d in ld for n in d.get("@graph", []) if n.get("@type") == "Product"]
        assert len(offers) == 2 and all("H100" in o["name"] and "family" not in o["name"].lower() for o in offers), offers
        assert {o["offers"]["lowPrice"] for o in offers} == {1.3, 1.9}, "one AggregateOffer per variant, never merged"
        assert 'href="/gpu/h100-80gb-sxm5"' in r.text
        assert client.get("/gpu/blackwell").status_code == 200, "architecture families too"
        assert client.get("/gpu/not-a-family-or-gpu").status_code == 404
        locs = re.findall(r"<loc>([^<]+)</loc>", client.get("/sitemap.xml").text)
        assert f"{BASE}/gpu/h100" in locs and len(locs) == len(set(locs)), "family pages in the sitemap"
    finally:
        FAKE.pop("NVIDIA H100 80GB PCIe", None)


def test_methodology_summaries():
    d = client.get("/v1/methodology").json()["data"]
    assert all("summary" in x for x in d)
    dk = next(x for x in d if x["name"] == "data-kinds")
    assert dk["summary"] and len(dk["summary"]) <= 200, dk


def test_methodology_api():
    d = client.get("/v1/methodology").json()
    names = [x["name"] for x in d["data"]]
    assert "data-kinds" in names and "README" not in names and "as_of" in d["meta"]
    r = client.get("/v1/methodology/data-kinds")
    assert r.status_code == 200 and r.text.startswith("# Data kinds") and r.headers["content-type"].startswith("text/markdown")
    assert client.get("/v1/methodology/..%2F..%2Fconfig").status_code == 404
    assert client.get("/v1/methodology/README").status_code == 404
    html = client.get("/methodology/data-kinds").text
    assert "<table>" in html and "<strong>observed</strong>" in html, "rendered server-side"


def test_markdown_renderer():
    r = markdown.render
    assert r("# Title *x*") == '<h1 id="title-x">Title <em>x</em></h1>'
    assert r("a **b** and _c_ and `d<e>`") == "<p>a <strong>b</strong> and <em>c</em> and <code>d&lt;e&gt;</code></p>"
    assert r("snake_case_name stays") == "<p>snake_case_name stays</p>", "underscores inside words are not italics"
    assert r("- a\n- b\n  - c\n- d") == "<ul><li>a</li><li>b<ul><li>c</li></ul></li><li>d</li></ul>"
    assert r("1. one\n2. two") == "<ol><li>one</li><li>two</li></ol>"
    t = r("| A | B |\n|---|--:|\n| 1 | **2** |")
    assert t == "<table><thead><tr><th>A</th><th style=text-align:right>B</th></tr></thead><tbody><tr><td>1</td><td style=text-align:right><strong>2</strong></td></tr></tbody></table>", t
    assert r("```py\nx = '<b>'\n```") == '<pre><code class="language-py">x = &#x27;&lt;b&gt;&#x27;</code></pre>'
    assert r("> quoted") == "<blockquote><p>quoted</p></blockquote>"
    assert r("---") == "<hr>"
    assert r("[doc](other-doc.md#sec)") == '<p><a href="/methodology/other-doc#sec">doc</a></p>', "relative doc links point at the page"
    assert r("[site](https://x.org/a?b=1&c=2)") == '<p><a href="https://x.org/a?b=1&amp;c=2" rel="noopener" target="_blank">site</a></p>'
    assert markdown.title("intro\n## The *Name*\n") == "The Name"


def test_markdown_is_xss_safe():
    r = markdown.render
    evil = [
        "<script>alert(1)</script>",
        "<img src=x onerror=alert(1)>",
        "[x](javascript:alert(1))",
        "[x](JaVaScRiPt:alert(1))",
        "[x](data:text/html,<script>alert(1)</script>)",
        "[x](//evil.example/a)",
        '[x](/a"onmouseover="alert(1))',
        "[x](vbscript:msgbox)",
        "# <svg onload=alert(1)>",
        "| <b>h</b> |\n|---|\n| <iframe> |",
        "`</code><script>x</script>`",
        "- <a href=javascript:1>x</a>",
        "**<script>**",
    ]
    for src in evil:
        out = r(src)
        assert "<script" not in out.lower() and "<img" not in out and "<iframe" not in out and "<svg" not in out and "<b>" not in out, f"{src!r} -> {out}"
        assert not re.search(r'<a [^>]*href="[^"]*(javascript|data:|vbscript)', out, re.I), f"{src!r} -> {out}"
        assert not re.search(r'href="(?!/|https?://|#|mailto:)', out), f"unsafe href: {src!r} -> {out}"
        assert not re.search(r"<a [^>]*\son\w+=", out), f"event handler attribute: {out}"
    assert r('[x](/a"onmouseover="alert(1))').count('"') % 2 == 0, "quotes cannot break out of the attribute"


def _req(method, path):
    return Request({"type": "http", "method": method, "path": path, "raw_path": path.encode(), "query_string": b"", "headers": []})


def test_is_public_matrix():
    allow = ["/", "/gpus", "/gpu/h100-80gb-sxm5", "/compare/a-vs-b", "/compare", "/methodology", "/methodology/data-kinds", "/explorer",
             "/static/core.js", "/static/logos/lium.png", "/robots.txt", "/sitemap.xml", "/keys", "/route", "/ops",  # page shells hold no data
             "/v1/gpus", "/v1/gpus/h100-80gb-sxm5", "/v1/markets/x", "/v1/indices", "/v1/history", "/v1/spreads", "/v1/events", "/v1/news",
             "/v1/providers/lambda", "/v1/heatmaps", "/v1/overview", "/v1/tape", "/v1/methodology", "/v1/methodology/data-kinds",
             "/v1/timeline", "/v1/best", "/v1/capabilities", "/v1/opportunities", "/v1/trust/listings",
             "/market", "/market/detail", "/listings", "/polling"]
    deny = ["/v1/route", "/v1/route/preview", "/v1/deployments", "/v1/deployments/dep_x", "/v1/keys", "/v1/admin/x", "/v1/ops", "/v1/ops/jobs",
            "/v1/me", "/v1/usage", "/v1/billing", "/v1/billing/invoices", "/v1/watchlists", "/v1/alerts", "/v1/credentials", "/v1/unknown",
            "/v1/gpusx", "/raw", "/raw/payload", "/mapping", "/unmapped", "/docs", "/openapi.json", "/classic", "/history", "/reference-prices", "/nope"]
    for p in allow:
        assert pages.is_public(_req("GET", p)), f"GET {p} should be public"
        assert pages.is_public(_req("HEAD", p)), f"HEAD {p} should be public"
    for p in deny:
        assert not pages.is_public(_req("GET", p)), f"GET {p} must not be public"
    for p in ["/", "/v1/gpus", "/v1/methodology", "/listings", "/fetch", "/normalize"]:
        for m in ("POST", "PUT", "DELETE", "PATCH"):
            assert not pages.is_public(_req(m, p)), f"{m} {p} must never be public"


def test_public_pages_through_the_middleware():
    settings.app_password, settings.public_pages = "s3cret", True
    try:
        assert client.get("/gpus").status_code == 200, "public page without a password"
        assert client.get("/static/core.js").status_code == 200
        assert client.get("/sitemap.xml").status_code == 200
        assert client.get("/raw").status_code == 401, "raw provider data stays private"
        assert client.post("/fetch").status_code == 401
        assert client.get("/v1/keys").status_code in (401, 404), "keys never public"
        settings.public_pages = False
        assert client.get("/gpus").status_code == 401, "off by default: pages need the password"
    finally:
        settings.app_password, settings.public_pages = None, False


EXECUTION_PAGES = ["/deployments/dep-0123abcd", "/onboarding", "/admin/execution", "/admin/checklist", "/admin/partners", "/admin/value"]


def test_execution_pages_are_private_and_noindex():
    """The execution / partner / admin pages: served (reload works), never indexed, never in the sitemap."""
    patterns = {p for p, *_ in pages.PAGES}
    for p in ("/deployments/{id}", "/onboarding", "/admin/execution", "/admin/checklist", "/admin/partners", "/admin/value"):
        assert p in patterns, p
        assert next(x for x in pages.PAGES if x[0] == p)[3] is False, f"{p} must not be indexable"
    locs = re.findall(r"<loc>([^<]+)</loc>", client.get("/sitemap.xml").text)
    robots = client.get("/robots.txt").text
    for path in EXECUTION_PAGES:
        r = client.get(path)
        assert r.status_code == 200 and 'id="page"' in r.text, path
        assert 'name="robots" content="noindex"' in r.text, f"{path}: noindex"
        assert not any(loc.endswith(path) or "/admin/" in loc or "/onboarding" in loc for loc in locs), f"{path} listed in the sitemap"
    for p in ("/onboarding", "/admin/execution", "/admin/checklist", "/admin/partners", "/admin/value"):
        assert f"Disallow: {p}" in robots, p
    # the data behind them is never public, whatever PUBLIC_PAGES says
    saved = settings.app_password, settings.public_pages
    settings.app_password, settings.public_pages = "s3cret", True
    try:
        anon = TestClient(main.app)
        for api in ("/v1/admin/execution/mode", "/v1/admin/orphans", "/v1/admin/checklist?provider=vast", "/v1/admin/partners", "/v1/economics",
                    "/v1/onboarding", "/v1/partners/me", "/v1/deployments/dep-0123abcd", "/v1/admin/funnel"):
            assert anon.get(api).status_code in (401, 403), api
        assert anon.get("/admin/execution").status_code == 200, "the page shell itself holds no data"
    finally:
        settings.app_password, settings.public_pages = saved


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok ", name)
    print("all passed")
