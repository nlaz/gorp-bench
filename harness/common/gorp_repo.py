"""Where the engine is, and where this harness keeps its data.

gorp-bench runs *against* gorp; it does not contain it. Both are checkouts
that sit side by side, the same arrangement `gorp` itself uses for `../ese`
and `../anny`, and the same one CI reproduces. That convention is the whole
dependency mechanism — there is no package to install and no version to
pin, so a harness run always measures the working tree next to it.

Three things cross the boundary, and they are all here so a fourth cannot
appear by accident somewhere in `harness/`:

  BIN     the binary under test
  REPO    the gorp checkout (for its fixtures and its shared eval library)
  path()  puts gorp's `eval/` on sys.path

The shared library matters more than it looks. `leakage.identifier`
is the predicate that decides whether a query names its own answer, and
both sides must apply *the same* one: the offline board stratifies by it,
this harness tiers harvested traces by it, and if the two drift then
§12.1's audit stops meaning anything. Importing it from the sibling
checkout is what makes that literally the same code rather than two copies
that agree today.

Overrides, for CI and for measuring a binary built elsewhere:

  GORP_REPO   path to the gorp checkout   (default: ../gorp)
  GORP_BIN    path to the binary          (default: <repo>/target/release/gorp)
"""

import os
import sys
from pathlib import Path

# This file is harness/common/gorp_repo.py, so the bench repo root is three up.
BENCH = Path(__file__).resolve().parents[2]

#: Everything this harness writes: campaign runs, checked-out instance repos,
#: vendored upstream harnesses, viewers. Gitignored — it is measurements and
#: rebuildable inputs, and it runs to tens of gigabytes.
DATA = BENCH / "data"

#: The gorp checkout. A sibling by default; `GORP_REPO` overrides.
REPO = Path(os.environ.get("GORP_REPO") or BENCH.parent / "gorp").resolve()

#: The binary under test. `GORP_BIN` overrides, which is how a campaign
#: measures a build from somewhere other than the sibling's target/.
BIN = Path(os.environ.get("GORP_BIN") or REPO / "target/release/gorp")


def require_bin():
    """The binary, or a message saying exactly how to get one.

    A campaign that starts without it burns real agent budget before
    failing, so every entry point checks first (RESEARCH.md §18's rule:
    the cheap gate runs before the expensive thing).
    """
    if not BIN.exists():
        raise SystemExit(
            f"no gorp binary at {BIN}\n"
            f"  build it:  (cd {REPO} && cargo build --release)\n"
            f"  or point GORP_BIN at one"
        )
    return BIN


def path():
    """Put gorp's `eval/` on sys.path and return it.

    Gives the harness `leakage`, `symbols`, `corpus_text` and `run_eval` —
    the scoring library the offline board also uses.
    """
    evaldir = REPO / "eval"
    if not evaldir.is_dir():
        raise SystemExit(
            f"no gorp checkout at {REPO} (looked for {evaldir})\n"
            f"  clone it beside this one, or set GORP_REPO"
        )
    p = str(evaldir)
    if p not in sys.path:
        sys.path.insert(0, p)
    return evaldir
