#!/usr/bin/env bash
# Drive one §27 rung, then gate it.
#
#   RUNG=150 ./campaign.sh              # R1, full provenance
#   RUNG=848 PROV=trace ./campaign.sh   # R2, the powered run
#
# PROV is full | trace | lean (sg_arms.PROV). `trace` keeps the transcripts —
# so the trajectories stay readable in viewer.py — while dropping the
# per-search stdout dumps, which shim.py writes inside every search's latency.
# That is the right level for a powered run: nothing the agent sees changes at
# any level, but `trace` is the one that adds nothing to the clock either.
#
# A rung is a *prefix* of eval/data/swexplore/bench-ladder.jsonl, because
# eval_runner applies `records[:limit]` before its resume filter. So each rung
# is `--limit N --resume` over the same registered order and nothing already
# paid for is re-run. `ladder_frame.py --check` asserts the prefix property.
#
# The loop shape is eval/locbench/campaign.sh's, deliberately, including its
# stall detector — abandoned cells make a row target unreachable and the run
# would otherwise spin forever. What is NOT copied is that script's flags:
# eval_runner has no --max-new, no --instances, no --budget-usd and no exit
# code 3, so progress is driven by --limit and re-invocation instead.
set -uo pipefail
cd "$(dirname "$0")"

RUNG="${RUNG:-150}"
# ONE run id for every rung, not one per rung. eval_runner's --resume looks in
# the output file for this id, so a per-rung id finds nothing and re-runs every
# instance the previous rung already paid for — $18 of pilot, twice. It also
# keeps runs/<id>/<instance>/<arm>/ stable, which is the layout triage_swex
# and displaycmp resolve against.
RUN_ID="${RUN_ID:-s27}"
CONDITIONS="${CONDITIONS:-cc cc-rg cc-sg}"
PROV="${PROV:-full}"
WORKERS="${WORKERS:-3}"
CACHE_GB="${CACHE_GB:-6}"
MODEL="${MODEL:-sonnet}"
PASSES="${PASSES:-6}"

# ../../data, not ../data: this script cd's to its own directory, which was
# eval/swexplore/ before the split and is harness/swexplore/ now — one level
# further from the data root. The third path in this file the split moved.
DATA=$(cd "$(dirname "$0")/../.." && pwd)/data/swexplore
UP=$DATA/upstream
ROOT=$(cd ../.. && pwd)
# gorp is a SIBLING checkout, not part of this repo. Before the split this
# script lived inside the engine tree and `$ROOT/target/release/gorp` was
# right; afterwards $ROOT is gorp-bench, that path does not exist, and every
# invocation died at the gate below with "cargo build --release" no matter how
# freshly the sibling had been built. harness/common/gorp_repo.py is the
# canonical resolver and harness/labbench/campaign.sh already does this;
# swexplore was the one that got missed. Env-overridable, which is how a
# campaign measures a binary built somewhere else.
GORP_REPO="${GORP_REPO:-$ROOT/../gorp}"
GORP_BIN="${GORP_BIN:-$GORP_REPO/target/release/gorp}"

command -v uv >/dev/null || { echo "uv not on PATH"; exit 4; }
[ -s "$DATA/bench-ladder.jsonl" ] || { echo "no bench-ladder.jsonl — run ladder_frame.py"; exit 4; }
[ -x "$GORP_BIN" ] || { echo "no release gorp binary at $GORP_BIN — cargo build --release"; exit 4; }

# The frame must reproduce from its seed before a rung spends anything on it;
# an order that drifted silently would break the prefix property that makes
# rungs poolable.
python3 ladder_frame.py --check >/dev/null || { echo "ladder frame does not reproduce"; exit 4; }

# A bare alias is not a record. s27..s33 every one passed `sonnet` and every
# one in fact ran claude-sonnet-5 — discoverable only by reading a raw
# transcript, and RESEARCH.md §32.3 calibrated against the paper's Sonnet-4.5
# row on the assumption it had not. Pin the id; provaudit re-checks per cell
# that what the CLI resolved is what was asked for.
case "$MODEL" in
  sonnet|opus|haiku|fable|default)
    echo "MODEL=$MODEL is an alias, not a model id. Pin the resolved id"
    echo "(e.g. MODEL=claude-sonnet-4-5-20250929) — an alias drifts between"
    echo "passes and leaves no record of what was actually measured."
    exit 4 ;;
esac

# The money gate. swexplore had none: the README pointed at locbench's
# preflight, which knows nothing about these arms. Skippable only deliberately.
PF=$(cd "$(dirname "$0")" && pwd)/preflight_swex.py
if [ "${SKIP_PREFLIGHT:-0}" != "1" ]; then
  echo "=== preflight ==="
  ( cd "$UP" && GORP_BIN="$GORP_BIN" GORP_REPO="$GORP_REPO" \
      uv run python "$PF" --arms "$(tr ' ' ',' <<<"$CONDITIONS")" --model "$MODEL" ) \
    || { echo "PREFLIGHT FAILED — nothing spent."; exit 4; }
  echo
fi

