# Site ranking: where a small datacenter could run on spare power

OpenGrid ranks US counties by how well a small datacenter (about 1 to 50 MW) could run there on spare, cheap
electricity. It joins four datasets OpenGrid already holds: the physical sites (plants, capacity factors, retirements,
interconnection queues), the nodal and hub prices, the wasted energy (curtailment and gas flaring) and the EIA-930
balancing authority series. Nothing is stored: every read computes the ranking from those tables.

Kind: **derived** (every part is computed from observed data by the rules below). Code: `power/siting.py`. Reads:
`GET /v1/ops/power/sites/ranking` and `GET /v1/ops/power/sites/near` (admin only). The inputs are described in
[power markets](power-markets.md) (prices, nodal summaries, physical sites) and [wasted energy](power-waste.md).

## Who may see what

The ranking mixes public sources (EIA-860M, EIA-923, EIA-930, Queued Up 2026, ERCOT) with internal ones (SPP prices,
CAISO and SPP curtailment, North Dakota flaring). The whole read is therefore **internal**: admin-only, with
`Cache-Control: private, no-store`, outside the OpenAPI schema. It is never mixed into GPU prices, listings or any GPU
statistic.

## The grain: a county

Plants, queue requests and North Dakota flaring all carry a county. Priced nodes do not: neither ERCOT nor SPP
publishes coordinates or counties for them. The county is therefore the common grain. A county is ranked when it has
any signal: a plant in the EIA-860M inventory, an active queue request, or regulator flaring data.

County names are compared without "County", "Parish" or "Borough" and without case, as in `power/sites/reads.py`.

## The parts

Each row shows every part on its own. The score is shown beside them, never instead of them.

**A missing part is shown as missing (null), never as zero.** A zero is used only where the source covers the place
and reports nothing there. Examples: no planned retirement among a county's known plants is 0 MW; no active request
in Queued Up (which covers every US queue) is 0 MW queued; a North Dakota county with no flaring row flared 0.

### Spare generation

- **Idle MW**: the sum over the county's plants of operable non-storage nameplate x (1 - capacity factor), for the
  latest complete EIA-923 year (methodology: [physical sites](power-markets.md#physical-sites)). Only plants with a
  capacity factor count. Missing when no plant in the county has an EIA-923 row.
- **MW retiring in the next 36 months**: operable generators (operating, standby, out of service) whose planned
  retirement month falls from the current month to 36 months later. A planned retirement already in the past while the
  generator still operates is left out, because the plan has slipped. Storage counts here: a retiring battery frees a
  connection too.
- **Large rarely-running plants**: plants with at least 100 MW of non-storage nameplate and a capacity factor below
  0.10 in that year. Missing when no plant has a capacity factor.

### Price

The share of intervals priced strictly below $20/MWh and strictly below $0 over the window (default 90 days, at most
365), at the best location that can honestly be tied to the county. Real-time prices are used where they are stored,
day-ahead otherwise. The window ends at `until`, or by default at the latest day stored for that location, so two
rows can cover different days. Every row says its grain, the location, how it was chosen and its window.

| Grain | When |
|---|---|
| `node` | ERCOT only: a resource node tied to a plant in the county by the rule below, with prices in the window. When several are tied, the one with the largest share below $20 is used and all are listed |
| `zone` | ERCOT only: the county's tied nodes have no prices in the window; their ERCOT load zone (when they all share one) is used |
| `hub` | ERCOT: the hub average `HB_HUBAVG`. SPP: `SPPSOUTH_HUB`, SPP's reference hub (SPP's hubs are not tied to counties). CAISO: the trading hub of the county, only for counties listed in `CAISO_COUNTY_HUB`. MISO: the state's hub where MISO prices one (`MICHIGAN.HUB`, `ILLINOIS.HUB`, `INDIANA.HUB`, `TEXAS.HUB`, `MINN.HUB`, `MS.HUB`, `ARKANSAS.HUB`, `LOUISIANA.HUB`), else `INDIANA.HUB`. PJM and ISO-NE: their reference hubs, as `power/geo.py` maps them |
| `none` | NYISO and CAISO counties outside the table (no single reference hub), places outside an ISO, and locations with no prices stored in the window. The row says which |

The county's market is the ISO of the balancing authority holding the most operable nameplate among its plants. A
county with no operable plant takes its state's market, only where all of the state's load is in one ISO
(`power/geo.py`); otherwise it has none.

`CAISO_COUNTY_HUB` lists only counties that lie wholly on one side of Path 15 inside the area of one trading hub (NP15:
the San Francisco Bay Area, Sacramento and the northern Central Valley; SP15: Los Angeles, Orange, San Diego,
Riverside, San Bernardino, Ventura, Santa Barbara). Counties near the Path 15 and Path 26 boundaries (Monterey, San
Benito, Fresno, Kings, Tulare, Kern, San Luis Obispo, Merced, Madera) are left out on purpose: they get no price rather
than a guessed one. The table was written from the hubs' published definitions and not checked against a county map.

### The ERCOT node-to-county rule

A node is tied to a county only through a plant, and only by exact equality:

1. A node's keys are its substation code from ERCOT's Settlement Points List and its name before the first `_`
   (letters and digits only, upper case).
2. A plant's keys are its EIA-860M name with letters and digits only, and the same name without generic words
   ("Wind", "Solar", "Energy", "Plant", "Station", directions and so on; `GENERIC` in the code). Only Texas plants
   reporting to ERCOT count.
3. A key shorter than five characters, or itself a generic word, is refused: too many plants share it.
4. A node whose keys reach plants in more than one county is refused as ambiguous.
5. No prefix, fuzzy or distance matching. ERCOT's codes are abbreviations (Limestone's units sit at substation
   `LEG`), so a near match would tie a node to the wrong county without anyone noticing.

