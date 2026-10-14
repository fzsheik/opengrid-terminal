"""Run the OpenGrid "Train Your First AI Model: PyTorch MNIST" job through the public API.

    pip install httpx
    export OPENGRID_URL=https://opengrid-terminal-production.up.railway.app
    export OPENGRID_API_KEY=opg_...        # scopes: route:preview, route:execute, deployments:read
    python examples/first_training_job.py --sandbox        # SIMULATED: no provider, no spend
    python examples/first_training_job.py                  # REAL: creates a quote that an admin must approve

A real job never launches from this script: submit creates a quote and a pending approval. An OpenGrid admin
approves it (website: the job page -> "Open approval ticket"), and only then is a GPU provisioned. The script
then follows the job until it is reconciled (GPU terminated and confirmed, cost reconciled) and downloads
model.pt. Ctrl-C stops following; it does NOT cancel the job (use --cancel <job_id> for that).
"""

import argparse
import os
import sys
import time
import uuid

import httpx

FINAL = {"reconciled", "validation_failed", "no_eligible_provider", "approval_rejected"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sandbox", action="store_true", help="SIMULATED run: no provider is called, nothing is billed")
    ap.add_argument("--epochs", type=int, default=3)
    ap.add_argument("--max-total-usd", type=float, default=2.0)
    ap.add_argument("--cancel", metavar="JOB_ID", help="cancel a job (terminates its GPU if it was launched)")
    ap.add_argument("--follow", metavar="JOB_ID", help="follow an existing job")
    a = ap.parse_args()

    base = os.environ.get("OPENGRID_URL", "https://opengrid-terminal-production.up.railway.app").rstrip("/")
    key = os.environ.get("OPENGRID_API_KEY")
    if not key:
        print("set OPENGRID_API_KEY (an opg_ key with route:preview, route:execute, deployments:read)")
        return 2
    c = httpx.Client(base_url=base, headers={"Authorization": f"Bearer {key}"}, timeout=60)

    if a.cancel:
        r = c.post(f"/v1/jobs/{a.cancel}/cancel", headers={"Idempotency-Key": str(uuid.uuid4())})
        print(r.status_code, r.json())
        return 0 if r.is_success else 1

    job_id = a.follow
    if not job_id:
        body = {
            "name": "opengrid-first-training-test", "job_type": "training", "template": "pytorch-mnist",
            "config": {"epochs": a.epochs, "batch_size": 128, "learning_rate": 0.001, "seed": 42},
            "resources": {"gpu": "auto", "gpu_count": 1, "region": "US", "allow_spot": False},
            "routing": {"strategy": "BALANCED"},
            "limits": {"max_price_per_gpu_hour_usd": 3, "max_total_cost_usd": a.max_total_usd, "max_runtime_minutes": 30},
            "sandbox": a.sandbox,
        }
        p = c.post("/v1/jobs/preview", json=body)
        if not p.is_success:
            print("preview refused:", p.status_code, p.json())
            return 1
        d = p.json()["data"]
        mb, er = d.get("market_best"), d.get("execution_ready")
        print("market best    :", mb and f"{mb.get('provider')} {mb.get('gpu')} ${mb.get('price_per_gpu_hour')}/GPU-h")
        print("execution ready:", er and f"{er.get('provider')} {er.get('gpu')} ${er.get('price_per_gpu_hour')}/GPU-h")
        for b in d.get("blockers") or []:
            print("blocker        :", b.get("message") if isinstance(b, dict) else b)
        if not a.sandbox and not er:
            print("nothing can run for real right now (see the blockers); try --sandbox")
            return 1

        r = c.post("/v1/jobs", json=body, headers={"Idempotency-Key": str(uuid.uuid4())})
        if not r.is_success:
            print("submit refused:", r.status_code, r.json())
            return 1
        job = r.json()["data"]
        job_id = job["job_id"]
        print(f"job {job_id}: {job['status']}" + ("  (SIMULATED)" if a.sandbox else ""))
        if job["status"] == "pending_approval":
            print(f"waiting for approval: {base}/jobs/{job_id}")

    last, after = None, 0
    try:
        while True:
            j = c.get(f"/v1/jobs/{job_id}").json()["data"]
            if j["status"] != last:
                last = j["status"]
                print(f"[{time.strftime('%H:%M:%S')}] {last}  outcome={j.get('outcome')}  "
                      f"resource={(j.get('resource') or {}).get('disposition')}")
            logs = c.get(f"/v1/jobs/{job_id}/logs", params={"after": after, "limit": 200}).json()["data"]
            for line in logs["items"]:
                print("   |", line["line"])
            after = logs["next_after"]
            done = last in FINAL or (j.get("sandbox") and j.get("outcome"))
            if done:
                break
            time.sleep(5)
    except KeyboardInterrupt:
        print("\nstopped following (the job keeps running; --cancel to stop it)")
        return 0

    m = c.get(f"/v1/jobs/{job_id}/metrics").json()["data"]
    print("metrics:", m)
    arts = c.get(f"/v1/jobs/{job_id}/artifacts").json()["data"]
    for art in arts:
        print(f"artifact {art['name']}  {art['size']} B  sha256 {art['sha256']}")
    names = {x["name"] for x in arts}
    if "model.pt" in names:
        r = c.get(f"/v1/jobs/{job_id}/artifacts/model.pt")
        open(f"{job_id}-model.pt", "wb").write(r.content)
        print(f"saved {job_id}-model.pt ({len(r.content)} B, sha256 {r.headers.get('x-artifact-sha256')})")
    return 0 if j.get("outcome") in ("succeeded", "simulated") else 1


if __name__ == "__main__":
    sys.exit(main())
