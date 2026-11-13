"""The OpenAPI document behind the public customer docs (docs-site/, docs.tryopengrid.com).

It is the Data API's own schema (main.data_api_openapi) made fit to publish:
    * only what a customer may call: Data API GETs plus the read-only POSTs in PUBLIC_POST (no product-event,
      news-refresh or other operator writes that share a Data API area);
    * short sidebar titles and one tag per area, in the order of docs/API_CONTRACT.md section 7;
    * the Bearer key and the optional OpenGrid-Version header on every operation;
    * every 200 described as the real envelope, `data` typed from the frozen contract snapshot
      (tests/contracts/<latest>.json: the field paths and JSON types the contract test enforces), and every error
      as the one error shape.

The docs build reads the committed files; tests/test_docs_site.py fails when they drift from the code.

Write them:  .venv/bin/python -m api.docs_openapi
"""

import json
import os
import re
from pathlib import Path

os.environ.setdefault("OPENGRID_NO_JOBS", "1")

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs-site" / "openapi.json"
DATA_HOST = "data.tryopengrid.com"
PUBLIC_POST = ("/v1/costs/estimate", "/v1/costs/rl-estimate")

# First path segment -> sidebar group, in reading order.
TAGS = {
    "gpus": "Reference", "families": "Reference", "providers": "Reference", "regions": "Reference",
    "markets": "Markets", "listings": "Listings", "reserved": "Reserved",
    "indices": "Indices", "fixings": "Fixings",
    "capacity": "Capacity", "costs": "Costs", "fx": "FX", "reliability": "Reliability",
    "events": "Events", "news": "News", "timeline": "News", "macro": "Macro",
    "methodology": "Methodology", "exports": "Bulk exports",
}
TAG_ORDER = list(dict.fromkeys(TAGS.values()))
TAG_DESCRIPTIONS = {
    "Reference": "GPUs, families, providers and regions: the ids every other route uses.",
    "Markets": "One row per GPU across every provider: price range, spread and history.",
    "Listings": "Every individual offer as the provider publishes it.",
    "Reserved": "Reserved and committed-term prices beside on-demand.",
    "Indices": "OpenGrid's GPU price indices and their levels over time.",
    "Fixings": "Official fixings: exact decimal values in a verifiable hash chain.",
    "Capacity": "What providers report as in stock, by SKU and region.",
    "Costs": "Egress, storage, billing increments, and the full cost of a job.",
    "FX": "The reference exchange rates used to convert non-USD prices.",
    "Reliability": "OpenGrid's own probes of provider APIs, plus provider-reported incidents.",
    "Events": "Market events and major shifts detected in the data.",
    "News": "Story-clustered GPU market news and the timeline around price moves.",
    "Macro": "Public context series (power, filings) beside prices.",
    "Methodology": "How every number is computed, as machine-readable documents.",
    "Bulk exports": "One completed UTC day of a dataset as a gzipped JSON-lines file.",
}

