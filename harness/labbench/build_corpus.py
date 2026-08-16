#!/usr/bin/env python3
"""Build the `md-v1` corpus: every frame task's documents, as markdown.

The operator has no room for upstream's ~3 GB of binary documents, and the
agent would only ever see them through an extractor anyway. So each frame
task's documents are materialized batch-by-batch out of the blobless clone,
converted with upstream's own judge-side extractor
(`evaluation.scoring._read_file_as_text` — pandoc/pandas/markitdown/
pdfplumber), and the originals dropped again: what the search tools and the
`read` tool see is exactly what the judge stack understands.

Naming contract (lab_arms.md_name): `documents/<rel>` becomes
`documents/<rel>.md`, suffix appended always — `memo.txt` → `memo.txt.md` —
so the original format stays visible in the name and no reverse mapping is
needed anywhere.

Mechanics that keep the disk bounded:
  * materialize = `git sparse-checkout add <task docs pattern>`; the blobless
    clone fetches those blobs on demand;
  * drop = restore the sparse pattern list captured at start. Git itself
    removes the tracked originals from the working tree — nothing is `rm`ed,
    so `git status` stays clean and a later `git checkout -- .` cannot
    resurrect 3 GB of documents. The untracked `.md` files stay.
  * `git gc` CANNOT reclaim the on-demand fetches: packs from a promisor
    remote are protected, and (measured here) `repack --filter` refuses or
    mangles them too. The working reclaim is a fresh blobless re-clone of
    .git — the recipe prints at the end when the pack has grown; fetch.sh
    re-applies the overlay afterwards and the untracked .md corpus is
    untouched throughout.

Extraction failures keep the extractor's own sentinel out of the corpus: a
"(error reading ...)" string would be searchable garbage, so the .md is not
written and the manifest records the failure. Same blindness for every arm.

The manifest (data/labbench/corpus-manifest.json) is incremental — it is the
resume state — keyed by docroot ("tasks/<...>/documents" or the shared dms),
with per-file status. `task.json` and `instructions.md` are never converted:
the rubric sits above the docroot and stays structurally out of search scope.

Runs from anywhere: re-execs itself under upstream's venv (`uv run
--project`) when the extractor's dependencies are missing.
"""

import argparse
import hashlib
import json
import os
import posixpath
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import gorp_repo as common  # noqa: E402

DATA = common.DATA / "labbench"
UPSTREAM = DATA / "upstream"
MANIFEST = DATA / "corpus-manifest.json"
CORPUS = "md-v1"
GC_EVERY = 5  # batches between promisor-pack gc passes

sys.path.insert(0, str(UPSTREAM))
try:
    from evaluation.scoring import _read_file_as_text  # noqa: E402
except ImportError:
    os.execvp("uv", ["uv", "run", "--project", str(UPSTREAM), "python",
                     os.path.abspath(__file__), *sys.argv[1:]])

sys.path.insert(0, str(Path(__file__).resolve().parent / "patches"))
from lab_arms import md_name  # noqa: E402


def _git(*args, check=True):
    return subprocess.run(["git", "-C", str(UPSTREAM), *args],
                          capture_output=True, text=True, check=check)


def frame_docroots(limit=None, only=None):
    """Docroot-relative-to-upstream paths for the frame, deduped in frame
    order (the shared dms appears once, at its first task's position)."""
    rows = [json.loads(l) for l in (DATA / "frame.jsonl").read_text().splitlines()
            if l.strip()]
    if only:
        keep = set(only)
        rows = [r for r in rows if r["task"] in keep]
    if limit:
        rows = rows[:limit]
    roots, seen = [], set()
    for r in rows:
        if r["shared_corpus"]:
            root = "tasks/" + posixpath.normpath(f"{r['task']}/{r['shared_corpus']}")
        else:
            root = f"tasks/{r['task']}/documents"
        if root not in seen:
            seen.add(root)
            roots.append(root)
    return roots


def _convert_one(job):
    """Worker: (src_abs, dst_abs, rel) -> manifest entry. The extractor
    already catches its own failures and returns a sentinel string."""
    src_abs, dst_abs, rel = job
    src = Path(src_abs)
    dst = Path(dst_abs)
    try:
        text = _read_file_as_text(src)
    except BaseException as e:  # noqa: BLE001 — a worker must always report
        return rel, {"status": "error", "error": f"{type(e).__name__}: {e}",
                     "src_bytes": src.stat().st_size}
    entry = {"src_bytes": src.stat().st_size}
    if text.startswith("(binary file:") or text.startswith("(error reading"):
        entry.update(status="error", error=text[:200])
        return rel, entry
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.rename(dst)
    entry.update(status="ok", md_bytes=dst.stat().st_size)
    return rel, entry


def load_manifest():
    if MANIFEST.exists():
        m = json.loads(MANIFEST.read_text())
        if m.get("corpus") != CORPUS:
            sys.exit(f"FATAL: manifest carries corpus {m.get('corpus')!r}, "
                     f"this script builds {CORPUS!r} — refusing to mix")
        return m
    return {"corpus": CORPUS, "upstream_sha": None, "docroots": {}, "files": {}}


