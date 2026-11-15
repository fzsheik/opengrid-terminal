"""Archived price pages, group A: Scaleway, Hetzner, Gcore, Civo (backfill/parsers/; IBM has no usable capture).
Fixtures are trimmed Internet Archive captures (tests/fixtures_backfill/, source URL in each file's first line).
EUR pages are converted at a stubbed ECB rate (fx.rate_at), exactly as the live EUR pollers' tests do: no database.

Run:  .venv/bin/python tests/test_backfill_group_a.py
"""

import sys
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import scratchdb  # noqa: E402,F401  (first: isolates the process from real networks)

import fx  # noqa: E402
from backfill import eur, load  # noqa: E402
from backfill.parsers import civo, gcore, hetzner, scaleway  # noqa: E402
from backfill.sources import sources  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures_backfill"
AT = datetime(2024, 6, 3, 12, tzinfo=timezone.utc)
RATE = D("1.0850")


def _fix(name):
    return (FIX / name).read_text()


def _src(url):
    return next(s for s in sources() if s.url == url)


def _raises(fn, *a, contains=""):
    try:
        fn(*a)
    except ValueError as exc:
        assert contains in str(exc), str(exc)
        return
    raise AssertionError("expected ValueError")


class _Rate:
    """fx.rate_at stubbed with one ECB rate dated the day before AT (or none)."""

    def __init__(self, value=RATE, day=date(2024, 6, 2)):
        self.fixed = None if value is None else fx.Rate("EUR", "USD", value, day)

    def __enter__(self):
        self.real = fx.rate_at
        fx.rate_at = lambda at, base="EUR", quote="USD": fx.pick([self.fixed] if self.fixed else [], at)

    def __exit__(self, *exc):
        fx.rate_at = self.real


def _eur_ok(l, eur_instance):
    assert l.currency == "USD" and l.original_currency == "EUR" and l.fx_rate == RATE and l.fx_source == "ecb"
    assert l.original_price_per_instance_hour == D(eur_instance), (l.listing_id, l.original_price_per_instance_hour)
    assert l.price_per_instance_hour == (D(eur_instance) * RATE).quantize(D("0.000001"), rounding=ROUND_HALF_UP)
    assert l.price_per_gpu_hour * l.gpu_count - l.price_per_instance_hour < D("0.00001")


# ---------------------------------------------------------------------------------------------------------- Civo

def test_civo_2024_single_price_column_through_the_live_normalizer():
    ls = {l.listing_id: l for l in civo.civo_single_price(_fix("civo_pricing_2024-02_single_price.html"), AT)}
    l40s = ls["nvidia-l40s-48gb:1x"]
    assert l40s.canonical_gpu_name == "NVIDIA L40S 48GB" and l40s.price_per_gpu_hour == D("1.785714")
    assert l40s.currency == "USD" and l40s.vcpu == 8 and l40s.ram_gb == 64.0 and l40s.provider_tier == "vm"
    assert ls["nvidia-l40s-48gb:8x"].price_per_instance_hour == D("14.285714") and ls["nvidia-l40s-48gb:8x"].gpu_count == 8
    assert ls["nvidia-a100-80gb:1x"].price_per_gpu_hour == D("2.380952")
    assert ls["nvidia-a100-80gb:1x"].canonical_gpu_name is None, "live leaves Civo's A100 (variant not stated) unmapped"
    assert all(l.market_type == "on_demand" for l in ls.values())
    # through the Source: the live parser rejects the design, the archived one names itself
    _, used = _src("www.civo.com/pricing").parse((FIX / "civo_pricing_2024-02_single_price.html").read_bytes(), AT)
    assert used == "backfill.parsers.civo.civo_single_price"


def test_civo_2025_on_demand_column_is_the_live_parser():
    ls, used = _src("www.civo.com/pricing").parse((FIX / "civo_pricing_2025-01_on_demand.html").read_bytes(), AT)
    assert used == "providers.civo (live parser)"
    by = {l.listing_id: l for l in ls}
    assert by["nvidia-h100-sxm:1x"].canonical_gpu_name == "NVIDIA H100 80GB SXM5"


