"""Recommendations, route preview and jobs: the calls shared by the CLI and the MCP server.

Safety boundary (deliberate, see README "What this client can never do"):
  - nothing here provisions compute, submits a real job, or executes code;
  - the only mutations are: a route PREVIEW (writes an audit/preview ticket server-side, spends nothing),
    a job or batch CANCEL (spend-reducing, Idempotency-Key, explicit confirmation by the caller), the MNIST
    SANDBOX demo and a SANDBOX batch (simulated: no provider is called, no GPU exists, nothing is billed).
"""

from __future__ import annotations

import re
from typing import Any

from . import workload
from .client import Client, OpenGridError, RefusedError, new_idempotency_key
from .market import bound, clip, seg

JOB_ID = re.compile(r"^job-[0-9a-f]{8,32}\Z")
IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._:\-]{8,128}\Z")
PREVIEW_NOTE = ("route preview records a preview ticket (route_request_id) and a quote record on the server for "
                "audit; it never provisions anything and spends nothing")
SANDBOX_TEMPLATE = "pytorch-mnist"


def check_job_id(job_id: str) -> str:
    job_id = (job_id or "").strip()
    if not JOB_ID.match(job_id):
        raise RefusedError(f"not a job id: {clip(job_id, 60)!r} (expected job-<hex>, e.g. job-a37ddf8cf1c1c13f)",
                           code="invalid_job_id")
    return job_id


def check_idempotency_key(key: str | None) -> str:
    if key is None:
        return new_idempotency_key()
    if not IDEMPOTENCY_KEY.match(key):
        raise RefusedError("idempotency key must be 8-128 chars of [A-Za-z0-9._:-]", code="invalid_idempotency_key")
    return key


# ---------------------------------------------------------------- local analysis (never sends anything)

def _analyzer():
    try:
        from opengrid_cli.analyze import analyze, plan  # maintained separately (contract section 4)
    except ImportError as e:
        raise OpenGridError(f"the local project analyzer is not available in this install ({e.__class__.__name__}: "
                            f"{e}); reinstall opengrid-cli", code="analyzer_unavailable") from None
    return analyze, plan


def analyze_local(root: str, *, exclude=(), max_files: int = 400, max_file_bytes: int = 256000,
                  dry_run: bool = False) -> dict:
    analyze, plan = _analyzer()
    if dry_run:
        out = plan(root, exclude=tuple(exclude), max_file_bytes=max_file_bytes, max_files=max_files)
        return out if isinstance(out, dict) else {"root": root, "files": list(out or [])}
    return analyze(root, exclude=tuple(exclude), max_file_bytes=max_file_bytes, max_files=max_files, dry_run=False)


# ---------------------------------------------------------------- recommendations (server-side engine)

def estimate(c: Client, req: dict) -> dict:
    r = c.post("/v1/recommend/estimate", req, anonymous_ok=True)
    return {"estimate": r.data, "request_id": r.request_id, "meta": r.meta}


def recommend(c: Client, req: dict, *, limit: int | None = None) -> dict:
    body = dict(req)
    if limit is not None:
        body["limit"] = max(1, min(int(limit), 20))
    r = c.post("/v1/recommend", body, anonymous_ok=True)
    return {"recommendation": r.data, "request_id": r.request_id, "meta": r.meta}


def soft_validate(req: dict) -> tuple[dict, list[str]]:
    """Validate against the contract mirror; on mismatch send the original (the server is the authority)."""
    try:
        return workload.validate(req), []
    except Exception as e:  # pydantic.ValidationError
        msg = str(e).splitlines()
        return workload.compact(req), [f"requirements do not match the client's contract mirror ({clip(' '.join(msg[:3]), 200)}); "
                                       f"sending as-is, the server validates"]


# ---------------------------------------------------------------- route preview (writes a ticket, never provisions)

_CAND = ("provider", "provider_display", "gpu", "gpu_count", "region", "price_per_gpu_hour", "price_per_instance_hour",
         "price_kind", "available", "observed_at", "age_seconds", "provisionable", "score", "explanation", "rank")


