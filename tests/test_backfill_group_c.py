"""Archived price history for E2E Networks, Atlantic.Net, Yotta, VESSL, Arkane, Phyntec and Koyeb (backfill/parsers/; fixtures
are trimmed Internet Archive captures, see each file's first line).

Run:  .venv/bin/python tests/test_backfill_group_c.py
"""

import sys
from datetime import date, datetime, timezone
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import scratchdb  # noqa: E402,F401  (first: isolates the process from real networks)

import fx  # noqa: E402
import json  # noqa: E402

from backfill.parsers import arkane, atlanticnet, e2e, koyeb, shadeform, simplepod, vessl, yotta  # noqa: E402
from backfill.sources import sources  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures_backfill"
AT = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _page(name):
    return (FIX / name).read_text()


def _raises(fn, *a, contains=""):
    try:
        fn(*a)
    except ValueError as exc:
        assert contains in str(exc), str(exc)
        return
    raise AssertionError("expected ValueError")


def _src(provider, url):
    return next(s for s in sources() if s.provider == provider and s.url == url)


# ---------------------------------------------------------------- E2E Networks

def test_e2e_feed_capture_through_the_live_normalizer():
    ls, used = _src("e2e", "www.e2enetworks.com/pricing.json").parse((FIX / "e2e_pricing-json_2026-10_feed.json").read_bytes(), AT)
    assert used.endswith("live parser)")
    by = {l.listing_id: l for l in ls}
    assert len(ls) == 12 and by["h200:1x"].price_per_gpu_hour == D("5.29") and by["h200:1x"].gpu_count == 1
    assert by["h200:1x"].raw_gpu_name == "NVIDIA H200 141GB" and by["h200:1x"].canonical_gpu_name == "NVIDIA H200 141GB SXM5"
    assert by["a100-80gb:1x"].raw_gpu_name == "NVIDIA A100 80GB" and by["a100-80gb:1x"].canonical_gpu_name is None
    assert all(l.currency == "USD" and l.observed_at == AT and l.provider_tier == "vm" for l in ls)


def test_e2e_page_tables_match_the_feed_ids():
    old = {l.listing_id: l for l in e2e.e2e_gpu_table(_page("e2e_pricing_2025-10_item-table.html"), AT)}
    assert len(old) == 8 and old["h200:1x"].price_per_gpu_hour == D("3.49") and old["l4:1x"].price_per_gpu_hour == D("0.57")
    assert old["a100-40gb:1x"].raw_gpu_name == "NVIDIA A100 40GB" and old["a100-40gb:1x"].price_per_instance_hour == D("2.55")
    assert old["h100:1x"].vcpu == 26 and old["h100:1x"].ram_gb == 250.0
    assert old["l40s:1x"].canonical_gpu_name == "NVIDIA L40S 48GB"
    new = {l.listing_id: l for l in e2e.e2e_gpu_table(_page("e2e_pricing_2026-10_gpu-table.html"), AT)}
    feed = {l.listing_id: l for l in e2e.e2e_pricing_json((FIX / "e2e_pricing-json_2026-10_feed.json").read_bytes(), AT)}
    # the page and the feed captured a day apart: same ids, names and prices
    assert set(new) == set(feed)
    assert all(new[k].price_per_gpu_hour == feed[k].price_per_gpu_hour and new[k].raw_gpu_name == feed[k].raw_gpu_name
               for k in new)
    assert new["rtx-pro-6000:1x"].price_per_gpu_hour == D("2.49") and new["b200:1x"].canonical_gpu_name == "NVIDIA B200 180GB SXM"