def test_civo_rejects_mixed_currencies_and_unknown_rows():
    _raises(civo.civo_single_price, _fix("civo_pricing_2023-09_mixed_currency.html"), AT, contains="different currencies")
    page = _fix("civo_pricing_2024-02_single_price.html")
    _raises(civo.civo_single_price, page.replace('data-option="hourly"', 'data-option="hour"'), AT,
            contains="no size or hourly")
    _raises(civo.civo_single_price, "<html>no GPUs here</html>", AT, contains="nvidia-gpus")


# ------------------------------------------------------------------------------------------------------- Hetzner

def test_hetzner_2024_server_rendered_reads_only_excl_vat():
    page = _fix(HZ_SERVER)   # every archived capture of this design is labelled "incl. 19 % VAT"
    with _Rate():
        _raises(hetzner.hetzner_server_rendered, page, AT, contains="not 'excl. VAT'")
        ls = {l.listing_id: l for l in hetzner.hetzner_server_rendered(page.replace("incl. 19 % VAT", "excl. VAT"), AT)}
    g = ls["gex44"]
    assert g.raw_gpu_name == "NVIDIA RTX 4000 SFF Ada Generation 20GB" and g.canonical_gpu_name == "NVIDIA RTX 4000 SFF Ada 20GB"
    assert g.gpu_count == 1 and g.provider_tier == "dedicated" and g.ram_gb == 64.0 and g.country == "DE"
    _eur_ok(g, HZ_SERVER_GEX44)


def test_hetzner_monthly_only_and_script_prices_are_not_read():
    with _Rate():
        _raises(hetzner.hetzner_server_rendered, _fix("hetzner_matrix-gpu_2024-03_monthly_only.html"), AT,
                contains="no card with an hourly price")
        _raises(hetzner.hetzner_price_slots, _fix(HZ_SCRIPT), AT, contains="loaded by script")
        src = _src("www.hetzner.com/dedicated-rootserver/matrix-gpu/")
        _raises(src.parse, (FIX / "hetzner_matrix-gpu_2024-03_monthly_only.html").read_bytes(), AT)


def test_hetzner_price_slots_and_fail_closed_without_a_rate():
    with _Rate():
        ls, used = _src("www.hetzner.com/dedicated-rootserver/matrix-gpu/").parse((FIX / HZ_SLOTS).read_bytes(), AT)
    assert used == "backfill.parsers.hetzner.hetzner_price_slots"
    by = {l.listing_id: l for l in ls}
    _eur_ok(by["gex44"], "0.2948")
    _eur_ok(by["gex130"], "1.3429")
    # this grid prints no VRAM: the name stays as printed, canonical.py knows the card by it
    assert by["gex130"].raw_gpu_name == "NVIDIA RTX 6000 Ada Generation"
    assert by["gex130"].canonical_gpu_name == "NVIDIA RTX 6000 Ada 48GB" and by["gex130"].ram_gb == 128.0
    assert not any(l.original_price_per_instance_hour == D("0.3285") for l in ls), "Hetzner's own USD slot is not read"
    with _Rate(None):
        _raises(hetzner.hetzner_price_slots, _fix(HZ_SLOTS), AT, contains="fail closed")
    with _Rate(day=date(2024, 5, 20)):   # 14 days old at AT: stale, fail closed
        try:
            hetzner.hetzner_price_slots(_fix(HZ_SLOTS), AT)
            raise AssertionError("a stale rate must not convert")
        except eur.NoRate:
            pass


# ------------------------------------------------------------------------------------------------------ Scaleway

def test_scaleway_2019_and_2020_render_s():
    with _Rate():
        a = scaleway.scaleway_pricing_2019(_fix(SW_2019), AT)
        b = scaleway.scaleway_pricing_2020(_fix(SW_2020), AT)
    for ls in (a, b):
        r = next(l for l in ls if l.listing_id == "RENDER-S")
        assert r.raw_gpu_name == "P100" and r.canonical_gpu_name is None, "live leaves RENDER-S's P100 unmapped"
        assert r.gpu_count == 1 and r.vcpu == 10 and r.provider_tier == "vm"
        _eur_ok(r, "1")
    with _Rate():
        _raises(scaleway.scaleway_pricing_2020, _fix(SW_2019), AT, contains="not the 2020-22")