The rule reports its hit rate (resource nodes tied / resource nodes) and every refusal, in the read's `node_match`
meta. A resource node is one ERCOT types `RN`, `PCCRN`, `LCCRN` or `PUN`, or one with a substation in the catalogue.

**Measured hit rate.** On the test fixtures, 0 of the 4 real resource nodes are tied (their substations `COBSW`,
`TOMSW`, `AMOCOOIL` name no fixture plant), and 1 of 8 once four hand-built nodes are added (one tied, one ambiguous,
two refused as too short or generic). The hit rate on the full production catalogue has not been measured yet. Expect
it to be low: the rule trades coverage for being right. Because a tie needs an exact, unique, non-generic key, a
tied node is very likely at the named plant. The counties it misses fall back to their zone or hub, labelled as such.
`NODE_MATCH = False` turns the rule off: every ERCOT county then uses the hub.

### Wasted energy

- **Gas flared in the county**: North Dakota's county volumes (NDIC) for the latest year loaded, summed over the
  months loaded, with the derived energy (MCF x 0.1037 MWh, [wasted energy](power-waste.md#energy-derived)). Shown as
  average MW = MWh / (months x 730 hours). The World Bank cells carry no county, so they are used only by the point
  query. Counties outside North Dakota have no county-level flaring (null).
- **Curtailment** (labelled `regional`): the wind and solar curtailment of the county's ISO over the window, as
  average MW. It is the whole ISO's figure, so it does not tell one county from another in the same market. ERCOT's is
  the HSL-minus-output proxy and is marked `estimate`.

### Competition

- **Generation queued in the county**: MW of active requests, from one source. For an ERCOT county, ERCOT's monthly
  GIS report; otherwise Berkeley Lab's Queued Up. The two are never added, because Queued Up already includes ERCOT's
  queue. Every source's total is listed beside it.
- **Large loads**: no public source lists large-load interconnection requests by project, so this is null everywhere,
  with that reason.

### Grid

- **Balancing authority**: as above.
- **Net exporter**: EIA-930 net generation minus demand of that balancing authority, hour by hour where both are
  reported, over the last 12 months to the latest day stored. Positive means a net exporter. Null where the BA's
  series are not stored.

## The score

Each part is scaled to 0..1, multiplied by its weight, and the products are added:

| Part | Scaled value | Default weight |
|---|---|---|
| `idle` | idle MW / 200, capped at 1 | 0.30 |
| `price_below_20` | share of intervals below $20 | 0.25 |
| `retiring` | MW retiring in 36 months / 200, capped at 1 | 0.10 |
| `flaring` | average MW flared / 50, capped at 1 | 0.10 |
| `exporter` | 1 if a net exporter, else 0 | 0.10 |
| `rarely_running` | large rarely-running plants / 2, capped at 1 | 0.05 |
| `price_below_0` | share of intervals below $0 | 0.05 |
| `curtailment` | average MW curtailed in the ISO / 1,000, capped at 1 | 0.05 |
| `queue` | MW queued / 2,000, capped at 1 | -0.15 |

Why these weights:
- Spare generation already connected is the strongest sign a connection has room.
- The price is what the datacenter would pay.
- Retirements free connections, but plans slip.
- Flared gas is energy with no buyer, but it is gas, not electricity.
- A net-exporting balancing authority has power to spare on the margin.
- Curtailment is ISO-wide, so it stays small.
- Queued generation competes for the same connection capacity, so it counts against a county.

The caps (`SCALES`) are sized for a 1-50 MW load. For example, 200 idle MW is four times the largest load in scope,
so more idle capacity does not make a site better for it.

The positive weights add up to 1. A county with every part at its best and nothing queued scores 1.

**Missing parts add nothing and are not re-weighted onto the others.** A county known only by its price therefore
cannot outrank one known on every count. `coverage` is the share of the total absolute weight that had data; ties
are broken by coverage.

The weights are `WEIGHTS` in `power/siting.py`. The reads accept other weights, merged over the defaults:
`?weights=idle:0.4,queue:-0.3` on the route, or `weights={"idle": 0.4}` in Python. An unknown name is refused (422).

Each row carries a plain sentence (`why`): where the county ranks, the three parts that lifted it most, what held it
back, and which parts are missing.

**Worked example (test data, `tests/test_power_siting.py`).** Nolan County, Texas, with a hand-built 150 MW wind
plant that ran at a 0.30 capacity factor and a tied node priced over three days:
- idle MW = 150 x (1 - 0.30) = 105, scaled 105 / 200 = 0.525;
- 144 of 288 real-time intervals were below $20 (0.5), and 48 were below $0 (0.167);
- nothing is retiring and nothing is queued (known zeros);
- ERCO was a net importer on the one EIA-930 day loaded (0);
- flaring and curtailment are missing.

Score = 0.30 x 0.525 + 0.25 x 0.5 + 0.05 x 0.167 + 0.10 x 0 = 0.291. Coverage = (1.15 - 0.10 - 0.05) / 1.15 = 87%.

## The point query

`near(lat, lon, km)` gives the same parts for the area within `km` of a point:
- the plants within the radius (idle MW, retirements, rarely-running plants);
- the flaring within the radius, per source, never summed across sources (a North Dakota section and a World Bank
  cell can be the same flares). North Dakota is used where present, else the World Bank (a whole year);
- the price of a node tied to a plant within the radius, else the county's location as above;
- the county's queue, where the county is the county of the nearest plant, labelled as such (no county boundaries
  are held);
- the balancing authority of the plants within the radius, its export balance and the ISO's curtailment.

## Reads

| Endpoint (admin only) | What |
|---|---|
| `/v1/ops/power/sites/ranking` | counties ranked; `?state iso min_idle_mw days weights until limit`. Meta: the weights, scales, the node rule's hit rate and refusals count, the price grains used |
| `/v1/ops/power/sites/near` | the parts around a point; `?lat lon km days weights until` |

Python: `power.siting.ranking(...)`, `near(...)`, `match_nodes()`, `score(raw, weights)`. They return plain dicts.

## Limits

- Counties are the grain because nodes have no location. A county's price at hub grain is the market's level, not
  the local price.
- Capacity factors are annual. A plant idle in spring and busy in summer looks the same as one idle all year.
- Queued MW counts requests, many of which will be withdrawn.
- No large-load queue is public, so competing loads are not counted.
- Flaring by county covers North Dakota only.
