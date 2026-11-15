# Bitcoin miners turned GPU clouds: what we can price, what we can route to

Written 2026-11-15 as a handoff to another session. Research is done (below); the work items are in section 3.
House rules: commit and push straight to `main` (no one-off branches), deploy with
`railway up --service opengrid-terminal --ci`, run tests with `.venv/bin/python tests/run_all.py`.

## 1. Why this matters

Bitcoin miners hold what AI data centers lack: large, already-connected power. Many are turning sites into GPU
hosting (see the power context in news: ~181 GW of behind-the-meter gas proposed for data centers, BNEF, Nov 2026).
For OpenGrid that is new GPU supply (prices, capacity) and a set of sales prospects (they are pricing capacity for the
first time and have no market data to price it against).

## 2. Findings (checked 2026-11-15)

| Company | What it sells | Public prices? | Routable? |
|---|---|---|---|
| **Bitdeer** (Bitdeer AI Cloud, bitdeer.ai) | Own GPU cloud: H100, H200, B200, B300 (VMs, 1–8 GPUs), GB200 / GB300 NVL72 (bare metal, 4–72 GPUs) | **Yes**: keyless JSON, `GET https://www.bitdeer.ai/api/gpu-pricing` | **Maybe**: self-serve console, pay-as-you-go; developers.bitdeer.ai lists "Instance APIs" (Server, Image, Snapshot) but the endpoints/auth are not public. Needs an account to confirm. |
| Crusoe | Own GPU cloud | Yes | **Already tracked** (`providers/crusoe.py`); began as a flare-gas crypto miner |
| IREN (iren.com) | GPU cloud H100–GB300 | No: every GPU says "Contact sales" | No |
| Hut 8 (Highrise AI, highrise.ai) | GPU cloud H100, H200, B300, GB200 | No: "Reserved pricing available on request"; mentions pay-as-you-go + a console login, no rates | No (sales relationship needed) |
| HIVE (BUZZ HPC) | Multi-year GPU cloud contracts ($30M–$350M) | No | No |
| Bit Digital (WhiteFiber) | GPU clusters | No: "Contact us to scope pricing" | No |
| Core Scientific, Galaxy (Helios), Cipher, TeraWulf | Powered shell / colocation leased to CoreWeave, Fluidstack, AWS | n/a: they do not rent GPUs | No; their capacity reaches the market through tenants we already track (CoreWeave) |

Today OpenGrid has none of these as a price provider. IREN and Applied Digital exist only in the news classifier
(`news/classify.py`, the `_P` list near line 182); Hut 8 and the others are not in the codebase at all.

### Bitdeer's price feed

Saved sample: `tests/fixtures_providers/bitdeer_gpu_pricing_2026-11-15.json` (the real, unauthenticated answer).
Cloudflare-fronted, `cache-control: no-store`; robots.txt allows `/` (only email-protection and one docs path are
disallowed). Shape:

```
{"code": 2000, "data": [
  {"model": "NVIDIA H100", "spec": "80GB VRAM", "type": "gpu", "machineType": "vm", "price": 2.9,
   "details": [{"cards": 1, "price": 2.9, "year1": {"price": "2.46500", "save": "3810.60"},
                "year3": {"price": "2.46500", "save": "11431.80"}}, {"cards": 2, ...}, ...]},
  ...
  {"model": "General Compute", "type": "cpu", "details": [{"cors": 1, "memorySize": 4096, ...}]}   <- CPU, skip
]}
```

Prices on 2026-11-15 (per GPU-hour, 1-card row; GB rows divided by 4 cards):

| GPU | machineType | On-demand | 1-year | 3-year | OpenGrid market median (neoclouds) |
|---|---|---|---|---|---|
| H100 80GB | vm | 2.90 | 2.465 | 2.465 | 3.74 |
| H200 141GB | vm | 3.56 | 3.026 | 3.026 | 5.40 |
| B200 180GB | vm | 5.50 | 4.675 | 4.675 | 8.50 |
| B300 288GB | vm | 9.00 | 7.65 | 7.65 | — |
| GB200 (node of 4) | bm | 27.764 / node = 6.94 | 6.04 | 5.00 | — |
| GB300 (node of 4) | bm | 41.646 / node = 10.41 | 9.06 | 7.50 | — |

Quirks to handle:
- `details[].price` is the **instance** price for that card count; `year1/year3.price` are strings, also per instance.
  Do not assume linear: B300's 2-card 1-year price is 16.74 (not 2 × 7.65) and its 3-year is 15.66. Read every row.
- GB200 / GB300 rows start at 4 cards and go to 72 (`machineType: "bm"`), so they are per-node / per-rack prices.
- `save` is a dollar amount over the term; ignore it (derivable).
- No region, stock or availability in this feed. `/api/resource/v1/ListRegions` and `ListZones` exist in the site JS
  (may need login; check). Without them, listings are region-less and `available=None` (like a price page).
- `code != 2000`, a missing `data` list, or a reshaped row must raise in normalize (the house rule: a reshape fails
  loudly, never silently lists nothing).

## 3. Work items