def _cand(x: dict | None) -> dict | None:
    if not x:
        return None
    return {k: clip(x.get(k)) for k in _CAND if k in x}


def route_preview(c: Client, gpu: str, *, count: int = 1, region: str | None = None, max_price: float | None = None,
                  mode: str = "BALANCED") -> dict:
    body: dict[str, Any] = {"gpu": gpu.strip(), "count": count, "mode": mode.upper()}
    if region:
        body["region"] = region
    if max_price is not None:
        body["max_price_per_gpu_hour"] = max_price
    r = c.post("/v1/route/preview", body)
    d = r.data or {}
    alts, _ = bound(d.get("alternatives") or [], 5)
    q = d.get("quote") or {}
    return {
        "route_request_id": d.get("route_request_id"), "preview": True, "provisioned": False,
        "gpu": d.get("gpu"), "count": d.get("count"), "mode": d.get("mode"), "region_group": d.get("region_group"),
        "selected": _cand(d.get("selected")), "can_provision_selected": d.get("can_provision_selected"),
        "best_provisionable": _cand(d.get("best_provisionable")),
        "alternatives": [_cand(a) for a in alts],
        "quote": {k: clip(v) for k, v in q.items() if not isinstance(v, list)} or None,
        "quote_record": d.get("quote_record"),
        "runtime": d.get("runtime"), "execution_mode": d.get("execution_mode"),
        "live_provisioning_enabled": d.get("live_provisioning_enabled"),
        "exclusions_total": d.get("exclusions_total"), "exclusions_by_code": d.get("exclusions_by_code") or {},
        "notes": [PREVIEW_NOTE, "candidate prices are observed market prices; the quote is a preview quote"],
        "request_id": r.request_id, "as_of": (r.meta or {}).get("as_of")}


# ---------------------------------------------------------------- jobs (read; cancel; sandbox demo)

_JOB = ("job_id", "name", "template", "job_type", "status", "outcome", "finished", "sandbox", "simulated",
        "simulated_note", "runner_phase", "created_at", "updated_at", "finished_at")


def job_summary(j: dict) -> dict:
    out = {k: clip(j.get(k)) for k in _JOB if k in j}
    sel = j.get("selection") or {}
    if sel:
        out["selection"] = {k: clip(sel.get(k)) for k in ("provider", "gpu", "region", "price_per_gpu_hour",
                                                          "price_kind", "execution_mode") if k in sel}
    res = j.get("resource") or {}
    if res:
        out["resource"] = {k: clip(res.get(k)) for k in ("deployment_id", "deployment_status", "disposition",
                                                         "disposition_note", "possibly_billing") if k in res}
    est = j.get("estimates") or {}
    if est:
        out["estimates"] = est
    if "cancel" in j:
        out["cancel"] = j["cancel"]
    return out


def jobs_list(c: Client, *, status: str | None = None, limit: int = 20) -> dict:
    r = c.get("/v1/jobs", params={"status": status, "limit": max(1, min(limit, 500))})
    rows = r.data if isinstance(r.data, list) else []
    return {"items": [job_summary(j) for j in rows], "returned": len(rows), "request_id": r.request_id,
            "as_of": (r.meta or {}).get("as_of")}


def job_status(c: Client, job_id: str) -> dict:
    job_id = check_job_id(job_id)
    r = c.get(f"/v1/jobs/{seg(job_id)}")
    return {"job": job_summary(r.data or {}), "request_id": r.request_id, "simulated": bool((r.meta or {}).get("simulated"))}


def job_logs(c: Client, job_id: str, *, after: int | None = None, limit: int = 200) -> dict:
    job_id = check_job_id(job_id)
    r = c.get(f"/v1/jobs/{seg(job_id)}/logs", params={"after": after, "limit": max(1, min(limit, 5000))})
    d = r.data or {}
    items = [{"id": x.get("id"), "at": x.get("at"), "stream": x.get("stream"), "line": clip(x.get("line"), 2000)}
             for x in d.get("items") or []]
    return {"job_id": job_id, "items": items, "next_after": d.get("next_after"), "more": bool(d.get("more")),
            "simulated": bool(d.get("simulated")), "request_id": r.request_id}


