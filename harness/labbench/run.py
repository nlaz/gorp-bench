#!/usr/bin/env python3
"""Drive LAB cells: frame × arm, agent then judge, one row per cell.

The expensive halves both live upstream — `harness.run` (the agent, in its
podman sandbox) and `evaluation.run_eval` (one judge call per rubric
criterion). This driver owns everything around them: the per-cell env that
delivers the arm and the shim contract (ToolExecutor runs in-process inside
`harness.run`, so env is the only channel), meta.json before any money is
spent, the shim-log rollup, and the results row.

Layout per cell (task keeps its slashes):

  data/labbench/runs/<RUN_ID>/<task>/<arm>/   meta.json, shim_log.jsonl,
                                              searches/, trace.jsonl
  data/labbench/upstream/results/<RUN_ID>/<task>/<arm>/
                                              upstream's own run dir:
                                              transcript, metrics.json,
                                              telemetry.json, scores.json,
                                              output/, workspace/

Resume semantics are locbench's: rows keyed (task, arm, model), `--resume`
skips status=="ok", failures count toward --max-attempts, and six
consecutive agent errors exit 3 so a campaign loop can tell an outage from
a task problem. `--limit N` is a frame prefix — the ladder mechanism.
"""

import argparse
import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import gorp_repo as common  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent / "patches"))
import lab_arms  # noqa: E402  (pure half: ARMS, ARM_TOOL, DESCS)

HERE = Path(__file__).parent
DATA = common.DATA / "labbench"
UPSTREAM = DATA / "upstream"
RG_BIN = os.environ.get("RG_BIN", "/opt/homebrew/bin/rg")
CONSECUTIVE_AGENT_ERRORS_FATAL = 6


def sha256_file(p: Path) -> str | None:
    import hashlib
    if not p.exists():
        return None
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16]


def read_shim_log(cell_dir: Path) -> dict:
    """Same rollup shape as sg_arms._read_shim_log, per-tool counts by name."""
    rows = []
    log = cell_dir / "shim_log.jsonl"
    if log.exists():
        for line in log.read_text(errors="ignore").splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    live = [r for r in rows if not r.get("blocked")]
    return {
        "n_invocations": len(live),
        "n_blocked": sum(1 for r in rows if r.get("blocked")),
        "n_gorp": sum(1 for r in live if r.get("tool") == "gorp"),
        "n_rg": sum(1 for r in live if r.get("tool") == "rg"),
        "n_empty": sum(1 for r in live if not r.get("stdout_bytes")),
        "total_stdout_bytes": sum(r.get("stdout_bytes") or 0 for r in live),
        "total_search_ms": round(sum(r.get("wall_ms") or 0 for r in live), 1),
        "n_nonzero_exit": sum(1 for r in live if r.get("exit")),
    }


def cell_env(arm: str, cell_dir: Path, run_id: str) -> dict:
    env = dict(os.environ)
    env.update({
        "LABBENCH_ARM": arm,
        "LABBENCH_DESC": os.environ.get("LABBENCH_DESC", "v1"),
        "LOCBENCH_SHIM_LOG": str(cell_dir / "shim_log.jsonl"),
        "LOCBENCH_STDOUT_DIR": str(cell_dir / "searches"),
        "GORP_NO_HINTS": "1",  # the self-teaching footer is a treatment (§16.10)
        # Sibling of runs/, not inside it — same lesson as sg_arms._env:
        # a cache dir under runs/<id>/ reads as an instance to any glob.
        "GORP_CACHE_DIR": str(DATA / "cache" / run_id / arm),
        "GORP_TRACE_FILE": str(cell_dir / "trace.jsonl"),
    })
    # Only this arm's tool gets a real binding; the other name stays blocked
    # in the shim, so an arm escaping its treatment shows as a blocked row,
    # never a silent success.
    for t in ("GORP", "RG"):
        env.pop(f"LOCBENCH_REAL_{t}", None)
        env.pop(f"LOCBENCH_{t}_FLAGS", None)
    tool = lab_arms.ARM_TOOL.get(arm)
    if tool == "gorp":
        env["LOCBENCH_REAL_GORP"] = str(common.BIN)
        flags = os.environ.get("LABBENCH_GORP_FLAGS", "")
        if flags:
            env["LOCBENCH_GORP_FLAGS"] = flags
    elif tool == "rg":
        env["LOCBENCH_REAL_RG"] = RG_BIN
    return env