def test_scaleway_2023_hourly_with_approx_month_and_floor_prices_skipped():
    with _Rate():
        ls = {l.listing_id: l for l in scaleway.scaleway_pricing_2023(_fix(SW_2023), AT)}
    assert ls["GPU-3070"].canonical_gpu_name == "NVIDIA RTX 3070 8GB"
    _eur_ok(ls["GPU-3070"], SW_2023_3070)
    assert "starting at €1.9/hour" in _fix(SW_2023), "the capture prints H100 rows with a floor price"
    assert "H100" not in ls and "H100x2" not in ls, "'starting at' H100 prices are floors: not read"
    _eur_ok(ls["RENDER-S"], "1.24")
    page = _fix(SW_2023).replace("/hour (~", "/hr (~")
    with _Rate():
        _raises(scaleway.scaleway_pricing_2023, page, AT, contains="hourly format")  # "/hr" was never this design


def test_scaleway_gpu_pricing_2024_live_ids_and_spot_note():
    with _Rate():
        ls, used = _src("www.scaleway.com/en/pricing/gpu/").parse((FIX / SW_GPU_2024).read_bytes(), AT)
    assert used == "backfill.parsers.scaleway.scaleway_gpu_pricing_2024"
    by = {l.listing_id: l for l in ls}
    # Shadeform's legacy ids, as the live poller keeps them, so the archived and live series join
    assert {"H100", "H100x2", "L4", "L4x8", "L40S", "L40Sx8"} <= set(by)
    h = by["H100"]
    assert h.canonical_gpu_name == "NVIDIA H100 80GB PCIe" and h.raw_gpu_name == "H100-PCIe 80GB"
    assert h.market_type == "spot" and h.interruptible, "the page says the H100 PCIe instance is a spot instance"
    _eur_ok(h, "2.52")
    l4 = by["L4x8"]
    assert l4.market_type == "on_demand" and l4.gpu_count == 8 and l4.canonical_gpu_name == "NVIDIA L4 24GB"
    _eur_ok(l4, "6")
    page = _fix(SW_GPU_2024).replace("L4-2-24G", "L4-3-24G")   # the id says 3 GPUs, the row 2
    with _Rate():
        _raises(scaleway.scaleway_gpu_pricing_2024, page, AT, contains="GPU(s)")


def test_scaleway_gpu_pricing_2026_per_zone():
    with _Rate():
        ls = {l.listing_id: l for l in scaleway.scaleway_gpu_pricing_2026(_fix(SW_GPU_2026), AT)}
    l4 = ls["L4"]
    assert l4.region == "fr-par-1" and l4.country == "FR" and l4.canonical_gpu_name == "NVIDIA L4 24GB"
    _eur_ok(l4, "0.79")
    assert "L40S" not in ls, "'Not available in this zone' rows are skipped"


# --------------------------------------------------------------------------------------------------------- Gcore

def test_gcore_2023_ai_gpu_flavor_tables():
    with _Rate():
        ls = {l.listing_id: l for l in gcore.gcore_ai_gpu_flavors(_fix("gcore_cloud-ai-gpu_2023-09_flavors.html"), AT)}
    bm = ls["bare-metal:8x-h100"]
    assert bm.raw_gpu_name == "H100 80GB" and bm.canonical_gpu_name is None, "SXM or PCIe not stated: unmapped"
    assert bm.provider_tier == "bare-metal" and bm.gpu_count == 8
    _eur_ok(bm, "31.24")
    vm = ls["vm:1x-h100"]
    assert vm.provider_tier == "vm" and vm.vcpu == 24 and vm.ram_gb == 232.0
    _eur_ok(vm, "2.31")
    assert ls["bare-metal:4x-a100"].raw_gpu_name == "A100 40GB"
    page = _fix("gcore_cloud-ai-gpu_2023-09_flavors.html").replace("(€3.91 per H100)", "(€3.71 per H100)")
    with _Rate():
        assert "bare-metal:8x-h100" not in {l.listing_id for l in gcore.gcore_ai_gpu_flavors(page, AT)}, "self-contradicting row skipped"


