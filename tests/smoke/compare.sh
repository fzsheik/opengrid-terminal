#!/bin/sh
# Website smoke test: every page on this checkout vs another git ref, each on its own seeded scratch database.
# Prints only the differences (new errors, changed counts). Empty output = the pages behave the same.
#   tests/smoke/compare.sh [base_ref=origin/main]
# Needs Google Chrome, node, and once: (cd tests/smoke && npm install). Not part of tests/run_all.py.
set -e
REF=${1:-origin/main}
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
HERE="$ROOT/tests/smoke"
TMP=$(mktemp -d)
PY="$ROOT/.venv/bin/python"
git -C "$ROOT" worktree add -q "$TMP/base" "$REF"
cleanup() { kill $P1 $P2 2>/dev/null; wait 2>/dev/null; git -C "$ROOT" worktree remove --force "$TMP/base"; rm -rf "$TMP"; }
trap cleanup EXIT
(cd "$TMP/base" && exec "$PY" "$HERE/serve.py" 8781 og_smoke_base) > "$TMP/base.log" 2>&1 & P1=$!
(cd "$ROOT" && exec "$PY" "$HERE/serve.py" 8782 og_smoke_head) > "$TMP/head.log" 2>&1 & P2=$!
for i in $(seq 1 90); do grep -q READY "$TMP/base.log" && grep -q READY "$TMP/head.log" && break; sleep 1; done
grep -q READY "$TMP/head.log" || { echo "server did not start:"; tail -20 "$TMP/head.log"; exit 1; }
node "$HERE/pages.js" http://127.0.0.1:8781 | sort > "$TMP/base.txt"
node "$HERE/pages.js" http://127.0.0.1:8782 | sort > "$TMP/head.txt"
echo "pages checked: $(grep -c COUNT "$TMP/head.txt")"
diff "$TMP/base.txt" "$TMP/head.txt" | awk -v ref="$REF" '/^</ {print "only on " ref ":  " substr($0, 3)} /^>/ {print "only here:  " substr($0, 3)}' || true