TITLES = {
    "GET /v1/gpus": "List GPUs",
    "GET /v1/gpus/{gpu}": "Get a GPU",
    "GET /v1/families": "List GPU families",
    "GET /v1/families/{family}": "Get a GPU family",
    "GET /v1/providers": "List providers",
    "GET /v1/providers/{provider}": "Get a provider",
    "GET /v1/providers/{provider}/gpus/{gpu}/context": "Provider price in context",
    "GET /v1/providers/{provider}/listings/context": "Listing price in context",
    "GET /v1/regions": "List region groups",
    "GET /v1/markets": "List markets",
    "GET /v1/markets/{gpu}": "Get a market",
    "GET /v1/markets/{gpu}/history": "Market history",
    "GET /v1/markets/{gpu}/history/daily": "Daily market history",
    "GET /v1/markets/{gpu}/history/archived": "Archived price history",
    "GET /v1/markets/{gpu}/history/dispersion": "Price dispersion history",
    "GET /v1/markets/{gpu}/history/providers": "Price history by provider",
    "GET /v1/markets/{gpu}/regions": "Market by region",
    "GET /v1/listings": "List listings",
    "GET /v1/listings/history": "Listing changes",
    "GET /v1/reserved": "List reserved prices",
    "GET /v1/reserved/summary": "Reserved price summary",
    "GET /v1/indices": "List indices",
    "GET /v1/indices/{index_id}": "Get an index",
    "GET /v1/indices/{index_id}/history": "Index history",
    "GET /v1/indices/{index_id}/fixings": "Index fixings",
    "GET /v1/fixings": "List latest fixings",
    "GET /v1/fixings/{fixing_id}": "Get a fixing",
    "GET /v1/fixings/{fixing_id}/verify": "Verify a fixing",
    "GET /v1/fixings/chain/verify": "Verify the fixing chain",
    "GET /v1/fixings/restatements": "List restatements",
    "GET /v1/fixings/methodology": "Fixing methodology",
    "GET /v1/capacity": "List capacity",
    "GET /v1/capacity/history": "Capacity changes",
    "GET /v1/costs/components": "Cost components",
    "GET /v1/costs/billing": "Billing increments",
    "GET /v1/costs/sources": "Cost sources",
    "GET /v1/costs/presets": "Reference jobs",
    "POST /v1/costs/estimate": "Estimate a job's cost",
    "GET /v1/costs/rl-presets": "RL reference jobs",
    "POST /v1/costs/rl-estimate": "Estimate an RL job's cost",
    "GET /v1/fx/rates": "Current FX rates",
    "GET /v1/fx/rates/history": "FX rate history",
    "GET /v1/reliability/providers": "Provider reliability",
    "GET /v1/reliability/providers/{provider}": "One provider's reliability",
    "GET /v1/reliability/providers/{provider}/dips": "Reliability dips",
    "GET /v1/reliability/incidents": "Provider incidents",
    "GET /v1/reliability/probes": "Raw probe results",
    "GET /v1/reliability/targets": "Probe targets",
    "GET /v1/reliability/agents": "Probe vantage points",
    "GET /v1/events": "List events",
    "GET /v1/events/types": "Event types",
    "GET /v1/events/shifts": "List shifts",
    "GET /v1/events/shifts/definition": "Shift definition",
    "GET /v1/events/shifts/{shift_id}": "Get a shift",
    "GET /v1/news": "List news",
    "GET /v1/news/{item_id}": "Get a news story",
    "GET /v1/news/sources": "News sources",
    "GET /v1/news/topics": "News topics",
    "GET /v1/news/families": "News GPU families",
    "GET /v1/timeline": "Timeline",
    "GET /v1/timeline/around": "Timeline around a moment",
    "GET /v1/macro/series": "List macro series",
    "GET /v1/macro/series/{series}": "Get a macro series",
    "GET /v1/macro/series/{series}/history": "Macro series history",
    "GET /v1/macro/power": "Listings beside power prices",
    "GET /v1/methodology": "List methodology documents",
    "GET /v1/methodology/{name}": "Get a methodology document",
    "GET /v1/exports": "List export datasets",
    "GET /v1/exports/{dataset}": "Download a day's export",
}

# Routes whose handler has no summary (FastAPI would title them after the function, e.g. "News List").
DESCRIPTIONS = {
    "GET /v1/news": "GPU market news clustered into stories, newest activity first. Filter by GPU, provider, "
                    "region, topic, source or free text (`q`).",
    "GET /v1/news/{item_id}": "One news item with its classification (topics, GPUs, providers it concerns).",
    "GET /v1/news/sources": "Every news source OpenGrid reads, with its health.",
    "GET /v1/news/topics": "The topic catalogue, with how many items each had in the last 30 days.",
    "GET /v1/news/families": "Every GPU family the news classifier recognises; `tracked: false` means OpenGrid has "
                             "no prices for it.",
    "GET /v1/timeline": "Price moves, events and news for a GPU and/or provider over a window, on one timeline. "
                        "Give `gpu`, `provider` or both.",
    "GET /v1/timeline/around": "What happened around one moment (`at`) for a GPU: the price move, events and news "
                               "within `window_hours`. Shown together, not claimed as cause and effect.",
}