def test_e2e_us_price_list_plan_rows():
    page = _page("e2e_us-pricing_2024-12_plan-rows.html")
    ls, used = _src("e2e", "www.e2enetworks.com/us/pricing").parse(page.encode(), AT)
    assert used == "backfill.parsers.e2e.e2e_us_plans"
    by = {l.listing_id: l for l in ls}
    assert by["h200:1x"].price_per_instance_hour == D("5.6") and by["h200:1x"].canonical_gpu_name == "NVIDIA H200 141GB SXM5"
    assert by["h100:8x"].gpu_count == 8 and by["h100:8x"].price_per_gpu_hour == D("4.5") and by["h100:8x"].vcpu == 208
    assert by["h100:1x"].price_per_gpu_hour == D("5"), "the Spot Instances table ($2/hr) is not read"
    a = by["GDC3.2xA10080-32.230GB"]
    assert a.gpu_count == 2 and a.raw_gpu_name == "NVIDIA A100 80GB" and a.price_per_gpu_hour == D("1.5")
    assert not any(k.startswith("GDCW") for k in by), "Windows plans (licence included) are not read"
    assert not any("V100" in k or "T4" in k for k in by), "memory not in the code: not read"
    assert len(ls) == 16
    # a row whose own GPU-memory cell contradicts its plan code is skipped, never read as the code says
    row = "<h1>Pricing - In $USD</h1><div>GDC.2xA30-32.180GB</div><div>{mem}</div><div>32 vCPUs</div><div>$2.7/hr</div>"
    assert e2e.e2e_us_plans(row.format(mem="2 x 24 GB"), AT)[0].price_per_gpu_hour == D("1.35")
    _raises(e2e.e2e_us_plans, row.format(mem="4 x 24 GB"), AT, contains="no Linux USD plan row")
    _raises(e2e.e2e_us_plans, row.format(mem="2 x 24 GB").replace("$2.7", "₹270"), AT, contains="no Linux USD plan row")
    _raises(e2e.e2e_us_plans, page.replace("In $USD", "In INR"), AT, contains="In $USD")


def test_e2e_inr_pages_and_garbage_are_refused():
    _raises(e2e.e2e_gpu_table, _page("e2e_pricing_2024-06_inr-only.html"), AT, contains="INR only")
    page = _page("e2e_pricing_2026-10_gpu-table.html")
    _raises(e2e.e2e_gpu_table, page.replace("per GPU-hour", "per instance-hour"), AT, contains="per GPU-hour")
    _raises(e2e.e2e_gpu_table, page.replace(">$5.29<", ">₹436<"), AT, contains="not in USD")
    _raises(e2e.e2e_gpu_table, page.replace(">$5.29<", ">$5.29/mo<"), AT, contains="is not '$x'")
    _raises(e2e.e2e_gpu_table, "<html><body>maintenance</body></html>", AT, contains="no 'NVIDIA GPU'")
    soon = page.replace(">NVIDIA B200<", ">NVIDIA B200 - Coming soon<", 1)
    assert "b200:1x" not in {l.listing_id for l in e2e.e2e_gpu_table(soon, AT)}, "Feb 2026: priced, not rentable yet"
    _raises(e2e.e2e_pricing_json, b'{"gpus": []}', AT, contains="no gpuInstances")


# ---------------------------------------------------------------- Atlantic.Net

def test_atlantic_2025_table_through_the_live_normalizer():
    for name in ("atlanticnet_gpu-server-hosting_2025-10_plain-th.html",
                 "atlanticnet_gpu-server-hosting_2026-05_action-column.html"):
        ls, used = _src("atlanticnet", "www.atlantic.net/gpu-server-hosting/").parse(_page(name).encode(), AT)
        assert used == "backfill.parsers.atlanticnet.atlantic_table_2025", name
        by = {l.listing_id: l for l in ls}
        assert len(ls) == 7, name
        l40s = by["AL40S.192GB"]
        assert l40s.gpu_count == 1 and l40s.price_per_instance_hour == D("1.668") and l40s.raw_gpu_name == "L40S"
        assert l40s.canonical_gpu_name == "NVIDIA L40S 48GB" and l40s.vcpu == 32 and l40s.ram_gb == 192.0
        h = by["AH100NVL.1920GB"]
        assert h.gpu_count == 8 and h.price_per_instance_hour == D("31.531") and h.canonical_gpu_name == "NVIDIA H100 94GB NVL"
        assert h.price_per_gpu_hour == D("31.531") / 8
        assert all(l.market_type == "on_demand" and l.provider_tier == "vm" for l in ls), "the term prices are never read"


