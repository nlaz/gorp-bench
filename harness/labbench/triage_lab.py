#!/usr/bin/env python3
"""Did the tool work, this time? The LAB gate between campaign rungs.

An adaptation of `locbench/triage.py`, not a fork: the tool and distress
checks are imported and run as-is over normalized rows, so a threshold
tightened there is tightened here. What is adapted, and why (the
triage_swex.py convention of listing every delta):

  1. Row key is (task, arm, model) — labbench rows carry `task`/`arm`, not
     `instance_id`/`condition`. The loader normalizes each row to carry
     both spellings so the imported checks work unchanged. Dropping the arm
     from the dedupe key silently keeps one row in three (§27's lesson).
  2. `triage.DATA` and `triage.cond_dir` are monkeypatched to labbench's
     layout: data/labbench/runs/<run_id>/<task>/<arm>/.
  3. Only lab-gorp rows feed the tool/distress/cache checks — an rg or base
     row has no engine invocations and would dilute every share into a
     vacuous pass (the 0/0 shape §16.10 taught this repo to distrust).
  4. The worktree gate is replaced by LAB harness health: context-overflow
     and finished-cleanly shares from the patched metrics, missing
     deliverables, judge failures, criterion-count drift against the frame,
     leaked `lab-sandbox-*` podman containers, and the
     registered-arms-present / task-missing-an-arm pair copied from
     swexplore (a paired analysis dies silently without it).
  5. Tool-invocation share per treatment arm is reported, not gated — it is
     the dilution factor analyze_lab.py conditions on.

    python3 harness/labbench/triage_lab.py --run-id lab1
    python3 harness/labbench/triage_lab.py --results ... --arms lab-base,lab-rg,lab-gorp

Exit codes: 0 all gates pass, 1 a gate failed (do not start the next rung),
2 the input could not be read.
"""

import argparse
import json
import os
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import gorp_repo as common  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "locbench"))
import triage  # noqa: E402

DATA = common.DATA / "labbench"

# LAB-specific gate thresholds, same contract as triage.py's: constants, so
# loosening one is a visible edit.
MAX_CONTEXT_OVERFLOW_SHARE = 0.05
MIN_FINISHED_CLEANLY_SHARE = 0.90
MAX_DELIVERABLE_MISSING = 0
MAX_JUDGE_FAILURES = 0
MAX_LEAKED_CONTAINERS = 0

# Two families share the tool axis; a triage invocation gates ONE family's
# arm set (--arms, default the api family) — the pairing gate would
# otherwise demand six rows per task from a three-arm campaign. The
# unexpected-labels gate checks against the union: any registered arm is a
# valid label, anything else is a driver bug.
ARMS = ("lab-base", "lab-rg", "lab-gorp")
ARMS_ALL = ARMS + ("lab-cc-base", "lab-cc-rg", "lab-cc-gorp")
ARM_TOOL = {"lab-rg": "rg", "lab-gorp": "gorp",
            "lab-cc-rg": "rg", "lab-cc-gorp": "gorp"}

# Adaptation 2: point the imported checks at labbench's layout.
triage.DATA = DATA
triage.cond_dir = lambda row: (DATA / "runs" / row["run_id"]
                               / row["task"] / row["arm"])


def load_rows(path, run_id=None, arms=None):
    rows = {}
    for line in Path(path).read_text().splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if run_id and r.get("run_id") != run_id:
            continue
        if arms and r.get("arm") not in arms:
            continue
        # Adaptation 1: dedupe on (task, arm, model), last write wins;
        # normalize so the imported tool/distress checks (which read
        # instance_id/condition) work unchanged.
        r["instance_id"], r["condition"] = r.get("task"), r.get("arm")
        rows[(r.get("task"), r.get("arm"), r.get("model"))] = r
    return list(rows.values())


