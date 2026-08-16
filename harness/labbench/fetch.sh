#!/usr/bin/env bash
# Fetch Harvey LAB (harveyai/harvey-labs) into data/labbench/ — code only.
#
# The upstream repo is ~3 GB because all 2,010 tasks ship their documents
# in-tree, ~61k binary .docx/.xlsx the operator has no room for and the
# agent would only ever see through an extractor anyway. So the clone is
# blobless and sparse: harness/evaluation code plus every task.json and
# instructions.md (~55 MB), zero documents. Documents for the frame tasks
# are materialized batch-by-batch and converted to markdown by
# build_corpus.py (the `md-v1` corpus), which deletes the originals.
#
# Our changes are applied from checked-in files rather than committed into
# the vendored copy, so `git status` inside upstream/ shows exactly what we
# changed — and a future upstream commit makes the patch fail loudly instead
# of silently applying to code that moved.
#
# Re-runnable; skips work already done. Needs network + uv.
set -euo pipefail
cd "$(dirname "$0")"

OUT=../../data/labbench
mkdir -p "$OUT"

# ---------------------------------------------------------------- harness
UPSTREAM_SHA=7be41d57fd5a6e97b5f246a029e810f83d09cd96
if [ ! -d "$OUT/upstream/.git" ]; then
  echo "cloning harvey-labs (blobless) at $UPSTREAM_SHA ..."
  git clone -q --filter=blob:none --no-checkout \
      https://github.com/harveyai/harvey-labs.git "$OUT/upstream"
fi
# Sparse patterns are (re)set on EVERY run, before any checkout: a checkout
# against a fresh .git with no patterns would materialize the full 3 GB.
# Non-cone patterns: the task metadata lives as two filenames scattered
# under tasks/, which cone mode (directory-granular) cannot express.
git -C "$OUT/upstream" sparse-checkout set --no-cone \
    '/harness/' '/evaluation/' '/sandbox/' '/utils/' '/docs/' \
    '/pyproject.toml' '/uv.lock' '/LICENSE' '/README.md' \
    'tasks/**/task.json' 'tasks/**/instructions.md'
git -C "$OUT/upstream" checkout -q "$UPSTREAM_SHA"
git -C "$OUT/upstream" checkout -q -- .          # drop any prior patch
cp patches/lab_arms.py "$OUT/upstream/lab_arms.py"
git -C "$OUT/upstream" apply "$(pwd)/patches/0001-run-arm-executor-metrics.patch"
python3 -m py_compile "$OUT/upstream/harness/run.py" "$OUT/upstream/lab_arms.py"
echo "harness ready: upstream@${UPSTREAM_SHA:0:7} + 1 file + 1 patch"

# ------------------------------------------------------------------- venv
# Materialize upstream's venv now, not at campaign time: build_corpus.py and
# every run ride `uv run` inside the checkout, and the first invocation
# resolving 60 packages mid-campaign is a preflight bypass.
(cd "$OUT/upstream" && uv run python -c "import evaluation.scoring" >/dev/null)
echo "venv ready: evaluation.scoring imports"

# ----------------------------------------------------------------- corpus
# Needs the frame (lab_frame.py writes it from task.json metadata alone).
if [ ! -s "$OUT/frame.jsonl" ]; then
  echo "no frame yet — run: python3 harness/labbench/lab_frame.py, then"
  echo "  python3 harness/labbench/build_corpus.py"
  exit 0
fi
python3 build_corpus.py