def run_cell(row_frame: dict, arm: str, args, run_id: str) -> dict:
    task = row_frame["task"]
    cell_dir = DATA / "runs" / run_id / task / arm
    (cell_dir / "searches").mkdir(parents=True, exist_ok=True)
    upstream_run_id = f"{run_id}/{task}/{arm}"
    upstream_run_dir = UPSTREAM / "results" / upstream_run_id

    manifest_sha = sha256_file(DATA / "corpus-manifest.json")
    (cell_dir / "meta.json").write_text(json.dumps({
        "task": task, "arm": arm, "model": args.model,
        "judge_model": args.judge_model, "run_id": run_id,
        "upstream_run_id": upstream_run_id,
        "upstream_sha": subprocess.run(
            ["git", "-C", str(UPSTREAM), "rev-parse", "HEAD"],
            capture_output=True, text=True).stdout.strip(),
        "corpus": lab_arms.CORPUS, "corpus_manifest_sha256": manifest_sha,
        "desc_version": os.environ.get("LABBENCH_DESC", "v1"),
        "max_turns": args.max_turns, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }, indent=1) + "\n")

    env = cell_env(arm, cell_dir, run_id)
    row = {
        "run_id": run_id, "task": task, "arm": arm, "model": args.model,
        "judge_model": args.judge_model, "corpus": lab_arms.CORPUS,
        "area": row_frame.get("area"), "n_docs": row_frame.get("n_docs"),
        "status": "ok", "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "cell_dir": str(cell_dir), "upstream_run_dir": str(upstream_run_dir),
    }

    t0 = time.time()
    agent_log = cell_dir / "agent_stdout.log"
    try:
        with agent_log.open("wb") as f:
            p = subprocess.run(
                ["uv", "run", "python", "-m", "harness.run",
                 "--model", args.model, "--task", task,
                 "--run-id", upstream_run_id,
                 "--max-turns", str(args.max_turns)],
                cwd=str(UPSTREAM), env=env, stdout=f, stderr=subprocess.STDOUT,
                timeout=args.timeout)
        if p.returncode != 0:
            row["status"] = "agent_error"
    except subprocess.TimeoutExpired:
        row["status"] = "timeout"
    row["harness_wall_s"] = round(time.time() - t0, 1)

    metrics = _load_json(upstream_run_dir / "metrics.json")
    if metrics:
        row.update({k: metrics.get(k) for k in (
            "turn_count", "input_tokens", "output_tokens", "total_tokens",
            "wall_clock_seconds", "finished_cleanly", "context_overflow",
            "documents_read", "total_documents", "files_written",
            "lab_searches", "lab_searches_empty")})
    elif row["status"] == "ok":
        row["status"] = "agent_error"  # exited 0 without metrics: not a run

    telemetry = _load_json(upstream_run_dir / "telemetry.json")
    if telemetry:
        row["warm"] = telemetry.get("warm")
        if telemetry.get("arm") != arm:
            # The env channel failed: whatever ran, it was not this arm.
            row["status"] = "harness_error"
            row["telemetry_arm"] = telemetry.get("arm")

    out_dir = upstream_run_dir / "output"
    row["deliverables_present"] = out_dir.is_dir() and any(out_dir.iterdir())
    if row["status"] == "ok" and not row["deliverables_present"]:
        row["status"] = "deliverable_missing"

    if row["status"] == "ok" and not args.no_judge:
        try:
            j = subprocess.run(
                ["uv", "run", "python", "-m", "evaluation.run_eval",
                 "--run-id", upstream_run_id, "--task", task,
                 "--judge-model", args.judge_model],
                cwd=str(UPSTREAM), env=env, capture_output=True,
                timeout=args.judge_timeout)
            scores = _load_json(upstream_run_dir / "scores.json")
            if j.returncode != 0 or not scores or "all_pass" not in scores:
                row["status"] = "judge_error"
                row["judge_stderr"] = j.stderr.decode(errors="ignore")[-400:]
            else:
                n = scores.get("n_criteria") or 0
                row.update(
                    all_pass=bool(scores["all_pass"]),
                    n_criteria=n, n_passed=scores.get("n_passed"),
                    criterion_pass_rate=round(scores.get("n_passed", 0) / n, 4)
                    if n else None)
        except subprocess.TimeoutExpired:
            row["status"] = "judge_error"
            row["judge_stderr"] = "judge timeout"

    row["search"] = read_shim_log(cell_dir)
    return row