def job_cancel(c: Client, job_id: str, *, confirm: bool, idempotency_key: str | None = None) -> dict:
    """Cancel only. Before launch the approval is rejected; after launch the GPU is terminated. Spend-reducing."""
    job_id = check_job_id(job_id)
    if confirm is not True:
        raise RefusedError(f"cancelling {job_id} needs explicit confirmation (CLI: --yes; MCP: confirm=true)",
                           code="confirmation_required")
    key = check_idempotency_key(idempotency_key)
    r = c.post(f"/v1/jobs/{seg(job_id)}/cancel", {}, idempotency_key=key)
    return {"job": job_summary(r.data or {}), "idempotency_key": key,
            "replayed": (r.headers or {}).get("idempotent-replayed") == "true", "request_id": r.request_id}


def demo_train(c: Client, *, epochs: int = 3, idempotency_key: str | None = None) -> dict:
    """The MNIST SANDBOX: simulated end to end. The body is built here with sandbox=true and cannot be changed."""
    key = check_idempotency_key(idempotency_key)
    body = {
        "name": "opengrid-cli-demo", "job_type": "training", "template": SANDBOX_TEMPLATE,
        "config": {"epochs": max(1, min(int(epochs), 10)), "batch_size": 128, "learning_rate": 0.001, "seed": 42},
        "resources": {"gpu": "auto", "gpu_count": 1, "region": "US", "allow_spot": False},
        "routing": {"strategy": "BALANCED"},
        "limits": {"max_price_per_gpu_hour_usd": 3, "max_total_cost_usd": 2, "max_runtime_minutes": 30},
        "sandbox": True,
    }
    assert body["sandbox"] is True
    r = c.post("/v1/jobs", body, idempotency_key=key)
    d = r.data or {}
    simulated = bool(d.get("sandbox") or d.get("simulated") or (r.meta or {}).get("simulated"))
    if not simulated:
        raise OpenGridError(f"the server did not confirm job {d.get('job_id')} as a SANDBOX run; check it now "
                            f"(`opengrid jobs status {d.get('job_id')}`) and cancel it if needed",
                            code="sandbox_not_confirmed", request_id=r.request_id)
    return {"job": job_summary(d), "idempotency_key": key, "simulated": True,
            "replayed": (r.headers or {}).get("idempotent-replayed") == "true", "request_id": r.request_id}


# ---------------------------------------------------------------- batches (preview, read, cancel; sandbox submit)

BATCH_ID = re.compile(r"^bat-[0-9a-f]{8,32}\Z")
_BATCH = ("batch_id", "name", "template", "status", "outcome", "finished", "sandbox", "simulated", "simulated_note",
          "shards_total", "counts", "created_at", "finished_at")


def check_batch_id(batch_id: str) -> str:
    batch_id = (batch_id or "").strip()
    if not BATCH_ID.match(batch_id):
        raise RefusedError(f"not a batch id: {clip(batch_id, 60)!r} (expected bat-<hex>)", code="invalid_batch_id")
    return batch_id


def batch_summary(b: dict, *, shards: bool = True) -> dict:
    out = {k: clip(b.get(k)) if not isinstance(b.get(k), dict) else b.get(k) for k in _BATCH if k in b}
    for k in ("budget", "best"):
        if b.get(k) is not None:
            out[k] = b[k]
    ap = b.get("approval") or {}
    if ap:
        out["approval"] = {k: ap.get(k) for k in ("status", "required", "approved_by", "approved_at")}
        out["approval"]["waiting"] = len(ap.get("waiting") or [])
    if b.get("providers"):
        out["providers"] = [{k: p.get(k) for k in ("provider", "shards", "by_state", "committed_usd", "spent_usd")}
                            for p in b["providers"]]
    if shards and b.get("shards"):
        rows, truncated = bound(b["shards"], 64)
        out["shards"] = [{k: r.get(k) for k in ("shard_index", "config", "status", "provider", "job_id", "attempts",
                                                 "metric", "reason_code")} for r in rows]
        out["shards_truncated"] = truncated
    if "cancel" in b:
        out["cancel"] = {k: v for k, v in (b["cancel"] or {}).items() if k != "jobs"}
    return out


