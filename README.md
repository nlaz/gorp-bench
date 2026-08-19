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
  labbench/   Harvey LAB — legal tasks, LLM-judge rubric; the first non-code harness
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

**`harness/labbench/`** wraps [Harvey's LAB](https://github.com/harveyai/harvey-labs)
(Legal Agent Benchmark) the same way swexplore wraps its upstream: a blobless
sparse clone at a pinned SHA, one copied file + one patch, arms additive.
It is the first harness whose corpus is documents, not a git checkout, and
it tests gorp on prose. The documents are converted to markdown in place
(`build_corpus.py`, corpus label `md-v1`) using upstream's own judge-side
extractor — the operator does not carry the ~3 GB of binary originals — so
search results cite the very files the agent reads, upstream's grep becomes
a functional baseline, and **numbers are not comparable to Harvey's
published leaderboard** (different input modality; arm-vs-arm contrasts are
unaffected). Three arms: `lab-base` (upstream verbatim), `lab-rg` (+ one
structured ripgrep tool), `lab-gorp` (+ the same tool shape backed by gorp);
the primary contrast is gorp − rg. Needs `uv`, `pandoc`, `podman`, and an
`ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN` in the vendored checkout's `.env`.

A second arm family, **`lab-cc-base` / `lab-cc-rg` / `lab-cc-gorp`**, runs
the same tasks, sandbox, and tool surfaces with ONE substitution: the agent
loop is the Claude Agent SDK (`lab_cc_run.py`) and the judge is `claude -p`
(`lab_cc_judge.py`), so both are funded by the operator's Claude Code
subscription login instead of an API key — no token is copied anywhere; the
CLI holds its own credentials. Claude Code subscription OAuth tokens are NOT
valid for the raw Messages API, and this family is the supported way to run
on one. Contrasts are valid within a family, never across (the loop is a
treatment), so analyses take `--family api|cc` and rows are never pooled.

```sh
bash harness/labbench/fetch.sh                    # vendor + venv + corpus
python3 harness/labbench/lab_frame.py --check     # the registered 150-task frame
python3 harness/labbench/preflight_lab.py         # the money gate, no API calls
RUNG=12 harness/labbench/campaign.sh              # a rung; triage_lab gates it
```

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

Preflight first — always. It exists because the §16.10 campaign spent $361
with 47% of one arm's searches silently returning nothing. `locbench` has had
its own since then; swexplore got `preflight_swex.py` only in §36, after the
repo split had left four cross-repo paths pointing at directories that no
longer exist — including the shim path, which would have made *every* search
in *every* arm fail while still producing rows that read as a clean null.

```sh
python3 harness/swexplore/preflight_swex.py              # before spending anything
MODEL=claude-sonnet-4-5-20250929 RUNG=150 \
  CONDITIONS="cc cc-gorp" harness/swexplore/campaign.sh  # a rung; both gates run inside
python3 harness/swexplore/triage_swex.py --run-id s36    # harness health
python3 harness/swexplore/provaudit.py --run-id s36      # one toolchain, one treatment
python3 harness/swexplore/analyze.py --run-id s36        # the endpoints
python3 harness/swexplore/viewer.py --run-id s36         # one offline HTML page
```

`campaign.sh` runs `preflight_swex.py` itself before spending and both gates
after, so the line above is the whole flow; the individual commands are for
re-reading a rung that already ran.

**The model is pinned, never an alias.** `campaign.sh` refuses a bare `sonnet`
/ `opus` / `haiku`. Campaigns §27–§33 all passed `sonnet` and all in fact ran
`claude-sonnet-5` — recoverable only by reading a raw transcript, and §32.3
calibrated the `cc` arm against the paper's *Sonnet-4.5* row on the assumption
they had not. `meta.json` now records `model_requested` and `model_resolved`
as two separate facts, and `provaudit.py` fails a run whose cells disagree.

`triage` is a gate, not a report: it exits nonzero on tool failures, agent
distress, or harness trouble, and a campaign that fails it does not get
analyzed. `provaudit` is the second gate and asks a different question — was
this rung *one* experiment? It fails on any mixture within a run: two resolved
models, two binaries, two descriptions, two sets of hidden engine flags. A
rung that half-ran on a rebuilt binary is two experiments pooled into one
number, and nothing used to look. `viewer.py` renders the numbers *and* the trajectories behind them
— every search an agent ran, what came back, and what the engine did — into a
single self-contained page that opens offline.

Campaign output lands in `data/` (gitignored, tens of gigabytes). The
scorers, the arms, and the registered frames are checked in, because those
are what make a published number reproducible.

## Publishing traces back to gorp

Campaigns produce something reusable for free: the searches agents actually
typed, with known gold. `publish_traces.py` turns them into a tiered trace
set that the engine repo scores without any agent, budget, or campaign.

```sh
python3 harness/common/publish_traces.py --out ../gorp/eval/queries/traces-v2.jsonl
```

It stamps each query `blind` / `guess` / `golden` using **gorp's** tier rule,
imported from the sibling checkout, so the repo that writes these files and
the repo that reads them cannot disagree about what a tier means.

## What the arm names mean

`cc`, `cc-rg`, `cc-sg`, `cc-gorp`, `desc-v4` … `desc-v12`, `semgrep`, `sg`,
`search` —
these are **frozen experiment labels, not the tool's current name.** The
engine has been `gorp` since 2026-08; every arm keeps the name it was
measured under, because a campaign compares against recorded runs and an arm
whose text changed is a different arm. The rename entered the eval as
`desc-v12`: `desc-v10` with the tool renamed and nothing else touched, so the
contrast isolates one variable. RESEARCH.md §19.9 in gorp is the cautionary
case — `desc-v9` changed the name *and* a clause, and could attribute neither.

`cc-gorp` (§36) is the shipped product measured as it ships: additive like
`cc-sg` — native Grep stays — with the tool named `gorp` and carrying
swexplore's `SG_LINE_V12`, which is `SG_LINE_V11` put through a token-exact
`sg` → `gorp` rename and nothing else. It is *derived* by substitution rather
than retyped, and `tests/test_swexplore_arms.py` asserts the round trip, so
"one variable changed" is mechanically true rather than carefully proofread.
Its contrast is `cc-gorp − cc`: does enabling gorp help an agent that still
has Grep? Note it does not pool with the §27–§33 ledger — those ran
`claude-sonnet-5`, and §36 pins `claude-sonnet-4-5-20250929` to make the
comparison against the paper's Sonnet-4.5 row like-for-like for the first time.

`cc-gorp-route` (§37, registered) is the routed-product candidate built
from what s36 measured: the only cell with a positive delta was gorp and
Grep used *together*; gorp substituting for grep was the worst cell; and the
harness block-steer had pushed 380 lexical grep intents into ranked search.
It bundles three deliberate deltas against `cc-gorp` — description `v13`
(= `v12` + one appended routing clause: grep for names you know, gorp for
guesses and wide sweeps), shell `grep`/`egrep`/`fgrep` pass through (still
shimmed, so logged), and a block-steer for `rg`/`sg` that names both tools.
Bundled on purpose: the registered question is "does the routed,
grep-permissive product beat `cc`?", not which delta did it. Grep-openness is
part of the arm's registration (`GREP_OPEN_ARMS` in `sg_arms.py`), never the
`SWEXPLORE_UNBLOCK_GREP` operator switch, so the arm means the same thing in
every shell.

For the same reason, harvested shim logs say `semgrep` where the agent typed
`semgrep`. Every parser here accepts each name the engine has shipped under.
Adding a name means adding it in six places for swexplore — `sg_arms.ARMS`
and `ARM_TOOL`, the eval_runner patch's `SG_ARMS` and `METHOD_MAP`, and
`ALL_ARM_TOOL` in `triage_swex.py`, `analyze.py` and `viewer.py` — which is
why `preflight_swex.py` and a test both check that they agree. Registering
`sub-sgb` one registry at a time cost three consecutive fix commits.
