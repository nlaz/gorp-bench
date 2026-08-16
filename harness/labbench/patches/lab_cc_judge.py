#!/usr/bin/env python3
"""Judge a LAB run through the `claude` CLI — the subscription-funded judge.

Everything that decides a verdict is upstream's, reused in-process:
`evaluation.scoring.score_rubric` does the deliverable matching, the
per-criterion context loading (same extractors), and the aggregation; the
prompt is `evaluation/prompts/rubric_criterion.txt` verbatim; the JSON
parsing is `Judge._parse_json` (a staticmethod — no API client is ever
constructed). The ONLY substitution is the transport: `score_rubric` calls
`judge.evaluate_from_file(...)`, and this file's `CliJudge` satisfies that
one-method contract by piping the formatted prompt through `claude -p`
(operator's Claude Code login) instead of the SDK.

Known deviations from upstream's judge, recorded in scores.json:
  * temperature is the CLI's default, not 0.0 (the CLI does not expose it);
  * no schema-enforced output — the verdict JSON is parsed from text, with
    one retry, and an unparseable answer counts as "fail" (never "pass":
    a broken judge must not flatter the tool under test).
scores.json gets judge_model "cc/<model>" so lab-cc rows are never pooled
with API-judged rows by accident.

    uv run python lab_cc_judge.py --run-id <id> --task <area>/<slug>
"""

import argparse
import json
import subprocess
from dataclasses import asdict
from pathlib import Path

from evaluation.judge import Judge
from evaluation.scoring import score_rubric
from harness.run import BENCH_ROOT, load_task

PROMPTS = BENCH_ROOT / "evaluation" / "prompts"


class CliJudge:
    """The one-method duck type score_rubric needs, over `claude -p`."""

    def __init__(self, model: str, timeout: int = 600):
        self.model = model
        self.timeout = timeout

    def _ask(self, prompt: str) -> str:
        p = subprocess.run(
            ["claude", "-p", "--model", self.model, "--max-turns", "1",
             "--setting-sources", "", "--strict-mcp-config",
             "--no-session-persistence"],
            input=prompt.encode(), capture_output=True, timeout=self.timeout)
        if p.returncode != 0:
            raise RuntimeError(
                f"claude -p exit {p.returncode}: "
                f"{p.stderr.decode(errors='ignore')[-200:]}")
        return p.stdout.decode(errors="ignore")

    def evaluate_from_file(self, prompt_name: str, variables: dict) -> dict:
        template = (PROMPTS / f"{prompt_name}.txt").read_text(encoding="utf-8")
        prompt = template.format(**variables)
        last = ""
        for _ in range(2):  # one retry on transport or parse failure
            try:
                last = self._ask(prompt)
                parsed = Judge._parse_json(last)
                if parsed.get("verdict"):
                    return parsed
            except (RuntimeError, subprocess.TimeoutExpired,
                    json.JSONDecodeError, ValueError):
                continue
        return {"verdict": "fail",
                "reasoning": f"judge unparseable after retry: {last[:160]}"}


def main(args):
    task = load_task(task_name=args.task)
    run_dir = BENCH_ROOT / "results" / args.run_id
    if not (run_dir / "metrics.json").exists():
        raise SystemExit(f"no completed run at {run_dir}")

    result = score_rubric(
        criteria=task["config"]["criteria"],
        run_dir=run_dir,
        judge=CliJudge(args.judge_model),
        task_desc=task["config"].get("title", args.task),
        parallel=args.parallel,
    )
    n_criteria = len(result.criteria_results)
    n_passed = sum(1 for c in result.criteria_results
                   if c["verdict"] == "pass")
    scores = {
        "task": args.task,
        "run_id": args.run_id,
        "judge_model": f"cc/{args.judge_model}",
        "all_pass": n_criteria > 0 and n_passed == n_criteria,
        "n_criteria": n_criteria,
        "n_passed": n_passed,
        "criteria_results": [c if isinstance(c, dict) else asdict(c)
                             for c in result.criteria_results],
    }
    (run_dir / "scores.json").write_text(json.dumps(scores, indent=2))
    print(f"judged {args.task}: {n_passed}/{n_criteria} criteria"
          + ("  ALL-PASS" if scores["all_pass"] else ""))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--task", required=True)
    ap.add_argument("--judge-model", default="sonnet")
    ap.add_argument("--parallel", type=int, default=4)
    main(ap.parse_args())
