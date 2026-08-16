"""Guards the LAB campaign gates with fabricated logs, because a vacuous
0/0 gate passed §16.10.

triage_lab.py is what stands between a broken rung and the next rung's
spend, so every gate is exercised here in both directions: a healthy run
passes, and each specific failure — context overflow, a missing
deliverable, every cell collapsed onto one arm, engine traces absent while
searches ran — trips its own gate and no other's. The loader's dedupe is
pinned too: --resume appends, so last-write-wins keyed (task, arm, model)
is what keeps a retried cell from counting twice.

Rows and cell dirs are built under tmp_path in the driver's real shapes
(run.py's results row, shim.py's log record, gorp's trace envelope); if a
field is renamed there, these builders are the place that breaks first.
"""
import importlib
import json
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BENCH / "harness" / "labbench"))
sys.path.insert(0, str(BENCH / "harness" / "locbench"))
sys.path.insert(0, str(BENCH / "harness"))

import triage  # noqa: E402
import triage_lab  # noqa: E402


def _reset(tmp_path):
    """Fresh gate state and a triage pointed at a scratch data dir."""
    triage.FAILURES.clear()
    triage_lab.DATA = tmp_path
    triage.DATA = tmp_path
    triage.cond_dir = lambda row: (tmp_path / "runs" / row["run_id"]
                                   / row["task"] / row["arm"])


def make_row(task, arm, status="ok", **over):
    row = {
        "run_id": "t1", "task": task, "arm": arm,
        "model": "anthropic/claude-sonnet-4-6", "status": status,
        "all_pass": False, "n_criteria": 5, "n_passed": 3,
        "criterion_pass_rate": 0.6, "finished_cleanly": True,
        "context_overflow": False, "deliverables_present": True,
        "total_tokens": 100000,
        "search": {"n_invocations": 2, "n_blocked": 0,
                   "n_gorp": 2 if arm == "lab-gorp" else 0,
                   "n_rg": 2 if arm == "lab-rg" else 0,
                   "n_empty": 0},
    }
    row.update(over)
    return row