def test_atlantic_refuses_what_it_cannot_read():
    page = _page("atlanticnet_gpu-server-hosting_2025-10_plain-th.html")
    _raises(atlanticnet.atlantic_table_2025, page.replace(">On-Demand<", ">Hourly<"), AT, contains="On-Demand")
    _raises(atlanticnet.atlantic_table_2025, page.replace("$1.668/hr", "$1.668/day"), AT, contains="'$x/hr'")
    _raises(atlanticnet.atlantic_table_2025, page.replace(">Transfer</th>", ">Bandwidth</th>"), AT, contains="header")
    _raises(atlanticnet.atlantic_table_2025, "<p>GPU Hosting: call us</p>", AT, contains="toggle")


# ---------------------------------------------------------------- Yotta

def _rate(currency, at, context=""):
    return fx.Rate(currency, "USD", D("0.0113"), date(2025, 11, 12), fx.CROSS_SOURCE)


def test_yotta_hourly_tab_converted_like_live():
    real = fx.usd_rate
    fx.usd_rate = _rate
    try:
        ls, used = _src("yotta", "shakticloud.ai/pricing/").parse(_page("yotta_pricing_2025-11_hourly-tab.html").encode(), AT)
    finally:
        fx.usd_rate = real
    assert used == "backfill.parsers.yotta.yotta_hourly_tab"
    by = {l.listing_id: l for l in ls}
    assert len(ls) == 5
    h = by["vm-2-x-h100-on-demand"]
    assert h.gpu_count == 2 and h.raw_gpu_name == "NVIDIA H100 SXM 80GB" and h.canonical_gpu_name == "NVIDIA H100 80GB SXM5"
    assert h.original_currency == "INR" and h.original_price_per_instance_hour == D("658")
    assert h.price_per_instance_hour == D("7.435400") and h.currency == "USD" and h.country == "IN"
    assert by["vm-1-x-l40s-on-demand"].original_price_per_instance_hour == D("128")
    assert not any("dedicated" in k for k in by), "the monthly Dedicated tab is never read"


def test_yotta_refuses_unstated_or_contradicting_units_and_missing_fx():
    real = fx.usd_rate
    fx.usd_rate = _rate
    try:
        _raises(yotta.yotta_hourly_tab, _page("yotta_pricing_2026-05_month-note.html"), AT, contains="not stated")
        page = _page("yotta_pricing_2025-11_hourly-tab.html")
        _raises(yotta.yotta_hourly_tab, page.replace(">Price Hourly<", ">1 Month Pricing<"), AT, contains="1 Month")
        _raises(yotta.yotta_hourly_tab, "<html>coming soon</html>", AT, contains="no 'On Demand GPU Compute' tab")
    finally:
        fx.usd_rate = real
    # no ECB rate in force at the capture: nothing is converted (fail closed), and the reason says so
    fx.usd_rate = lambda *a, **k: None
    try:
        _raises(yotta.yotta_hourly_tab, _page("yotta_pricing_2025-11_hourly-tab.html"), AT, contains="fail closed")
    finally:
        fx.usd_rate = real


# ---------------------------------------------------------------- VESSL

