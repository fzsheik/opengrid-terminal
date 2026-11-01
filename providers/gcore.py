"""Gcore GPU Cloud: the public AI pricing page, no key; EUR list prices converted to USD at the ECB rate (fx.py).

gcore.com/pricing/ai is server-rendered (Angular SSR). Two of its tables are read, as printed:

    Bare Metal GPU Clusters   one column per server: "8×H100 SXM", a "Get started" / "Contact sales" button,
                              "€21.82 /hr €2.73 per GPU", then VRAM, RAM and vCPU rows for the whole server.
    Virtual GPU               one row per VM size: "1×H100", "80 GB", "24 vCPU / 232 GB RAM", "1250 GB", "€2.82".

Read: every bare-metal column with a "Get started" button (self-serve) and every Virtual GPU row. The hourly
price is the whole server / VM, in EUR, excluding VAT ("Prices do not include VAT"); the per-GPU figure Gcore prints
for bare metal must equal price / GPUs to the cent or the column is skipped (logged). Converted to USD at the ECB
EUR->USD reference rate in force when the page was fetched; no usable rate: no listing (fail closed, logged).

Skipped: "Contact sales" columns (8×B300 shows €48.00/hr but is sold through sales; GB300 NVL72 shows no price),
and the "Everywhere Inference" table (a per-minute serverless inference product whose memory column is not
per-GPU consistent: 4×A100 "240 GB", 8×A100 "960 GB"). The page's pricing-model switch renders a single model
server-side, the one stored here: hourly rates with no term stated ("Pay only while powered on" for Virtual GPU).

GPU names. Bare metal: the column's model plus the per-GPU VRAM (server VRAM / GPUs): "H100 SXM 80GB",
"A100 SXM 80GB", "L40S PCIe 48GB", "H200 SXM 141GB". Virtual GPU rows say only "H100" and "80 GB" per GPU, not SXM
or PCIe: "H100 80GB" stays unmapped (never guessed, although the clusters carry InfiniBand).

No region is printed (one price list for every GPU Cloud region) and no stock signal: region, available and capacity
stay None.

Terms (checked 2026-11-01): robots.txt allows /pricing/ai for every agent and sets Content-Signal "ai-train=no,
search=yes, ai-input=no" (no AI training on, or AI answers from, its pages: OpenGrid reads prices into a market
table, so flagged in the report, not a block). Gcore's legal page (Master Services Agreement, Acceptable Use Policy)
has no clause on automated reading of the public site. A list price page: one request an hour.

Gcore is a general-purpose cloud and CDN, so it never counts toward neocloud indices.
"""

import html as _html
import logging
import re
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx

import canonical
import fx
from models import ComputeListing, RawResponse
from providers.base import PollingPolicy, Provider, find_endpoint, get_page

if TYPE_CHECKING:
    from tables import RawSnapshot

log = logging.getLogger(__name__)

BASE_URL = "https://gcore.com"
ENDPOINT = "/pricing/ai"

_TABLE = re.compile(r"<table\b.*?</table>", re.S)
_ROW = re.compile(r"<tr\b.*?</tr>", re.S)
_CELL = re.compile(r"<t[hd]\b.*?</t[hd]>", re.S)
_NOISE = re.compile(r"<style.*?</style>|<script.*?</script>|<svg.*?</svg>", re.S)
_EUR = r"€\s*(\d+(?:,\d{3})*(?:\.\d+)?)"
_BM_PRICE = re.compile(rf"^{_EUR}\s*/hr\s+{_EUR}\s+per GPU$")
_VM_PRICE = re.compile(rf"^{_EUR}$")
_SHAPE = re.compile(r"^(?P<count>\d+)\s*×\s*(?P<model>[A-Za-z0-9][A-Za-z0-9 ]*?)$")
_INT = re.compile(r"^\d+(?:,\d{3})*$")
_CPU_RAM = re.compile(r"^(?P<vcpu>\d+)\s*vCPU\s*/\s*(?P<ram>\d+)\s*GB RAM$")
_GB = re.compile(r"^(?P<gb>\d+(?:\.\d+)?)\s*GB$")


def _text(fragment: str) -> str:
    s = _NOISE.sub(" ", fragment)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", _html.unescape(s)).strip()


def _rows(table: str) -> list[list[str]]:
    return [[_text(c.group(0)) for c in _CELL.finditer(r.group(0))] for r in _ROW.finditer(table)]


def extract(page: str) -> dict:
    """The bare-metal table (columns) and the Virtual GPU table (rows), as printed.

    Raises when either table is missing or has no data, so a redesigned page fails the poll instead of recording
    nothing.
    """
    bare_metal, virtual = None, None
    for m in _TABLE.finditer(page):
        rows = _rows(m.group(0))
        if not rows:
            continue
        labels = {r[0]: r[1:] for r in rows[1:] if r}
        header = rows[0]
        if "Price" in labels and "GPU" in labels and bare_metal is None:
            bare_metal = {"header": header[1:], "rows": labels}
        elif header[:5] == ["GPU Configuration", "GPU Memory", "CPU/RAM", "Storage", "Cost per hour"] and virtual is None:
            virtual = [r for r in rows[1:] if r]
    if bare_metal is None:
        raise ValueError("gcore: bare-metal GPU table (Price / GPU rows) not found - page reshaped")
    if not virtual:
        raise ValueError("gcore: Virtual GPU table not found or empty - page reshaped")
    width = len(bare_metal["header"])
    if width == 0 or any(len(bare_metal["rows"].get(k) or []) != width for k in ("Price", "GPU")):
        raise ValueError("gcore: bare-metal table columns do not line up - page reshaped")
    return {"bare_metal": bare_metal, "virtual": virtual}


