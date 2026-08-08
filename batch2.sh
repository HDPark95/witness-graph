#!/usr/bin/env bash
# Pilot one case, prove it adjudicated, then run the other ten two at a time.
cd "$HOME/witness-graph" || exit 1

echo "=== pilot MTI-003 ==="
python3 harness/run.py --case cases/MTI-003.json --out runs/ > /tmp/wg-MTI-003.log 2>&1
tail -8 /tmp/wg-MTI-003.log

if ! python3 - <<'PY'
import json, sys
ok = any((r := json.loads(l)).get("node_id") == "adjudicate" and r.get("status") == "ok"
         for l in open("runs/MTI-003.jsonl"))
sys.exit(0 if ok else 1)
PY
then
  echo "PILOT FAILED: no verdict. Stopping before spending the other ten."
  exit 1
fi

echo "=== pilot ok, running the remaining ten ==="
ls cases/MTI-*.json | grep -v MTI-003 | \
  xargs -P 2 -I{} sh -c 'n=$(basename {} .json); python3 harness/run.py --case {} --out runs/ > /tmp/wg-$n.log 2>&1; echo "done $n"'
echo "=== all runs finished ==="