def save_manifest(m):
    m["upstream_sha"] = _git("rev-parse", "HEAD").stdout.strip()
    m["built_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    ok = sum(1 for e in m["files"].values() if e["status"] == "ok")
    m["n_ok"], m["n_error"] = ok, len(m["files"]) - ok
    tmp = MANIFEST.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(m, indent=1, sort_keys=True) + "\n")
    tmp.rename(MANIFEST)


def build(roots, workers, retry_errors=False):
    import shutil
    if not shutil.which("pandoc"):
        sys.exit("FATAL: pandoc not on PATH — it extracts every .docx, and "
                 "running without it records thousands of bogus errors "
                 "(brew install pandoc)")
    manifest = load_manifest()
    # Base = the current pattern list minus any docroot glob a crashed run
    # left behind; resetting to it immediately both heals that state and
    # reclaims whatever documents the crash had materialized.
    base_patterns = [p for p in _git("sparse-checkout", "list").stdout.splitlines()
                     if not (p.startswith("/tasks/") and p.endswith("/**"))]
    if not base_patterns:
        sys.exit("FATAL: upstream is not a sparse checkout — run fetch.sh first")
    _git("sparse-checkout", "set", "--no-cone", *base_patterns)

    if retry_errors:
        prefixes = tuple(r + "/" for r in roots)
        dropped = [rel for rel, e in manifest["files"].items()
                   if e["status"] == "error" and rel.startswith(prefixes)]
        for rel in dropped:
            del manifest["files"][rel]
        for r in roots:
            manifest["docroots"].pop(r, None)
        print(f"retry-errors: cleared {len(dropped)} error entries")

    todo = [r for r in roots if manifest["docroots"].get(r) != "done"]
    print(f"corpus {CORPUS}: {len(roots)} docroots, {len(todo)} to build")
    for i, root in enumerate(todo):
        t0 = time.time()
        _git("sparse-checkout", "add", f"/{root}/**")
        # Sources are the TRACKED files under the docroot — our own untracked
        # .md outputs (from a prior interrupted run) never re-enter as inputs,
        # and a genuine upstream .md document still converts (to <name>.md.md,
        # keeping the untracked-output rule uniform).
        # -z: em-dashes and spaces are common in legal filenames, and
        # newline-terminated output C-quotes them into paths that don't exist.
        tracked = [p for p in _git("ls-tree", "-r", "--name-only", "-z",
                                   "HEAD", "--", root).stdout.split("\0") if p]
        jobs, n_have = [], 0
        for rel in sorted(tracked):
            dst = UPSTREAM / md_name(rel)
            if manifest["files"].get(rel, {}).get("status") == "ok" and dst.exists():
                n_have += 1
                continue
            jobs.append((str(UPSTREAM / rel), str(dst), rel))
        n_err = 0
        with ProcessPoolExecutor(max_workers=workers) as pool:
            for rel, entry in pool.map(_convert_one, jobs, chunksize=8):
                manifest["files"][rel] = entry
                n_err += entry["status"] == "error"
        # Restoring the start-of-run pattern list makes git drop the tracked
        # originals from the working tree; the untracked .md files remain.
        _git("sparse-checkout", "set", "--no-cone", *base_patterns)
        manifest["docroots"][root] = "done"
        save_manifest(manifest)
        print(f"  [{i + 1}/{len(todo)}] {root}: {len(jobs)} converted "
              f"({n_have} cached, {n_err} errors, {time.time() - t0:.0f}s)")
        if (i + 1) % GC_EVERY == 0:
            _git("gc", "--prune=now", "--quiet", check=False)
    _git("gc", "--prune=now", "--quiet", check=False)
    pack = sum(f.stat().st_size for f in
               (UPSTREAM / ".git" / "objects" / "pack").glob("*.pack"))
    if pack > 200e6:
        print(f"note: .git holds {pack/1e9:.1f} GB of promisor packs gc "
              f"cannot reclaim. To drop them (the corpus is untouched):\n"
              f"  cd {UPSTREAM.parent} && "
              f"git clone -q --filter=blob:none --no-checkout "
              f"https://github.com/harveyai/harvey-labs.git fresh-tmp && "
              f"rm -rf upstream/.git && mv fresh-tmp/.git upstream/.git && "
              f"rm -rf fresh-tmp\n"
              f"  then re-run fetch.sh")
    return manifest


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", help="comma-separated task ids (default: whole frame)")
    ap.add_argument("--limit", type=int, help="first N frame tasks only")
    ap.add_argument("--workers", type=int, default=os.cpu_count())
    ap.add_argument("--retry-errors", action="store_true",
                    help="re-attempt files previously recorded as errors")
    args = ap.parse_args()
    if not (DATA / "frame.jsonl").exists():
        sys.exit("FATAL: no frame.jsonl — run lab_frame.py first")
    roots = frame_docroots(limit=args.limit,
                           only=args.tasks.split(",") if args.tasks else None)
    m = build(roots, args.workers, retry_errors=args.retry_errors)
    sha = hashlib.sha256(MANIFEST.read_bytes()).hexdigest()[:16]
    print(f"corpus manifest: {m['n_ok']} ok, {m['n_error']} errors, "
          f"sha256={sha}")