def test_vessl_gpu_tables_both_2026_layouts():
    jun, used = _src("vessl", "vessl.ai/en/pricing").parse(_page("vessl_en-pricing_2026-06_two-tables.html").encode(), AT)
    assert used == "backfill.parsers.vessl.vessl_gpu_tables"
    by = {l.listing_id: l for l in jun}
    assert set(by) == {"nvidia-a100-sxm-80gb:1x", "nvidia-h100-sxm-80gb:1x", "nvidia-l40s-48gb:1x", "nvidia-b200-192gb:1x",
                       "nvidia-b300-288gb:1x"}, "both tables, no mobile duplicates"
    h = by["nvidia-h100-sxm-80gb:1x"]
    assert h.raw_gpu_name == "NVIDIA H100 SXM 80GB" and h.canonical_gpu_name == "NVIDIA H100 80GB SXM5"
    assert h.price_per_gpu_hour == D("2.39") and h.gpu_count == 1 and h.provider_tier == "container"
    aug = {l.listing_id: l for l in vessl.vessl_gpu_tables(_page("vessl_en-pricing_2026-08_reserved-column.html"), AT)}
    assert aug["nvidia-h100-sxm-80gb:1x"].price_per_gpu_hour == D("2.98") and aug["nvidia-a100-sxm-80gb:1x"].price_per_gpu_hour == D("1.48")
    assert aug["nvidia-h200-sxm-141gb:1x"].canonical_gpu_name == "NVIDIA H200 141GB SXM5"
    assert not any("gb300" in k for k in aug), "'Reserved only' has no on-demand price"
    assert len(aug) == 6


def test_vessl_refuses_garbage():
    page = _page("vessl_en-pricing_2026-08_reserved-column.html")
    _raises(vessl.vessl_gpu_tables, page.replace("$2.98<span", "$2.98/mo<span"), AT, contains="not '$x/hr'")
    _raises(vessl.vessl_gpu_tables, page.replace(">80GB<", ">80GB × N<", 1), AT, contains="not one GPU's")
    _raises(vessl.vessl_gpu_tables, page.replace(">Hourly<", ">Monthly<"), AT, contains="Hourly")
    _raises(vessl.vessl_gpu_tables, "<button>Hourly</button><p>MLOps plans</p>", AT, contains="no 'GPU | VRAM")


# ---------------------------------------------------------------- Arkane

def test_arkane_three_older_designs():
    src = _src("arkane", "arkanecloud.com/pricing/")
    t, used = src.parse(_page("arkane_pricing_2026-04_price-per-gpu-table.html").encode(), AT)
    assert used == "backfill.parsers.arkane.arkane_on_demand_table"
    by = {l.listing_id: l for l in t}
    assert len(t) == 9 and by["nvidia-h100-80gb:1x"].price_per_gpu_hour == D("2.99") and by["nvidia-h100-80gb:1x"].vcpu == 32
    assert by["nvidia-b300-288gb:1x"].canonical_gpu_name == "NVIDIA B300 288GB SXM" and by["rtx-a6000-48gb:1x"].ram_gb == 60.0
    sep, used = src.parse(_page("arkane_pricing_2025-09_gpu-cards-on-demand.html").encode(), AT)
    assert used == "backfill.parsers.arkane.arkane_gpu_cards"
    by = {l.listing_id: l for l in sep}
    assert by["nvidia-h200-141gb:1x"].price_per_gpu_hour == D("6.0") and by["nvidia-l40s-48gb:1x"].price_per_gpu_hour == D("2.0")
    assert not any("b200" in k or "a4000" in k for k in by), "the B200 card names an RTX A4000: skipped"
    jun = arkane.arkane_gpu_cards(_page("arkane_pricing_2025-06_gpu-cards-reserved.html"), AT)
    assert [l.listing_id for l in jun] == ["rtx-a5000-24gb:1x"], "reserved-cluster and coming-soon cards are not read"
    srv, used = src.parse(_page("arkane_pricing_2024-03_server-cards.html").encode(), AT)
    assert used == "backfill.parsers.arkane.arkane_server_cards"
    by = {l.listing_id: l for l in srv}
    a6 = by["rtx-a6000-48gb:8x"]
    assert a6.gpu_count == 8 and a6.price_per_gpu_hour == D("1.6") and a6.price_per_instance_hour == D("12.8")
    assert a6.canonical_gpu_name == "NVIDIA RTX A6000 48GB" and a6.vcpu == 64 and a6.ram_gb == 450.0
    assert by["nvidia-h100-80gb:8x"].price_per_gpu_hour == D("5") and len(srv) == 6


