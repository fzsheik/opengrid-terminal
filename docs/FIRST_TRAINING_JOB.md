# Your first real GPU training job on OpenGrid

This runbook covers one PyTorch MNIST training job ("Train Your First AI Model"), end to end through OpenGrid: preview → submit →
approval → Lambda GPU → training → model.pt → automatic termination → cost reconciliation.

**Who does what.**
- Everything that spends money is done by a human: the Lambda validation run, enabling customer launches, and approving the quote.
- OpenGrid never turns these on by itself. As shipped, production runs in `PREVIEW_ONLY` mode, the Lambda adapter is `simulated`, and `supervised_enabled` is off. In that state a real job is refused with a clear reason, and nothing is faked.

**Cost bound for this job.**
- The defaults cap the run at **≤ $3.00 per GPU-hour, ≤ 30 minutes and ≤ $2.00 total**.
- The quote, the max-price cap and the runtime ceiling all enforce those limits. Lambda bills per minute.
- A typical MNIST run on one A10/A100/H100 takes about 6–15 minutes in total. Most of that is the pip install; training itself takes about 1 minute.

---

## 0. One-time: validate Lambda (only once ever, not per job)

Follow **methodology/first-live-route.md → "The supervised Lambda validation run (exact procedure)"**. It takes 12 steps, costs at most about $1.65, and can also be done on `/admin/execution`.

1. Set the mode to SUPERVISED.
2. Run the kill-switch drills.
3. Send an ops test alert.
4. Get the preconditions green.
5. Create the validation route.
6. Approve it within 5 minutes.
7. Watch the instance run.
8. Terminate it.
9. Wait for reconciliation.
10. Mark Lambda validated.
11. Check for leftovers.

Production already has everything that procedure needs: `ROUTING_LIVE_PROVISIONING=true`, `LAMBDA_API_KEY`, `ROUTING_LAUNCH_DEFAULTS` with the `opengrid-validation` key, the ops webhook and secret, the pepper and the encryption key. **The mode is still `PREVIEW_ONLY`, so nothing can launch until you set it.**

Lambda counts as validated only when that cycle has really happened. Mocked tests never mark it validated.

## 1. Allow customer launches on Lambda (after validation)

Website: **Admin → Execution → provider execution flags → Lambda → Supervised** toggle (a reason is required). API equivalent (operator login):

```sh
B=https://opengrid-terminal-production.up.railway.app
A=(-u "opengrid:$APP_PASSWORD" -H 'X-OpenGrid-Request: 1' -H 'Content-Type: application/json')
curl "${A[@]}" -X POST $B/v1/admin/execution/providers/lambda -d '{"supervised_enabled": true, "reason": "first MNIST training job"}'
curl "${A[@]}" "$B/v1/admin/execution/mode"        # effective_mode must be SUPERVISED (never LIVE for this)
```

Then open **Jobs → New training job**. The readiness panel should be green: `curl … $B/v1/jobs/readiness` should return `blockers: []`.

**Undo at any time:**
- set `supervised_enabled` to false, or
- use **STOP ALL LIVE PROVISIONING** (`POST /v1/admin/execution/kill`).

## 2. Run the job from the website (click by click)

1. Log in → in the left nav, **Jobs** (just above Route).
2. On the card **Train Your First AI Model — PyTorch MNIST**, click **New training job**.
3. Leave the defaults:
   - name `opengrid-first-training-test`
   - 3 epochs, batch size 128, learning rate 0.001, seed 42
   - GPU **auto**, 1 GPU, region US
   - strategy Balanced
   - max $3/GPU-h, max total $2, max runtime 30 min
4. Optionally tick **sandbox** first to try a SIMULATED run. That needs no provider and costs nothing; untick it for the real run.
5. Click **Preview route**. You see three cards:
   - **MARKET RECOMMENDATION**: the best observed listing anywhere.
   - **EXECUTION-READY ROUTE**: what OpenGrid can actually run now, for example Lambda once it is validated.
   - **SANDBOX**.

   If the Execution-ready card is empty, the blockers list says exactly why, with a link to fix each one.
6. Click **Submit for approval…** and confirm. This creates a quote and a job in **PENDING APPROVAL**. **Nothing has launched yet.**
7. On the job page, click **Open approval ticket →**. This opens the existing `/route?rr=…` ticket. Check the price, the 30-minute deadline and the cost ceiling, then click **Approve & launch…**, type `lambda`, and confirm.
   - The quote lives 5 minutes. If it expired, the ticket offers a re-quote.
   - If approval answers "outcome unknown", **do not approve again**. Use "Retry (same Idempotency-Key)"; it can never launch twice.
8. Back on the job page, which refreshes every 4 seconds, watch two strips: the **Job lifecycle** strip and the **Deployment** strip next to it. The job runs through:

   | Phase | What happens |
   |---|---|
   | provisioning | Lambda accepted the launch |
   | initializing | the instance booted and the runner called back |
   | training | per-epoch loss and accuracy appear under **Training progress** |
   | saving artifacts | the model files upload |
   | training completed | the run finished |
   | terminating → termination confirmed | the GPU is shut down and Lambda confirms it gone |
   | reconciled | actual cost reconciled |

9. **Results:**
   - The **12 checks** turn from pending to pass, from "GPU instance created" through "Termination verified with the provider".
   - **Artifacts** lists `model.pt`, `config.json`, `metrics.json`, `env.json` and `manifest.json` with their sha256 hashes. Click to download.
   - The **Actual cost** panel shows the reconciled cost.
   - **Resource** must end as *terminated (confirmed)*. If it ever says **POSSIBLY BILLING**, see the abort rule in §5.

