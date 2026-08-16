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
