"""The three §27 arms, as SWE-Explore explorers.

Dropped into the upstream tree at `explorers/sg_arms.py` by `fetch.sh`.
Subclasses their `ClaudeCodeExplorer` so `EXPLORE_PROMPT` and
`parse_relevant_files` are inherited **verbatim** — the task contract and the
output parser are theirs, which is what keeps `cc` comparable to the row
published in arXiv 2606.07297 (HitReg 0.531 / HitFile 0.667 / CtxEff 0.829).

    cc      Read,Glob,Grep                  their baseline
    cc-rg   + Bash(rg *)                    Bash-balanced control
    cc-sg   + Bash(sg *)                    treatment

Three contrasts: cc→cc-sg is the product question, cc-rg→cc-sg isolates
gorp from Bash, cc→cc-rg prices the Bash confound itself.

What this adds on top of their explorer, and nothing else:

  * `--output-format stream-json --verbose`, teed to `transcript.jsonl`.
    Theirs uses `--output-format json`, which returns one blob: no per-turn
    usage, no cost, no tool-call sequence. Cost is the co-primary endpoint
    here, so the stream is not optional.
  * `telemetry.jsonl` — `total_cost_usd`, `num_turns`, `usage`, `duration_ms`
    off the terminal `{"type":"result"}` event. Same fields and the same
    keep-`usage`-whole convention as `locbench/run.py:696-703`, so a new
    usage field survives without a harness change.
  * `common/shim.py` on PATH, unchanged — it derives the real binary from
    `argv[1]`, so `sg` needs no edit there. Buys per-invocation argv, exit,
    stdout bytes and wall ms, which is what the sg-invocation tripwire reads.
  * `GORP_CACHE_DIR` per arm and `GORP_TRACE_FILE` per cond dir.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from .base import ExplorerResult
from .claude_code import ClaudeCodeExplorer

# --------------------------------------------------------------------------
# Tool lines
# --------------------------------------------------------------------------
# These are NOT desc-v9 verbatim, and the plan said they would be. Two things
# forced the change, both of which would have made the description false:
#
#   1. desc-v9 opens "The only code search tool available is `sg`" and closes
#      "Read and Glob are also available." Both are wrong in an *additive*
#      arm, where Grep and Glob remain. Telling an agent it has no grep when
#      it does is not a description variant, it is a lie the agent can check.
#   2. desc-v9 says "top 10". The shipped default has been `k: 5` since §26.3.
#
# So the availability framing and the count changed; the mechanism sentence,
# the example, and the "ranked, not exhaustive" caveat are desc-v9's own words
# and are asserted against it by `tools_lines_track_descv9` below.
#
# The rg line is deliberately built to the SAME shape — opener, mechanism,
# worked example — because §7.3 and §19 both measured that description quality
# moves agent behaviour. An rg arm described worse than the sg arm would turn
# the cc-rg→cc-sg contrast into a description contrast. locbench's own `rg`
# line has no example, which is exactly why CLAUDE.md keeps a separate
# `rg-strong` condition; this is the strong form.

RG_LINE = (
    "Additionally, `rg` (ripgrep) is available via Bash: `rg [flags] <regex> "
    "[path]` for exact or regex content matching, printing every match as "
    "path:line:text. "
    'Example: rg "fn backoff_delay" src/ → '
    "src/net/retry.rs:142:fn backoff_delay(attempt: u32). "
    "Exhaustive, not ranked — if it floods, narrow the path or the pattern."
)

SG_LINE = (
    "Additionally, `sg` is available via Bash, a ranked code search. "
    "Give it anything — an identifier, a phrase, or a question: "
    '`sg "query" [path]` returns the most relevant locations as '
    "path:line:text (top 5; `-k N` for more). "
    'Example: sg "retry_backoff backoff_delay compute_delay" → '
    "src/net/retry.rs:142:fn backoff_delay(attempt: u32). "
    "Ranked, not exhaustive — if the answer isn't there, rephrase."
)

# The desc-v10 framing (wide search as the default, the path as a deliberate
# second step — §28.2's "gold scoped away" bucket is the target). Selected by
# SWEXPLORE_SG_DESC, defaulting to the §27/§28 registered line: those arms ran
# with SG_LINE and their 364 rate-limited sub-sg cells must complete under the
# registered treatment or the arm becomes a mixture of two descriptions. §30
# sets SWEXPLORE_SG_DESC=v10 and registers the arms on this line instead.
SG_LINE_V10 = (
    "Additionally, `sg` is available via Bash, a ranked code search. "
    "Give it anything — an identifier, a phrase, or a question: "
    '`sg "query"` searches the whole repository and returns the most '
    "relevant locations as path:line:text (top 5; `-k N` for more). "
    "Start wide: add a path argument only to narrow further after a wide "
    "search has pointed somewhere. "
    'Example: sg "retry_backoff backoff_delay compute_delay" → '
    "src/net/retry.rs:142:fn backoff_delay(attempt: u32). "
    "Ranked, not exhaustive — if the answer isn't there, rephrase."
)

# arm -> (tools surface, auto-approve allowlist, appended system prompt)
#
# §27 arms are ADDITIVE: native Grep stays and a Bash tool is offered beside it.
# §28 `sub-*` arms are SUBSTITUTIVE: Grep is removed from `--tools` entirely, so
# the Bash tool is the only content search available. That is the point — §27
# measured 41% delivery because the agent kept choosing Grep, and three separate
# measurements now show it always will when a lexical tool is present.
#
# Removal is enforced in two places, and both are needed: dropping `Grep` from
# `--tools` takes away the native tool, and the PATH shims block shell
# `grep`/`egrep`/`fgrep`. `--allowedTools` does NOT enforce anything under
# `--permission-mode dontAsk` (see _make_shims).
#
# Which sg description the sg arms carry. Default is the §27/§28 registered
# line; `v10` is the wide-by-default rewrite (§29.3). An env switch rather than
# an edit because both must stay runnable: §28 still owes 364 rate-limited
# sub-sg cells that have to finish under the description they started with.
# v11 (§30.3): v10 plus the routed exact-match escape hatch. The §30 pilot
# measured sg-arm agents attempting shell grep 1.2×/session against the
# harness block — the campaign's single largest cost — and the blocked argvs
# were exactly the two intents this clause routes: verify a known name (-e's
# job) and OR-of-candidates regexes (the ranked multi-word query's job).
# §16.10's "never name -e" was measured on an unconditional footer; this is a
# conditional mention inside a ranked-first identity, and exact-share is a
# registered tripwire — collapse onto -e kills the variant.
SG_LINE_V11 = (
    "Additionally, `sg` is available via Bash, a ranked code search. "
    "Give it anything — an identifier, a phrase, or a question: "
    '`sg "query"` searches the whole repository and returns the most '
    "relevant locations as path:line:text (top 5; `-k N` for more). "
    "Start wide: add a path argument only to narrow further after a wide "
    "search has pointed somewhere. When you are unsure of a name, list "
    "several candidate spellings in one query rather than a regex. "
    "When you already know the exact string — a name you have seen in the "
    'code, an error message — `sg -e "the_exact_string"` returns every '
    "literal match, grep-style. Default to ranked search; use -e only to "
    "verify or count something you have already seen spelled out. "
    'Example: sg "retry_backoff backoff_delay compute_delay" → '
    "src/net/retry.rs:142:fn backoff_delay(attempt: u32). "
    "Ranked, not exhaustive — if the answer isn't there, rephrase."
)

# v12 (§36): SG_LINE_V11 with ONE variable changed — the tool's name, after
# the semgrep/sg -> gorp rename. It is derived from v11 by substitution rather
# than retyped, so "nothing else moved" is true by construction instead of by
# careful proofreading; tests/test_swexplore_arms.py asserts the round trip.
# This is the description the plugin actually ships (gorp's README, "Point
# your agent at it"), which is the point: the shipped wording stays a measured
# one. §19.9 is the cautionary case — desc-v9 changed the name AND a clause
# and could attribute neither.
#
# The rename is token-exact. A blind str.replace("sg", "gorp") would also hit
# the "sg" inside other words; \b anchors it to the whole token.
SG_LINE_V12 = re.sub(r"\bsg\b", "gorp", SG_LINE_V11)

# v13 (§37): v12 plus ONE appended routing clause, derived by concatenation so
# "v12 is a prefix of v13" is true by construction (tests assert it). Every
# sentence encodes an s36 forensic finding:
#   route:  the only positive cell was gorp AND Grep together (Δ+0.014);
#           gorp substituting for grep was the worst (Δ-0.022); exact-string
#           work favoured grep (-e hit gold 54% vs ranked 74%).
#   query:  verbatim issue titles ranked gold top-5 in 56% of instances (#1
#           in two sessions that never saw gold); 30 queries had gold buried
#           at median rank 12, just under the default window — widen, don't
#           rephrase.
#   verify: cc-gorp answers carried +31% wrong-file regions (109 vs 83) —
#           plausible semantic neighbours trusted straight into the answer.
#           Verification is anchored to the COMMIT POINT ("before a hit
#           anchors your answer... what you keep, not every search") so it
#           bounds at ~5 checks/session instead of taxing every search —
#           §30.3 measured what an unconditional follow-up habit costs.
# Formatted in the built-in tools' idiom (verified against the bundled
# Grep/Glob descriptions in claude 2.1.235): one identity line, then one
# bullet per fact, examples inlined in parens inside the bullet they
# illustrate, prescription reserved for tool ROUTING and always paired with
# its reason. Capability statements are domain-neutral (files, paths, the
# directory tree); code flavour lives only in the examples — Grep's own
# convention.
# The arm that carries this line also unblocks shell grep — the clause must
# not advertise a blocked path.
ROUTE_CLAUSE_V13 = (
    " Route by what you already know: when the task names the thing — an "
    "identifier from a traceback, an exact error string — the Grep tool or "
    "shell grep finds it directly. Use gorp when you know what the code "
    "does but not what it is called, or to sweep an unfamiliar area before "
    "narrowing. Query in the task's own words first: an error line or "
    "issue title pasted verbatim ranks well. If the answer should be there "
    "but the top 5 misses, widen with -k 20 before rephrasing. gorp "
    "returns candidates, not conclusions — before a hit anchors your "
    "answer, confirm it with Grep or by reading it. Verify what you keep, "
    "not every search."
)
SG_LINE_V13 = SG_LINE_V12 + ROUTE_CLAUSE_V13

# v14 (§37): the integrated rewrite, superseding v13 BEFORE any cell ran —
# legitimate only because cc-gorp-route has zero recorded cells; it freezes at
# first spend. v13's concatenation preserved attribution but read assembled it
# contradicted itself: the opener invited identifier queries the routing
# clause then sent to grep; -e and "confirm with Grep" named two different
# verification tools; "rephrase" and "widen before rephrasing" gave opposite
# escalation orders 100 words apart. v14 resolves the seams and cuts v13's
# 257 words to ~150:
#   * "ranked semantic search", not "code search" — the engine also searches
#     prose (labbench's md-v1 corpus is legal documents).
#   * -e is not advertised: shell grep ships beside it in this arm and -e was
#     the weakest query shape s36 measured (54% vs ranked 74%).
#   * the v10 "start wide / narrow only after" prescription is dropped:
#     s36's scoped searches hit gold at high rates once scored
#     scope-aware, so the path argument is stated as a capability in
#     the mechanics parenthetical, not a policy.
#   * the §31 multi-phrase pipe syntax stays UNADVERTISED, per §31.2's failed
#     gate (merged ranking covered 68.9% of the sequential union, bar >=95%;
#     merging re-imports an agent's abandoned reformulation as a live phrase).
#   * kept: candidate-spellings + the example (74%, best shape), verbatim
#     text-as-query, widen-then-rephrase (bare -k means 20 in the shipped CLI),
#     and verify-at-commit.
SG_LINE_V14 = (
    "Additionally, `gorp` is available via Bash: a powerful ranked "
    "semantic search tool.\n"
    "- Use it when you know what something does but not what it is called, "
    "or to sweep an unfamiliar area\n"
    '- `gorp "query"` takes a phrase or a question and returns the most '
    "relevant locations as path:line:text (top 5; `-k N` for more; a path "
    "argument narrows the scope)\n"
    "- When you are unsure of a name, list candidate spellings in one query "
    'rather than a regex (e.g. gorp "retry_backoff backoff_delay '
    'compute_delay" → src/net/retry.rs:142:fn backoff_delay(attempt: u32))\n'
    "- Verbatim text works well: an error message or a sentence describing "
    "the problem, pasted as-is\n"
    "- When you already have the name — an identifier from a traceback, an "
    "exact string — use grep, not gorp; it finds the name directly\n"
    "- Ranked, not exhaustive: if the answer should be there but the top 5 "
    "misses, widen with -k 20 before rephrasing\n"
    "- Results are candidates, not conclusions — before a hit anchors your "
    "answer, confirm it with Grep or by reading it. Verify what you keep, "
    "not every search."
)

# v15 (§37.3): v14 with ONE clause appended to the when-to-use bullet — where
# gorp actually earned its keep in the s37 trajectories. The paired forensics
# put the whole efficiency gain in the 7/30 sessions where cc needed >=5
# actions to reach gold (route got there 2.3 actions sooner, -3.1 turns);
# where cc reached gold in <=2 actions gorp was pure overhead (+5.1 turns).
# The clause names that boundary for the agent: gorp is for when lexical
# search has no anchor. Derived by substitution so "one clause moved" is
# mechanically true; cc-gorp-route stays frozen on v14 (s37 is recorded
# against it) and v15 gets a NEW arm.
_V15_OLD = ("- Use it when you know what something does but not what it is "
            "called, or to sweep an unfamiliar area\n")
_V15_NEW = ("- Use it when you know what something does but not what it is "
            "called, or to sweep an unfamiliar area. It is strongest where "
            "lexical search has no anchor: unfamiliar naming, cross-language "
            "codebases, prose\n")
assert _V15_OLD in SG_LINE_V14
SG_LINE_V15 = SG_LINE_V14.replace(_V15_OLD, _V15_NEW)

SG_DESC = os.environ.get("SWEXPLORE_SG_DESC", "v9")
_SG_LINE = {"v9": SG_LINE, "v10": SG_LINE_V10, "v11": SG_LINE_V11}[SG_DESC]

ARMS = {
    "cc":     ("Read,Glob,Grep",      [],             ""),
    "cc-rg":  ("Read,Glob,Grep,Bash", ["Bash(rg *)"], RG_LINE),
    "cc-sg":  ("Read,Glob,Grep,Bash", ["Bash(sg *)"], _SG_LINE),
    "sub-rg": ("Read,Glob,Bash",      ["Bash(rg *)"], RG_LINE),
    "sub-sg": ("Read,Glob,Bash",      ["Bash(sg *)"], _SG_LINE),
    # §33: identical surface to sub-sg — same description, same prompt, same
    # index — plus engine flags the agent never sees (bridge expansion). The
    # arm IS the flag delta; everything else must match sub-sg exactly.
    "sub-sgb": ("Read,Glob,Bash",     ["Bash(sg *)"], _SG_LINE),
    # §36: the shipped product, measured. Additive like the other cc-* arms
    # (native Grep stays), tool named `gorp` as the plugin names it, and the
    # description is v12 — v11 renamed. Its contrast is cc-gorp − cc: does
    # enabling gorp help an agent that still has Grep? Deliberately NOT
    # SWEXPLORE_SG_DESC-switchable: cc-sg's description is an env variable
    # because §28 owed cells under an older line, but cc-gorp is registered on
    # one description and pinning it here is what makes the arm name mean
    # something.
    "cc-gorp": ("Read,Glob,Grep,Bash", ["Bash(gorp *)"], SG_LINE_V12),
    # §37: the routed-product candidate. THREE deliberate deltas from
    # cc-gorp, bundled because the registered question is "does the routed,
    # grep-permissive product beat cc?", not which delta did it:
    #   1. description v13 = v12 + one routing clause (grep for known names,
    #      gorp for guesses — what s36's win/loss cells actually measured);
    #   2. shell grep/egrep/fgrep pass through for this arm (still shimmed,
    #      so every call is logged);
    #   3. the block-steer for rg/sg names both tools — s36's cc-gorp steer
    #      sent 380 lexical grep intents into ranked search instead.
    "cc-gorp-route": ("Read,Glob,Grep,Bash",
                      ["Bash(gorp *)", "Bash(grep *)"], SG_LINE_V14),
    # §37.2: the Bash-toll control. s37 measured cc-gorp-route at +$0.044 vs
    # cc with a clean regression on Bash search round-trips ($0.031/call,
    # intercept -$0.019) — but cc has no Bash AT ALL, so that contrast
    # bundles "has a shell" with "has gorp". This arm is cc plus Bash plus
    # open shell grep and NOTHING else: no engine, no description. Its
    # clause parallels cc-rg's shape so shell grep gets the same billing
    # the route arm gives it. cc-bash − cc prices the shell; cc-gorp-route
    # − cc-bash is gorp + v14 net of the shell.
    "cc-bash": ("Read,Glob,Grep,Bash", ["Bash(grep *)"], ""),
    # §37.3: cc-gorp-route's surfaces exactly, description v15 (the when-it-
    # shines clause). route froze on v14 at s37's first spend, so the clause
    # gets a new arm rather than an edit — the §19.9 rule, again.
    "cc-gorp-route2": ("Read,Glob,Grep,Bash",
                       ["Bash(gorp *)", "Bash(grep *)"], SG_LINE_V15),
}
ARM_TOOL = {"cc-rg": "rg", "cc-sg": "sg", "sub-rg": "rg", "sub-sg": "sg",
            "sub-sgb": "sg", "cc-gorp": "gorp", "cc-gorp-route": "gorp",
            "cc-gorp-route2": "gorp"}

# Every search-tool name the shims cover, in one place. These names appeared as
# four separate literals (the shim list, the two env-scrub loops and the
# blocked-name tuple) and adding `sub-sgb` to one registry at a time cost three
# consecutive fix commits (42c192a, d2e2b90, 27f3c08). A name missing from the
# shim list is the dangerous one: it reaches the real binary on PATH and
# escapes the harness entirely.
SEARCH_TOOLS = ("rg", "sg", "gorp")

# --------------------------------------------------------------------------
# Engine flags for the sg arm (§30)
# --------------------------------------------------------------------------
# Injected by shim.py into the REAL invocation and never shown to the agent,
# so its commands and the logged argv stay the plain `sg "query"` an agent
# would type. Empty by default: §27/§28 ran the shipped defaults, and their
# unfinished cells must not silently acquire a new engine.
#
# The chunking half must ALSO reach the index build, and that is the trap this
# pair of constants exists to avoid. A repo-local `.gorp/` is exempt from
# cache-tag matching by design (cache::discover — "the user built it
# deliberately"), so an index built with line windows will happily answer a
# function-chunked search: set only the search half and every DIRECTORY scope
# runs untreated while file scopes (which resolve no index) run treated. That
# is a half-dose reported as a diluted null — the same failure guessplay.py's
# CONFIGS comment documents for the rendering levers.
# §37.3 lesson: the CLI auto-updated between two arms of one rung (2.1.235 ->
# 2.1.236) and provaudit caught the mixture only after $28 was spent. The CLI
# is toolchain, and toolchain gets pinned like the model: point this at a
# versioned binary (~/.local/share/claude/versions/<v>) to freeze it for a
# campaign. Default stays "claude" so ad-hoc runs behave as before.
CLAUDE_BIN = os.environ.get("SWEXPLORE_CLAUDE_BIN", "claude")

SG_SEARCH_FLAGS = os.environ.get("SWEXPLORE_SG_FLAGS", "")
SG_INDEX_FLAGS = os.environ.get("SWEXPLORE_SG_INDEX_FLAGS", "")

# §30.3's conclusion, as a switch: the substitutive design's grep block taxed
# the sg arm 1.3 turns/session because ripgrep IS a grep and sg is not — the
# block subsidised the arm whose treatment resembled the thing removed. With
# this set, shell grep/egrep/fgrep pass through to the real binaries IN BOTH
# ARMS (still shimmed, so every call is logged); the native Grep *tool* stays
# removed, which is what keeps delivery high. Default off: §27/§28/§30 ran
# with the block, and their cells must stay comparable to themselves.
UNBLOCK_GREP = os.environ.get("SWEXPLORE_UNBLOCK_GREP", "") == "1"

# Arms whose *registration* includes open shell grep, independent of the env
# switch above. UNBLOCK_GREP is an operator knob and must stay off for the
# frozen arms; cc-gorp-route is registered WITH grep open, so gating it on an
# env var would make the arm's identity depend on the shell it was launched
# from. Grep stays shimmed either way — pass-through, but logged.
GREP_OPEN_ARMS = frozenset({"cc-gorp-route", "cc-bash", "cc-gorp-route2"})

# --------------------------------------------------------------------------
# The one clause of upstream's prompt we rewrite, and why
# --------------------------------------------------------------------------
# Their EXPLORE_PROMPT instructs "Use Glob, Grep, and Read tools to explore the
# codebase." The first smoke measured the consequence: across every arm and
# every instance, `bash_calls` was **0** — neither `sg` nor `rg` was invoked
# once, all three arms returned identical answers, and the treatment was
# simply never delivered. An appended system prompt saying the tool exists
# does not survive a user prompt naming three others; §25's "availability is
# not use", in a new place.
#
# So the clause is amended for the two treatment arms and ONLY that clause:
# same position, same shape, one tool name added. `cc` keeps upstream's prompt
# byte-for-byte, which is what preserves it as the calibration anchor against
# the published row.
#
# `_amend` asserts the clause is present. If a future upstream edits the
# wording, the arm dies loudly rather than quietly reverting to a no-op
# treatment that would read as a clean null.
PROMPT_CLAUSE = "Use Glob, Grep, and Read tools to explore the codebase."
ARM_CLAUSE = {
    "cc": None,
    "cc-rg": "Use Glob, Grep, Read, and the `rg` command (via Bash) to explore the codebase.",
    "cc-sg": "Use Glob, Grep, Read, and the `sg` command (via Bash) to explore the codebase.",
    # The sub-* clauses drop Grep, because for these arms it does not exist and
    # naming it would send the agent at a tool it cannot call. Otherwise the
    # sentence keeps its position and shape, so the only difference between
    # cc-sg and sub-sg is the presence of Grep — which is the treatment.
    "sub-rg": "Use Glob, Read, and the `rg` command (via Bash) to explore the codebase.",
    "sub-sg": "Use Glob, Read, and the `sg` command (via Bash) to explore the codebase.",
    "sub-sgb": "Use Glob, Read, and the `sg` command (via Bash) to explore the codebase.",
    # Keeps Grep, like the other cc-* clauses: cc-gorp is additive.
    "cc-gorp": "Use Glob, Grep, Read, and the `gorp` command (via Bash) to explore the codebase.",
    # §37: names both shell commands, because both are open in this arm.
    "cc-gorp-route": "Use Glob, Grep, Read, and the `gorp` and `grep` commands (via Bash) to explore the codebase.",
    # §37.2: cc-rg's clause shape with grep in the rg slot, so the shell
    # search gets equal billing across the arms being contrasted.
    "cc-bash": "Use Glob, Grep, Read, and the `grep` command (via Bash) to explore the codebase.",
    "cc-gorp-route2": "Use Glob, Grep, Read, and the `gorp` and `grep` commands (via Bash) to explore the codebase.",
}


def _amend(prompt: str, arm: str) -> str:
    clause = ARM_CLAUSE[arm]
    if clause is None:
        return prompt
    if PROMPT_CLAUSE not in prompt:
        raise RuntimeError(
            f"{arm}: upstream EXPLORE_PROMPT no longer contains the clause this "
            f"arm rewrites ({PROMPT_CLAUSE!r}). Re-register the arm rather than "
            f"running a treatment that is never delivered.")
    return prompt.replace(PROMPT_CLAUSE, clause)

HERE = Path(__file__).resolve().parent
# This file does not run from where it is checked in: fetch.sh copies it into
# data/swexplore/upstream/explorers/, so paths resolve from *there*.
#   <bench>/data/swexplore/upstream/explorers
#      [0]=upstream [1]=swexplore [2]=data [3]=<bench repo root>
# This said parents[4] and was right while the bench lived at gorp/eval/, one
# level deeper. After the split it resolved to Flower-Computer/, which made
# SHIM a path that does not exist — so every shim wrapper would have exec'd a
# missing file and every single search would have failed. An arm whose tool
# always errors still produces rows, still costs money, and reads as a clean
# null: §16.10 exactly. Hence the assertion rather than a comment.
BENCH_ROOT = HERE.parents[3]
LOCBENCH = BENCH_ROOT / "harness" / "locbench"
SHIM = BENCH_ROOT / "harness" / "common" / "shim.py"
if not SHIM.exists():
    raise RuntimeError(
        f"sg_arms: shim not found at {SHIM} (BENCH_ROOT={BENCH_ROOT}). "
        f"Every search would fail silently; refusing to run.")
# The engine is a sibling checkout, not part of this repo. Deliberately not
# imported from harness/common: this file runs from inside the vendored
# upstream, where that package is not on the path.
GORP_REPO = Path(os.environ.get("GORP_REPO") or BENCH_ROOT.parent / "gorp")
GORP_BIN = Path(os.environ.get("GORP_BIN", GORP_REPO / "target/release/gorp"))
RG_BIN = os.environ.get("RG_BIN", "/opt/homebrew/bin/rg")

# Provenance level. `full` is for the ladder rungs, where the point is to be
# able to read what happened; `lean` is for the powered run, where 2,544 cells
# of transcripts and search dumps is ~650 MB nobody will open.
#
# The switch is safe to flip mid-campaign, and that is a load-bearing claim
# rather than a convenience: every artifact it drops is written *after* the
# bytes have already been replayed to the agent, so agent-visible behaviour is
# identical and lean rungs pool with full ones. What it must NOT touch:
#   * the shim itself and its block messages — that is the mechanism that
#     keeps shell `grep` away from the arms, not logging;
#   * `GORP_NO_HINTS` — §16.10 measured that footer moving an agent's
#     ranked share from 7% to 98%, so it is a treatment;
#   * `--stats` / `--stats-json` — those write to stderr, which shim.py
#     replays to the agent, so they are agent-visible by construction and are
#     never used here at any level.
#
# Three levels, not two (§36). `lean` drops the transcript, and the transcript
# is the only place the agent's trajectory survives — so a powered run that
# still wants its trajectories read by hand had to pay for `full`, which also
# dumps every search's stdout to its own file. That dump is the one artifact
# with a cost the agent can feel: shim.py writes it BEFORE replaying the bytes,
# so it sits in the latency of every single search.
#
#   full   stdout dumps + transcripts    ladder rungs; everything readable
#   trace  transcripts only              powered runs; trajectories kept,
#                                        nothing added to search latency
#   lean   neither                       when only the endpoints matter
#
# None of the three changes a byte the agent sees, which is what lets rungs at
# different levels pool.
PROV = os.environ.get("SWEXPLORE_PROV", "full")
if PROV not in ("full", "trace", "lean"):
    raise RuntimeError(f"SWEXPLORE_PROV={PROV!r}; expected full|trace|lean")


def _index_matches(meta_path: Path, flags: list[str]) -> bool:
    """Does this built index actually carry the chunking the arm asked for?

    Only `--chunking` is checked, because it is the one index-side flag §30
    sets and the only one whose absence is invisible: a repo-local `.gorp`
    is exempt from cache-tag matching, so a window-chunked index answers a
    function-chunked search with no error anywhere.
    """
    want = "window"
    if "--chunking" in flags:
        want = flags[flags.index("--chunking") + 1]
    try:
        meta = json.loads(meta_path.read_text())
    except Exception:  # noqa: BLE001
        return False
    got = (meta.get("params") or {}).get("function")
    return (got is not None) if want == "function" else (got is None)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# --------------------------------------------------------------------------
# Provenance (§36)
# --------------------------------------------------------------------------
# Everything below is written AFTER the bytes have been replayed to the agent,
# or before the agent starts at all — never in its path. That is the same
# invariant PROV documents above, and it is what lets a lean rung pool with a
# full one. None of it touches the shim, the block messages, GORP_NO_HINTS or
# stderr.
#
# The gap this closes: meta.json recorded `"model": "sonnet"`, the alias, and
# never what the alias resolved to. Every campaign s27..s33 in fact ran
# claude-sonnet-5, which is only discoverable by reading a transcript — and
# RESEARCH.md §32.3 calibrated our `cc` arm against the paper's Sonnet-4.5 row
# on the assumption it had not. An alias is not a record.

_CLAUDE_VERSION: str | None = None
_GORP_VERSION: dict | None = None


def _run_ok(argv: list[str]) -> str:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=30)
        return p.stdout.strip() if p.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


def _claude_version() -> str:
    """`claude --version`, once per process. locbench has always recorded this
    (run.py:740); swexplore never did, so a CLI upgrade mid-campaign was
    invisible."""
    global _CLAUDE_VERSION
    if _CLAUDE_VERSION is None:
        _CLAUDE_VERSION = _run_ok([CLAUDE_BIN, "--version"]).splitlines()[:1]
        _CLAUDE_VERSION = _CLAUDE_VERSION[0] if _CLAUDE_VERSION else ""
    return _CLAUDE_VERSION


def _gorp_version() -> dict:
    """The binary's own identity block: commit, dirty, profile, embed dim,
    compat key.

    The engine's git SHA reaches disk today only inside trace.jsonl, which
    exists only for gorp arms — so the `cc` control records nothing at all
    about the engine it is a control for, and a rebuild between arms would
    leave no trace anywhere. A sha256 says two binaries differ; this says how.
    """
    global _GORP_VERSION
    if _GORP_VERSION is None:
        out, meta = _run_ok([str(GORP_BIN), "--version"]), {}
        for line in out.splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                meta[k.strip().replace(" ", "_")] = v.strip()
            elif line.strip():
                meta["version"] = line.strip()
        # `commit: db4ef4a (dirty)` -> the flag is worth its own field, because
        # "dirty" means the binary does not correspond to any commit at all.
        c = meta.get("commit", "")
        meta["git_dirty"] = c.endswith("(dirty)")
        meta["commit"] = c.replace("(dirty)", "").strip()
        _GORP_VERSION = meta
    return _GORP_VERSION


def _git_head(repo: Path) -> dict:
    sha = _run_ok(["git", "-C", str(repo), "rev-parse", "HEAD"])
    dirty = _run_ok(["git", "-C", str(repo), "status", "--porcelain"])
    return {"sha": sha[:12], "dirty": bool(dirty.strip())}


# Which description each arm carries. cc/cc-rg have none. cc-gorp is pinned to
# v12 at registration; the sg arms follow SWEXPLORE_SG_DESC, which is exactly
# the variable that was previously unrecorded — inferable only by matching
# system_prompt_sha256 against a string you already had to guess.
def _desc_version(arm: str) -> str | None:
    if arm == "cc-gorp":
        return "v12"
    if arm == "cc-gorp-route":
        return "v14"
    if arm == "cc-gorp-route2":
        return "v15"
    return SG_DESC if ARM_TOOL.get(arm) in ("sg", "gorp") else None


_RUN_PROV_DONE = False


def _write_run_provenance(run_dir: Path) -> None:
    """One runs/<run_id>/provenance.json, written before the first cell.

    Per-cell meta.json answers "what ran here"; this answers "what was this
    machine, on this day, for this whole campaign" — the vendored upstream's
    exact state included, which no cell can see. Written once per process and
    only if absent, so a resumed rung does not overwrite the record of the
    pass that paid for most of it.
    """
    global _RUN_PROV_DONE
    if _RUN_PROV_DONE:
        return
    _RUN_PROV_DONE = True
    out = run_dir / "provenance.json"
    if out.exists():
        return
    up = HERE.parents[0]                       # data/swexplore/upstream
    data = up.parent                           # data/swexplore
    frame = data / "ladder-frame.json"
    try:
        frame_sha = json.loads(frame.read_text()).get("sha256") if frame.exists() else None
    except Exception:  # noqa: BLE001
        frame_sha = None
    rec = {
        "run_id": run_dir.name,
        "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "host": platform.node(),
        "platform": platform.platform(),
        "provenance_level": PROV,
        "claude_version": _claude_version(),
        "gorp_bin": str(GORP_BIN),
        "gorp_sha256": _binary_sha(),
        "gorp_version": _gorp_version(),
        "gorp_repo": {"path": str(GORP_REPO), **_git_head(GORP_REPO)},
        # The vendored harness is a measurement input too: a dirty overlay is
        # an unregistered treatment, and fetch.sh's own delta is the baseline
        # to compare against.
        "upstream": {
            "sha": _run_ok(["git", "-C", str(up), "rev-parse", "HEAD"])[:12],
            "delta": sorted(
                l for l in _run_ok(
                    ["git", "-C", str(up), "status", "--porcelain"]).splitlines() if l),
        },
        "ladder_frame_sha256": frame_sha,
        "env": {k: os.environ.get(k) for k in (
            "SWEXPLORE_SG_DESC", "SWEXPLORE_SG_FLAGS", "SWEXPLORE_SG_INDEX_FLAGS",
            "SWEXPLORE_SGB_EXTRA", "SWEXPLORE_UNBLOCK_GREP", "SWEXPLORE_CACHE_GB",
            "SWEXPLORE_ROLLING", "SWEXPLORE_PROV")},
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rec, indent=1) + "\n")


_BIN_SHA: str | None = None


def _binary_sha() -> str | None:
    """sha256 of the sg binary, computed once. 39 MB × 2,544 runs is 4 minutes
    of hashing for a constant, so it is cached rather than recomputed."""
    global _BIN_SHA
    if _BIN_SHA is None and GORP_BIN.exists():
        h = hashlib.sha256()
        with GORP_BIN.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        _BIN_SHA = h.hexdigest()
    return _BIN_SHA


def tools_lines_track_descv9() -> dict:
    """Assert our sg line still carries desc-v9's own mechanism sentences.

    Run by `preflight.py`. The two clauses below are the parts §19 measured;
    if a future desc-vN edits them, this fires and the arm is re-registered
    deliberately rather than drifting into a different treatment silently.
    """
    sys.path.insert(0, str(LOCBENCH))
    import run as locbench  # noqa: E402

    v9 = locbench.TOOL_LINES["desc-v9"]
    shared = [
        "Give it anything — an identifier, a phrase, or a question",
        'Example: sg "retry_backoff backoff_delay compute_delay"',
        "Ranked, not exhaustive — if the answer isn't there, rephrase.",
    ]
    missing = [s for s in shared if s not in v9 or s not in SG_LINE]
    return {"ok": not missing, "missing": missing,
            "descv9_sha256": _sha(v9), "sg_line_sha256": _sha(SG_LINE)}


@dataclass
class ArmExplorer(ClaudeCodeExplorer):
    """One §27 arm. `repo_root`, `model`, `timeout` come from the base class."""

    arm: str = "cc"
    run_dir: Path = field(default_factory=lambda: Path("runs/adhoc"))
    # Their explorer hardcodes bypassPermissions. That skips permission checks
    # entirely, which makes --allowed-tools advisory rather than binding — so
    # `cc` could shell out and the confound design would be void. `dontAsk` is
    # what locbench has used for every campaign since §16 and it binds the
    # allowlist. Overridable so the two can be compared head to head, which
    # preflight does on one instance before anything is spent.
    permission_mode: str = "dontAsk"

    # ---------------------------------------------------------------- env
    def _cond_dir(self, instance_id: str) -> Path:
        d = self.run_dir / instance_id / self.arm
        (d / "searches").mkdir(parents=True, exist_ok=True)
        return d

    def _make_shims(self, bin_dir: Path) -> None:
        """One wrapper per shimmed *and* blocked name, front of PATH.
        Mirrors run.py:485-494.

        BLOCKED is load-bearing and was learned the hard way. `--allowedTools
        Bash(rg *)` does NOT bind under `--permission-mode dontAsk` — dontAsk
        means "do not prompt", not "restrict". The first amended smoke caught
        the consequence: cc-rg ran `grep -n "n_jobs" sklearn/...` straight
        through the Bash tool and never touched rg at all. Left in, every
        Bash-enabled arm can do lexical search without invoking its own
        treatment, all three arms converge on shell grep, and the campaign
        reports a clean null that means nothing.

        shim.py blocks any name with no LOCBENCH_REAL_* binding, so listing
        them here is the whole mechanism. `git` goes too, for the reason
        run.py:477-482 gives: history after base_commit contains the real
        fix, and `git log --all --grep=<issue>` is ground truth.
        """
        bin_dir.mkdir(parents=True, exist_ok=True)
        for tool in (*SEARCH_TOOLS, "grep", "egrep", "fgrep", "git"):
            w = bin_dir / tool
            w.write_text(
                f'#!/bin/sh\nexec /usr/bin/env python3 "{SHIM}" {tool} "$@"\n'
            )
            w.chmod(0o755)

    def _env(self, cond_dir: Path) -> tuple[dict, str]:
        bin_dir = cond_dir / "bin"
        self._make_shims(bin_dir)
        path = f"{bin_dir}:{os.environ['PATH']}"
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        env.update({
            "PATH": path,
            "LOCBENCH_SHIM_LOG": str(cond_dir / "shim_log.jsonl"),
            "LOCBENCH_STDOUT_DIR": str(cond_dir / "searches"),
            # Sibling of runs/, not inside it. Under run_dir it created
            # runs/<run>/gorp-cache/<arm>/, which sits in the same namespace
            # as the instance directories — so anything globbing
            # runs/<run>/*/ picks up a cache dir as if it were an instance.
            "GORP_CACHE_DIR": str(self.run_dir.parent.parent / "cache"
                                     / self.run_dir.name / self.arm),
            "GORP_NO_HINTS": "1",
            "GORP_TRACE_FILE": str(cond_dir / "trace.jsonl"),
            "LOCBENCH_STDOUT_DUMP": "on" if PROV == "full" else "off",
        })
        # Only this arm's own tool gets a real binding; the other falls into
        # shim.py's blocked path, so an arm escaping its treatment is visible
        # as a blocked row rather than silently succeeding.
        for t in SEARCH_TOOLS:
            env.pop(f"LOCBENCH_REAL_{t.upper()}", None)
            env.pop(f"LOCBENCH_{t.upper()}_FLAGS", None)
        tool = ARM_TOOL.get(self.arm)
        if tool == "rg":
            env["LOCBENCH_REAL_RG"] = RG_BIN
        elif tool in ("sg", "gorp"):
            # One binary, two names. `sg` and `gorp` are the same engine under
            # the name its arm was registered with; recorded arms keep the
            # name they were measured under (CLAUDE.md), so both bindings
            # exist and point at the same GORP_BIN.
            env[f"LOCBENCH_REAL_{tool.upper()}"] = str(GORP_BIN)
            flags = SG_SEARCH_FLAGS
            if self.arm == "sub-sgb":
                extra = os.environ.get("SWEXPLORE_SGB_EXTRA", "--bridge-expand 8")
                flags = f"{flags} {extra}".strip()
            if flags:
                env[f"LOCBENCH_{tool.upper()}_FLAGS"] = flags
        grep_open = UNBLOCK_GREP or self.arm in GREP_OPEN_ARMS
        if grep_open:
            for g in ("grep", "egrep", "fgrep"):
                env[f"LOCBENCH_REAL_{g.upper()}"] = f"/usr/bin/{g}"
        # Steer rather than just refuse: shim.py writes these on both stdout
        # and stderr, because an agent piping into `head` sees silence
        # otherwise and reads it as "no matches" (run.py:514-531).
        # §37: in a grep-open arm the steer names both tools. s36's cc-gorp
        # steer said "use the gorp command instead" to 380 blocked shell-grep
        # attempts — routing lexical intent into ranked search, the exact
        # anti-routing the v13 clause exists to undo.
        if tool and self.arm in GREP_OPEN_ARMS:
            steer = f"use the {tool} command or grep instead"
        elif tool:
            steer = f"use the {tool} command instead"
        elif grep_open:
            steer = "use grep or the Grep tool instead"
        else:
            steer = "use the Grep and Glob tools instead"
        blocked_names = SEARCH_TOOLS if grep_open else \
            ("grep", "egrep", "fgrep", *SEARCH_TOOLS)
        for t in blocked_names:
            env[f"LOCBENCH_BLOCKMSG_{t.upper()}"] = (
                f"{t}: unavailable in this environment — {steer}")
        env["LOCBENCH_BLOCKMSG_GIT"] = (
            "git: unavailable in this environment — do not retry git commands; "
            "search the working tree instead")
        return env, path

    def _ensure_index(self) -> dict:
        """Build .gorp in the checkout. Only the sg arm gets one."""
        tool = ARM_TOOL.get(self.arm)
        if tool == "gorp":
            # Registered intent (§36): shipped default. The engine builds
            # lazily into its global cache (~/.cache/gorp) on the first
            # ranked search of a scope — s36 measured that at 216 ms median
            # in-session — so there is nothing to pre-build here, and saying
            # "arm has no sg" made a working treatment read like a miss.
            return {"built": False,
                    "reason": "gorp builds lazily into its global cache "
                              "(shipped default)"}
        if tool != "sg":
            return {"built": False, "reason": "arm has no sg"}
        idx = Path(self.repo_root) / ".gorp" / "meta.json"
        flags = shlex.split(SG_INDEX_FLAGS)
        if idx.exists() and idx.stat().st_mtime >= GORP_BIN.stat().st_mtime \
                and _index_matches(idx, flags):
            return {"built": False, "reason": "reused"}
        t0 = time.time()
        p = subprocess.run([str(GORP_BIN), "index", str(self.repo_root), *flags],
                           capture_output=True, timeout=3600)
        out = {"built": p.returncode == 0, "index_s": round(time.time() - t0, 2),
               "returncode": p.returncode, "index_flags": SG_INDEX_FLAGS,
               "stderr": p.stderr.decode(errors="ignore")[-400:] if p.returncode else ""}
        # Readback, for the same reason guessplay.py asserts its own: a build
        # that quietly produced the previous geometry is indistinguishable from
        # a null, and a repo-local index is exempt from cache-tag matching, so
        # nothing downstream would catch it.
        if p.returncode == 0 and not _index_matches(idx, flags):
            raise RuntimeError(
                f"{self.arm}: index at {idx} does not carry {SG_INDEX_FLAGS!r} "
                f"after a successful build — the arm would run untreated")
        return out

    # ------------------------------------------------------------- explore
    def explore(self, *, instance_id: str, query: str, top_k: int = 5
                ) -> List[ExplorerResult]:
        from .claude_code import EXPLORE_PROMPT  # their prompt, untouched
        from .parsing import parse_relevant_files

        cond_dir = self._cond_dir(instance_id)
        _write_run_provenance(self.run_dir)
        tools, allowed, sysline = ARMS[self.arm]
        prompt = _amend(EXPLORE_PROMPT.format(issue=query, top_k=top_k), self.arm)
        index = self._ensure_index()
        env, path = self._env(cond_dir)

        cmd = [
            CLAUDE_BIN, "-p",
            "--output-format", "stream-json", "--verbose",
            "--permission-mode", self.permission_mode,
            "--model", self.model,
            "--tools", tools,
            "--no-session-persistence",
            # The first smoke exposed 36 tools, 32 of them MCP servers from
            # the operator's own config (Google Drive, Gmail, Playwright).
            # That is contamination three ways: capabilities the benchmark
            # never granted, a system prompt inflated by 32 tool schemas —
            # which is cache-creation tokens, i.e. the co-primary endpoint —
            # and a configuration nobody else could reproduce. Both flags,
            # because --strict-mcp-config alone still lets settings files in.
            "--strict-mcp-config",
            "--setting-sources", "",
            # Belt and braces against a login shell resetting PATH, exactly
            # as run.py:652 does — without it the shims are bypassed and the
            # agent talks to the real binary with no log.
            "--settings", json.dumps({"env": {"PATH": path}}),
        ]
        if allowed:
            cmd += ["--allowedTools", *allowed]
        if sysline:
            cmd += ["--append-system-prompt", sysline]

        (cond_dir / "meta.json").write_text(json.dumps({
            "instance_id": instance_id, "arm": self.arm, "model": self.model,
            "tools": tools, "allowed_tools": allowed,
            "permission_mode": self.permission_mode,
            "provenance": PROV,
            # Both recorded: the amended prompt this arm actually sent, and
            # upstream's unamended one. cc's two are equal by construction, so
            # the pair is the audit trail for exactly what diverged and where.
            "user_prompt_sha256": _sha(prompt),
            "upstream_prompt_sha256": _sha(EXPLORE_PROMPT.format(issue=query, top_k=top_k)),
            "system_prompt_sha256": _sha(sysline) if sysline else None,
            "gorp_sha256": _binary_sha(),
            # The toolchain actually under the cell. `model` stays as the
            # alias that was requested; model_resolved is filled in after the
            # run from the transcript's system/init line, because only the CLI
            # knows what an alias means on the day it ran.
            "model_requested": self.model,
            "model_resolved": None,
            "claude_version": _claude_version(),
            "gorp_bin": str(GORP_BIN),
            "gorp_version": _gorp_version(),
            "gorp_repo": _git_head(GORP_REPO),
            # The treatment variables. Every one of these was previously
            # unrecorded, so two cells differing in description or in hidden
            # engine flags were indistinguishable on disk.
            "desc_version": _desc_version(self.arm),
            "sg_search_flags": SG_SEARCH_FLAGS,
            "sg_index_flags": SG_INDEX_FLAGS,
            "unblock_grep": UNBLOCK_GREP or self.arm in GREP_OPEN_ARMS,
            "index": index, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }, indent=1) + "\n")

        transcript = cond_dir / "transcript.jsonl"
        status, t0 = "ok", time.time()
        # Binary mode throughout: the transcript is streamed straight to disk
        # so a mid-run crash still leaves everything up to that point, and the
        # prompt goes in as bytes to match. Theirs buffers stdout in memory
        # and loses it on a timeout.
        with open(transcript, "wb") as tf:
            try:
                p = subprocess.run(cmd, cwd=str(self.repo_root),
                                   input=prompt.encode(),
                                   stdout=tf, stderr=subprocess.PIPE,
                                   timeout=self.timeout, env=env)
                if p.returncode != 0:
                    status = "agent_error"
            except subprocess.TimeoutExpired:
                status = "timeout"
        wall = round(time.time() - t0, 2)

        result_text, agent = "", {"status": status, "harness_wall_s": wall}
        bash_calls = grep_calls = 0
        model_resolved = None
        for line in transcript.read_text(errors="ignore").splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            # `message` is sometimes a bare string and `content` sometimes a
            # bare string — the same shape that crashed displaychmp.walk()
            # (§25.3). Guard both, or a single such event kills the arm after
            # the money has already been spent.
            # The CLI announces what the alias resolved to, once, in its init
            # event. This is the only place the truth appears.
            if ev.get("type") == "system" and ev.get("subtype") == "init":
                model_resolved = ev.get("model")
            msg = ev.get("message")
            content = msg.get("content") if isinstance(msg, dict) else None
            for block in (content if isinstance(content, list) else []):
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    if block.get("name") == "Bash":
                        bash_calls += 1
                    elif block.get("name") == "Grep":
                        grep_calls += 1
            if ev.get("type") == "result":
                agent.update(
                    num_turns=ev.get("num_turns"),
                    total_cost_usd=ev.get("total_cost_usd"),
                    duration_ms=ev.get("duration_ms"),
                    duration_api_ms=ev.get("duration_api_ms"),
                    usage=ev.get("usage") or {},   # whole and unparsed
                )
                result_text = ev.get("result") or ""
                if ev.get("is_error") and agent["status"] == "ok":
                    agent["status"] = "agent_error"

        # meta.json is written before the run so a crash still leaves one;
        # the resolved model can only be known after. Patch the field rather
        # than rewriting the file from scratch, so nothing else can drift.
        meta_path = cond_dir / "meta.json"
        try:
            meta = json.loads(meta_path.read_text())
            meta["model_resolved"] = model_resolved
            meta_path.write_text(json.dumps(meta, indent=1) + "\n")
        except Exception:  # noqa: BLE001
            pass   # provenance must never be able to fail a paid cell
        agent["model_resolved"] = model_resolved

        shim = self._read_shim_log(cond_dir)
        agent.update(bash_calls=bash_calls, grep_calls=grep_calls, **shim)
        agent.update(instance_id=instance_id, arm=self.arm, index=index)
        # One file per cell rather than a shared append log: the runner joins
        # this back by (instance_id, arm) and a per-cell file needs no lock,
        # no scan, and is overwritten cleanly by a --resume retry.
        (cond_dir / "telemetry.json").write_text(json.dumps(agent, indent=1) + "\n")

        # The transcript is *written* at both levels — it is streamed to disk so
        # a crash still leaves everything up to that point, and the cost fields
        # above are parsed out of it. Under `lean` it is discarded once parsed,
        # which is why the endpoint survives losing the file.
        if PROV == "lean":
            transcript.unlink(missing_ok=True)

        return parse_relevant_files(result_text, instance_id, top_k=top_k)

    @staticmethod
    def _read_shim_log(cond_dir: Path) -> dict:
        log = cond_dir / "shim_log.jsonl"
        rows = []
        if log.exists():
            for line in log.read_text(errors="ignore").splitlines():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        live = [r for r in rows if not r.get("blocked")]
        return {
            "n_invocations": len(live),
            "n_blocked": sum(1 for r in rows if r.get("blocked")),
            # SEARCH_TOOLS, not a literal ("rg", "sg"): this was the fifth
            # copy of that tuple, and the one with no loud failure mode — a
            # cc-gorp cell ran six gorp searches and reported n_gorp absent,
            # so every invocation-rate downstream read zero.
            "n_by_tool": {t: sum(1 for r in live if r.get("tool") == t)
                          for t in SEARCH_TOOLS},
            "total_stdout_bytes": sum(r.get("stdout_bytes") or 0 for r in live),
            "total_search_ms": round(sum(r.get("wall_ms") or 0 for r in live), 1),
            "n_nonzero_exit": sum(1 for r in live if r.get("exit")),
        }
