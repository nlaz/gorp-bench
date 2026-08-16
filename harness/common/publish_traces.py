#!/usr/bin/env python3
"""Harvested agent searches -> a tiered trace set the engine repo can score.

This is the one artifact that crosses from gorp-bench into gorp. Everything
else here needs live agents, API budget and hours; a trace set needs none of
them, which is the point — an engine change should be gateable against real
agent queries without re-running a campaign.

    python3 harness/common/publish_traces.py --out ../gorp/eval/queries/traces-v1.jsonl

What it does:

  1. reads the shim logs through `harvest.py` (or a file it already wrote),
  2. joins each row's `instance_id` to the benchmark's gold,
  3. stamps `blind` / `guess` / `golden` with **gorp's** `traces.tier_of`,
     imported from the sibling checkout so one rule serves both repos,
  4. writes one JSONL row per distinct (query, instance).

Deduping by (query, instance) and not by row is deliberate. The shim logs
hold 22k invocations but far fewer distinct questions: agents retry, rephrase
around a typo, and re-run the same search after reading a file. Scoring the
raw log would weight a query by how often an agent repeated it — which is a
measure of the agent's confusion, not of the engine's retrieval. Frequency is
kept on the row (`n_invocations`) for anyone who wants to weight by it.

`--min-words 2` drops single-token invocations by default. They are 34% of
the log and almost all `-e` verification of a symbol the agent had already
found, which is exact mode doing its job rather than a retrieval question.
"""

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "locbench"))
from common import gorp_repo as common  # noqa: E402

common.path()
import traces  # noqa: E402  — gorp's tier rule, the shared one


def load_gold(dataset):
    """`instance_id -> {gold, repo, sha}` from the Loc-Bench dataset.

    `edit_functions` entries are `path.py:Qual.name`; a bare path (no colon)
    is a file-only gold, which the benchmark does emit.
    """
    gold = {}
    with open(dataset) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            raw = row.get("edit_functions") or []
            if isinstance(raw, str):
                raw = json.loads(raw.replace("'", '"'))
            files = sorted({g.split(":", 1)[0] for g in raw})
            funcs = sorted(g for g in raw if ":" in g)
            if files or funcs:
                gold[row["instance_id"]] = {
                    # What a replayer has to check out to run this query
                    # again: the tree as it was *before* the fix landed.
                    "repo": row.get("repo"),
                    "sha": row.get("base_commit"),
                    "gold": {"files": files, "funcs": funcs},
                }
    return gold


def query_of(row):
    """The question the agent actually asked.

    `patterns` holds the alternation ladder of an `a\\|b\\|c` invocation; the
    ranked engine takes the whole string, so the joined form is what it would
    have been given. A bare guess is a one-rung ladder and joins to itself.
    """
    pats = row.get("patterns") or ([row["pattern"]] if row.get("pattern") else [])
    return " ".join(p for p in pats if p).strip()


def build(rows, gold, min_words):
    """Trace rows, plus a census of what was dropped and why.

    The census is not decoration: a silent drop here becomes a missing
    stratum that reads as "agents never type that" (`harvest.py` makes the
    same argument about its own reconciliation gate).
    """
    merged, dropped = {}, defaultdict(int)
    for r in rows:
        q = query_of(r)
        if not q:
            dropped["empty-query"] += 1
            continue
        if len(q.split()) < min_words:
            dropped["too-few-words"] += 1
            continue
        entry = gold.get(r.get("instance_id"))
        if not entry:
            dropped["no-gold-for-instance"] += 1
            continue
        g = entry["gold"]
        key = (q, r["instance_id"])
        if key in merged:
            merged[key]["provenance"]["n_invocations"] += 1
            continue
        try:
            tier = traces.tier_of(q, g)
        except ValueError:
            dropped["ungoldable"] += 1
            continue
        merged[key] = {
            "id": hashlib.sha1(f"{q}\0{r['instance_id']}".encode()).hexdigest()[:16],
            "query": q,
            "tier": tier,
            "provenance": {
                "source": "harvested",
                "harness": "locbench",
                "run_id": r.get("run_id"),
                "condition": r.get("condition"),
                # The name the agent typed, kept as the recorded fact it is.
                # `semgrep` and `sg` are what the tool was called then.
                "tool": r.get("tool"),
                "kind": r.get("kind"),
                "n_invocations": 1,
            },
            "target": {
                "repo": entry["repo"],
                "sha": entry["sha"],
                "instance_id": r["instance_id"],
            },
            "gold": g,
            "invocation": {
                "mode": r.get("mode"),
                "flags": r.get("flags") or [],
                "k": r.get("k"),
                "scopes_rel": r.get("scopes_rel") or [],
            },
        }
    return list(merged.values()), dict(dropped)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--harvest", type=Path,
                    help="a file harvest.py already wrote (default: harvest live)")
    ap.add_argument("--runs", type=Path, default=common.DATA / "locbench" / "runs")
    ap.add_argument("--dataset", type=Path,
                    default=common.DATA / "locbench" / "dataset.jsonl")
    ap.add_argument("--out", type=Path, required=True,
                    help="destination, normally ../gorp/eval/queries/traces-<tag>.jsonl")
    ap.add_argument("--min-words", type=int, default=2)
    args = ap.parse_args()

    if args.harvest:
        rows = [json.loads(l) for l in open(args.harvest) if l.strip()]
    else:
        import harvest
        rows = harvest.harvest(args.runs)

    gold = load_gold(args.dataset)
    out, dropped = build(rows, gold, args.min_words)
    out.sort(key=lambda r: (r["target"]["instance_id"], r["query"]))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w") as f:
        for r in out:
            f.write(json.dumps(r, sort_keys=True) + "\n")

    counts = traces.counts(out)
    total = sum(counts.values()) or 1
    print(f"read {len(rows)} invocations -> {len(out)} distinct (query, instance)")
    print("tiers: " + "  ".join(
        f"{t}={counts[t]} ({counts[t] / total:.0%})" for t in traces.TIERS))
    if dropped:
        print("dropped: " + "  ".join(f"{k}={v}" for k, v in sorted(dropped.items())))
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