# Path parameters: what they are, and a working value the playground starts with.
PATH_PARAMS = {
    "gpu": ("GPU id, e.g. `nvidia-h100-80gb-sxm5`. Older short names (`h100-80gb-sxm5`) are accepted as aliases; "
            "responses always return the id.", "nvidia-h100-80gb-sxm5"),
    "provider": ("Provider id, e.g. `lambda`.", "lambda"),
    "family": ("GPU family id, e.g. `h100`.", "h100"),
    "index_id": ("Index id, e.g. `h100-80gb-sxm5` (index ids are frozen, see IDs).", "h100-80gb-sxm5"),
    "fixing_id": ("Fixing id, as returned by `GET /v1/fixings`.", None),
    "shift_id": ("Shift id, as returned by `GET /v1/events/shifts`.", None),
    "item_id": ("News story id, as returned by `GET /v1/news`.", None),
    "series": ("Series id, as returned by `GET /v1/macro/series`.", None),
    "name": ("Methodology document name, e.g. `indices`.", "indices"),
    "dataset": ("`listings`, `listing_changes`, `index_levels` or `fixings`.", "listings"),
}

ERROR_RESPONSES = {
    "400": "Bad request: an unknown `OpenGrid-Version`, a cursor this API did not issue, or a malformed body.",
    "401": "No API key, or the key is invalid, revoked or expired.",
    "403": "The key lacks the `data:read` scope, or the account is suspended.",
    "404": "No such resource (an unknown GPU, provider, index ...).",
    "422": "A parameter failed validation; `error.param` names it.",
    "429": "Over the per-key or per-account rate limit; wait `Retry-After` seconds.",
}

META = {
    "type": "object",
    "description": "Present on every success. List routes add `limit`, `next_cursor` (null on the last page) and "
                   "`total`.",
    "properties": {
        "as_of": {"type": "string", "format": "date-time", "description": "When the response was built (UTC)."},
        "api_version": {"type": "string", "description": "The version the body is shaped for.",
                        "example": "2026-11-03"},
        "request_id": {"type": "string", "description": "Quote it when reporting a problem.",
                       "example": "req_5bd092f30d380c07d2f32f8f"},
        "next_cursor": {"type": ["string", "null"], "description": "Pass back as `cursor` for the next page."},
        "limit": {"type": "integer"},
        "total": {"type": "integer"},
    },
    "required": ["as_of", "api_version", "request_id"],
}

ERROR = {
    "type": "object",
    "properties": {"error": {
        "type": "object",
        "properties": {
            "type": {"type": "string", "description": "Broad class: `invalid_request`, `authentication`, "
                                                      "`permission`, `rate_limit` or `api_error`."},
            "code": {"type": "string", "description": "Machine-readable reason. Part of the contract.",
                     "example": "authentication_required"},
            "message": {"type": "string", "description": "Human-readable; may change. Do not parse it."},
            "param": {"type": ["string", "null"], "description": "The parameter at fault, when there is one."},
            "request_id": {"type": "string"},
            "docs_url": {"type": "string", "description": "This error on the Errors page."},
        },
        "required": ["type", "code", "message", "param", "request_id", "docs_url"],
    }},
    "required": ["error"],
}


