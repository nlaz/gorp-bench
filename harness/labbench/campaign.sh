#!/usr/bin/env bash
# Drive one LAB rung, then gate it.
#
#   RUNG=12 ./campaign.sh                # R1, the pilot rung
#   RUNG=60 ./campaign.sh                # R2
#   RUNG=150 ANALYZE=1 ./campaign.sh     # R3, the registered final rung
#
# A rung is a *prefix* of data/labbench/frame.jsonl (run.py applies
# frame[:limit] before its resume filter), so each rung is `--limit N
# --resume` over the same registered order and nothing already paid for is
# re-run. The loop shape is swexplore/campaign.sh's, including the stall
# detector — abandoned cells make the row target unreachable and the run
# would otherwise spin forever.
#
# Cost note (recalibrate after R1): a cell is a full LAB task — a 200-turn
# sandboxed agent run plus one judge call per rubric criterion. Expect
# dollars per cell, not cents; R1 at 12×3=36 cells is the sanity buy.
set -uo pipefail
cd "$(dirname "$0")"

RUNG="${RUNG:-12}"
# ONE run id for every rung, not one per rung — --resume dedupes against the
# results file by (task, arm, model), and the runs/<id>/<task>/<arm>/ layout
# is what triage_lab resolves against.
RUN_ID="${RUN_ID:-lab1}"
CONDITIONS="${CONDITIONS:-lab-base lab-rg lab-gorp}"
MODEL="${MODEL:-anthropic/claude-sonnet-4-6}"
JUDGE="${JUDGE:-claude-sonnet-4-6}"
MAX_TURNS="${MAX_TURNS:-200}"
PASSES="${PASSES:-6}"

DATA=../../data/labbench
ROOT=$(cd ../.. && pwd)
GORP_BIN="${GORP_BIN:-$ROOT/../gorp/target/release/gorp}"

command -v uv >/dev/null || { echo "uv not on PATH"; exit 4; }
[ -s "$DATA/frame.jsonl" ] || { echo "no frame.jsonl — run lab_frame.py"; exit 4; }
[ -x "$GORP_BIN" ] || { echo "no gorp binary at $GORP_BIN — cargo build --release"; exit 4; }

# The frame must reproduce from its seed before a rung spends anything on it.
python3 lab_frame.py --check >/dev/null || { echo "frame does not reproduce"; exit 4; }

# The money gate: full preflight, nothing skipped. This is the harness whose
# one rule is that every gate runs before the expensive thing.
python3 preflight_lab.py || { echo "preflight failed — do not spend"; exit 4; }

ARMS_CSV=$(tr ' ' ',' <<<"$CONDITIONS")
TARGET=$((RUNG * $(wc -w <<<"$CONDITIONS")))

echo "=== LAB rung $RUN_ID: limit=$RUNG arms=($CONDITIONS) target=$TARGET rows"

count_ok() {
  # Counts ok rows for THIS RUNG'S ARMS over the rung's frame prefix only —
  # swexplore's count_ok lesson: counting every arm/task in the file makes a
  # widened campaign print "rung complete" and run nothing.
  python3 - "$DATA/results.jsonl" "$RUN_ID" "$ARMS_CSV" "$RUNG" "$DATA/frame.jsonl" <<'PY'
import json, pathlib, sys
res, run, arms, rung, frame = sys.argv[1:6]
arms = set(arms.split(","))
tasks = set()
for i, line in enumerate(pathlib.Path(frame).read_text().splitlines()):
    if i >= int(rung):
        break
    tasks.add(json.loads(line)["task"])
seen = set()
p = pathlib.Path(res)
if p.exists():
    for line in p.read_text().splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (r.get("run_id") == run and r.get("arm") in arms
                and r.get("task") in tasks and r.get("status") == "ok"):
            seen.add((r["task"], r["arm"]))
print(len(seen))
PY
}

prev=""
for pass_i in $(seq 1 "$PASSES"); do
  ok=$(count_ok)
  echo "--- pass $pass_i: $ok/$TARGET ok rows"
  if [ "$ok" -ge "$TARGET" ]; then echo "rung complete"; break; fi
  if [ "$prev" = "$ok" ]; then
    echo "no progress last pass ($ok rows); the rest are cells that keep failing."
    echo "Inspect before re-running — a stall is a finding, not a retry."
    break
  fi
  prev=$ok
  python3 run.py --run-id "$RUN_ID" --arms "$ARMS_CSV" --limit "$RUNG" \
    --model "$MODEL" --judge-model "$JUDGE" --max-turns "$MAX_TURNS" --resume
  rc=$?
  if [ "$rc" -eq 3 ]; then
    echo "run.py exit 3: consecutive agent errors — an outage, not a task problem."
    exit 3
  fi
  [ "$rc" -ne 0 ] && { echo "pass failed rc=$rc"; exit "$rc"; }
done

echo
echo "=== gate ==="
python3 triage_lab.py --run-id "$RUN_ID" --arms "$ARMS_CSV" \
  --json "$DATA/$RUN_ID-gate.json"
gate=$?
echo
if [ "$gate" -ne 0 ]; then
  echo "RUNG $RUN_ID GATED OFF. Do not fund the next rung."
  exit 1
fi
# The analysis does not run at intermediate rungs (swexplore's §30.3 lesson:
# an auto-run interim look is an unregistered look). Set ANALYZE=1 on the
# registered final rung.
if [ "${ANALYZE:-0}" != "1" ]; then
  echo "analysis skipped (interim rung — set ANALYZE=1 on the final rung)"
  echo
  echo "RUNG $RUN_ID PASSED."
  exit 0
fi
python3 analyze_lab.py --run-id "$RUN_ID"
echo
echo "RUNG $RUN_ID PASSED."
