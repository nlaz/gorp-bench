# gorp-bench

Agent-evaluation harnesses and the perf benchmark for [gorp](https://github.com/nlaz/gorp).

This repo answers one question the engine's own tests cannot: **does a real
agent, given this tool, find the code faster?** Everything here runs a live
agent against a real repository and scores what it found — which costs money
and takes hours, which is why it lives apart from the engine.

## Layout

```
harness/
  common/     shim.py (the PATH wrapper every arm runs behind), gorp_repo.py
  swexplore/  SWE-Explore-Bench — the flagship, 848 instances, line-level gold
  locbench/   Loc-Bench V1 — 560 instances, function-level gold
bench/        perf vs grep/ripgrep/ugrep/ack
tests/        the scorers, which decide every published number
```

**`harness/swexplore/` vendors the real upstream harness** rather than
imitating it (`fetch.sh` clones it at a pinned SHA and applies one patch plus
three files). That is what makes the `cc` arm comparable to the row published
in arXiv 2606.07297 — the task prompt and the answer parser are theirs, and
`git diff` inside the vendored checkout always shows exactly what we changed.

**`harness/locbench/`** is ours, for a different benchmark, and it carries
machinery both use: `shim.py`, `harvest.py`, and the campaign-gate pattern
`triage.py`.

## The sibling-checkout convention

gorp-bench measures gorp; it does not contain it. Clone them side by side:

```
Flower-Computer/
├── gorp/        the engine        (github.com/nlaz/gorp)
├── gorp-bench/  this repo
├── ese/         static embeddings ─┐ gorp's own siblings
└── anny/        HNSW              ─┘
```

`harness/common/gorp_repo.py` is the only place that crosses the boundary:
it resolves the binary (`GORP_BIN`, default `../gorp/target/release/gorp`),
the checkout (`GORP_REPO`, default `../gorp`), and puts gorp's `eval/` on
`sys.path` so both repos share one scoring library. That sharing is
deliberate: `leakage.identifier` decides whether a query names its own
answer, the offline board stratifies by it and this harness tiers traces by
it, and two copies would drift.

```sh
cd ../gorp && cargo build --release      # the binary under test
cd ../gorp-bench && python3 -m pytest tests -q
```

## Running a campaign

Preflight first — always. It is five checks against the shapes agents
actually type, and it exists because the §16.10 campaign spent $361 with 47%
of one arm's searches silently returning nothing.

```sh
python3 harness/locbench/preflight.py                    # before spending anything
RUNG=150 harness/swexplore/campaign.sh                   # a rung, with provenance
python3 harness/swexplore/triage_swex.py --run s27       # the gate between tiers
python3 harness/swexplore/analyze.py                     # the endpoints
python3 harness/swexplore/viewer.py                      # one offline HTML page
```

`triage` is a gate, not a report: it exits nonzero on tool failures, agent
distress, or harness trouble, and a campaign that fails it does not get
analyzed. `viewer.py` renders the numbers *and* the trajectories behind them
— every search an agent ran, what came back, and what the engine did — into a
single self-contained page that opens offline.

Campaign output lands in `data/` (gitignored, tens of gigabytes). The
scorers, the arms, and the registered frames are checked in, because those
are what make a published number reproducible.

## What the arm names mean

`cc`, `cc-rg`, `cc-sg`, `desc-v4` … `desc-v12`, `semgrep`, `sg`, `search` —
these are **frozen experiment labels, not the tool's current name.** The
engine has been `gorp` since 2026-08; every arm keeps the name it was
measured under, because a campaign compares against recorded runs and an arm
whose text changed is a different arm. The rename entered the eval as
`desc-v12`: `desc-v10` with the tool renamed and nothing else touched, so the
contrast isolates one variable. RESEARCH.md §19.9 in gorp is the cautionary
case — `desc-v9` changed the name *and* a clause, and could attribute neither.

For the same reason, harvested shim logs say `semgrep` where the agent typed
`semgrep`. Every parser here accepts each name the engine has shipped under.