def test_gcore_2025_on_demand_row_like_live():
    with _Rate():
        ls, used = _src("gcore.com/pricing/ai").parse((FIX / GC_OD).read_bytes(), AT)
    assert used == "backfill.parsers.gcore.gcore_on_demand_columns"
    by = {l.listing_id: l for l in ls}
    assert set(by) == {"bare-metal:8x-h100-sxm", "bare-metal:8x-a100-sxm", "bare-metal:8x-l40s-pcie"}, \
        "'Contact sales' columns (H200, GB200) are not self-serve"
    h = by["bare-metal:8x-h100-sxm"]
    assert h.raw_gpu_name == "H100 SXM 80GB" and h.canonical_gpu_name == "NVIDIA H100 80GB SXM5" and h.vcpu == 224
    _eur_ok(h, GC_OD_H100)
    assert not any(l.original_price_per_instance_hour == D(GC_OD_H100_R12) for l in ls), "reserved rows never read"


def test_gcore_commitment_only_tables_are_not_read():
    with _Rate():
        _raises(_src("gcore.com/pricing/ai").parse, (FIX / "gcore_pricing-ai_2025-02_commitment_only.html").read_bytes(), AT)
        _raises(gcore.gcore_on_demand_columns, _fix("gcore_pricing-ai_2025-02_commitment_only.html"), AT,
                contains="On-demand/h")


# ---------------------------------------------------------------------------------------------------------- load

def test_archived_eur_rows_keep_their_fx_provenance_in_the_note():
    with _Rate():
        ls = scaleway.scaleway_gpu_pricing_2026(_fix(SW_GPU_2026), AT)
    rows, rejected = load._rows(ls, source="wayback", source_url="https://web.archive.org/web/x", sha256="0" * 64,
                                parser="p", basis="capture")
    assert not rejected and rows[0]["currency"] == "USD"
    assert rows[0]["note"] == "EUR 0.790000/instance-hour as printed, converted at the ecb EUR->USD rate 1.0850 of 2024-06-02"
    assert all(r["visibility"] == "public" for r in rows), "Scaleway is a public source"


# Fixture names whose month came from the archive's captures (see the files' first lines).
HZ_SERVER, HZ_SERVER_GEX44 = "hetzner_matrix-gpu_2024-04_incl_vat.html", "0.3536"
HZ_SLOTS = "hetzner_matrix-gpu_2024-12_price_slots.html"
HZ_SCRIPT = "hetzner_matrix-gpu_2026-03_script_prices.html"
SW_2019 = "scaleway_pricing_2019-05_p100_column.html"
SW_2020 = "scaleway_pricing_2020-06_month_hour.html"
SW_2023, SW_2023_3070 = "scaleway_pricing_2024-03_hour_approx_month.html", "0.98"
SW_GPU_2024 = "scaleway_pricing-gpu_2024-05_hour.html"
SW_GPU_2026 = "scaleway_pricing-gpu_2026-10_per_zone.html"
GC_OD, GC_OD_H100, GC_OD_H100_R12 = "gcore_pricing-ai_2025-03_on_demand_row.html", "22.04", "17.63"

TESTS = [test_civo_2024_single_price_column_through_the_live_normalizer, test_civo_2025_on_demand_column_is_the_live_parser,
         test_civo_rejects_mixed_currencies_and_unknown_rows,
         test_hetzner_2024_server_rendered_reads_only_excl_vat, test_hetzner_monthly_only_and_script_prices_are_not_read,
         test_hetzner_price_slots_and_fail_closed_without_a_rate,
         test_scaleway_2019_and_2020_render_s, test_scaleway_2023_hourly_with_approx_month_and_floor_prices_skipped,
         test_scaleway_gpu_pricing_2024_live_ids_and_spot_note, test_scaleway_gpu_pricing_2026_per_zone,
         test_gcore_2023_ai_gpu_flavor_tables, test_gcore_2025_on_demand_row_like_live,
         test_gcore_commitment_only_tables_are_not_read, test_archived_eur_rows_keep_their_fx_provenance_in_the_note]

if __name__ == "__main__":
    for t in TESTS:
        t()
        print(t.__name__, "ok")