### A. Terms check (do first)
Read Bitdeer AI's terms of service / acceptable use for anything on scraping, automated access or republishing prices.
- Allowed or silent: public source (default).
- Forbids republishing: add an `InternalSource` entry in `provider_meta.py` (`INTERNAL_SOURCES`, like Fireworks /
  DeepInfra) so it is used for routing and recommendations but never in a public page, index, fixing or export.
Record the finding (URL + quote + date) in the provider's module docstring.

### B. Bitdeer price provider
Template: `providers/lium.py` / `providers/hotaisle.py` (small keyless provider). Steps:
1. `providers/bitdeer.py`: fetch the one endpoint (store the body raw, trimmed to GPU rows); `normalize()` emits one
   on-demand listing per (model, card count) with `listing_id` like `{model-slug}:{cards}`, `market_type="on_demand"`,
   `price_per_instance_hour`, `price_per_gpu_hour`, `gpu_count`, `raw_gpu_name` from `model` + `spec`.
2. Reserved terms: add a reader in `reserved.py` (`SOURCES` / `READERS`, like the AWS Savings Plan reader added
   2026-11-13) for `year1` and `year3`; founders' rule holds: reserved prices are never listings.
3. Register it: `providers/__init__.py`, `fetch.py` (`build_fetchers`), `provider_meta.py` (`ProviderMeta("bitdeer",
   "Bitdeer AI", "neocloud", "public_api", "www.bitdeer.ai", "https://www.bitdeer.ai", "Bitcoin miner's GPU cloud")`),
   `mapping.py` (a `BITDEER = ProviderMapping(...)` mapping each model to its canonical GPU; add it to the list near
   line 2259). Polling: list-price page cadence (look at how hotaisle/baseten are set; hourly is plenty).
4. Tests: `tests/test_provider_bitdeer.py` on the saved fixture: per-GPU math for every card count, the B300
   non-linear rows, GB200 per-node rows, CPU rows skipped, reshape raises, reserved rows (1y/3y) built, one
   scratch-DB pipeline test like `tests/test_provider_lium.py::test_pipeline` (listed, nothing quarantined).
5. Docs: a row in `CAPABILITIES.md`, an entry in `docs/UPDATES.md`, and `methodology/reserved-prices.md` if reserved
   rows are added.
6. Deploy and verify in prod (read-only query): listings fresh, endpoint ok, the H100 1-GPU listing at 2.90.

### C. Routing (blocked on a Bitdeer account)
Needs the founders to open a Bitdeer AI account and create an API key. Then: read developers.bitdeer.ai GPU Cloud
"Authentication" and "Instance APIs" pages; if create / list / stop / delete exist, write
`routing/adapters/bitdeer.py` against `routing/adapters/base.py` (look at `lambda_labs.py` / `verda.py`), add its
capability row in `routing/capabilities.py`, and run the validation cycle in `methodology/execution-safety.md`
(launch → observed running → termination confirmed → cost reconciled) before any flag is enabled. Never enable
SUPERVISED/LIVE without the founders.

### D. News: track the miners as capacity, not prices
Add to the `_P` list in `news/classify.py` (same tuple shape as the `iren` row):
Hut 8 (`\bHut ?8\b`, also Highrise AI), Core Scientific, Galaxy Digital (and "Helios" only with Galaxy in context),
Cipher Mining, TeraWulf, Bitdeer, HIVE Digital (and BUZZ HPC), Bit Digital (and WhiteFiber), plus any of CleanSpark,
Riot, MARA if they announce AI hosting. Class: `neocloud` for the ones renting GPUs (Bitdeer, Hut 8, HIVE, Bit
Digital), and decide a class for pure landlords (Core Scientific, Galaxy, Cipher, TeraWulf), e.g. a new
`datacenter` value if `provider_meta` allows it, otherwise leave `None`. Watch for false positives ("Galaxy" alone,
"Riot" alone): require the full name or context. Add classifier tests with real headlines.

### E. Not now
IREN, Hut 8 (Highrise), HIVE, WhiteFiber publish no prices: nothing to build. Re-check each quarter; if one adds a
public price page or API, it becomes a normal provider build. They are sales prospects (see the GPU price brief
built 2026-11-14) rather than data sources.

## 4. Sources
- Bitdeer pricing page: https://www.bitdeer.ai/en/pricing/gpu-compute (feed: https://www.bitdeer.ai/api/gpu-pricing)
- Bitdeer developer docs: https://developers.bitdeer.ai/ (API reference: https://developers.bitdeer.ai/en/docs/api)
- IREN AI Cloud: https://iren.com/solutions/gpu-cloud/ai-cloud
- Highrise AI (Hut 8): https://www.highrise.ai/gpus, https://www.hut8.com/compute
- HIVE BUZZ HPC contracts: https://www.stocktitan.net/news/HIVE/hive-s-buzz-hpc-signs-350-million-ai-cloud-services-agreement-with-4z2k7i6u0i6m.html
- WhiteFiber pricing: https://www.whitefiber.com/gpu-pricing
- Miners moving to AI (overview, verify against filings): https://www.amcompute.com/bitcoin-miners-ai
