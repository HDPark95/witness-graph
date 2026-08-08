#!/usr/bin/env bash
# Run the benchmark: pilot one case, prove it adjudicated, then run the rest
# two at a time.
#
# Two at a time is the ceiling on one box: each case fans out three parallel
# witness investigations, so two cases is six concurrent model calls while
# DataHub, OpenSearch and Kafka are all up.
set -u
cd "$(dirname "$0")" || exit 1

# The harness talks to DataHub through the official MCP server, and that import
# only exists inside the venv. Calling bare `python3` here starts every node,
# lets every lookup fail with "No module named 'mcp'", and still writes a
# ledger — a full run that looks complete and contains no evidence.
PY="$PWD/.venv/bin/python"
[ -x "$PY" ] || { echo "no venv at $PY; create it before running the benchmark"; exit 1; }
"$PY" -c "import mcp" 2>/dev/null || { echo "venv has no mcp module"; exit 1; }

OUT="${1:-runs/}"
PILOT="${PILOT_CASE:-MTI-003}"
mkdir -p "$OUT"

echo "=== warehouses ==="
"$PY" harness/warehouse.py --all --out warehouse/ >/dev/null || exit 1
"$PY" harness/warehouse.py --self-check || exit 1

echo "=== pilot $PILOT -> $OUT ==="
"$PY" harness/run.py --case "cases/$PILOT.json" --out "$OUT" > "/tmp/wg-$PILOT.log" 2>&1
tail -8 "/tmp/wg-$PILOT.log"

if ! "$PY" - "$OUT/$PILOT.jsonl" <<'PY'
import json, sys
ok = any((r := json.loads(l)).get("node_id") == "adjudicate" and r.get("status") == "ok"
         for l in open(sys.argv[1]))
sys.exit(0 if ok else 1)
PY
then
  echo "PILOT FAILED: no verdict. Stopping before spending the other ten."
  exit 1
fi

echo "=== pilot ok, running the remaining ten ==="
ls cases/MTI-*.json | grep -v "$PILOT" | \
  PY="$PY" OUT="$OUT" xargs -P 2 -I{} sh -c \
    'n=$(basename {} .json); "$PY" harness/run.py --case {} --out "$OUT" > /tmp/wg-$n.log 2>&1; echo "done $n"'

echo "=== all runs finished; scoring ==="
"$PY" harness/score.py --cases cases/ --runs "$OUT" --out "report-$(basename "$OUT").json"
