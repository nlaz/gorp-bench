#!/usr/bin/env python3
"""Write data/labbench/frame.jsonl: the registered task frame for LAB.

The driver applies `frame[:limit]` before its resume filter, so `--limit N
--resume` is a growing *prefix* — the same ladder mechanism as swexplore's
bench-ladder.jsonl, for the same reason: order the tasks once and every rung
is a longer prefix of the same file, nothing already paid for is re-paid.

Population filter (each rule a constant below, so the registration is
readable in one screen):

  * retrieval-shaped: `work_type` in {analyze, review, research} OR the slug
    verb in {extract, identify, compare, analyze, review}. There is NO
    `extract` work_type upstream — the verb only exists in slugs — which is
    why the filter needs both halves. Draft-shaped tasks are out: deliverable
    production dominates them and a search treatment cannot act. A shared
    `docs_dir` task is retrieval-shaped BY CONSTRUCTION — firm-knowledge
    tasks carry no work_type and numbered slugs, and finding the right
    documents in a 9k-file DMS is the flagship search setting.
  * searchable corpus: >= MIN_DOCS documents, or a shared `docs_dir`.
  * shared-corpus cap: left proportional, the 250 firm-knowledge tasks would
    be half of every rung — one corpus over-weighted 25×. Their group is
    truncated (after the seeded interleave, so the cut is registered) to at
    most SHARED_MAX_SHARE of the merged population.
  * task.json carries the fields the harness and judge need (a light inline
    check; preflight_lab.py runs upstream's own validate_task_config on the
    selected frame, with its venv).

Ordering: within a practice area, `tierframe.interleaved_by_repo` with the
slug verb standing in for "repo" — reused, not reimplemented, because it
carries the §18.6 fix (sorting after a shuffle quietly undoes it). Areas are
then merged by proportional stride (`proportional_merge`, same shape as
ladder_frame.py's) so every rung prefix keeps the population's area mix.

Documents are counted from `git ls-tree` — names only, no blobs fetched —
so this runs against the sparse checkout before any corpus exists.

    python3 harness/labbench/lab_frame.py            # write it
    python3 harness/labbench/lab_frame.py --check    # verify an existing one
"""

import argparse
import collections
import hashlib
import json
import posixpath
import random
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import gorp_repo as common  # noqa: E402

HERE = Path(__file__).parent
DATA = common.DATA / "labbench"
UPSTREAM = DATA / "upstream"
sys.path.insert(0, str(HERE.parent / "locbench"))
from tierframe import interleaved_by_repo  # noqa: E402

SEED = 46
RUNGS = (12, 60, 150)
FRAME_N = RUNGS[-1]
MIN_DOCS = 8
RETRIEVAL_WORK_TYPES = {"analyze", "review", "research"}
RETRIEVAL_VERBS = {"extract", "identify", "compare", "analyze", "review"}
SHARED_MAX_SHARE = 0.2


def proportional_merge(groups):
    """Merge per-area orders so any prefix keeps population proportions.

    Sainte-Laguë stride, same as ladder_frame.proportional_merge: each area
    advances at a rate set by its share, so after k draws every area has
    contributed about k * share tasks. Plain round-robin would put the
    11-task `diligence` area in every rung's head.
    """
    groups = {a: g for a, g in groups.items() if g}
    total = sum(len(g) for g in groups.values())
    heap = []
    for area, g in groups.items():
        share = len(g) / total
        heap.append([0.5 / share, area, 0, share])
    out = []
    while len(out) < total:
        heap.sort()
        pos, area, taken, share = heap[0]
        out.append(groups[area][taken])
        taken += 1
        if taken >= len(groups[area]):
            heap.pop(0)
        else:
            heap[0] = [(taken + 0.5) / share, area, taken, share]
    return out


def slug_of(task_id):
    """The task's own directory name, skipping scenario-N leaves."""
    parts = task_id.split("/")
    leaf = parts[-1]
    return parts[-2] if leaf.startswith("scenario-") and len(parts) > 2 else leaf


def verb_of(task_id):
    return slug_of(task_id).split("-")[0]


def area_of(task_id):
    return task_id.split("/")[0]


def is_retrieval_shaped(work_type, verb):
    return (work_type in RETRIEVAL_WORK_TYPES) or (verb in RETRIEVAL_VERBS)


def task_json_usable(config):
    """The fields the harness and judge dereference. Deliberately lighter
    than upstream's validate_task_config (which needs their venv): preflight
    runs the real one over the frame."""
    if not config.get("title") or not isinstance(config.get("criteria"), list):
        return False
    if not config["criteria"]:
        return False
    return all(c.get("id") and c.get("title") and c.get("match_criteria")
               for c in config["criteria"])


