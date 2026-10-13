#!/usr/bin/env bash
# The OpenGrid MNIST training job with cURL. Needs curl + python3 (for reading JSON; jq works too).
#   export OPENGRID_URL=https://opengrid-terminal-production.up.railway.app
#   export OPENGRID_API_KEY=opg_...      # scopes: route:preview, route:execute, deployments:read
#   SANDBOX=true bash examples/first_training_job.sh     # SIMULATED, no provider, no spend
#   bash examples/first_training_job.sh                  # REAL: a quote an admin must approve before any launch
set -euo pipefail
B="${OPENGRID_URL:-https://opengrid-terminal-production.up.railway.app}"
H=(-sS -H "Authorization: Bearer ${OPENGRID_API_KEY:?set OPENGRID_API_KEY}" -H 'Content-Type: application/json')
PY=""; for c in python3 python; do "$c" -c "" 2>/dev/null && { PY=$c; break; }; done
[ -n "$PY" ] || { echo "needs python3 to read JSON"; exit 2; }
json() { "$PY" -c "import json,sys; d=json.load(sys.stdin); print(eval(sys.argv[1], {'d': d}))" "$1"; }
uuid() { "$PY" -c 'import uuid; print(uuid.uuid4())'; }

BODY='{"name":"opengrid-first-training-test","job_type":"training","template":"pytorch-mnist",
 "config":{"epochs":3,"batch_size":128,"learning_rate":0.001,"seed":42},
 "resources":{"gpu":"auto","gpu_count":1,"region":"US","allow_spot":false},
 "routing":{"strategy":"BALANCED"},
 "limits":{"max_price_per_gpu_hour_usd":3,"max_total_cost_usd":2,"max_runtime_minutes":30},
 "sandbox":'"${SANDBOX:-false}"'}'

echo "== 1. readiness (what blocks a real run right now)"
curl "${H[@]}" "$B/v1/jobs/readiness" | json "[(i['code'], i['ok']) for i in d['data']['checks']]"

echo "== 2. preview (free; nothing is created)"
P=$(curl "${H[@]}" -X POST "$B/v1/jobs/preview" -d "$BODY")
echo "$P" | json "{'market_best': d['data']['market_best'] and d['data']['market_best']['provider'], 'execution_ready': d['data']['execution_ready'] and d['data']['execution_ready']['provider'], 'blockers': d['data']['blockers']}"

echo "== 3. submit (Idempotency-Key: retry with the SAME key after a network error, never a new one)"
IK=$(uuid)
S=$(curl "${H[@]}" -X POST "$B/v1/jobs" -H "Idempotency-Key: $IK" -d "$BODY")
JOB=$(echo "$S" | json "d['data']['job_id']")
echo "job $JOB: $(echo "$S" | json "d['data']['status']")   ->  $B/jobs/$JOB"

echo "== 4. follow (a real job waits in pending_approval until an admin approves the quote)"
for _ in $(seq 1 120); do
  J=$(curl "${H[@]}" "$B/v1/jobs/$JOB")
  ST=$(echo "$J" | json "d['data']['status']"); OUT=$(echo "$J" | json "d['data']['outcome']")
  echo "  $(date +%T) $ST outcome=$OUT"
  case "$ST" in reconciled|validation_failed|no_eligible_provider|approval_rejected) break;; esac
  [ "$OUT" = simulated ] && break
  sleep 10
done

echo "== 5. results"
curl "${H[@]}" "$B/v1/jobs/$JOB/metrics" | json "d['data']['epochs']"
curl "${H[@]}" "$B/v1/jobs/$JOB/logs?after=0&limit=20" | json "[i['line'] for i in d['data']['items']]"
curl "${H[@]}" "$B/v1/jobs/$JOB/artifacts" | json "[(a['name'], a['size'], a['sha256'][:16]) for a in d['data']]"
if [ "${SANDBOX:-false}" != true ]; then
  curl "${H[@]}" -o "$JOB-model.pt" "$B/v1/jobs/$JOB/artifacts/model.pt" && echo "saved $JOB-model.pt"
fi
# Cancel (terminates the GPU if it was launched; before launch it rejects the quote):
#   curl "${H[@]}" -X POST "$B/v1/jobs/$JOB/cancel" -H "Idempotency-Key: $(uuid)"