EXPL=(); for c in $CONDITIONS; do EXPL+=(--explorers "$c"); done
TARGET=$((RUNG * $(wc -w <<<"$CONDITIONS")))

echo "=== §27 rung $RUN_ID: limit=$RUNG arms=($CONDITIONS) prov=$PROV target=$TARGET rows"

count_ok() {
  # Counts ok rows for THIS RUNG'S ARMS ONLY. The first version globbed
  # `<run>-*.jsonl` and counted every arm in the results directory, which is
  # fine while a run id has one arm set and silently fatal once it has two:
  # adding sub-rg/sub-sg under RUN_ID=s27 started the loop at 2,544 ok rows
  # against a TARGET of 1,696, so it printed "rung complete" and exited having
  # run nothing at all. A no-op that reports success is the worst shape a bug
  # can take in a campaign driver.
  python3 - "$DATA/results" "$RUN_ID" "$CONDITIONS" <<'PY'
import json, pathlib, sys
d, run = pathlib.Path(sys.argv[1]), sys.argv[2]
arms = set(sys.argv[3].split())
seen = set()
for arm in sorted(arms):
    p = d / f"{run}-{arm}.jsonl"
    if not p.exists():
        continue
    for line in p.read_text().splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        # swexplore rows carry status under agent, not at top level
        if r.get("explorer") not in arms:
            continue
        if ((r.get("agent") or {}).get("status") or "ok") == "ok":
            seen.add((r.get("instance_id"), r.get("explorer")))
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
  ( cd "$UP" && \
    SWEXPLORE_DATA="$(cd ../ && pwd)" \
    SWEXPLORE_RUN_ID="$RUN_ID" \
    SWEXPLORE_PROV="$PROV" \
    SWEXPLORE_CACHE_GB="$CACHE_GB" \
    GORP_BIN="$GORP_BIN" GORP_REPO="$GORP_REPO" \
    uv run --with typer --with rich python eval_runner.py \
      --bench ../bench-ladder.jsonl --repos ../repos --issue-map ../issue_map.json \
      "${EXPL[@]}" -k 5 --limit "$RUNG" --workers "$WORKERS" --resume \
      --claude-model "$MODEL" -o "../results/$RUN_ID-{explorer}.jsonl" )
  rc=$?
  [ "$rc" -ne 0 ] && { echo "pass failed rc=$rc"; exit "$rc"; }
done

ARMS_CSV=$(tr ' ' ',' <<<"$CONDITIONS")
echo
echo "=== gate ==="
python3 triage_swex.py --run-id "$RUN_ID" --arms "$ARMS_CSV" \
  --json "$DATA/results/$RUN_ID-gate-$(tr ' ' '_' <<<"$CONDITIONS").json"
gate=$?
echo
if [ "$gate" -ne 0 ]; then
  echo "RUNG $RUN_ID GATED OFF. Do not fund the next rung."
  exit 1
fi

# triage asks whether the harness was healthy; this asks whether the rung was
# ONE experiment — one binary, one resolved model, one description across every
# cell. A rung that half-ran on a rebuilt binary is two experiments pooled, and
# until §36 nothing looked.
echo "=== provenance audit ==="
if ! python3 provaudit.py --run-id "$RUN_ID" --arms "$ARMS_CSV" \
       --json "$DATA/results/$RUN_ID-prov-$(tr ' ' '_' <<<"$CONDITIONS").json"; then
  echo "RUNG $RUN_ID GATED OFF (provenance). Do not fund the next rung."
  exit 1
fi
echo
# THE ANALYSIS DOES NOT RUN AT INTERMEDIATE RUNGS. §30's registration bound
# the endpoints to one computation on the pooled 848, and this script then
# auto-ran them at the 120-rung — an unregistered interim look that §30.3 had
# to disclose. A gate is harness health; an endpoint is not. Set ANALYZE=1
# (the final rung does) to compute them.
if [ "${ANALYZE:-0}" != "1" ]; then
  echo "analysis skipped (interim rung — set ANALYZE=1 on the registered final rung)"
  echo
  echo "RUNG $RUN_ID PASSED."
  exit 0
fi
# Contrasts must be named, not inferred. analyze.py's defaults are §27's
# (cc, cc-rg, cc-sg), so a rung whose arms are anything else died here with
# "contrast references arm(s) not in --arms" AFTER the rung had been paid for
# — the gate passes, the numbers never print. Default to "last − first" of
# CONDITIONS, which is treatment − control for every arm pair this script has
# run, and let CONTRASTS override when that is not the intent.
if [ -z "${CONTRASTS:-}" ]; then
  first=$(awk '{print $1}' <<<"$CONDITIONS")
  last=$(awk '{print $NF}' <<<"$CONDITIONS")
  [ "$first" = "$last" ] || CONTRASTS="$last:$first"
fi
python3 analyze.py --run-id "$RUN_ID" --arms "$ARMS_CSV" \
  ${CONTRASTS:+--contrasts "$CONTRASTS"} \
  --json "$DATA/results/$RUN_ID-analysis-$(tr ' ' '_' <<<"$CONDITIONS").json"
echo
echo "RUNG $RUN_ID PASSED."