def _eur(s: str) -> Decimal:
    return Decimal(s.replace(",", ""))


def _int(cell: str | None) -> int | None:
    cell = (cell or "").strip()
    return int(cell.replace(",", "")) if _INT.match(cell) else None


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def raw_gpu_name(model: str, vram_per_gpu: int | None) -> str:
    """'H100 SXM' + 80 -> 'H100 SXM 80GB' (the per-GPU VRAM is always appended when known)."""
    return f"{model} {vram_per_gpu}GB" if vram_per_gpu else model


def eur_offers(snapshot_payload: dict) -> list[dict]:
    """Every readable offer on the stored page, in EUR. Pure; unreadable columns / rows are skipped (logged)."""
    out = []
    bm = snapshot_payload["bare_metal"]
    rows = bm["rows"]
    for i, head in enumerate(bm["header"]):
        cell = lambda label: (rows.get(label) or [None] * len(bm["header"]))[i]  # noqa: E731
        shape, price = _SHAPE.match(cell("GPU") or ""), _BM_PRICE.match(cell("Price") or "")
        if not head.endswith("Get started"):
            continue  # "Contact sales": not self-serve (B300, GB300)
        if shape is None or price is None:
            log.warning("gcore: bare-metal column %r unreadable (%r, %r); skipped", head, cell("GPU"), cell("Price"))
            continue
        count = int(shape["count"])
        total, per_gpu = _eur(price.group(1)), _eur(price.group(2))
        if count <= 0 or abs(total / count - per_gpu) > Decimal("0.01"):
            log.warning("gcore: %s per-GPU €%s is not €%s / %d; skipped", cell("GPU"), per_gpu, total, count)
            continue
        vram = _int(cell("VRAM, GB"))
        out.append({
            "kind": "bare-metal", "shape": cell("GPU"), "model": shape["model"].strip(), "gpu_count": count,
            "vram_per_gpu": vram // count if vram and vram % count == 0 else None,
            "eur_per_instance_hour": total, "vcpu": _int(cell("vCPUs")), "ram_gb": _int(cell("RAM, GB")),
            "storage": cell("Storage"),
        })
    for r in snapshot_payload["virtual"]:
        if len(r) < 5:
            log.warning("gcore: Virtual GPU row %r too short; skipped", r)
            continue
        shape, mem, cpu, disk, price = _SHAPE.match(r[0]), _GB.match(r[1]), _CPU_RAM.match(r[2]), _GB.match(r[3]), \
            _VM_PRICE.match(r[4])
        if shape is None or mem is None or price is None:
            log.warning("gcore: Virtual GPU row %r unreadable; skipped", r)
            continue
        count, total_mem = int(shape["count"]), Decimal(mem["gb"])
        out.append({
            "kind": "vm", "shape": r[0], "model": shape["model"].strip(), "gpu_count": count,
            "vram_per_gpu": int(total_mem / count) if count and total_mem % count == 0 else None,
            "eur_per_instance_hour": _eur(price.group(1)),
            "vcpu": int(cpu["vcpu"]) if cpu else None, "ram_gb": int(cpu["ram"]) if cpu else None,
            "storage_gb": float(disk["gb"]) if disk else None,
        })
    return out


class GcoreProvider(Provider):
    name = "gcore"
    # One page load; list prices change rarely.
    polling = PollingPolicy(interval_seconds=3600, timeout_seconds=45)

    def __init__(self, client: httpx.AsyncClient | None = None):
        super().__init__(
            client
            or httpx.AsyncClient(
                base_url=BASE_URL,
                headers={"user-agent": "opengrid-terminal/0.1"},
                timeout=45,
                follow_redirects=True,
            )
        )

    async def fetch(self) -> list[RawResponse]:
        return [await get_page(self, ENDPOINT, extract)]

    @classmethod
    def normalize(cls, by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ComputeListing]:
        """One self-serve bare-metal server or Virtual GPU size becomes one USD listing."""
        rows = find_endpoint(by_endpoint, ENDPOINT)
        if not rows:
            return []
        snapshot = rows[0]
        body = snapshot.payload
        if not isinstance(body, dict) or "bare_metal" not in body or "virtual" not in body:
            raise ValueError("gcore: stored page has no bare_metal / virtual tables - reshaped")
        offers = eur_offers(body)
        if not offers:
            raise ValueError("gcore: no readable GPU offer on the page - reshaped")
        rate = fx.usd_rate("EUR", snapshot.fetched_at, context=f"gcore page fetched {snapshot.fetched_at:%Y-%m-%d %H:%M}Z")
        if rate is None:
            return []  # fail closed: nothing is converted without a fresh ECB rate (fx.usd_rate logged why)
        listings = []
        for o in offers:
            name = raw_gpu_name(o["model"], o["vram_per_gpu"])
            sku = f"{o['kind']}:{o['shape'].replace('×', 'x')}"
            listings.append(ComputeListing(
                provider=cls.name,
                sku=sku,
                listing_id=f"{o['kind']}:{o['gpu_count']}x-{_slug(o['model'])}",
                raw_gpu_name=name,
                canonical_gpu_name=canonical.canonical_gpu_name(name),
                gpu_count=o["gpu_count"],
                region=None,  # one price list for every GPU Cloud region
                country=None,
                **fx.convert(o["eur_per_instance_hour"], o["gpu_count"], "EUR", rate),
                market_type="on_demand",
                provider_tier=o["kind"],
                interruptible=False,
                available=None,  # the page says nothing about stock
                capacity=None,
                capacity_unit=None,
                vcpu=o["vcpu"],
                ram_gb=float(o["ram_gb"]) if o["ram_gb"] is not None else None,
                storage_gb=o.get("storage_gb"),
                observed_at=snapshot.fetched_at,
            ))
        return listings