def slug(text: str) -> str:
    """A title as a URL segment: the operation's page name, and (for a tag) its sidebar folder."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _tokens(path: str) -> list[str]:
    return re.findall(r"\[\]|\.\*|\.[^.\[]+", "" if path == "$" else path)


def schema_from_shape(shape: dict) -> dict:
    """A contract snapshot ({field path: [JSON types]}) -> a JSON schema of the same shape."""
    root: dict = {}
    for path, types in shape.items():
        node = root
        for t in _tokens(path):
            if t == "[]":
                node = node.setdefault("items", {})
            elif t == ".*":
                node = node.setdefault("values", {})
            else:
                node = node.setdefault("props", {}).setdefault(t[1:], {})
        node["types"] = types

    def build(node: dict) -> dict:
        types = node.get("types") or []
        out: dict = {}
        if types:
            out["type"] = types[0] if len(types) == 1 else types
        if "props" in node:
            out["properties"] = {k: build(v) for k, v in sorted(node["props"].items())}
        if "values" in node:
            out["additionalProperties"] = build(node["values"])
        if "items" in node:
            out["items"] = build(node["items"])
        elif "array" in types:
            out["items"] = {}
        return out

    return build(root)


def _snapshot_for(template: str, snapshot: dict) -> dict | None:
    """The snapshot recorded for a route template: the literal path, or the one sample path that fills it."""
    if template in snapshot:
        return snapshot[template]
    rx = re.compile("^" + re.sub(r"\\\{[a-z_]+\\\}", "[^/]+", re.escape(template)) + "$")
    hits = [p for p in snapshot if rx.match(p)]
    return snapshot[hits[0]] if len(hits) == 1 else None


def estimate_body() -> dict:
    """POST /v1/costs/estimate reads its body by hand (api/cost.parse_job), so FastAPI publishes none: described
    here from the same limits and choices the parser enforces."""
    from api import cost

    return {"required": True, "content": {"application/json": {"schema": {
        "type": "object", "additionalProperties": False,
        "description": f"A job. Start from a `preset` and override fields, or give `gpu`, `count` and `hours`. "
                       f"Terabytes are decimal (1 TB = 1,000 GB). Body at most {cost.MAX_BODY} bytes.",
        "properties": {
            "preset": {"type": "string", "enum": list(cost.est.PRESETS),
                       "description": "A reference job (`GET /v1/costs/presets`)."},
            "gpu": {"type": "string", "description": "GPU id or alias.", "example": "nvidia-h100-80gb-sxm5"},
            "count": {"type": "integer", "minimum": 1, "maximum": cost.MAX_COUNT, "example": 8},
            "hours": {"type": "number", "minimum": round(1 / 60, 6), "maximum": cost.MAX_HOURS, "example": 24},
            "egress_tb": {"type": "number", "minimum": 0, "maximum": cost.MAX_GB / 1000,
                          "description": "Give `egress_tb` or `egress_gb`, not both."},
            "egress_gb": {"type": "number", "minimum": 0, "maximum": cost.MAX_GB},
            "storage_tb": {"type": "number", "minimum": 0, "maximum": cost.MAX_GB / 1000,
                           "description": "Give `storage_tb` or `storage_gb`, not both."},
            "storage_gb": {"type": "number", "minimum": 0, "maximum": cost.MAX_GB},
            "storage_class": {"type": "string", "enum": list(cost.est.STORAGE_ORDER), "default": "auto"},
            "min_fabric_gbps": {"type": "number", "minimum": 1, "maximum": 100_000,
                                "description": "Only providers whose interconnect is at least this fast."},
        },
    }, "example": {"gpu": "nvidia-h100-80gb-sxm5", "count": 8, "hours": 24, "egress_tb": 2}}}}


def rl_body() -> dict:
    """POST /v1/costs/rl-estimate reads its body by hand (cost/rl.parse): described from the same limits."""
    from api import cost

    num = lambda lo, hi, **kw: {"type": "number", "minimum": lo, "maximum": hi, **kw}  # noqa: E731
    integer = lambda lo, hi, **kw: {"type": "integer", "minimum": lo, "maximum": hi, **kw}  # noqa: E731
    return {"required": True, "content": {"application/json": {"schema": {
        "type": "object", "additionalProperties": False,
        "description": f"An RL post-training job. Start from a `preset` and override fields (nested ones too), or give "
                       f"`trainer`, `rollouts`, `weights_gb`, `syncs_per_hour` and `hours`. GB are decimal. Body at "
                       f"most {cost.MAX_BODY} bytes. Methodology: /methodology/rl-cost.",
        "properties": {
            "preset": {"type": "string", "enum": list(cost.rl.PRESETS), "description": "`GET /v1/costs/rl-presets`."},
            "trainer": {"type": "object", "additionalProperties": False, "properties": {
                "gpu": {"type": "string", "example": "nvidia-h100-80gb-sxm5"},
                "count": integer(1, 64, description="GPUs per trainer node."), "nodes": integer(1, 128, default=1)}},
            "rollouts": {"type": "object", "additionalProperties": False, "properties": {
                "gpu": {"type": "string", "default": "auto", "description": '"auto" or a GPU id.'},
                "count": integer(1, 4096, description="Rollout GPUs in total."),
                "min_vram_gb": num(0, 1024, default=0), "interruptible": {"type": "boolean", "default": False},
                "interruption_overhead": num(1, 5, default=cost.rl.DEFAULT_OVERHEAD),
                "rank_by": {"type": "string", "enum": list(cost.rl.RANK_BY), "default": "price"}}},
            "sandboxes": {"type": ["object", "null"], "additionalProperties": False, "properties": {
                "vcpu": num(0, 1024), "ram_gb": num(0, 8192), "count": integer(0, 100_000),
                "price_per_hour": num(0, 1_000_000, description="Your price for every sandbox together, per hour.")}},
            "weights_gb": num(0.001, 100_000, description="GB sent per weight sync."),
            "syncs_per_hour": num(0, 3600), "hours": num(round(1 / 60, 6), 24 * 365),
            "sync_fanout": integer(1, 1024, description="Copies per sync leaving the trainer's provider. Default: one "
                                                        "per rollout provider."),
            "storage_gb": num(0, 10_000_000), "storage_tb": num(0, 10_000, description="Give `storage_tb` or `storage_gb`."),
            "storage_class": {"type": "string", "enum": list(cost.est.STORAGE_ORDER), "default": "auto"},
            "same_cluster_only": {"type": "boolean", "default": False},
        },
    }, "example": {"preset": "RL1", "rollouts": {"interruptible": True}}}}}


def _refs(v, out: set) -> set:
    if isinstance(v, dict):
        if isinstance(v.get("$ref"), str):
            out.add(v["$ref"].rsplit("/", 1)[-1])
        for x in v.values():
            _refs(x, out)
    elif isinstance(v, list):
        for x in v:
            _refs(x, out)
    return out


def build() -> dict:
    import main
    from api import contract

    spec = main.app.openapi()
    snapshot = json.loads((ROOT / "tests" / "contracts" / f"{contract.LATEST}.json").read_text())
    version_param = {
        "name": contract.HEADER, "in": "header", "required": False,
        "description": "The API version to shape the response for. Default: the version pinned to your key.",
        "schema": {"type": "string", "enum": list(contract.VERSIONS), "example": contract.LATEST},
    }
    paths: dict = {}
    for path, item in spec["paths"].items():
        if not contract.is_data_path(path):
            continue
        for method, op in item.items():
            if method != "get" and not (method == "post" and path in PUBLIC_POST):
                continue
            key = f"{method.upper()} {path}"
            area = path[4:].split("/", 1)[0]
            summary = op.get("summary") or ""
            doc = op.get("description") or ""
            out = {k: v for k, v in op.items() if k not in ("tags", "summary", "description", "operationId",
                                                            "responses", "parameters")}
            params = []
            for p in op.get("parameters", []):
                p = dict(p)
                if p.get("in") == "path" and p["name"] in PATH_PARAMS:
                    text, example = PATH_PARAMS[p["name"]]
                    p["description"] = text
                    p["schema"] = {k: v for k, v in p["schema"].items() if k != "title"}
                    if example:
                        p["example"] = example
                params.append(p)
            shape = _snapshot_for(path, snapshot) if method == "get" else None
            data = schema_from_shape(shape) if shape else {
                "description": "Field-level schema not published for this route yet; send a request to see one."}
            if path == "/v1/exports/{dataset}":
                ok = {"description": "The day's rows as gzipped JSON lines.",
                      "content": {"application/gzip": {"schema": {"type": "string", "format": "binary"}}}}
            else:
                ok = {"description": "Success",
                      "content": {"application/json": {"schema": {
                          "type": "object", "properties": {"data": data, "meta": {"$ref": "#/components/schemas/Meta"}},
                          "required": ["data", "meta"]}}}}
            responses = {"200": ok}
            responses.update({code: {"description": text, "content": {"application/json": {
                "schema": {"$ref": "#/components/schemas/Error"}}}} for code, text in ERROR_RESPONSES.items()})
            paths.setdefault(path, {})[method] = {
                "tags": [TAGS[area]],
                "summary": TITLES.get(key) or summary or key,
                "description": DESCRIPTIONS.get(key) or "\n\n".join(
                    x for x in (summary.rstrip(".") + "." if summary else "", doc) if x),
                "operationId": slug(TITLES.get(key) or key),
                "parameters": params + [version_param],
                **out,
                **({"requestBody": estimate_body()} if path == "/v1/costs/estimate" else {}),
                **({"requestBody": rl_body()} if path == "/v1/costs/rl-estimate" else {}),
                "responses": responses,
            }
    paths = dict(sorted(paths.items(), key=lambda kv: (TAG_ORDER.index(TAGS[kv[0][4:].split("/", 1)[0]]), kv[0])))
    schemas = spec.get("components", {}).get("schemas", {})
    keep, todo = set(), _refs(paths, set())
    while todo:
        name = todo.pop()
        if name in keep or name not in schemas:
            continue
        keep.add(name)
        todo |= _refs(schemas[name], set())
    return {
        "openapi": spec.get("openapi", "3.1.0"),
        "info": {"title": "OpenGrid Data API", "version": contract.LATEST,
                 "description": "Real-time GPU compute market data: prices, indices, fixings, capacity, costs, "
                                "reliability, events and news."},
        "servers": [{"url": f"https://{DATA_HOST}"}],
        "security": [{"bearer": []}],
        "tags": [{"name": t, "description": TAG_DESCRIPTIONS[t]} for t in TAG_ORDER],
        "paths": paths,
        "components": {
            "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer",
                                           "description": "Your API key: `Authorization: Bearer opg_live_...`"}},
            "schemas": {**{k: schemas[k] for k in sorted(keep)}, "Meta": META, "Error": ERROR},
        },
    }


REFERENCE = ROOT / "docs-site" / "content" / "docs" / "api-reference"


def files() -> dict[Path, str]:
    """Every generated file: the schema, and one sidebar meta.json per tag folder listing its pages in route order
    (the pages themselves are built from the schema at docs build time, under api-reference/<tag>/<operationId>)."""
    spec = build()
    out = {OUT: json.dumps(spec, indent=1, ensure_ascii=False) + "\n"}
    root = {"title": "API reference", "description": "Every Data API endpoint, with a playground", "root": True,
            "icon": "SquareTerminal", "pages": ["index", "---Endpoints---", *[slug(t) for t in TAG_ORDER]]}
    out[REFERENCE / "meta.json"] = json.dumps(root, indent=1) + "\n"
    for tag in TAG_ORDER:
        ops = [op["operationId"] for item in spec["paths"].values() for op in item.values() if op["tags"] == [tag]]
        meta = {"title": tag, "pages": ops}
        out[REFERENCE / slug(tag) / "meta.json"] = json.dumps(meta, indent=1) + "\n"
    return out


if __name__ == "__main__":
    for path, text in files().items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        print(f"wrote {path.relative_to(ROOT)}")
