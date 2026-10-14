"""`opengrid`: OpenGrid from the terminal. Market data, local project analysis, workload-aware recommendations.

No command can spend money. There is deliberately no launch / route execute / real job submit command. The only
server-side writes are: `route preview` (records a preview ticket), `jobs cancel` (spend-reducing, needs --yes),
and `demo train` (the MNIST SANDBOX: simulated, no GPU, no spend).

Exit codes:
  0  ok
  1  API error, invalid response, or a doctor check failed
  2  usage error (bad arguments)
  3  authentication / authorization: no key, invalid key, or the key lacks the needed scope
  4  not found (unknown GPU, provider or job)
  5  OpenGrid unreachable (connection refused, DNS, TLS, timeout)
  6  refused by a safety rule (confirmation missing, non-interactive without --yes, not a sandbox run)
  130 interrupted (Ctrl-C)
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time
from typing import Any

from . import actions, hosts, market, workload
from .client import (DEFAULT_URL, ENV_KEY, ENV_URL, EXIT_ERROR, EXIT_INTERRUPTED, EXIT_OK, EXIT_REFUSED,
                     EXIT_UNREACHABLE, PRODUCTION_URL, AuthError, Client, OpenGridError, RefusedError,
                     credentials_mode_ok, credentials_path, delete_stored_key, key_hint, redact, resolve_key,
                     resolve_url, store_key, version)

# Tests replace these (httpx.MockTransport; a fake tty check).
TRANSPORT = None


def stdin_is_tty() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


# ---------------------------------------------------------------- output helpers

class Out:
    def __init__(self, args):
        self.json = bool(getattr(args, "json", False))
        self.command = getattr(args, "_command", "opengrid")

    def emit(self, data: Any, human=None) -> None:
        if self.json:
            print(json.dumps({"ok": True, "command": self.command, "data": data}, indent=2, default=str))
        elif human is not None:
            human(data)

    def note(self, msg: str) -> None:
        """Human context line: stderr in --json mode so stdout stays machine-readable."""
        print(msg, file=sys.stderr if self.json else sys.stdout)


def fmt_price(v) -> str:
    return "-" if v is None else f"${v:,.2f}"


def fmt(v, suffix: str = "") -> str:
    if v is None:
        return "-"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return f"{v:,.2f}{suffix}"
    return f"{v}{suffix}"


def table(rows: list[list[Any]], headers: list[str]) -> None:
    cells = [[str(h) for h in headers]] + [["-" if c is None else str(c) for c in r] for r in rows]
    widths = [max(len(r[i]) for r in cells) for i in range(len(headers))]
    for n, r in enumerate(cells):
        print("  ".join(c.ljust(widths[i]) for i, c in enumerate(r)).rstrip())
        if n == 0:
            print("  ".join("-" * w for w in widths))


def bullet(items, prefix="  - ") -> None:
    for x in items or []:
        print(prefix + (x if isinstance(x, str) else json.dumps(x, default=str)))


def client(args) -> Client:
    return Client(getattr(args, "url", None), timeout=getattr(args, "timeout", 20.0) or 20.0, transport=TRANSPORT)


def confirm(question: str, *, yes: bool, what: str) -> None:
    """--yes skips; a non-interactive stdin without --yes refuses (nothing is sent)."""
    if yes:
        return
    if not stdin_is_tty():
        raise RefusedError(f"{what}: confirmation needed but stdin is not a terminal; re-run with --yes "
                           f"(nothing was sent)", code="confirmation_required")
    sys.stderr.write(f"{question} [y/N] ")
    sys.stderr.flush()
    ans = sys.stdin.readline().strip().lower()
    if ans not in ("y", "yes"):
        raise RefusedError(f"{what}: not confirmed; nothing was sent", code="not_confirmed")


# ---------------------------------------------------------------- doctor / auth / version

def cmd_version(args) -> int:
    Out(args).emit({"opengrid_cli": version(), "python": sys.version.split()[0]},
                   lambda d: print(f"opengrid-cli {d['opengrid_cli']} (python {d['python']})"))
    return EXIT_OK


def cmd_doctor(args) -> int:
    out = Out(args)
    url = resolve_url(args.url)
    key, source = resolve_key()
    checks: list[dict] = []

    def add(name, ok, detail, **extra):
        checks.append({"check": name, "ok": ok, "detail": detail, **extra})

    add("config", True, f"url {url} (from {'--url' if args.url else ENV_URL if os.environ.get(ENV_URL) else 'default'})"
        f"; API key: {key_hint(key) + ' from ' + source if key else 'none (public market data only)'}",
        url=url, key_source=source, key_prefix=key_hint(key))
    mode_ok = credentials_mode_ok()
    if mode_ok is not None:
        add("credentials_file", mode_ok, f"{credentials_path()} " + ("is 0600" if mode_ok else
                                                                      "is readable by others: chmod 600 it"))
    c = client(args)
    reachable = False
    try:
        t0 = time.monotonic()
        h = c.get("/health")
        ms = int((time.monotonic() - t0) * 1000)
        reachable = True
        add("health", (h.data or {}).get("status") == "ok", f"{(h.data or {}).get('status')} db={(h.data or {}).get('db')} "
            f"in {ms} ms", latency_ms=ms, request_id=h.request_id)
    except OpenGridError as e:
        add("health", False, str(e))
    if reachable:
        try:
            o = c.get("/openapi.json")
            info, paths = (o.data or {}).get("info") or {}, (o.data or {}).get("paths") or {}
            add("server_version", True, f"{info.get('title')} {info.get('version')}", server_version=info.get("version"))
            rec = all(p in paths for p in ("/v1/recommend", "/v1/recommend/estimate"))
            add("recommend_api", True, "POST /v1/recommend and /v1/recommend/estimate available" if rec else
                "this server has no /v1/recommend yet: `estimate` and `recommend` will fail (other commands work)",
                warn=not rec)
        except OpenGridError as e:
            add("server_version", False, str(e))
        try:
            g = c.get("/v1/gpus", params={"limit": 1})
            add("market_data", True, f"{(g.meta or {}).get('total')} GPUs, as of {(g.meta or {}).get('as_of')}",
                request_id=g.request_id)
        except OpenGridError as e:
            add("market_data", False, str(e))
        try:
            me = c.get("/v1/me")
            d = me.data or {}
            scopes = d.get("scopes") or []
            add("auth", True, f"{d.get('principal')} (account {(d.get('account') or {}).get('name')}); scopes: "
                f"{', '.join(scopes) or 'none'}", principal=d.get("principal"), scopes=scopes)
            def has(s):
                return "*" in scopes or s in scopes
            add("capabilities", True, "; ".join(f"{name}: {'yes' if has(s) else 'no (needs ' + s + ')'}" for name, s in
                                                (("route preview", "route:preview"), ("jobs list/status", "deployments:read"),
                                                 ("jobs cancel / demo train", "route:execute"))),
                capabilities={s: has(s) for s in ("data:read", "route:preview", "deployments:read", "route:execute")})
        except AuthError as e:
            if key:
                add("auth", False, str(e))
            else:
                add("auth", True, "anonymous: public market data, estimate and recommend only; "
                                  "`opengrid auth login` for jobs and route preview", principal="anonymous", scopes=[])
        except OpenGridError as e:
            add("auth", False, str(e))
    c.close()
    try:
        import mcp  # noqa: F401
        from importlib.metadata import version as dist_version

        from . import mcp_server

        srv = mcp_server.build_server()
        import anyio

        tools = anyio.run(srv.list_tools)
        roots = mcp_server.allowed_roots()
        add("mcp", True, f"mcp {dist_version('mcp')}; {len(tools)} tools; analyze allowed roots: "
            f"{', '.join(map(str, roots)) or 'none (set OPENGRID_ALLOWED_ROOTS)'}", tools=[t.name for t in tools])
    except Exception as e:
        add("mcp", False, f"MCP server not ready: {type(e).__name__}: {redact(e)}")
    try:
        actions._analyzer()
        add("analyzer", True, "local project analyzer available")
    except OpenGridError as e:
        add("analyzer", False, e.message)
    ok = all(x["ok"] for x in checks)

    def human(d):
        for x in d["checks"]:
            tag = "FAIL" if not x["ok"] else "warn" if x.get("warn") else "ok"
            print(f"[{tag}] {x['check']}: {x['detail']}")
        print("all checks passed" if d["ok"] else "some checks failed")
    out.emit({"ok": ok, "checks": checks}, human)
    if not reachable:
        return EXIT_UNREACHABLE
    return EXIT_OK if ok else EXIT_ERROR


def cmd_auth(args) -> int:
    out = Out(args)
    if args.auth_cmd == "login":
        if args.stdin:
            key = sys.stdin.readline().strip()
        else:
            if not stdin_is_tty():
                raise RefusedError("auth login: stdin is not a terminal; pipe the key with --stdin", code="no_tty")
            key = getpass.getpass("OpenGrid API key (input hidden): ").strip()
        if not key.startswith("opg_"):
            raise RefusedError("that does not look like an OpenGrid API key (expected opg_live_...); nothing stored",
                               code="invalid_key_format")
        info = None
        if not args.no_verify:
            c = Client(args.url, api_key=key, transport=TRANSPORT)
            try:
                info = c.get("/v1/me").data or {}
            finally:
                c.close()
        p = store_key(key)
        data = {"stored": str(p), "mode": "0600", "key_prefix": key_hint(key), "verified": info is not None,
                "scopes": (info or {}).get("scopes")}
        out.emit(data, lambda d: print(f"stored {d['key_prefix']} in {d['stored']} (mode 0600)"
                                       + (f"; scopes: {', '.join(d['scopes'] or [])}" if d["verified"] else
                                          "; not verified (--no-verify)")
                                       + (f"\nnote: {ENV_KEY} is set and takes precedence over the stored key"
                                          if os.environ.get(ENV_KEY) else "")))
        return EXIT_OK
    if args.auth_cmd == "logout":
        removed = delete_stored_key()
        out.emit({"removed": removed, "path": str(credentials_path())},
                 lambda d: print(("removed " if d["removed"] else "no stored key at ") + d["path"]))
        return EXIT_OK
    # status
    key, source = resolve_key()
    data: dict = {"url": resolve_url(args.url), "key_source": source, "key_prefix": key_hint(key),
                  "credentials_file": str(credentials_path()), "credentials_mode_ok": credentials_mode_ok()}
    if key:
        c = client(args)
        try:
            d = c.get("/v1/me").data or {}
            data.update(principal=d.get("principal"), scopes=d.get("scopes"),
                        account=(d.get("account") or {}).get("name"), valid=True)
        finally:
            c.close()

    def human(d):
        if d["key_source"] == "none":
            print(f"no API key (set {ENV_KEY} or run `opengrid auth login`); public market data only")
            return
        print(f"key {d['key_prefix']} from {d['key_source']}: valid; {d.get('principal')} "
              f"(account {d.get('account')}); scopes: {', '.join(d.get('scopes') or [])}")
    out.emit(data, human)
    return EXIT_OK


# ---------------------------------------------------------------- market

def cmd_gpu_search(args) -> int:
    with client(args) as c:
        d = market.search_gpus(c, model=args.model, vendor=args.vendor, min_vram=args.min_vram,
                               max_price=args.max_price, available=args.available, sort=args.sort, limit=args.limit)

    def human(d):
        if not d["items"]:
            print("no GPUs match")
        else:
            table([[x["slug"], x["vendor"], fmt(x["vram_gb"], " GB"), fmt_price(x["low_price_per_gpu_hour"]),
                    fmt_price(x["median_price_per_gpu_hour"]), x["providers"], x["available_listings"]]
                   for x in d["items"]],
                  ["GPU", "VENDOR", "VRAM", "LOW/GPU-H", "MEDIAN", "PROVIDERS", "AVAILABLE"])
        print(f"\n{d['returned']} of {d['total_matched']} matches; observed prices as of {d['as_of']}")
        bullet(d["notes"][1:], "  ")
        print("  prices: observed provider list prices per GPU-hour (not quotes); availability as reported")
    Out(args).emit(d, human)
    return EXIT_OK


def cmd_gpu_show(args) -> int:
    with client(args) as c:
        d = market.gpu_specs(c, args.gpu, max_providers=args.providers)

    def human(d):
        if d["kind"] == "family":
            print(f"{d['family']} ({d['slug']}) is a GPU family; its variants are different products:")
            table([[v["slug"], fmt_price(v["low_price_per_gpu_hour"]), fmt_price(v["median_price_per_gpu_hour"]),
                    v["providers"], v["available_listings"]] for v in d["variants"]],
                  ["VARIANT", "LOW/GPU-H", "MEDIAN", "PROVIDERS", "AVAILABLE"])
            print("\nrun `opengrid gpu show <variant>` for one product")
            return
        h, m = d["hardware"], d["market"]
        print(f"{d['gpu']}  ({d['slug']})")
        print(f"  {h.get('vendor')} {h.get('architecture')}, {fmt(h.get('vram_gb'), ' GB')} {h.get('memory_type', '')}, "
              f"{fmt(h.get('memory_bandwidth_tbps'), ' TB/s')}, BF16 {fmt(h.get('bf16_tflops_dense'))} TFLOPS dense, "
              f"{h.get('form_factor')}, {h.get('interconnect') or ''} {h.get('nvlink') or ''}".rstrip())
        print("  specs: vendor peak figures, not a workload benchmark")
        lst = m["listings"]
        print(f"\nmarket now (observed): low {fmt_price(m['low_price_per_gpu_hour'])}  median "
              f"{fmt_price(m['median_price_per_gpu_hour'])}  high {fmt_price(m['high_price_per_gpu_hour'])} per GPU-hour"
              f" across {m['providers']} providers")
        print(f"  listings: {lst.get('live')} live, {lst.get('priced')} priced, {lst.get('available')} available, "
              f"{lst.get('sold_out')} sold out, {lst.get('availability_unknown')} availability unknown"
              + (f"; {m['fragmentation_label']}" if m.get("fragmentation_label") else ""))
        if d["cheapest_by_provider"]:
            print()
            table([[p["provider"], fmt_price(p["price_per_gpu_hour"]), p["region"], fmt(p["available"]),
                    p["listings"], "-" if p["change_24h_pct"] is None else f"{p['change_24h_pct'] * 100:+.1f}%"]
                   for p in d["cheapest_by_provider"]],
                  ["PROVIDER", "LOWEST/GPU-H", "REGION", "AVAILABLE", "LISTINGS", "24H"])
            if d["providers_truncated"]:
                print(f"  (first {len(d['cheapest_by_provider'])} providers; --providers N for more)")
        if d["alternatives"]:
            print("\nsimilar GPUs (not equivalent products): " + ", ".join(d["alternatives"]))
        print(f"\nas of {d['as_of']}; prices are observed listings, not quotes. Ranking for a route: "
              f"`opengrid gpu market {d['slug']}`")
    Out(args).emit(d, human)
    return EXIT_OK


def cmd_gpu_compare(args) -> int:
    with client(args) as c:
        d = market.compare_gpus(c, args.a, args.b)

    def human(d):
        a, b = d["a"], d["b"]
        rows = [["vendor / arch", f"{a['hardware'].get('vendor')} {a['hardware'].get('architecture')}",
                 f"{b['hardware'].get('vendor')} {b['hardware'].get('architecture')}"]]
        for label, key, suf in (("VRAM", "vram_gb", " GB"), ("memory bandwidth", "memory_bandwidth_tbps", " TB/s"),
                                ("BF16 dense TFLOPS", "bf16_tflops_dense", ""), ("FP8 dense TFLOPS", "fp8_tflops_dense", ""),
                                ("form factor", "form_factor", ""), ("TDP", "tdp_w", " W")):
            rows.append([label, fmt(a["hardware"].get(key), suf), fmt(b["hardware"].get(key), suf)])
        for label, key in (("low $/GPU-h (observed)", "low_price_per_gpu_hour"),
                           ("median $/GPU-h (observed)", "median_price_per_gpu_hour")):
            rows.append([label, fmt_price(a[key]), fmt_price(b[key])])
        rows += [["providers", a["providers"], b["providers"]],
                 ["available listings", a["available_listings"], b["available_listings"]]]
        for k in sorted(set(a["median_price_per_capability_hour"]) | set(b["median_price_per_capability_hour"])):
            va, vb = a["median_price_per_capability_hour"].get(k), b["median_price_per_capability_hour"].get(k)
            rows.append([f"median $ {k.replace('_', ' ')}", "-" if va is None else f"${va:.4g}",
                         "-" if vb is None else f"${vb:.4g}"])
        table(rows, ["", a["gpu"] or "a", b["gpu"] or "b"])
        print(f"\n{d['note'] or ''}\n{d['capability_kind']}; as of {d['as_of']}")
    Out(args).emit(d, human)
    return EXIT_OK


def cmd_gpu_market(args) -> int:
    with client(args) as c:
        d = market.market_context(c, args.gpu, count=args.count, region=args.region, max_price=args.max_price,
                                  max_candidates=args.limit)

    def human(d):
        cur = d["current"] or {}
        print(f"{d['gpu']}: now lowest {fmt_price(cur.get('lowest'))}, median {fmt_price(cur.get('median'))}, "
              f"highest {fmt_price(cur.get('highest'))} per GPU-hour across {cur.get('providers')} providers (observed)")
        bullet(d["summaries"])
        if d["label_reason"]:
            print(f"  history: {d['label_reason']}")
        print(f"\nbest available now ({d['ranking_mode']} ranking, inferred from observed prices; not quotes):")
        if d["best_available_now"]:
            table([[x["rank"], x["provider"], x["region"], fmt_price(x["price_per_gpu_hour"]), fmt(x["available"]),
                    f"{x['age_seconds'] // 60}m" if isinstance(x.get("age_seconds"), int) else "-",
                    fmt(x["provisionable_by_opengrid"])] for x in d["best_available_now"]],
                  ["#", "PROVIDER", "REGION", "$/GPU-H", "AVAILABLE", "AGE", "OPENGRID CAN LAUNCH"])
        else:
            print("  no eligible listing")
        if d["exclusions_by_code"]:
            print("  excluded: " + ", ".join(f"{k} {v}" for k, v in d["exclusions_by_code"].items()))
    Out(args).emit(d, human)
    return EXIT_OK


def cmd_providers(args) -> int:
    with client(args) as c:
        d = market.providers(c, limit=args.limit)

    def human(d):
        table([[p["provider"], p["display_name"], p["provider_class"], p["source_type"], p["gpus"], p["live_listings"],
                (p["last_ok_fetch"] or "-")[:16]] for p in d["items"]],
              ["PROVIDER", "NAME", "CLASS", "SOURCE", "GPUS", "LISTINGS", "LAST OK FETCH"])
        print(f"\n{d['returned']} of {d['total']} providers; as of {d['as_of']}")
    Out(args).emit(d, human)
    return EXIT_OK


# ---------------------------------------------------------------- analyze / estimate / recommend

def show_analysis(d: dict, *, dry_run: bool) -> None:
    if dry_run:
        print(f"dry run: nothing was read. Files that WOULD be read under {d.get('root')}:")
        for key in ("files", "would_read", "files_inspected", "files_planned"):
            if isinstance(d.get(key), list):
                for f in d[key]:
                    print(f"  + {f.get('path') if isinstance(f, dict) else f}"
                          + (f"  ({f.get('reason')})" if isinstance(f, dict) and f.get("reason") else ""))
        for key in ("would_skip", "files_skipped", "skipped"):
            if isinstance(d.get(key), list):
                print("would skip:")
                for f in d[key]:
                    print(f"  - {f.get('path') if isinstance(f, dict) else f}"
                          + (f"  ({f.get('reason')})" if isinstance(f, dict) and f.get("reason") else ""))
        return
    print(f"analyzed {d.get('root')} locally (nothing was sent anywhere)")
    fi, fs = d.get("files_inspected") or [], d.get("files_skipped") or []
    print(f"\nread {len(fi)} file(s):")
    for f in fi[:50]:
        print(f"  + {f.get('path')}  {f.get('bytes', '?')} B  ({f.get('reason', '')})")
    if len(fi) > 50:
        print(f"  ... {len(fi) - 50} more (--json for all)")
    print(f"skipped {len(fs)}:")
    for f in fs[:30]:
        print(f"  - {f.get('path')}  ({f.get('reason', '')})")
    if len(fs) > 30:
        print(f"  ... {len(fs) - 30} more")
    show_requirements(d.get("requirements") or {})
    if d.get("questions"):
        print("\nopen questions:")
        bullet([q.get("question") if isinstance(q, dict) else q for q in d["questions"]])
    if d.get("warnings"):
        print("\nwarnings:")
        bullet(d["warnings"])


def show_requirements(req: dict, title: str = "extracted requirements") -> None:
    body = workload.compact({k: v for k, v in req.items() if k != "evidence"})  # nulls = unknown, not shown
    print(f"\n{title} (this is all that would be sent):")
    print("  " + json.dumps(body, indent=2, default=str).replace("\n", "\n  "))
    ev = req.get("evidence") or []
    if ev:
        print("evidence:")
        for e in ev[:30]:
            print(f"  {e.get('field')} = {e.get('value')!r}  from {e.get('source')}  [{e.get('confidence') or '?'}]")


def cmd_analyze(args) -> int:
    d = actions.analyze_local(args.dir, exclude=args.exclude or (), max_files=args.max_files,
                              max_file_bytes=args.max_file_bytes, dry_run=args.dry_run)
    Out(args).emit(d, lambda d: show_analysis(d, dry_run=args.dry_run))
    return EXIT_OK


def requirements_from_args(args, out: Out) -> tuple[dict, dict | None]:
    flags = workload.from_flags(
        params_b=args.params, model=args.model, method=args.method, workload=args.workload, precision=args.precision,
        quantization=args.quantization, seq_len=args.seq_len, batch=args.batch, lora_rank=args.lora_rank,
        context_len=args.context_len, concurrency=args.concurrency, engine=args.engine, budget=args.budget,
        max_price=args.max_price, runtime_hours=args.runtime_hours, region=args.region, vendor=args.vendor,
        max_gpus=args.max_gpus, gpus=args.gpu, frameworks=args.framework)
    analysis = None
    req: dict = {}
    if args.dir:
        analysis = actions.analyze_local(args.dir, exclude=args.exclude or ())
        req = dict(analysis.get("requirements") or {})
        out.note(f"analyzed {analysis.get('root') or args.dir} locally: {len(analysis.get('files_inspected') or [])} "
                 f"file(s) read, {len(analysis.get('files_skipped') or [])} skipped")
    if flags:
        src = req.get("source")
        req = workload.merge(req, flags)
        if src:
            req["source"] = src
    if not req:
        raise RefusedError("nothing to send: give a project directory or flags such as --params 7 --method lora",
                           code="no_requirements")
    req, warnings = actions.soft_validate(req)
    for w in warnings:
        out.note("warning: " + w)
    return req, analysis


def _send_gate(args, out: Out, req: dict, analysis, endpoint: str) -> None:
    if out.json:
        print(json.dumps({"will_send": {"endpoint": endpoint, "requirements": req}}, indent=2), file=sys.stderr)
    else:
        show_requirements(req, title=f"requirements to send to {endpoint}")
    if analysis is not None:  # values derived from local files: the user confirms before they leave the machine
        confirm(f"send ONLY these requirements to {resolve_url(args.url)}{endpoint}?", yes=args.yes,
                what=args._command)


def show_estimate(e: dict) -> None:
    ng = e.get("needs_gpu")
    print(f"needs GPU: {'unknown' if ng is None else 'yes' if ng else 'NO'}  {e.get('needs_gpu_reason') or ''}")
    m = e.get("memory") or {}
    if m:
        rng = m.get("range_gb")
        print(f"memory ({m.get('kind', 'estimated')}, {m.get('method')}, confidence {m.get('confidence')}): "
              f"min {fmt(m.get('min_total_gb'), ' GB')}, recommended {fmt(m.get('recommended_gb'), ' GB')}"
              + (f", range {rng[0]}-{rng[1]} GB" if isinstance(rng, list) and len(rng) == 2 else "")
              + f", per GPU {fmt(m.get('per_gpu_gb'), ' GB')}")
        comp = m.get("components_gb") or {}
        if comp:
            print("  components: " + ", ".join(f"{k} {v} GB" if v is not None else f"{k} not estimated"
                                             for k, v in comp.items()))
        if m.get("assumptions"):
            print("  assumptions:")
            bullet(m["assumptions"], "    - ")
    g = e.get("gpu_count") or {}
    if g:
        print(f"GPU count: min {g.get('min')}, suggested {g.get('suggested')}  {g.get('reason') or ''}")
    ic = e.get("interconnect") or {}
    if ic:
        print(f"interconnect needed: {fmt(ic.get('needed'))}  {ic.get('reason') or ''}")
    rt = e.get("runtime") or {}
    print(f"runtime: {'known' if rt.get('known') else 'not estimated'}  {rt.get('reason') or ''}")
    if e.get("questions"):
        print("questions that would sharpen this:")
        for q in e["questions"]:
            print(f"  - {q.get('question')}  ({q.get('why', '')})" if isinstance(q, dict) else f"  - {q}")
    if e.get("methodology"):
        print(f"methodology: {e['methodology']}")


def cmd_estimate(args) -> int:
    out = Out(args)
    req, analysis = requirements_from_args(args, out)
    _send_gate(args, out, req, analysis, "/v1/recommend/estimate")
    with client(args) as c:
        d = actions.estimate(c, req)
    data = {"requirements_sent": req, "estimate": d["estimate"], "request_id": d["request_id"]}

    def human(d):
        print("\nestimate (computed by the OpenGrid server; figures are estimates):")
        show_estimate(d["estimate"] or {})
    out.emit(data, human)
    return EXIT_OK


def _opt_line(o: dict) -> str:
    mf = o.get("memory_fit") or {}
    return (f"{o.get('gpu')} x{o.get('gpu_count')} at {o.get('provider_display') or o.get('provider')} "
            f"{o.get('region') or ''}: {fmt_price(o.get('price_per_gpu_hour'))}/GPU-h ({o.get('price_kind', 'observed')}"
            f"{', ' + str(o.get('age_seconds') // 60) + 'm old' if isinstance(o.get('age_seconds'), int) else ''}"
            f"{'' if o.get('fresh', True) else ', STALE'}); fits {fmt(mf.get('needed_per_gpu_gb'), ' GB')} of "
            f"{fmt(mf.get('vram_gb'), ' GB')}" + (f" ({mf.get('headroom_pct')}% headroom)" if mf.get("headroom_pct")
                                                   is not None else ""))


def show_recommendation(r: dict) -> None:
    e = r.get("estimate") or {}
    print("\nestimate (server-side; estimated):")
    show_estimate(e)
    if e.get("needs_gpu") is False:
        print("\nthis workload does not need a GPU on the evidence given; no GPU options are recommended.")
    opts = {o.get("option_id"): o for o in r.get("options") or []}
    picks = r.get("picks") or {}
    print("\npicks:")
    for key, label in (("cheapest_market", "cheapest market"), ("best_for_workload", "best for workload"),
                       ("best_executable", "best executable")):
        p = picks.get(key)
        if not p:
            print(f"  {label:18}: none")
            continue
        o = opts.get(p.get("option_id"))
        print(f"  {label:18}: {_opt_line(o) if o else p.get('option_id')}")
        if p.get("reason"):
            print(f"  {'':18}  why: {p['reason']}")
    if r.get("options"):
        print("\noptions:")
    for o in r.get("options") or []:
        print(f"  [{o.get('tier')}] {_opt_line(o)}")
        cost = o.get("cost") or {}
        for s in cost.get("scenarios") or []:
            print(f"      cost {s.get('hours')} h: {fmt_price(s.get('total_usd'))} ({s.get('assumption')})")
        if cost.get("budget_hours") is not None:
            print(f"      budget buys ~{cost['budget_hours']} h at this price")
        if cost.get("unknown_costs"):
            print(f"      not included: {', '.join(cost['unknown_costs'])}")
        if o.get("why"):
            print(f"      why: {'; '.join(o['why'])}")
        if o.get("limitations"):
            print(f"      limitations: {'; '.join(o['limitations'])}")
        ex = o.get("execution") or {}
        rs = [x.get("reason", "") for x in ex.get("reasons") or [] if isinstance(x, dict)]
        reasons = "; ".join(rs[:2]) + (f"; +{len(rs) - 2} more (--json)" if len(rs) > 2 else "")
        print(f"      execution: {'executable by OpenGrid' if ex.get('executable') else 'not executable'}"
              + (f" ({reasons})" if reasons else "")
              + (f"; preview: {ex['route_preview_url']}" if ex.get("route_preview_url") else ""))
    if r.get("excluded"):
        total = r.get("excluded_total") or len(r["excluded"])
        by_code = r.get("excluded_by_code") or {}
        print(f"\nexcluded: {total}" + (" (" + ", ".join(f"{k} {v}" for k, v in by_code.items()) + ")" if by_code else ""))
        for x in r["excluded"][:8]:
            print(f"  - {x.get('gpu')}: {x.get('reason')} [{x.get('code')}]")
        if total > 8:
            print(f"  ... {total - 8} more (--json)")
    if r.get("notes"):
        print("\nnotes:")
        bullet(r["notes"])
    print(f"\nas of {r.get('as_of')}; methodology {r.get('methodology')}. Nothing was launched.")


def cmd_recommend(args) -> int:
    out = Out(args)
    req, analysis = requirements_from_args(args, out)
    _send_gate(args, out, req, analysis, "/v1/recommend")
    with client(args) as c:
        d = actions.recommend(c, req, limit=args.limit)
    data = {"requirements_sent": req, "recommendation": d["recommendation"], "request_id": d["request_id"]}
    out.emit(data, lambda d: show_recommendation(d["recommendation"] or {}))
    return EXIT_OK


# ---------------------------------------------------------------- route preview

def cmd_route_preview(args) -> int:
    out = Out(args)
    out.note(f"route preview: records a preview ticket and quote record on the server (audit), needs an API key with "
             f"route:preview; it NEVER provisions and spends nothing")
    with client(args) as c:
        d = actions.route_preview(c, args.gpu, count=args.count, region=args.region, max_price=args.max_price,
                                  mode=args.mode)

    def human(d):
        s = d["selected"]
        print(f"preview ticket {d['route_request_id']} ({d['mode']}, {d['gpu']} x{d['count']}); execution mode "
              f"{d['execution_mode']}; provisioned: no")
        if s:
            print(f"selected: {s.get('provider_display') or s.get('provider')} {s.get('region')} "
                  f"{fmt_price(s.get('price_per_gpu_hour'))}/GPU-h (observed), available {fmt(s.get('available'))}; "
                  f"OpenGrid can provision it: {fmt(d['can_provision_selected'])}")
            if s.get("explanation"):
                print(f"  why: {s['explanation']}")
        else:
            print("selected: none (no eligible listing)")
        if d["best_provisionable"]:
            b = d["best_provisionable"]
            print(f"best provisionable: {b.get('provider')} {b.get('region')} {fmt_price(b.get('price_per_gpu_hour'))}")
        if d["alternatives"]:
            print("alternatives:")
            for a in d["alternatives"]:
                print(f"  - {a.get('provider')} {a.get('region')} {fmt_price(a.get('price_per_gpu_hour'))}")
        if d["quote"]:
            q = d["quote"]
            print(f"preview quote: {json.dumps({k: v for k, v in q.items() if not isinstance(v, dict)}, default=str)}")
        if d["exclusions_by_code"]:
            print("excluded: " + ", ".join(f"{k} {v}" for k, v in d["exclusions_by_code"].items()))
    out.emit(d, human)
    return EXIT_OK


# ---------------------------------------------------------------- jobs

def _job_line(j: dict) -> list:
    sel = j.get("selection") or {}
    return [j.get("job_id"), j.get("template"), j.get("status"), j.get("outcome"),
            "SIMULATED" if j.get("sandbox") or j.get("simulated") else "real",
            f"{sel.get('provider') or '-'} {sel.get('gpu') or ''}".strip()]


def cmd_jobs(args) -> int:
    out = Out(args)
    with client(args) as c:
        if args.jobs_cmd == "list":
            d = actions.jobs_list(c, status=args.status, limit=args.limit)
            out.emit(d, lambda d: (table([_job_line(j) for j in d["items"]],
                                         ["JOB", "TEMPLATE", "STATUS", "OUTCOME", "KIND", "SELECTION"])
                                   if d["items"] else print("no jobs")))
            return EXIT_OK
        if args.jobs_cmd == "status":
            d = actions.job_status(c, args.job_id)

            def human(d):
                j = d["job"]
                print(f"{j.get('job_id')}: {j.get('status')}  outcome {j.get('outcome') or '-'}"
                      + ("  (SIMULATED: no GPU, no spend)" if j.get("sandbox") else ""))
                for k in ("selection", "resource", "estimates"):
                    if j.get(k):
                        print(f"  {k}: {json.dumps(j[k], default=str)}")
            out.emit(d, human)
            return EXIT_OK
        if args.jobs_cmd == "logs":
            return _logs(c, args, out)
        if args.jobs_cmd == "cancel":
            actions.check_job_id(args.job_id)
            if not args.yes:
                raise RefusedError(f"jobs cancel {args.job_id}: add --yes to confirm (cancel rejects the approval "
                                   f"before launch, or terminates the GPU after launch)", code="confirmation_required")
            d = actions.job_cancel(c, args.job_id, confirm=True, idempotency_key=args.idempotency_key)
            out.emit(d, lambda d: print(f"{d['job'].get('job_id')}: {d['job'].get('status')}"
                                        + (f"  ({(d['job'].get('cancel') or {}).get('note')})"
                                           if d["job"].get("cancel") else "")
                                        + f"\nidempotency key {d['idempotency_key']}"
                                        + (" (replayed)" if d["replayed"] else "")))
            return EXIT_OK
    return EXIT_ERROR


FINISHED = {"reconciled", "validation_failed", "no_eligible_provider", "approval_rejected", "training_completed",
            "failed", "cancelled"}


def _logs(c: Client, args, out: Out) -> int:
    after = args.after
    collected: list = []
    while True:
        d = actions.job_logs(c, args.job_id, after=after, limit=args.limit)
        for line in d["items"]:
            if out.json:
                collected.append(line)
            else:
                print(f"{(line.get('at') or '')[:19]}  {line.get('line')}")
        after = d["next_after"]
        if not args.follow:
            break
        if not d["more"]:
            st = actions.job_status(c, args.job_id)["job"]
            if st.get("finished") or st.get("status") in FINISHED:
                if not out.json:
                    print(f"-- job {st.get('status')} (outcome {st.get('outcome')})")
                break
            time.sleep(args.interval)
    if out.json:
        out.emit({"job_id": args.job_id, "items": collected, "next_after": after})
    return EXIT_OK


# ---------------------------------------------------------------- demo (sandbox only)

def cmd_demo(args) -> int:
    out = Out(args)
    out.note("demo train: runs the PyTorch MNIST template as a SANDBOX job: SIMULATED, no provider is called, no "
             "GPU exists, nothing is billed. Needs an API key with route:execute (the submit endpoint's scope).")
    with client(args) as c:
        d = actions.demo_train(c, epochs=args.epochs, idempotency_key=args.idempotency_key)
        j = d["job"]
        if args.follow and not out.json:  # in --json mode follow with `opengrid jobs logs <id> --follow --json`
            print(f"job {j.get('job_id')}: {j.get('status')} (SIMULATED); following logs")
            ns = argparse.Namespace(job_id=j.get("job_id"), after=None, limit=500, follow=True, interval=args.interval)
            _logs(c, ns, out)
    out.emit(d, lambda d: print(f"job {d['job'].get('job_id')}: {d['job'].get('status')}  (SIMULATED: no GPU, no "
                                f"spend)\nfollow: opengrid jobs logs {d['job'].get('job_id')} --follow"))
    return EXIT_OK


# ---------------------------------------------------------------- mcp

def cmd_mcp(args) -> int:
    if getattr(args, "mcp_cmd", None) == "config":
        url = resolve_url(args.url)
        try:
            s = hosts.snippet(args.host, url=url, from_repo=args.from_repo)
        except ValueError as e:
            raise RefusedError(str(e), code="mcp_config") from None

        def human(s):
            print(f"# {s['host']}: {s['file']}")
            print(s["config"])
            if s["cli"]:
                print(f"\n# or from the command line:\n{s['cli']}")
            print()
            bullet(s["notes"], "# ")
        Out(args).emit(s, human)
        return EXIT_OK
    from . import mcp_server

    if args.url:
        os.environ[ENV_URL] = args.url
    return mcp_server.run(http=args.http, host=args.host, port=args.port, allow_origins=args.allow_origin)


# ---------------------------------------------------------------- parser

def _common(json_default=argparse.SUPPRESS) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--json", action="store_true", default=json_default, help="stable machine-readable output on stdout")
    p.add_argument("--url", default=argparse.SUPPRESS if json_default is argparse.SUPPRESS else None,
                   help=f"OpenGrid base URL (default ${ENV_URL} or {DEFAULT_URL}; production: {PRODUCTION_URL})")
    p.add_argument("--timeout", type=float, default=argparse.SUPPRESS if json_default is argparse.SUPPRESS else 20.0,
                   help="HTTP timeout in seconds (default 20)")
    return p


def _req_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("dir", nargs="?", help="project directory to analyze locally (optional if flags are given)")
    p.add_argument("--exclude", action="append", metavar="GLOB", help="skip paths matching GLOB (repeatable)")
    g = p.add_argument_group("requirements (override what the analyzer found)")
    g.add_argument("--params", type=float, metavar="B", help="model parameters in billions, e.g. 7")
    g.add_argument("--model", help="model name / Hugging Face id")
    g.add_argument("--method", choices=["full", "lora", "qlora", "inference"])
    g.add_argument("--workload", choices=["training", "fine_tuning", "inference", "batch", "no_gpu"])
    g.add_argument("--precision", choices=["fp32", "bf16", "fp16", "fp8", "int8", "int4"])
    g.add_argument("--quantization", choices=["none", "int8", "int4", "nf4", "gptq", "awq", "fp8"])
    g.add_argument("--seq-len", type=int)
    g.add_argument("--batch", type=int, help="micro batch size")
    g.add_argument("--lora-rank", type=int)
    g.add_argument("--context-len", type=int, help="inference context length")
    g.add_argument("--concurrency", type=int, help="inference concurrent requests")
    g.add_argument("--engine", choices=["vllm", "tgi", "transformers", "llama.cpp"])
    g.add_argument("--budget", type=float, help="total budget in USD")
    g.add_argument("--max-price", type=float, help="max price per GPU-hour, USD")
    g.add_argument("--runtime-hours", type=float)
    g.add_argument("--region", help="region group, e.g. us, eu")
    g.add_argument("--vendor", choices=["NVIDIA", "AMD"])
    g.add_argument("--max-gpus", type=int)
    g.add_argument("--gpu", action="append", help="a GPU you have in mind (repeatable)")
    g.add_argument("--framework", action="append", help="framework, e.g. pytorch (repeatable)")
    p.add_argument("--yes", "-y", action="store_true", help="skip the confirmation before sending analyzer-derived "
                                                            "requirements")


def build_parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(
        prog="opengrid", parents=[_common(json_default=False)],
        description="OpenGrid from the terminal: GPU market data, local project analysis, workload-aware "
                    "recommendations, and a local MCP server. No command can spend money.",
        epilog="exit codes: 0 ok, 1 API error / failed check, 2 usage, 3 auth (key missing/invalid/scope), "
               "4 not found, 5 unreachable, 6 refused by a safety rule, 130 interrupted",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    common = _common()
    sub = top.add_subparsers(dest="cmd", metavar="COMMAND")

    def add(name, help_, fn, parent=sub, **kw):
        p = parent.add_parser(name, help=help_, parents=[common], description=help_, **kw)
        p.set_defaults(fn=fn)
        return p

    add("version", "print the client version", cmd_version)
    add("doctor", "check connectivity, server version, auth + scopes, MCP readiness", cmd_doctor)

    auth = sub.add_parser("auth", help="store / check / remove your API key", parents=[common])
    asub = auth.add_subparsers(dest="auth_cmd", required=True, metavar="ACTION")
    lp = add("login", "store an API key (hidden prompt; 0600 file in the user config dir)", cmd_auth, asub)
    lp.add_argument("--stdin", action="store_true", help="read the key from stdin (for scripts)")
    lp.add_argument("--no-verify", action="store_true", help="store without checking it against /v1/me")
    add("status", "which key is used, and its scopes", cmd_auth, asub)
    add("logout", "delete the stored key", cmd_auth, asub)

    gpu = sub.add_parser("gpu", help="GPU market data (public, no key needed)", parents=[common])
    gsub = gpu.add_subparsers(dest="gpu_cmd", required=True, metavar="ACTION")
    s = add("search", "search GPUs by model, vendor, memory, price, availability", cmd_gpu_search, gsub)
    s.add_argument("--model", help="substring of the name or slug, e.g. H100")
    s.add_argument("--vendor", choices=["NVIDIA", "AMD", "Intel"], type=lambda v: {"nvidia": "NVIDIA", "amd": "AMD",
                                                                                  "intel": "Intel"}.get(v.lower(), v))
    s.add_argument("--min-vram", type=float, metavar="GB")
    s.add_argument("--max-price", type=float, metavar="USD", help="lowest observed price per GPU-hour at most this")
    s.add_argument("--available", action="store_true", help="only GPUs with a listing reported available")
    s.add_argument("--sort", choices=["providers", "price", "vram"], default="providers")
    s.add_argument("--limit", type=int, default=50)
    s = add("show", "one GPU: specs and its market now", cmd_gpu_show, gsub)
    s.add_argument("gpu", help="slug or name, e.g. h100-80gb-sxm5 (a family like h100 lists its variants)")
    s.add_argument("--providers", type=int, default=10, help="providers to list (default 10)")
    s = add("compare", "two GPUs side by side", cmd_gpu_compare, gsub)
    s.add_argument("a")
    s.add_argument("b")
    s = add("market", "price context and the best-available-now ranking for one GPU", cmd_gpu_market, gsub)
    s.add_argument("gpu")
    s.add_argument("--count", type=int, default=1)
    s.add_argument("--region")
    s.add_argument("--max-price", type=float)
    s.add_argument("--limit", type=int, default=10)

    s = add("providers", "every provider: coverage and feed health", cmd_providers)
    s.add_argument("--limit", type=int, default=100)

    s = add("analyze", "analyze a project directory LOCALLY (nothing is sent)", cmd_analyze)
    s.add_argument("dir")
    s.add_argument("--dry-run", action="store_true", help="only list the files that would be read")
    s.add_argument("--exclude", action="append", metavar="GLOB")
    s.add_argument("--max-files", type=int, default=400)
    s.add_argument("--max-file-bytes", type=int, default=256000)

    s = add("estimate", "memory / GPU-count estimate (sends only the requirements)", cmd_estimate)
    _req_flags(s)
    s = add("recommend", "workload-aware recommendation: picks, tiers, cost scenarios, readiness", cmd_recommend)
    _req_flags(s)
    s.add_argument("--limit", type=int, default=None, help="max options (1-20)")

    route = sub.add_parser("route", help="route preview (records a ticket; never provisions)", parents=[common])
    rsub = route.add_subparsers(dest="route_cmd", required=True, metavar="ACTION")
    s = add("preview", "dry-run a routing decision (needs route:preview; writes a preview ticket; never provisions)",
            cmd_route_preview, rsub)
    s.add_argument("gpu")
    s.add_argument("--count", type=int, default=1)
    s.add_argument("--region")
    s.add_argument("--max-price", type=float)
    s.add_argument("--mode", default="BALANCED", choices=["CHEAPEST", "FASTEST_AVAILABLE", "BALANCED", "MOST_STABLE"],
                   type=str.upper)

    jobs = sub.add_parser("jobs", help="your jobs (needs deployments:read; cancel needs route:execute)", parents=[common])
    jsub = jobs.add_subparsers(dest="jobs_cmd", required=True, metavar="ACTION")
    s = add("list", "list your jobs", cmd_jobs, jsub)
    s.add_argument("--status", help="open or a job state")
    s.add_argument("--limit", type=int, default=20)
    s = add("status", "one job", cmd_jobs, jsub)
    s.add_argument("job_id")
    s = add("logs", "runner log lines", cmd_jobs, jsub)
    s.add_argument("job_id")
    s.add_argument("--follow", "-f", action="store_true", help="poll until the job finishes")
    s.add_argument("--after", type=int, help="log cursor (id)")
    s.add_argument("--limit", type=int, default=500)
    s.add_argument("--interval", type=float, default=3.0, help=argparse.SUPPRESS)
    s = add("cancel", "cancel a job (needs --yes; never launches anything)", cmd_jobs, jsub)
    s.add_argument("job_id")
    s.add_argument("--yes", "-y", action="store_true", help="confirm the cancel")
    s.add_argument("--idempotency-key", help="reuse to retry the same cancel safely")

    demo = sub.add_parser("demo", help="the MNIST SANDBOX demo (simulated, no GPU, no spend)", parents=[common])
    dsub = demo.add_subparsers(dest="demo_cmd", required=True, metavar="ACTION")
    s = add("train", "submit the PyTorch MNIST template as a SANDBOX job (needs route:execute)", cmd_demo, dsub)
    s.add_argument("--epochs", type=int, default=3)
    s.add_argument("--follow", "-f", action="store_true")
    s.add_argument("--idempotency-key")
    s.add_argument("--interval", type=float, default=3.0, help=argparse.SUPPRESS)

    m = add("mcp", "run the OpenGrid MCP server on stdio (or `mcp config <host>`)", cmd_mcp)
    m.add_argument("--http", action="store_true", help="serve streamable HTTP on loopback instead of stdio")
    m.add_argument("--host", default="127.0.0.1", help="HTTP bind address (loopback only)")
    m.add_argument("--port", type=int, default=8765)
    m.add_argument("--allow-origin", action="append", metavar="ORIGIN", help="extra allowed Origin for --http")
    msub = m.add_subparsers(dest="mcp_cmd", metavar="ACTION")
    s = add("config", "print the MCP config snippet for a host (never contains a key)", cmd_mcp, msub)
    s.add_argument("host", choices=list(hosts.HOSTS))
    s.add_argument("--from-repo", action="store_true", help="launch via `uv run --project <this checkout>/cli`")
    return top


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "fn", None):
        parser.print_help()
        return EXIT_OK
    for k, v in (("json", False), ("url", None), ("timeout", 20.0)):
        if not hasattr(args, k):
            setattr(args, k, v)
    args._command = " ".join(x for x in (args.cmd, getattr(args, "gpu_cmd", None), getattr(args, "auth_cmd", None),
                                         getattr(args, "route_cmd", None), getattr(args, "jobs_cmd", None),
                                         getattr(args, "demo_cmd", None), getattr(args, "mcp_cmd", None)) if x)
    try:
        return args.fn(args)
    except OpenGridError as e:
        key, _ = resolve_key()
        msg = redact(str(e), key)
        if args.json:
            d = e.as_dict()
            d["error"]["message"] = redact(d["error"]["message"], key)
            print(json.dumps({"ok": False, "command": args._command, **d}, indent=2))
        else:
            print(f"error: {msg}", file=sys.stderr)
        return e.exit_code
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return EXIT_INTERRUPTED
    except BrokenPipeError:
        return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