def check_harness_lab(rows, arms):
    print("\n[4/4] LAB harness health")
    st = Counter(r.get("status") for r in rows)
    for k, v in st.most_common():
        print(f"  ---   {v:4d}  {k}")
    triage.gate("non-ok rows", sum(v for k, v in st.items() if k != "ok"), 0)

    finished = [r for r in rows if r.get("finished_cleanly") is not None]
    if finished:
        share = sum(1 for r in finished if r["finished_cleanly"]) / len(finished)
        triage.gate("finished-cleanly share", share,
                    MIN_FINISHED_CLEANLY_SHARE, worse="below", pct=True)
        over = sum(1 for r in finished if r.get("context_overflow")) / len(finished)
        triage.gate("context-overflow share", over,
                    MAX_CONTEXT_OVERFLOW_SHARE, pct=True)
    else:
        # The patched metrics carry these; their absence means the metrics
        # fix did not ship, and both shares above would be vacuous.
        triage.gate("rows carrying finished_cleanly", 0, 1, worse="below",
                    detail="the 0001 patch's metrics hunk is not in effect")

    triage.gate("missing deliverables",
                sum(1 for r in rows if r.get("status") == "deliverable_missing"),
                MAX_DELIVERABLE_MISSING)
    triage.gate("judge failures",
                sum(1 for r in rows if r.get("status") == "judge_error"),
                MAX_JUDGE_FAILURES)

    # Criterion-count drift: the judge scored a different rubric than the
    # frame registered — a silently edited task.json or a partial score file.
    frame = {}
    fp = DATA / "frame.jsonl"
    if fp.exists():
        for line in fp.read_text().splitlines():
            fr = json.loads(line)
            frame[fr["task"]] = fr["n_criteria"]
    drift = [r["task"] for r in rows
             if r.get("n_criteria") is not None
             and frame.get(r["task"]) not in (None, r["n_criteria"])]
    triage.gate("criterion-count drift vs frame", len(drift), 0,
                detail=", ".join(drift[:3]))

    # A leaked sandbox is podman state the next cell inherits.
    try:
        ps = subprocess.run(["podman", "ps", "-a", "--format", "{{.Names}}"],
                            capture_output=True, text=True, timeout=60).stdout
        leaked = [n for n in ps.splitlines() if n.startswith("lab-sandbox-")]
        triage.gate("leaked lab-sandbox containers", len(leaked),
                    MAX_LEAKED_CONTAINERS, detail=", ".join(leaked[:3]))
    except (FileNotFoundError, subprocess.TimeoutExpired):
        print("  ---   podman not reachable; container check skipped")

    # Registered-arms pair (from check_harness_swex): every row's arm is
    # registered, and every task with any ok row has an ok row per arm.
    labels = {r.get("arm") for r in rows}
    bad = labels - set(ARMS_ALL)
    triage.gate("unexpected arm labels", len(bad), 0, detail=", ".join(sorted(bad)))
    done = defaultdict(set)
    for r in rows:
        if r.get("status") == "ok":
            done[r["task"]].add(r["arm"])
    incomplete = [t for t, a in done.items() if not set(arms) <= a]
    triage.gate("tasks missing an arm", len(incomplete), 0,
                detail="a driver env bug collapses every cell onto one arm "
                       "and a paired analysis dies silently; "
                       + ", ".join(incomplete[:3]))

    # Adaptation 5: invocation share per treatment arm — reported, not gated.
    for arm in arms:
        tool = ARM_TOOL.get(arm)
        if not tool:
            continue
        armed = [r for r in rows if r["arm"] == arm and r.get("status") == "ok"]
        if armed:
            used = sum(1 for r in armed
                       if (r.get("search") or {}).get(f"n_{tool}", 0) > 0)
            print(f"  ---   {arm}: {used}/{len(armed)} sessions invoked "
                  f"{tool} ({used / len(armed):.0%}) — the dilution factor")

    free_gb = os.statvfs(DATA).f_bavail * os.statvfs(DATA).f_frsize / 1e9
    triage.gate("free disk (GB)", round(free_gb, 1), 2.0, worse="below",
                detail="a campaign that fills the disk loses the chunk, "
                       "not the row")
    spend_tok = sum(r.get("total_tokens") or 0 for r in rows)
    print(f"  ---   {spend_tok/1e6:.1f}M agent tokens over {len(rows)} rows")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", type=Path, default=DATA / "results.jsonl")
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--arms", default=",".join(ARMS))
    ap.add_argument("--examples", type=int, default=3)
    ap.add_argument("--json", type=Path, default=None)
    args = ap.parse_args()

    if not args.results.exists():
        print(f"no such results file: {args.results}")
        sys.exit(2)
    arms = [a for a in args.arms.split(",") if a]
    rows = load_rows(args.results, run_id=args.run_id, arms=arms)
    if not rows:
        print(f"no rows in {args.results}"
              f"{f' for run {args.run_id}' if args.run_id else ''}")
        sys.exit(2)

    by_arm = Counter(r["arm"] for r in rows)
    print(f"triage: {len(rows)} rows "
          f"({', '.join(f'{k}={v}' for k, v in by_arm.most_common())})")

    # Adaptation 3: only the gorp arm carries engine traces.
    gorp_rows = [r for r in rows if ARM_TOOL.get(r["arm"]) == "gorp"]
    summary = {}
    if gorp_rows:
        summary |= triage.check_tool(gorp_rows, args.examples)
        summary |= triage.check_distress(gorp_rows, args.examples)
        summary |= triage.check_cache(gorp_rows)
    else:
        print("\n[1-3/4] no gorp-arm rows; tool checks skipped")
    check_harness_lab(rows, arms)

    print()
    if triage.FAILURES:
        print(f"GATE FAILED — {len(triage.FAILURES)} check(s). Do not start "
              f"the next rung until these are understood:")
        for name, value, limit, detail in triage.FAILURES:
            print(f"  · {name}: {value} (limit {limit})"
                  f"{(' — ' + detail) if detail else ''}")
    else:
        print("GATE PASSED — the harness delivered what the arms promised.")
    if args.json:
        args.json.write_text(json.dumps(
            {"summary": summary, "failures": triage.FAILURES,
             "passed": not triage.FAILURES}, indent=1))
    sys.exit(1 if triage.FAILURES else 0)


if __name__ == "__main__":
    main()