def _load_json(p: Path):
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def append_row(out: Path, row: dict) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(json.dumps(row) + "\n")
        fcntl.flock(f, fcntl.LOCK_UN)


def load_done(out: Path):
    done, attempts = set(), Counter()
    if out.exists():
        for line in out.read_text().splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = (r.get("task"), r.get("arm"), r.get("model"))
            if r.get("status") == "ok":
                done.add(key)
            else:
                attempts[key] += 1
    return done, attempts


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--frame", default=str(DATA / "frame.jsonl"))
    ap.add_argument("--arms", default="lab-base,lab-rg,lab-gorp")
    ap.add_argument("--limit", type=int, help="frame prefix (the rung)")
    ap.add_argument("--model", default="anthropic/claude-sonnet-4-6")
    ap.add_argument("--judge-model", default="claude-sonnet-4-6")
    ap.add_argument("--run-id", default=time.strftime("%Y%m%d-%H%M%S"))
    ap.add_argument("--max-turns", type=int, default=200)
    ap.add_argument("--timeout", type=int, default=5400,
                    help="wall seconds per agent run")
    ap.add_argument("--judge-timeout", type=int, default=1800)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--max-new", type=int, default=0,
                    help="stop after N new cells (0 = no cap)")
    ap.add_argument("--max-attempts", type=int, default=3)
    ap.add_argument("--no-judge", action="store_true")
    ap.add_argument("--out", default=str(DATA / "results.jsonl"))
    ap.add_argument("--disk-floor-gb", type=float, default=2.0)
    args = ap.parse_args()

    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    unknown = [a for a in arms if a not in lab_arms.ARMS]
    if unknown:
        sys.exit(f"unknown arms {unknown}; registered: {sorted(lab_arms.ARMS)}")
    if any(lab_arms.ARM_TOOL.get(a) == "gorp" for a in arms):
        common.require_bin()
    if any(lab_arms.ARM_TOOL.get(a) == "rg" for a in arms) \
            and not Path(RG_BIN).exists():
        sys.exit(f"no ripgrep at {RG_BIN} (set RG_BIN)")
    if not (UPSTREAM / "lab_arms.py").exists():
        sys.exit("upstream not vendored/patched — run fetch.sh first")
    if not (DATA / "corpus-manifest.json").exists():
        sys.exit("no corpus manifest — run build_corpus.py first")

    frame = [json.loads(l) for l in Path(args.frame).read_text().splitlines()
             if l.strip()]
    if args.limit:
        frame = frame[:args.limit]
    out = Path(args.out)
    done, attempts = load_done(out)

    cells = [(fr, arm) for fr in frame for arm in arms]
    consecutive_agent_errors = 0
    n_new = 0
    for fr, arm in cells:
        key = (fr["task"], arm, args.model)
        if args.resume and key in done:
            continue
        if attempts[key] >= args.max_attempts:
            print(f"abandon {fr['task']} × {arm}: {attempts[key]} attempts")
            continue
        free_gb = shutil.disk_usage(DATA).free / 1e9
        if free_gb < args.disk_floor_gb:
            sys.exit(f"disk floor: {free_gb:.1f} GB free < {args.disk_floor_gb}")
        if args.max_new and n_new >= args.max_new:
            print(f"--max-new {args.max_new} reached")
            break

        print(f"[{n_new + 1}] {fr['task']} × {arm}", flush=True)
        row = run_cell(fr, arm, args, args.run_id)
        append_row(out, row)
        n_new += 1
        if row["status"] in ("agent_error", "timeout", "harness_error"):
            consecutive_agent_errors += 1
            if consecutive_agent_errors >= CONSECUTIVE_AGENT_ERRORS_FATAL:
                # An outage, not a task problem: stop burning budget and let
                # the campaign loop see a distinct exit code.
                sys.exit(3)
        else:
            consecutive_agent_errors = 0
        print(f"    {row['status']}"
              + (f"  all_pass={row.get('all_pass')}"
                 f" crit={row.get('criterion_pass_rate')}"
                 if "all_pass" in row else "")
              + f"  searches={row['search']['n_invocations']}"
                f" (blocked {row['search']['n_blocked']})", flush=True)

    print(f"done: {n_new} new cells -> {out}")


if __name__ == "__main__":
    main()