def make_cell(tmp_path, row, searches=2, with_traces=True, empty=0):
    """A cell dir with a shim log and (optionally) trace envelopes."""
    d = tmp_path / "runs" / row["run_id"] / row["task"] / row["arm"]
    d.mkdir(parents=True, exist_ok=True)
    tool = {"lab-gorp": "gorp", "lab-rg": "rg"}.get(row["arm"])
    if not tool:
        return
    shim, traces = [], []
    for i in range(searches):
        is_empty = i < empty
        shim.append({"seq": i, "tool": tool, "blocked": False,
                     "argv": ["--json", "-k", "10", f"query {i}", "."],
                     "exit": 0, "stdout_bytes": 0 if is_empty else 400,
                     "wall_ms": 90.0})
        traces.append({"kind": "search",
                       "input": {"mode": "ranked", "query": f"query {i}",
                                 "root": "."},
                       "results": {"n_hits": 0 if is_empty else 5,
                                   "files_walked": 12,
                                   "n_chunks_considered": 0 if is_empty else 40}})
    (d / "shim_log.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in shim))
    if with_traces and tool == "gorp":
        (d / "trace.jsonl").write_text(
            "".join(json.dumps(e) + "\n" for e in traces))


def write_results(tmp_path, rows):
    out = tmp_path / "results.jsonl"
    out.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return out


def healthy_rows(tmp_path, n_tasks=3):
    rows = []
    for i in range(n_tasks):
        for arm in ("lab-base", "lab-rg", "lab-gorp"):
            row = make_row(f"area/task-{i}", arm)
            make_cell(tmp_path, row)
            rows.append(row)
    return rows


def run_gates(tmp_path, rows, arms=("lab-base", "lab-rg", "lab-gorp")):
    _reset(tmp_path)
    loaded = triage_lab.load_rows(write_results(tmp_path, rows), arms=list(arms))
    gorp_rows = [r for r in loaded if r["arm"] == "lab-gorp"]
    if gorp_rows:
        triage.check_tool(gorp_rows, 0)
        triage.check_distress(gorp_rows, 0)
    triage_lab.check_harness_lab(loaded, list(arms))
    return [f[0] for f in triage.FAILURES]


def test_a_healthy_rung_passes_every_gate(tmp_path):
    assert run_gates(tmp_path, healthy_rows(tmp_path)) == []


def test_context_overflow_share_above_five_percent_trips_only_its_gate(tmp_path):
    rows = healthy_rows(tmp_path, n_tasks=4)
    rows[0]["context_overflow"] = True  # 1/12 = 8.3%
    failed = run_gates(tmp_path, rows)
    assert failed == ["context-overflow share"]


def test_a_missing_deliverable_trips_the_gate_and_the_non_ok_gate(tmp_path):
    rows = healthy_rows(tmp_path)
    rows[0]["status"] = "deliverable_missing"
    failed = run_gates(tmp_path, rows)
    assert "missing deliverables" in failed
    assert "non-ok rows" in failed


def test_every_cell_collapsed_onto_one_arm_fails_the_pairing_gate(tmp_path):
    # The driver env bug this guards: LABBENCH_ARM never arrives, every
    # row records lab-base, and a naive mean would still print numbers.
    rows = []
    for i in range(3):
        row = make_row(f"area/task-{i}", "lab-base")
        rows.append(row)
    failed = run_gates(tmp_path, rows)
    assert "tasks missing an arm" in failed


def test_searches_without_trace_envelopes_fail_instead_of_passing_vacuously(tmp_path):
    rows = []
    for i in range(2):
        row = make_row(f"area/task-{i}", "lab-gorp")
        make_cell(tmp_path, row, with_traces=False)
        rows.append(row)
    failed = run_gates(tmp_path, rows, arms=("lab-gorp",))
    assert "engine traces present" in failed


def test_empty_ranked_share_reads_traces_and_trips_above_two_percent(tmp_path):
    rows = []
    for i in range(2):
        row = make_row(f"area/task-{i}", "lab-gorp")
        make_cell(tmp_path, row, searches=10, empty=2)  # 20% empty
        rows.append(row)
    failed = run_gates(tmp_path, rows, arms=("lab-gorp",))
    assert "ranked searches returning nothing" in failed


def test_judge_error_rows_trip_their_gate(tmp_path):
    rows = healthy_rows(tmp_path)
    rows[-1]["status"] = "judge_error"
    failed = run_gates(tmp_path, rows)
    assert "judge failures" in failed


def test_criterion_count_drift_against_the_frame_trips(tmp_path):
    rows = healthy_rows(tmp_path)
    (tmp_path / "frame.jsonl").write_text("".join(
        json.dumps({"task": f"area/task-{i}", "n_criteria": 5}) + "\n"
        for i in range(3)))
    rows[2]["n_criteria"] = 4  # judge scored a different rubric
    failed = run_gates(tmp_path, rows)
    assert "criterion-count drift vs frame" in failed


def test_the_loader_dedupes_last_write_wins_per_task_arm_model(tmp_path):
    first = make_row("area/task-0", "lab-base", status="agent_error")
    retry = make_row("area/task-0", "lab-base", status="ok")
    out = write_results(tmp_path, [first, retry])
    loaded = triage_lab.load_rows(out)
    assert len(loaded) == 1
    assert loaded[0]["status"] == "ok"


def test_rows_carrying_no_finished_cleanly_fail_the_metrics_patch_gate(tmp_path):
    rows = [make_row(f"area/task-{i}", arm, finished_cleanly=None)
            for i in range(2) for arm in ("lab-base", "lab-rg", "lab-gorp")]
    for r in rows:
        make_cell(tmp_path, r)
    failed = run_gates(tmp_path, rows)
    assert "rows carrying finished_cleanly" in failed
