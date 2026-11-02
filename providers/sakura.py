"""Sakura Internet, Koukaryoku DOK (高火力 DOK): GPU containers billed per second, from its public page; JPY converted at
the ECB cross rate. No key.

ai.sakura.ad.jp/gpu/koukaryoku-dok/ is server-rendered (Next.js). Its 料金・仕様 section (id "price-spec") has a price
table, one column per plan:

    料金            <strong>0.28</strong> 円/秒   1時間あたり 1,008円     (JPY per second; per hour)
    最低利用時間     60 秒                                              (minimum billed time per task)

and a spec table in the same column order (プラン名 h100-80gb, GPUシリーズ "NVIDIA H100 x8", GPUメモリー "80GBx8",
vCPUコア数, メモリー). Plans on 2026-11-01: V100 (v100-32gb, ¥0.016/s, ending 2027-03-31), H100 (h100-80gb, ¥0.28/s),
H100 8-GPU dedicated, beta (h100-8gpu-80gb, ¥0.83/s = ¥2,988/h for the whole 8-GPU box).

Price read: the per-HOUR figure printed (1時間あたり), for the whole plan (the 8-GPU plan is per box: 2,988 / 8 per
GPU), checked against per-second x 3600; JPY converted to USD at the ECB JPY->USD CROSS rate in force at fetch
(fx.usd_rate: (EUR->USD) / (EUR->JPY), one ECB date; fail closed without a fresh pair).

TAX: the page says 表示価格は税込です (prices shown include tax) and does not state the rate, so the price is kept
tax-INCLUSIVE as printed (not grossed down by an assumed rate) and every listing is marked provider_tier
"tax_inclusive". It is therefore not a like-for-like pre-tax list price.

GPU names: the page states neither SXM nor PCIe for the H100, so "NVIDIA H100 80GB" stays unmapped (canonical.py's
rule: form factor not stated, never guessed), as does "NVIDIA V100 32GB" (no canonical 32 GB V100). They are stored
and shown but cast no vote in the market until Sakura states the board. Region: the page names none; country JP.

Terms (checked 2026-11-01): ai.sakura.ad.jp has no robots.txt (404); the site's footer links (privacy, security,
service agreement, brand guidelines) carry no clause on automated reading of public pages. One request an hour.
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

BASE_URL = "https://ai.sakura.ad.jp"
ENDPOINT = "/gpu/koukaryoku-dok/"
CURRENCY = "JPY"
TAX_INCLUSIVE = "表示価格は税込です"
TIER = "tax_inclusive"

_SECTION = re.compile(r'<section[^>]*id="price-spec".*?</section>\s*</div>\s*</section>|<section[^>]*id="price-spec".*', re.S)
_TABLE = re.compile(r"<table\b.*?</table>", re.S)
_TR = re.compile(r"<tr>(?P<v>.*?)</tr>", re.S)
_CELL = re.compile(r"<t[hd]\b[^>]*>(?P<v>.*?)</t[hd]>", re.S)
_PER_SEC = re.compile(r"(?P<v>\d+(?:\.\d+)?)\s*円/秒")
_PER_HOUR = re.compile(r"1時間あたり\s*(?P<v>\d[\d,]*(?:\.\d+)?)\s*円")
_GPUS = re.compile(r"^(?P<model>NVIDIA [A-Z0-9]+)(?:\s*x\s*(?P<n>\d+))?$")
_MEM = re.compile(r"^(?P<gb>\d+)\s*GB")
_NUM = re.compile(r"^(?P<v>\d+)")


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def _rows(table: str) -> list[list[str]]:
    return [[_text(c["v"]) for c in _CELL.finditer(tr["v"])] for tr in _TR.finditer(table)]


def extract(page: str) -> dict:
    """Both tables of the price-spec section as printed, plus the tax note. Raises when they are not found."""
    m = re.search(r'id="price-spec"', page)
    if not m:
        raise ValueError("sakura: no price-spec section - page reshaped")
    section = page[m.start():]
    tables = _TABLE.findall(section)[:2]
    if len(tables) < 2:
        raise ValueError("sakura: price or spec table missing - page reshaped")
    price, spec = _rows(tables[0]), _rows(tables[1])
    if len(price) < 2 or not any(r and r[0] == "プラン名" for r in spec):
        raise ValueError("sakura: price rows or plan names missing - page reshaped")
    end = section.find("</section>", section.find(tables[1]))
    notes = _text(section[: end if end > 0 else len(section)])
    return {"price": price, "spec": spec, "tax_inclusive": TAX_INCLUSIVE in notes}


def plans(body: dict) -> list[dict]:
    """Column-wise plans: plan id, GPUs, GPU memory, JPY/s and JPY/h, minimum seconds, vCPU, RAM."""
    head = body["price"][0][1:]
    rows = {r[0]: r[1:] for r in body["price"][1:] if r}
    spec = {r[0]: r[1:] for r in body["spec"] if r}
    fee = next((v for k, v in rows.items() if k == "料金"), None)
    minimum = next((v for k, v in rows.items() if k.startswith("最低利用時間")), [None] * len(head))
    if fee is None or "プラン名" not in spec or len(fee) != len(head):
        raise ValueError("sakura: 料金 row or plan names missing - reshaped")
    out = []
    for i, label in enumerate(head):
        cell = fee[i]
        sec, hour = _PER_SEC.search(cell), _PER_HOUR.search(cell)
        gpus = _GPUS.match(spec.get("GPUシリーズ", [""] * len(head))[i])
        mem = _MEM.match(spec.get("GPUメモリー", [""] * len(head))[i])
        if not (sec and hour and gpus and mem):
            log.warning("sakura: plan %r unreadable (%r); skipped", label, cell)
            continue
        per_sec, per_hour = Decimal(sec["v"]), Decimal(hour["v"].replace(",", ""))
        if abs(per_sec * 3600 - per_hour) > Decimal("1"):
            log.warning("sakura: plan %r per-second %s x 3600 != per-hour %s; skipped", label, per_sec, per_hour)
            continue
        vcpu = _NUM.match(spec.get("vCPUコア数", [""] * len(head))[i])
        ram = _MEM.match(spec.get("メモリー", [""] * len(head))[i])
        mn = _NUM.match(minimum[i] or "") if minimum[i] else None
        out.append({
            "label": label,
            "plan": spec["プラン名"][i],
            "gpu_count": int(gpus["n"] or 1),
            "gpu_name": f"{gpus['model']} {mem['gb']}GB",
            "jpy_per_second": per_sec,
            "jpy_per_hour": per_hour,
            "minimum_seconds": int(mn["v"]) if mn else None,
            "vcpu": int(vcpu["v"]) if vcpu else None,
            "ram_gb": float(ram["gb"]) if ram else None,
            "beta": "β版" in label,
        })
    return out


class SakuraProvider(Provider):
    name = "sakura"
    polling = PollingPolicy(interval_seconds=3600, timeout_seconds=45)

    def __init__(self, client: httpx.AsyncClient | None = None):
        super().__init__(
            client
            or httpx.AsyncClient(base_url=BASE_URL, headers={"user-agent": "opengrid-terminal/0.1"}, timeout=45,
                                 follow_redirects=True)
        )

    async def fetch(self) -> list[RawResponse]:
        return [await get_page(self, ENDPOINT, extract)]

    @classmethod
    def normalize(cls, by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ComputeListing]:
        """One DOK plan becomes one USD listing at its tax-inclusive hourly rate."""
        rows = find_endpoint(by_endpoint, ENDPOINT)
        if not rows:
            return []
        snapshot = rows[0]
        body = snapshot.payload
        if not isinstance(body, dict) or not isinstance(body.get("price"), list):
            raise ValueError("sakura: stored page has no price table - reshaped")
        if not body.get("tax_inclusive"):
            # the tax note is what TIER asserts; without it the basis of the price is unknown
            raise ValueError("sakura: the tax-inclusive note is gone; price basis unknown - reshaped")
        offers = plans(body)
        if not offers:
            raise ValueError("sakura: no readable plan - reshaped")
        rate = fx.usd_rate(CURRENCY, snapshot.fetched_at, context=f"sakura page fetched {snapshot.fetched_at:%Y-%m-%d %H:%M}Z")
        if rate is None:
            return []  # fail closed: nothing is converted without a fresh ECB cross rate
        return [ComputeListing(
            provider=cls.name,
            sku=o["plan"],
            listing_id=o["plan"],
            raw_gpu_name=o["gpu_name"],
            canonical_gpu_name=canonical.canonical_gpu_name(o["gpu_name"]),
            gpu_count=o["gpu_count"],
            region=None,
            country="JP",
            **fx.convert(o["jpy_per_hour"], o["gpu_count"], CURRENCY, rate),
            market_type="on_demand",
            provider_tier=TIER,
            interruptible=False,
            available=None,
            capacity=None,
            capacity_unit=None,
            vcpu=o["vcpu"],
            ram_gb=o["ram_gb"],
            storage_gb=None,
            observed_at=snapshot.fetched_at,
        ) for o in offers]
