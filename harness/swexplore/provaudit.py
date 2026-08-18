#!/usr/bin/env python3
"""The mixture gate: did every cell of this run actually share one toolchain?

`triage_swex.py` asks whether the harness was *healthy*. This asks whether the
campaign was *one campaign* — because a rung that half-ran on a rebuilt binary,
or picked up a new `claude` release overnight, or resolved a model alias
differently on Tuesday than on Monday, is two experiments reported as one, and
nothing else in the harness would notice.

That is not hypothetical. Every campaign s27..s33 recorded `"model": "sonnet"`
and nothing else; the alias in fact resolved to `claude-sonnet-5` throughout,
which is only discoverable by reading a raw transcript. RESEARCH.md §32.3 then
calibrated the `cc` arm against the paper's *Sonnet-4.5* row and called it
apples-to-apples. The comparison was wrong, the data to catch it was on disk,
and no gate read it. This is that gate.

Contract is triage's: exits nonzero so it stops a campaign rather than
describing one.

    python3 harness/swexplore/provaudit.py --run-id s36 --arms cc,cc-gorp
    python3 harness/swexplore/provaudit.py --run-id s36 --json audit.json
"""

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "harness"))
from common import gorp_repo as common  # noqa: E402

DATA = common.DATA / "swexplore"

# Fields that must be constant across every cell of one (run, arm). Each is a
# treatment variable or a toolchain identity: if it moves mid-run, the rows on
# either side of the move are not comparable and pooling them is the error.
#
# `desc_version` and the flag fields matter most, because they are the ones an
# operator changes deliberately between passes and then forgets — an env var
# set in one shell and not the next leaves no other trace on disk.
INVARIANT = (
    "model_resolved",
    "claude_version",
    "gorp_sha256",
    "desc_version",
    "sg_search_flags",
    "sg_index_flags",
    "unblock_grep",
    "tools",
)
# Nested one level down, under the key named first.
INVARIANT_NESTED = (
    ("gorp_version", "commit"),
    ("gorp_version", "compat_key"),
    ("gorp_version", "git_dirty"),
    ("index", "index_flags"),
)


def cells(run_id, arms):
    """Every meta.json under runs/<run>/<instance>/<arm>/, by arm."""
    root = DATA / "runs" / run_id
    if not root.is_dir():
        sys.exit(f"no run directory at {root}")
    out = defaultdict(list)
    for meta in sorted(root.glob("*/*/meta.json")):
        arm = meta.parent.name
        if arms and arm not in arms:
            continue
        try:
            rec = json.loads(meta.read_text())
        except (json.JSONDecodeError, OSError) as e:
            out[arm].append({"_unreadable": f"{meta}: {e}",
                             "instance_id": meta.parent.parent.name})
            continue
        rec.setdefault("instance_id", meta.parent.parent.name)
        out[arm].append(rec)
    return out


def _get(rec, key):
    if isinstance(key, tuple):
        outer, inner = key
        v = rec.get(outer)
        return (v or {}).get(inner) if isinstance(v, dict) else None
    return rec.get(key)


def _label(key):
    return ".".join(key) if isinstance(key, tuple) else key


def audit(run_id, arms):
    by_arm = cells(run_id, arms)
    failures, summary = [], {}

    if not by_arm:
        failures.append(f"no cells found for run {run_id} (arms={sorted(arms) or 'any'})")
        return failures, summary

    for arm in sorted(arms) if arms else sorted(by_arm):
        recs = by_arm.get(arm) or []
        if not recs:
            failures.append(f"{arm}: registered arm has no cells")
            continue
        bad = [r["_unreadable"] for r in recs if "_unreadable" in r]
        failures += [f"{arm}: unreadable meta.json — {b}" for b in bad]
        recs = [r for r in recs if "_unreadable" not in r]
        info = {"n_cells": len(recs)}

        for key in (*INVARIANT, *INVARIANT_NESTED):
            seen = defaultdict(list)
            for r in recs:
                seen[json.dumps(_get(r, key), sort_keys=True)].append(r["instance_id"])
            info[_label(key)] = (
                json.loads(next(iter(seen))) if len(seen) == 1 else
                {v: len(ids) for v, ids in seen.items()})
            if len(seen) > 1:
                detail = "; ".join(
                    f"{v} in {len(ids)} cells (e.g. {ids[0]})"
                    for v, ids in sorted(seen.items(), key=lambda kv: -len(kv[1])))
                failures.append(
                    f"{arm}: {_label(key)} is not constant across the run — {detail}")

        # A null resolved model means the transcript never carried an init
        # event: the cell ran, but what it ran on is unrecorded. That is the
        # exact hole this whole gate exists to close, so it is a failure and
        # not a warning.
        missing = [r["instance_id"] for r in recs if not r.get("model_resolved")]
        if missing:
            failures.append(
                f"{arm}: {len(missing)} cells recorded no resolved model "
                f"(e.g. {missing[0]}) — the alias was never pinned to a real id")

        # The requested alias and what it resolved to must be recorded as two
        # distinct facts; a cell carrying only the alias is pre-§36 data.
        if any("model_requested" not in r for r in recs):
            failures.append(f"{arm}: cells predate the §36 provenance fields")
        summary[arm] = info

    # Across arms, the engine and the CLI must still be the same one, or the
    # contrast is confounded by the toolchain rather than by the treatment.
    for key in ("gorp_sha256", "claude_version", "model_resolved"):
        vals = {a: i.get(key) for a, i in summary.items() if not isinstance(i.get(key), dict)}
        if len(set(map(json.dumps, vals.values()))) > 1:
            failures.append(
                f"arms disagree on {key}: {vals} — the contrast is between "
                f"toolchains, not between arms")

    prov = DATA / "runs" / run_id / "provenance.json"
    summary["_run"] = json.loads(prov.read_text()) if prov.exists() else None
    if summary["_run"] is None:
        failures.append(f"no provenance.json for run {run_id}")
    else:
        up = summary["_run"].get("upstream") or {}
        unexpected = [d for d in (up.get("delta") or [])
                      if d not in {"M eval_runner.py", "?? _sg_repos.py",
                                   "?? explorers/sg_arms.py",
                                   "?? explorers/sg_static.py"}]
        if unexpected:
            failures.append(
                f"vendored upstream carries an unregistered delta: {unexpected}")
        if (summary["_run"].get("gorp_version") or {}).get("git_dirty"):
            failures.append(
                "the binary under test was built from a dirty tree — it "
                "corresponds to no commit and the run cannot be reproduced")
    return failures, summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--arms", default="",
                    help="comma-separated; default audits whatever is on disk")
    ap.add_argument("--json", help="write the summary here")
    a = ap.parse_args()
    arms = {x.strip() for x in a.arms.split(",") if x.strip()}

    failures, summary = audit(a.run_id, arms)

    for arm in sorted(k for k in summary if k != "_run"):
        i = summary[arm]
        print(f"{arm:>10}  {i['n_cells']:>4} cells  "
              f"model={i.get('model_resolved')}  desc={i.get('desc_version')}  "
              f"gorp={str(i.get('gorp_version.commit'))}")
    if a.json:
        Path(a.json).parent.mkdir(parents=True, exist_ok=True)
        Path(a.json).write_text(json.dumps(
            {"run_id": a.run_id, "failures": failures,
             "passed": not failures, "summary": summary}, indent=1) + "\n")
    print()
    if failures:
        print(f"PROVENANCE AUDIT FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("provenance audit passed — one toolchain, one treatment, all cells")
    return 0


if __name__ == "__main__":
    sys.exit(main())