Discord: every job transition is posted to `#opengrid-deployments`, next to the deployment's own posts.

## 3. The same job with an API key

Keys are created on the website at **Keys** (left nav). An operator can also create one for a customer account with `POST /v1/admin/accounts/{id}/keys`.
- The key needs `route:preview`, `route:execute` and `deployments:read`.
- `route:execute` is the scope that can spend money. The secret is shown **once**; it starts with `opg_`.
- The key can preview and submit. Approval still needs the operator (site login, or a platform-admin key).

```sh
export OPENGRID_URL=https://opengrid-terminal-production.up.railway.app
export OPENGRID_API_KEY=opg_...

SANDBOX=true bash examples/first_training_job.sh      # SIMULATED end to end (verified locally)
bash examples/first_training_job.sh                   # REAL: stops at pending_approval until approved on the website

pip install httpx
python examples/first_training_job.py --sandbox       # SIMULATED (verified locally)
python examples/first_training_job.py                 # REAL: prints the job URL, waits for approval, follows to
                                                      # reconciled, downloads <job>-model.pt
python examples/first_training_job.py --cancel job-…  # cancel (terminates the GPU if it was launched)
```

The raw calls (the envelope is `{"data": …, "meta": …}`; errors are `{"detail": {"code", "message"}}`):

```sh
H=(-H "Authorization: Bearer $OPENGRID_API_KEY" -H 'Content-Type: application/json')
curl "${H[@]}" $OPENGRID_URL/v1/jobs/readiness
curl "${H[@]}" -X POST $OPENGRID_URL/v1/jobs/preview -d @job.json
curl "${H[@]}" -X POST $OPENGRID_URL/v1/jobs -H "Idempotency-Key: $(uuidgen)" -d @job.json     # 202 pending_approval
curl "${H[@]}" $OPENGRID_URL/v1/jobs/<job_id>
curl "${H[@]}" "$OPENGRID_URL/v1/jobs/<job_id>/logs?after=0&limit=200"      # then ?after=<next_after>
curl "${H[@]}" $OPENGRID_URL/v1/jobs/<job_id>/metrics
curl "${H[@]}" $OPENGRID_URL/v1/jobs/<job_id>/artifacts
curl "${H[@]}" -o model.pt $OPENGRID_URL/v1/jobs/<job_id>/artifacts/model.pt  # X-Artifact-SHA256 header
curl "${H[@]}" -X POST $OPENGRID_URL/v1/jobs/<job_id>/cancel -H "Idempotency-Key: $(uuidgen)"
```

`job.json` is the request body shown in `examples/first_training_job.sh`. Keep the same `Idempotency-Key` when retrying after a network error. A new key means a new job.

## 4. What happens on the GPU (for questions in the demo)

- Lambda receives a cloud-init `user_data` script, about 197 KB with Lambda's limit at 1 MB. It contains the pinned runner, which is hash-checked.
- The script installs `torch==2.4.1+cu121` and `torchvision==0.19.1` from the official PyTorch index with `pip --require-hashes`. If that fails, it falls back to Lambda Stack's own PyTorch and records the fallback.
- It checks `nvidia-smi` and `torch.cuda.is_available()`, trains the small CNN on MNIST and uploads the artifacts to OpenGrid over HTTPS.
- It authenticates with a per-job token. The token is derived, never stored, and expires at the deadline plus 15 minutes.
- **OpenGrid, not the instance, terminates the GPU through the Lambda API.** On Lambda, an OS shutdown keeps billing.
- With no SSH key in the request, an ephemeral key is generated and its private half discarded, so nobody can log in, OpenGrid included.
- Expected result for 3 epochs: about 98.9% test accuracy. That is the measured result on a local RTX 3070 with the same pinned stack; Lambda's own run is the real proof.

## 5. If something goes wrong

**Job failure states.**
- `bootstrap_failed`, `training_failed`, `out_of_memory`, `training_timed_out` and `artifact_upload_failed` all still **terminate the GPU automatically**.
- A job that is running but sends no runner callback for 20 minutes becomes `bootstrap_failed` and is terminated.

**Abort rule** (the same as for validation). Act if any of these happens:
- OpenGrid is unreachable for more than 5 minutes after approval;
- approval answered "outcome unknown" and no instance appears within 15 minutes;
- the deployment isn't `terminated` by its deadline plus 5 minutes.

Then:
1. In the Lambda console, terminate `og-<deployment_id>` and delete its `og-*` SSH key.
2. Once OpenGrid answers, call `POST /v1/admin/execution/kill`. Reconciliation catches up on recovery.

**Cancel at any time:** use **Cancel job…** or **Cancel & terminate…** on the job page.

**Diagnosing.**
- Logs: the job page's Logs panel.
- `GET /v1/admin/trace/{deployment_id}`.
- On the instance (only with your own SSH key): `/var/log/opengrid-job.log`.

## 6. Verified vs not yet verified

| Evidence | Status |
|---|---|
| Whole flow against a fake Lambda API (submit → quote → approval → launch with the rendered bootstrap → callbacks → artifacts → terminate → reconcile) | automated tests, SIMULATED provider |
| Real MNIST training on CPU through the runner and reporter | tested locally |
| Bootstrap run as root in `ubuntu:22.04` on a real local GPU (RTX 3070): 98.94% after 3 epochs, all artifacts uploaded | tested locally, fake callback server |
| These examples (sandbox) | verified against a local server |
| Lambda boot behaviour, outbound network, install time, real termination, invoice match | **NOT verified until you run §0 and §2** |
