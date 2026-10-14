"""python -m opengrid_cli.analyze <dir> [--dry-run] [--json] [--exclude PATTERN ...]"""

from __future__ import annotations

import argparse
import json
import sys

from .engine import analyze, plan


def _summary(out: dict) -> str:
    if out.get("dry_run"):
        lines = [f"Dry run for {out['root']} (nothing was read)", "", "Would read:"]
        lines += [f"  {f['path']}  ({f['bytes']} B)  {f['reason']}" for f in out["files_planned"]] or ["  (none)"]
        lines += ["", "Would skip:"]
        lines += [f"  {f['path']}  {f['reason']}" for f in out["files_skipped"]] or ["  (none)"]
        lines += [""] + [f"note: {n}" for n in out["notes"]]
        return "\n".join(lines)
    r = out["requirements"]
    m, t, inf, hw = r["model"], r["training"], r["inference"], r["hardware_hints"]

    def show(label: str, value: object) -> str:
        return f"  {label:<22} {value if value not in (None, []) else 'unknown'}"

    lines = [f"Project: {out['root']}", "",
             show("workload", r["workload_type"]),
             show("frameworks", ", ".join(r["frameworks"]) or None),
             show("model", m["name"]),
             show("parameters (B)", m["parameters_b"]),
             show("precision", r["precision"]),
             show("quantization", r["quantization"])]
    if r["workload_type"] in ("training", "fine_tuning"):
        lines += [show("method", t["method"]), show("lora rank", t["lora_rank"]),
                  show("micro batch", t["micro_batch_size"]), show("seq len", t["seq_len"]),
                  show("grad checkpointing", t["gradient_checkpointing"]), show("zero stage", t["zero_stage"]),
                  show("cpu offload", t["cpu_offload"])]
    if r["workload_type"] in ("inference", "batch"):
        lines += [show("engine", inf["engine"]), show("context len", inf["context_len"]),
                  show("concurrency", inf["concurrency"])]
    lines += [show("gpu count", hw["gpu_count"]), show("cuda", hw["cuda_min"]),
              show("gpus mentioned", ", ".join(hw["gpus_mentioned"]) or None), "",
              f"Inspected {len(out['files_inspected'])} files, skipped {len(out['files_skipped'])}."]
    if out["questions"]:
        lines += ["", "Questions:"] + [f"  - {q['question']}" for q in out["questions"]]
    if out["warnings"]:
        lines += ["", "Warnings:"] + [f"  ! {w}" for w in out["warnings"]]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m opengrid_cli.analyze",
                                description="Analyze a local ML project (allowlisted files only; nothing is "
                                            "executed or uploaded).")
    p.add_argument("dir")
    p.add_argument("--dry-run", action="store_true", help="list the files that would be read; read nothing")
    p.add_argument("--json", action="store_true", help="print JSON")
    p.add_argument("--exclude", action="append", default=[], help="gitignore-style pattern to skip (repeatable)")
    p.add_argument("--max-file-bytes", type=int, default=256_000)
    p.add_argument("--max-files", type=int, default=400)
    a = p.parse_args(argv)
    try:
        if a.dry_run:
            out = plan(a.dir, exclude=a.exclude, max_file_bytes=a.max_file_bytes, max_files=a.max_files)
        else:
            out = analyze(a.dir, exclude=a.exclude, max_file_bytes=a.max_file_bytes, max_files=a.max_files)
    except (FileNotFoundError, NotADirectoryError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(json.dumps(out, indent=2) if a.json else _summary(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