def test_arkane_refuses_garbage():
    page = _page("arkane_pricing_2024-03_server-cards.html")
    _raises(arkane.arkane_server_cards, page.replace("$12.8/hour", "$13.8/hour"), AT, contains="per server")
    tbl = _page("arkane_pricing_2026-04_price-per-gpu-table.html")
    _raises(arkane.arkane_on_demand_table, tbl.replace("$2.99/hr", "€2.99/hr"), AT, contains="is not '$x/hr'")
    _raises(arkane.arkane_on_demand_table, tbl.replace("<th>Price per GPU</th>", "<th>Price per server</th>"), AT,
            contains="no table headed")
    _raises(arkane.arkane_gpu_cards, _page("arkane_pricing_2025-06_gpu-cards-reserved.html").replace("On Demand", "Soon"),
            AT, contains="no card labelled")


# ---------------------------------------------------------------- Koyeb

def test_koyeb_cards_registered_with_live_ids():
    ls, used = _src("koyeb", "www.koyeb.com/pricing").parse(_page("koyeb_2025-11.html").encode(), AT)
    assert used == "backfill.parsers.koyeb.koyeb_cards"
    by = {l.listing_id: l for l in ls}
    assert by["2-gpu-nvidia-a100-sxm"].raw_gpu_name == "gpu-nvidia-a100-sxm 80GB"
    assert by["2-gpu-nvidia-a100-sxm"].canonical_gpu_name == "NVIDIA A100 80GB SXM4"
    assert by["2-gpu-nvidia-a100-sxm"].price_per_gpu_hour == D("2.15") and by["2-gpu-nvidia-a100-sxm"].gpu_count == 2
    assert by["gpu-nvidia-l40s"].canonical_gpu_name == "NVIDIA L40S 48GB" and by["gpu-nvidia-l40s"].price_per_gpu_hour == D("1.55")
    assert by["gpu-nvidia-rtx-4000-sff-ada"].canonical_gpu_name == "NVIDIA RTX 4000 SFF Ada 20GB"
    assert all(l.provider_tier == "serverless" for l in ls), "the live tier"
    _raises(koyeb.koyeb_cards, "<div>Pricing</div>", AT, contains="no priced instance card")


# ---------------------------------------------------------------- Phyntec (via Shadeform)

def test_phyntec_former_shadeform_name_evergreen():
    body = (FIX / "phyntec_shadeform-types_2025-11_evergreen.json").read_bytes()
    ls, used = _src("phyntec", shadeform.URL).parse(body, AT)
    assert used.startswith("backfill.parsers.shadeform (PhyntecProvider")
    by = {l.listing_id: l for l in ls}
    assert set(by) == {"RTX4090x8", "RTX5090x8"} and all(l.provider == "phyntec" for l in ls), "Crusoe's row is not read"
    assert by["RTX4090x8"].gpu_count == 8 and by["RTX4090x8"].price_per_gpu_hour == D("0.4")
    assert by["RTX4090x8"].canonical_gpu_name == "NVIDIA RTX 4090 24GB"
    assert by["RTX5090x8"].price_per_instance_hour == D("5.6") and by["RTX5090x8"].canonical_gpu_name == "NVIDIA RTX 5090 32GB"
    # the other clouds' sources never pick the renamed rows up
    assert not _src("crusoe", shadeform.URL).parse(body, AT)[0][0].listing_id.startswith("RTX")


def test_koyeb_nov_2024_cards_print_their_vram():
    ls, used = _src("koyeb", "www.koyeb.com/pricing").parse(_page("koyeb_pricing_2024-11_vram-cards.html").encode(), AT)
    assert used == "backfill.parsers.koyeb.koyeb_vram_cards"
    by = {l.listing_id: l for l in ls}
    assert len(ls) == 6 and by["gpu-nvidia-rtx-a6000"].price_per_gpu_hour == D("0.73") and by["gpu-nvidia-rtx-a6000"].vcpu == 10
    assert by["gpu-nvidia-l40s"].raw_gpu_name == "gpu-nvidia-l40s 48GB" and by["gpu-nvidia-l40s"].canonical_gpu_name == "NVIDIA L40S 48GB"
    assert all(int(l.raw_gpu_name.split(" ")[1][:-2]) == koyeb.LIVE_VRAM[l.listing_id[11:]] for l in ls), "printed = live VRAM"
    _raises(koyeb.koyeb_vram_cards, "<p>H100 $3.30 /hr</p>", AT, contains="no '<GPU> VRAM")