def load_population():
    """One git ls-tree for the whole tree: names only, no blobs."""
    # -z: legal filenames carry spaces and em-dashes, which git C-quotes in
    # newline-terminated output — a quoted name never matches a real path.
    out = subprocess.run(
        ["git", "-C", str(UPSTREAM), "ls-tree", "-r", "--name-only", "-z",
         "HEAD", "tasks"], capture_output=True, text=True, check=True).stdout
    names = [n for n in out.split("\0") if n]

    docs_count = collections.Counter()
    task_ids = []
    for n in names:
        if n.endswith("/task.json"):
            task_ids.append(n[len("tasks/"):-len("/task.json")])
        elif "/documents/" in n:
            docs_count[n[len("tasks/"):].split("/documents/")[0]] += 1
        # shared corpora sit outside any documents/ dir, keyed by their own path
    dms_count = collections.Counter()
    for n in names:
        if "/dms/" in n and "/documents/" not in n:
            dms_count[n[len("tasks/"):].split("/dms/")[0] + "/dms"] += 1

    side = {}
    for tid in task_ids:
        cfg_path = UPSTREAM / "tasks" / tid / "task.json"
        try:
            config = json.loads(cfg_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        docs_dir = config.get("docs_dir")
        shared = bool(docs_dir)
        if shared:
            rel = posixpath.normpath(f"{tid}/{docs_dir}")
            n_docs = dms_count.get(rel, 0) or docs_count.get(rel, 0)
        else:
            n_docs = docs_count.get(tid, 0)
        side[tid] = {
            "task": tid,
            "area": area_of(tid),
            "work_type": config.get("work_type"),
            "slug_verb": verb_of(tid),
            "n_docs": n_docs,
            "shared_corpus": str(docs_dir) if shared else None,
            "n_criteria": len(config.get("criteria") or []),
            "difficulty": config.get("difficulty"),
            "usable": task_json_usable(config),
        }
    return side


def build(side=None):
    side = side if side is not None else load_population()
    eligible = [
        tid for tid, r in sorted(side.items())
        if r["usable"]
        and (r["shared_corpus"]
             or (is_retrieval_shaped(r["work_type"], r["slug_verb"])
                 and r["n_docs"] >= MIN_DOCS))
    ]
    rng = random.Random(SEED)
    by_area = collections.defaultdict(list)
    for tid in eligible:
        by_area[side[tid]["area"]].append(tid)
    verb_of_task = {tid: side[tid]["slug_verb"] for tid in eligible}
    ordered = {area: interleaved_by_repo(ids, verb_of_task, rng)
               for area, ids in sorted(by_area.items())}
    # Cap the shared-corpus group AFTER its seeded interleave, so which
    # tasks survive the cut is itself a registered draw.
    n_other = sum(len(ids) for area, ids in ordered.items()
                  if not all(side[t]["shared_corpus"] for t in ids))
    cap = max(1, int(SHARED_MAX_SHARE / (1 - SHARED_MAX_SHARE) * n_other))
    for area in list(ordered):
        if ordered[area] and all(side[t]["shared_corpus"] for t in ordered[area]):
            ordered[area] = ordered[area][:cap]
    order = proportional_merge(ordered)
    assert len(order) == len(set(order)), "order must not repeat tasks"
    return order[:FRAME_N], order, side


def write(frame, order, side):
    out = DATA / "frame.jsonl"
    out.write_text("".join(
        json.dumps({k: v for k, v in side[t].items() if k != "usable"}) + "\n"
        for t in frame))
    meta = {
        "seed": SEED,
        "rungs": list(RUNGS),
        "n_frame": len(frame),
        "n_eligible": len(order),
        "filters": {
            "work_types": sorted(RETRIEVAL_WORK_TYPES),
            "verbs": sorted(RETRIEVAL_VERBS),
            "min_docs": MIN_DOCS,
        },
        "sha256": hashlib.sha256(out.read_bytes()).hexdigest()[:16],
        "order": frame,
    }
    (DATA / "frame-meta.json").write_text(json.dumps(meta, indent=1) + "\n")
    return out, meta


def report(frame, order, side):
    pop = collections.Counter(side[t]["area"] for t in order)
    areas = sorted(pop, key=pop.get, reverse=True)
    print(f"{'rung':>6} {'n':>5}  " + " ".join(f"{a[:10]:>10}" for a in areas[:8]))
    print(f"{'pop':>6} {len(order):>5}  " +
          " ".join(f"{pop[a] / len(order):10.0%}" for a in areas[:8]))
    ok = True
    for n in RUNGS:
        c = collections.Counter(side[t]["area"] for t in frame[:n])
        print(f"{n:>6} {n:>5}  " + " ".join(f"{c[a] / n:10.0%}" for a in areas[:8]))
    verbs = collections.Counter(side[t]["slug_verb"] for t in frame)
    wts = collections.Counter(str(side[t]["work_type"]) for t in frame)
    print(f"\nframe verbs: {dict(verbs.most_common(8))}")
    print(f"frame work_types: {dict(wts.most_common())}")
    shared = sum(1 for t in frame if side[t]["shared_corpus"])
    print(f"shared-corpus tasks in frame: {shared}/{len(frame)}")
    for a, b in zip(RUNGS, RUNGS[1:]):
        if frame[:a] != frame[:b][:a]:
            print(f"FAIL: rung {a} is not a prefix of rung {b}"); ok = False
    big = areas[0]
    for n in RUNGS[1:]:
        c = collections.Counter(side[t]["area"] for t in frame[:n])
        drift = abs(c[big] / n - pop[big] / len(order))
        flag = "ok " if drift <= 0.10 else "FAIL"
        if drift > 0.10:
            ok = False
        print(f"{flag} rung {n}: {big} share drifts {drift:+.1%} from population")
    return ok


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="verify the on-disk frame instead of rewriting it")
    args = ap.parse_args()
    frame, order, side = build()
    if args.check:
        have = json.loads((DATA / "frame-meta.json").read_text())
        if have["order"] != frame:
            print(f"FAIL: on-disk frame does not reproduce from seed {SEED}")
            sys.exit(1)
        print(f"ok  frame-meta.json reproduces from seed {SEED}")
    else:
        out, meta = write(frame, order, side)
        print(f"wrote {out} ({meta['n_frame']} of {meta['n_eligible']} eligible, "
              f"sha256={meta['sha256']})")
    sys.exit(0 if report(frame, order, side) else 1)
