# Demo readiness: first paid GPU training job (demo Thursday 2026-10-15)

Written on 2026-10-13. Every item is classified by what proves it. Nothing here is claimed live on a real GPU
provider until you run it.

## Status at a glance

| Area | State | Evidence |
|---|---|---|
| Job API (preview, submit, read, logs, metrics, artifacts, cancel, readiness, templates) | **built, tested** | test_jobs_api (10), test_jobs (23) |
| Workload-aware GPU selection (`gpu:auto`, market best vs execution-ready, blockers) | **built, tested** | test_workloads (24) |
| Existing router, quote, supervised approval, launch, terminate, reconcile | **reused, unchanged** except the bootstrap render in `deployments.launch` | test_jobs real path; the full suite |
| Cloud-init bootstrap and runner (pinned torch 2.4.1+cu121, hash-locked) | **built, tested**; run as root in ubuntu:22.04 on a **local RTX 3070**, reaching 98.94% after 3 epochs | test_bootstrap (8), test_runner (16), runner agent's local run |
| End to end over HTTP: real reporter, real CPU training, fake Lambda | **SIMULATED provider, passing** | test_jobs_e2e |
| Security review of the jobs surface | **done**; 3 issues fixed with regression tests | test_jobs_security (15) |
| Website: Jobs list, New training job, job detail | **built, reviewed locally** with fixture data | test_web_jobs.js; screenshots |
| cURL and Python examples | **verified locally** (sandbox), real mode verified up to pending_approval | examples/ |
| Full test suite | **46/46 Python files, 5/5 JS files passing** | |
| Production deploy | see the final report (safe: mode PREVIEW_ONLY, Lambda simulated) | |
| **Real Lambda run** | **NOT DONE**: you do it (docs/FIRST_TRAINING_JOB.md §0–§2) | — |

## What you must do before the demo (in order)

1. **Lambda validation run.** Follow methodology/first-live-route.md: 12 steps, about 30 minutes, ≤ $1.85 worst case. This is the gate; OpenGrid cannot mark Lambda validated any other way.
2. Turn **supervised_enabled** on for Lambda; the mode stays SUPERVISED.
3. **Run the first MNIST job yourself** from the website (docs/FIRST_TRAINING_JOB.md §2). Expect 6–15 minutes and well under $2. The job page should end with 12/12 checks, five artifacts, `reconciled` and *terminated (provider confirmed)*.
4. Check the Lambda console: no `og-*` instance and no `og-*` key left. Compare the invoice line with the job's actual cost.
5. For the demo itself, repeat step 3 live, or show the finished job. A **sandbox** run is a safe fallback; it is labelled SIMULATED everywhere.

## Known risks for the first real run (not verifiable without Lambda)

| Risk | Mitigation already in place |
|---|---|
| Lambda's cloud-init behaves differently than assumed (root, systemd) | The script also handles non-root, and falls back to `setsid` if systemd is missing. No callback for 20 minutes → `bootstrap_failed` → auto-terminate |
| Pip install of torch is slow or blocked from Lambda | Hash-locked install. Fallback is the image's own PyTorch (Lambda Stack), recorded as `preinstalled_fallback` |
| MNIST mirror down (yann.lecun.com already 404s) | torchvision's second mirror (S3) worked with MD5 checks |
| Instance on an ARM GH200 | Excluded by selection (`host_cpu_unsupported`: ARM Grace host, and there are no cu121 aarch64 wheels) |
| OpenGrid down mid-run | The runner spools and retries; the deadline auto-terminate plus the abort rule (FIRST_TRAINING_JOB.md §5) |
| Quote expires before you approve (5 minutes) | The ticket offers a re-quote; nothing launches on an expired quote |

## Open low-severity items (accepted for the demo)

- **Runner results are runner-reported.** A customer who supplies their own SSH key could forge their own job's metrics; there's no attestation yet. With the ephemeral key, which is the default, nobody can log in.
- **Runner error text reaches the Discord deployments channel.** It is capped at 2,000 characters with mentions disabled.
- **`/v1/jobs/readiness` shows platform gate status** to any key with `route:preview`.
- **S3 artifact backend needs `boto3`,** which is not installed. Postgres storage is the default and is durable.
- **The CPU training path can't produce a "succeeded" real job.** This is by design: success needs CUDA metrics.