def test_koyeb_catalogue_capture_agrees_with_the_page_mapping():
    body = (FIX / "koyeb_catalog-instances_2025-03_api.json").read_bytes()
    ls, used = _src("koyeb", "app.koyeb.com/v1/catalog/instances?limit=100").parse(body, AT)
    assert used.endswith("live parser)")
    by = {l.listing_id: l for l in ls}
    assert set(by) == {"gpu-nvidia-l40s", "gpu-nvidia-a100-sxm", "2-gpu-nvidia-h100", "gpu-nvidia-h100"}, "Tenstorrent $0: none"
    assert by["2-gpu-nvidia-h100"].price_per_instance_hour == D("6.60") and by["2-gpu-nvidia-h100"].gpu_count == 2
    # the API states the VRAM the page omits; the page parser's LIVE_VRAM gives the same raw names
    for lid, l in by.items():
        assert l.raw_gpu_name.endswith(f" {koyeb.LIVE_VRAM[lid.split('gpu-nvidia-')[1]]}GB"), lid
    _raises(koyeb.koyeb_catalog, b'{"error": "not found"}', AT, contains="no instances list")


# ---------------------------------------------------------------- SimplePod (API captures)

def test_simplepod_vm_market_whole_book_only():
    # simplepod_market-list-vm_2026-06_whole-book.json: the unmodified capture
    # https://web.archive.org/web/20260617160316/https://api.simplepod.ai/instances/market/list?mode=vm&rentalStatus=active
    body = (FIX / "simplepod_market-list-vm_2026-06_whole-book.json").read_bytes()
    ls, used = _src("simplepod", "api.simplepod.ai/instances/market/list?mode=vm&rentalStatus=active").parse(body, AT)
    assert "live parser" in used and len(ls) == 11
    by = {l.listing_id: l for l in ls}
    o = by["vm:500"]
    assert o.raw_gpu_name == "RTX 4090 (24 GB)" and o.canonical_gpu_name == "NVIDIA RTX 4090 24GB"
    assert o.price_per_gpu_hour == D("0.59") and o.provider_tier == "vm/enterprise" and o.observed_at == AT
    assert all(l.price_per_gpu_hour * l.gpu_count == l.price_per_instance_hour for l in ls), "pricePerGpu is per offer"
    # 30 offers = the API's default page: maybe part of the book, refused (as live refuses a full page)
    offers = json.loads(body)
    full = json.dumps((offers * 3)[:30]).encode()
    docker = _src("simplepod", "api.simplepod.ai/instances/market/list?rentalStatus=active")
    _raises(docker.parse, full, AT, contains="default page")
    _raises(docker.parse, b'{"hydra:member": []}', AT, contains="not a list")


TESTS = (test_koyeb_nov_2024_cards_print_their_vram, test_simplepod_vm_market_whole_book_only, test_koyeb_catalogue_capture_agrees_with_the_page_mapping, test_e2e_us_price_list_plan_rows, test_phyntec_former_shadeform_name_evergreen, test_vessl_gpu_tables_both_2026_layouts, test_vessl_refuses_garbage,
         test_arkane_three_older_designs, test_arkane_refuses_garbage, test_e2e_feed_capture_through_the_live_normalizer, test_e2e_page_tables_match_the_feed_ids,
         test_e2e_inr_pages_and_garbage_are_refused, test_atlantic_2025_table_through_the_live_normalizer,
         test_atlantic_refuses_what_it_cannot_read, test_yotta_hourly_tab_converted_like_live,
         test_yotta_refuses_unstated_or_contradicting_units_and_missing_fx, test_koyeb_cards_registered_with_live_ids)

if __name__ == "__main__":
    for t in TESTS:
        t()
        print(t.__name__, "ok")