def batch_preview(c: Client, body: dict) -> dict:
    """POST /v1/batches/preview: validation + the placement plan. Writes nothing, spends nothing."""
    if not isinstance(body, dict):
        raise RefusedError("the batch request must be a JSON object", code="invalid_request")
    r = c.post("/v1/batches/preview", body)
    d = r.data or {}
    p = d.get("plan") or {}
    shards, truncated = bound(p.get("shards") or [], 64)
    return {"submission": d.get("submission"), "totals": p.get("totals"), "cap": p.get("cap"),
            "alternatives": p.get("alternatives"), "warnings": p.get("warnings") or [], "would": p.get("would"),
            "providers": [{k: x.get(k) for k in ("provider", "shards", "price_per_gpu_hour", "est_max_total_usd",
                                                  "listed_capacity_shards", "approval_mode")}
                          for x in p.get("providers") or []],
            "shards": [{k: x.get(k) for k in ("shard_index", "config", "provider", "price_per_gpu_hour",
                                               "est_max_total_usd", "reservation_usd", "reason")} for x in shards],
            "shards_truncated": truncated, "unplaced": len(p.get("unplaced") or []),
            "request_id": r.request_id, "as_of": (r.meta or {}).get("as_of")}


def batches_list(c: Client, *, status: str | None = None, limit: int = 20) -> dict:
    r = c.get("/v1/batches", params={"status": status, "limit": max(1, min(limit, 200))})
    rows = r.data if isinstance(r.data, list) else []
    return {"items": [batch_summary(b, shards=False) for b in rows], "returned": len(rows),
            "request_id": r.request_id}


def batch_status(c: Client, batch_id: str) -> dict:
    batch_id = check_batch_id(batch_id)
    r = c.get(f"/v1/batches/{seg(batch_id)}")
    return {"batch": batch_summary(r.data or {}), "request_id": r.request_id,
            "simulated": bool((r.meta or {}).get("simulated"))}


def batch_cancel(c: Client, batch_id: str, *, confirm: bool, idempotency_key: str | None = None) -> dict:
    """Cancel only: queued shards cancelled, submitted ones through the jobs cancel path. Spend-reducing."""
    batch_id = check_batch_id(batch_id)
    if confirm is not True:
        raise RefusedError(f"cancelling {batch_id} needs explicit confirmation (CLI: --yes)",
                           code="confirmation_required")
    key = check_idempotency_key(idempotency_key)
    r = c.post(f"/v1/batches/{seg(batch_id)}/cancel", {}, idempotency_key=key)
    return {"batch": batch_summary(r.data or {}), "idempotency_key": key,
            "replayed": (r.headers or {}).get("idempotent-replayed") == "true", "request_id": r.request_id}


def batch_sandbox(c: Client, body: dict, *, idempotency_key: str | None = None) -> dict:
    """A batch as a SANDBOX: sandbox=true is forced here and the server must confirm it; never a real run."""
    if not isinstance(body, dict):
        raise RefusedError("the batch request must be a JSON object", code="invalid_request")
    key = check_idempotency_key(idempotency_key)
    body = {**body, "sandbox": True}
    r = c.post("/v1/batches", body, idempotency_key=key)
    d = r.data or {}
    if not (d.get("sandbox") or (r.meta or {}).get("simulated")):
        raise OpenGridError(f"the server did not confirm batch {d.get('batch_id')} as a SANDBOX run; check it now "
                            f"(`opengrid batch status {d.get('batch_id')}`) and cancel it if needed",
                            code="sandbox_not_confirmed", request_id=r.request_id)
    return {"batch": batch_summary(d), "idempotency_key": key, "simulated": True,
            "replayed": (r.headers or {}).get("idempotent-replayed") == "true", "request_id": r.request_id}
