# GPU families and variants

A GPU family ("H100") is a name for a set of canonical GPUs that are different products; OpenGrid lists
the variants side by side and never merges them into one price.

Code: `families.py` (single source of truth, built from `news/classify.py`), `api/families.py`.
Endpoints: `GET /v1/families`, `GET /v1/families/{family}`, `GET /v1/news/families`.

## Families and variants

A **variant** is one canonical GPU name from `canonical.py`, e.g. `NVIDIA H100 80GB SXM5` (slug
`h100-80gb-sxm5`). Variants of one model differ in memory size, memory bandwidth, form factor (SXM vs
PCIe vs NVL), interconnect and power, so their prices are not interchangeable: an H100 PCIe is not a
cheap H100 SXM. A **family** is the set of variants a model name covers:

| kind | example | variants |
|---|---|---|
| `model` | `H100` (slug `h100`) | every canonical `NVIDIA H100 ...` name |
| `architecture` | `Blackwell`, `Hopper` | the datacenter / cloud parts of that generation (`member_families`) |

The variant lists come from the news classifier's hand-maintained table (`news/classify.py`
`FAMILIES` / `ARCHITECTURES`): a regular expression over canonical names, MIG slices excluded. The same
table decides which family a news item mentions, so news and markets agree on what "H100" means. A
family is listed only when it has at least one canonical variant (`GET /v1/news/families` also lists
the families the classifier recognises without a variant yet, with `tracked: false`).

Family slugs never collide with GPU slugs (checked by `tests/test_followup.py`): `h100` is a family,
`h100-80gb-sxm5` is a GPU. When a value is both, the canonical GPU wins.

## What a family response contains

`GET /v1/families/{family}` returns, for each variant: its current `low` / `median` / `high` (one vote
per provider, as in [dispersion](/methodology/dispersion)), `providers`, `available_listings`, its own
index (`index_id`, `index_level`) and `change_24h` from that index (null with a `reason` when the index
is unpublished or its history does not cover 24h), and `links` to the variant's market, history,
context, regions and best-execution pages.

`cheapest_variant_now` names the variant with the lowest current price. It is a **cross-variant fact**
("the cheapest thing called H100 right now is the PCIe variant at $x"), not a family price, and it is
labelled so. There is no family median, family index level or family history: those would merge
different products. The indices module's equal-weight composite over the same variants
(`composite_index`, e.g. `h100-class`) is a separate, explicitly defined index (see
[indices](/methodology/indices)).

## Family names in GPU paths

`/v1/markets/{gpu}`, `/v1/history/{gpu}`, `/v1/gpus/{gpu}`, `/v1/markets/{gpu}/context` and
`/v1/markets/{gpu}/regions` accept a family name: they answer HTTP 200 with the family payload above,
`meta.kind = "family"`, `meta.resolved_as = "family"`, `meta.endpoint` (which per-variant link to
follow) — never a merged price. `/v1/best/{family}` ranks each variant separately and returns each
variant's top candidates in `by_variant[]`. Routing accepts a family only with `allow_variants: true`
(see [routing](/methodology/routing)).
