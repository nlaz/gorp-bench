#!/usr/bin/env python3
"""LAB endpoints: paired arm contrasts over the results rows.

The statistics are locbench's (`boot_ci`, `mcnemar` from ab_analyze.py —
imported, never forked) applied to LAB's endpoints. The registered families:

  PRIMARY    all_pass         gorp − rg: ranking vs exact, same tool shape
  COPRIMARY  total_tokens, turn_count      (cost of getting there)
  SECONDARY  criterion_pass_rate, wall_clock_seconds, doc_read_share
             (Holm-corrected within the family)

Contrasts are paired on the tasks every arm completed, so an in-flight rung
analyzes the finished intersection rather than mixing ns. The mediation
section conditions on the treatment arm actually invoking its tool — from
the shim logs, via the row's `search` block — and is labelled exploratory
because conditioning on post-treatment behaviour is not a registered
endpoint, it is the mechanism question.

    python3 harness/labbench/analyze_lab.py --run-id lab1
"""

import argparse
import collections
import json
import math
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import gorp_repo as common  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "locbench"))
from ab_analyze import boot_ci, mcnemar  # noqa: E402

DATA = common.DATA / "labbench"

# Families are analyzed separately, never pooled: the loop is a treatment.
# --family cc swaps every arm name; the contrast structure is identical.
FAMILY_PREFIX = {"api": "lab-", "cc": "lab-cc-"}
ARMS = ("lab-base", "lab-rg", "lab-gorp")
ARM_TOOL = {"lab-rg": "rg", "lab-gorp": "gorp",
            "lab-cc-rg": "rg", "lab-cc-gorp": "gorp"}
# Arm names contain hyphens; contrast pairs separate with ':' if overridden.
CONTRASTS = (("lab-gorp", "lab-rg", "PRIMARY  gorp − rg (ranking vs exact)"),
             ("lab-rg", "lab-base", "confound rg − base (search tool at all)"),
             ("lab-gorp", "lab-base", "product  gorp − base"))


def family_arms(family):
    prefix = FAMILY_PREFIX[family]
    rename = lambda n: n.replace("lab-", prefix, 1)  # noqa: E731
    arms = tuple(rename(a) for a in ARMS)
    contrasts = tuple((rename(a), rename(b), label)
                      for a, b, label in CONTRASTS)
    return arms, contrasts
PRIMARY = ("all_pass", "all-pass")
COPRIMARY = (("total_tokens", "tokens"), ("turn_count", "turns"))
SECONDARY = (("criterion_pass_rate", "crit-pass"),
             ("wall_clock_seconds", "wall s"),
             ("doc_read_share", "docs-read"))


def val(row, key):
    if key == "all_pass":
        return 1.0 if row.get("all_pass") else 0.0
    if key == "doc_read_share":
        total = row.get("total_documents") or 0
        return (row.get("documents_read") or 0) / total if total else None
    return row.get(key)


def load(results, run_id):
    rows = {}
    for line in Path(results).read_text().splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if run_id and r.get("run_id") != run_id:
            continue
        if r.get("status") == "ok":
            rows[(r["task"], r["arm"])] = r
    by_arm = collections.defaultdict(dict)
    for (task, arm), r in rows.items():
        by_arm[arm][task] = r
    return by_arm


def pairs_for(by_arm, a, b, key, tasks):
    out = []
    for t in tasks:
        x, y = val(by_arm[a][t], key), val(by_arm[b][t], key)
        if x is not None and y is not None:
            out.append((x, y))
    return out


