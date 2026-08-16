# gorp-bench (repo dir: gorp-bench/)

Agent-eval harnesses and the perf benchmark for `../gorp`. See README.md for
the layout and how to run a campaign. The engine's own research log
(`../gorp/RESEARCH.md`) is where every §-number cited in this repo resolves.

## The one rule

**A campaign costs real money and hours. Every gate runs before the expensive
thing, never after.** `preflight.py` before a run, `triage.py` between tiers.
This is not caution for its own sake: the §16.10 campaign spent $361 and 1,115
runs measuring a tool whose searches returned nothing 47% of the time, and
nothing in the harness noticed. Every check in `preflight.py` and `triage.py`
exists because something went wrong that no unit test could see.

## Layout

- `harness/common/` — what both harnesses share.
  - `shim.py` — the PATH wrapper every search runs behind. Resolves
    `LOCBENCH_REAL_{TOOL}`, appends `LOCBENCH_{TOOL}_FLAGS` **without showing
    them to the agent** (this is how an engine A/B is run blind), passes
    stdout/stderr/exit through byte-exact, and logs under `flock`. An unset
    real-binary var is the *blocked* path: it writes a steer message to both
    stdout and stderr, because stderr-only meant agents piping `2>/dev/null`
    saw silence that looked like a real answer.
  - `gorp_repo.py` — the only module that crosses into `../gorp`. Resolves
    `BIN`/`REPO`, and `path()` puts gorp's `eval/` on `sys.path` so both repos
    share one scoring library.
- `harness/swexplore/` — SWE-Explore-Bench, the flagship. **Vendors upstream**
  at a pinned SHA (`fetch.sh`) and adds three arms as `Explorer` subclasses
  (`patches/sg_arms.py`), inheriting their prompt and answer parser verbatim.
  Arms are *additive* (Grep/Glob stay; the treatment adds a tool).
  `patches/*` run from inside the vendored checkout, so they resolve paths
  from `data/swexplore/upstream/explorers/` — they deliberately do not import
  `harness/common`, which is not on their path.
- `harness/locbench/` — Loc-Bench V1. Ours, and *isolating* (one search tool,
  everything else blocked, including `git` — history after `base_commit`
  contains the real fix). `run.py` is the driver, `harvest.py` mines shim logs
  into guess corpora, `guessplay.py` replays them against real gold.
- `harness/labbench/` — Harvey LAB (harveyai/harvey-labs), the first
  **non-code** harness: legal documents, LLM-judge rubric, all-pass endpoint.
  Vendors upstream *code only* at a pinned SHA (blobless sparse clone; the
  3 GB of task documents never land on disk). Delta = `patches/lab_arms.py`
  copied in + one git patch; arms are additive `ToolExecutor` subclasses
  selected by env `LABBENCH_ARM` (`lab-base` is upstream byte-for-byte).
  The corpus is `md-v1`: `build_corpus.py` converts each frame task's
  documents to markdown in place with upstream's judge-side extractor and
  drops the originals — a frozen label like an arm name, and the reason LAB
  numbers never compare to Harvey's leaderboard. Search hits cite the same
  `.md` files the agent reads (`<original>.md`, suffix appended always);
  `task.json` is never mirrored or searchable — the anti-cheat boundary is
  structural. Gates: `preflight_lab.py` before spend, `triage_lab.py`
  between rungs (an adaptation of locbench's `triage.py`, not a fork).
  Scoring is upstream's judge — no scorer of ours exists; our tests cover
  the naming round-trip, the frame, the gates, and the descriptions.
- `harness/common/publish_traces.py` — **the one artifact that crosses into
  gorp.** Harvested searches + benchmark gold -> a tiered trace set
  (`blind`/`guess`/`golden`), written to `../gorp/eval/queries/traces-*.jsonl`
  and committed there. The tier rule is *gorp's* (`eval/traces.py`, imported
  through `common.path()`), so one rule serves both repos and gorp's
  `validate_queries --traces` can recompute every tier as a drift guard.
  Deduped by (query, instance): the log holds 22k invocations but 10k distinct
  questions, and scoring the raw log would weight a query by how often an
  agent repeated it.
- `bench/` — perf vs grep/ripgrep/ugrep/ack. Competitors are invoked by
  **absolute path** because dev shells wrap `grep`; stdout goes to a real temp
  file, not `/dev/null`, because ugrep short-circuits on the null device.
  Numbers are warm-cache and must be quoted as such.
- `tests/` — the scorers. `pytest tests -q`.

## Conventions

- **Never edit a frozen arm.** Arm names and tool-description text are recorded
  experiment labels; a campaign compares against prior runs, so an edited arm
  is a different arm reported under the old name. New behavior gets a new arm
  (`desc-v12` is `desc-v10` with the tool renamed and nothing else moved).
- **Never rewrite recorded data.** Shim logs and results say `semgrep` where
  the agent typed `semgrep`. Parsers accept every name the engine has shipped
  under (`gorp`, `semgrep`, `sg`, `search`); the tool-name tuples in
  `harvest.py`, `triage.py`, `capture.py` and `viewer.py`'s `ENGINE_KEYS` are
  all that list, and a new alias goes in all of them.
- `data/` is gitignored and holds irreplaceable measurements —
  `data/locbench/runs/` alone is real agent spend. Nothing here deletes it;
  moves are done by hand.
- Scoring changes need a test. `tests/` decides every published number, which
  is why over-credit gets special attention: function matching is scored at
  two strictnesses and both are stored, because over-credit is the failure
  that flatters the tool under test.