def line(label, pairs, holm_note=""):
    if not pairs:
        print(f"    {label:<12} (no pairs)")
        return None
    d, lo, hi = boot_ci(pairs)
    w, l, p = mcnemar(pairs)
    star = "*" if not (lo <= 0 <= hi) else " "
    sd = st.pstdev([x - y for x, y in pairs]) if len(pairs) > 1 else 0.0
    mde = 2.80 * sd / math.sqrt(len(pairs)) if pairs else float("nan")
    # The two tests can disagree: the bootstrap carries magnitude, the sign
    # test direction. Both printed; a starred CI with more losses than wins
    # is a handful of tasks moving far, not a consistent effect.
    flag = "  <- sign disagrees" if (star == "*" and
                                     ((d > 0) != (w > l)) and w + l > 0) else ""
    print(f"    {label:<12} Δ={d:+.3f} [{lo:+.3f},{hi:+.3f}]{star} "
          f"w/l={w}/{l} p={p:.3f} n={len(pairs)} mde={mde:.3f}"
          f"{holm_note}{flag}")
    return p


def holm(ps):
    """Holm-adjusted significance marks for the secondary family."""
    order = sorted((p, i) for i, p in enumerate(ps) if p is not None)
    marks = [""] * len(ps)
    m = len(order)
    for rank, (p, i) in enumerate(order):
        if p * (m - rank) < 0.05:
            marks[i] = "  (holm<.05)"
        else:
            break
    return marks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", type=Path, default=DATA / "results.jsonl")
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--family", choices=sorted(FAMILY_PREFIX), default="api",
                    help="which arm family to analyze (never pooled)")
    args = ap.parse_args()

    arms, contrasts = family_arms(args.family)
    base_arm, rg_arm, gorp_arm = arms
    if not args.results.exists():
        sys.exit(f"no such results file: {args.results}")
    by_arm = load(args.results, args.run_id)
    missing = [a for a in arms if not by_arm.get(a)]
    if missing:
        sys.exit(f"no ok rows for arm(s) {missing}"
                 f"{f' in run {args.run_id}' if args.run_id else ''}")
    common_tasks = sorted(set.intersection(
        *(set(by_arm[a]) for a in arms)))
    print(f"analyze [{args.family}]: {len(common_tasks)} tasks complete "
          f"across all {len(arms)} arms "
          f"({', '.join(f'{a}={len(by_arm[a])}' for a in arms)})")
    if not common_tasks:
        sys.exit("nothing to pair yet")

    base_rate = sum(val(by_arm[base_arm][t], "all_pass")
                    for t in common_tasks) / len(common_tasks)
    print(f"{base_arm} all-pass rate: {base_rate:.1%} (the anchor)\n")

    for a, b, label in contrasts:
        print(f"  {label}")
        line(PRIMARY[1], pairs_for(by_arm, a, b, PRIMARY[0], common_tasks))
        for key, lbl in COPRIMARY:
            line(lbl, pairs_for(by_arm, a, b, key, common_tasks))
        sec = [(lbl, pairs_for(by_arm, a, b, key, common_tasks))
               for key, lbl in SECONDARY]
        ps = []
        printed = []
        for lbl, pairs in sec:
            if not pairs:
                ps.append(None)
                printed.append((lbl, pairs))
                continue
            _, _, p = mcnemar(pairs)
            ps.append(p)
            printed.append((lbl, pairs))
        marks = holm(ps)
        for (lbl, pairs), mark in zip(printed, marks):
            line(lbl, pairs, holm_note=mark)
        print()

    print("  mediation (exploratory — conditions on post-treatment behaviour)")
    for arm in arms:
        tool = ARM_TOOL.get(arm)
        if not tool:
            continue
        rows = [by_arm[arm][t] for t in common_tasks]
        invoked = [r["task"] for r in rows
                   if (r.get("search") or {}).get(f"n_{tool}", 0) > 0]
        share = len(invoked) / len(rows) if rows else 0.0
        print(f"    {arm}: invoked {tool} in {len(invoked)}/{len(rows)} "
              f"sessions ({share:.0%})")
        if invoked and arm == gorp_arm:
            sub = [t for t in invoked if t in by_arm[rg_arm]]
            print("      primary over the invoking subset:")
            line("      " + PRIMARY[1],
                 pairs_for(by_arm, gorp_arm, rg_arm, PRIMARY[0], sub))


if __name__ == "__main__":
    main()
