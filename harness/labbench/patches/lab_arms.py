"""The three LAB arms, as a ToolExecutor extension.

Dropped into the vendored harvey-labs root as `lab_arms.py` by `fetch.sh`;
`0001-run-arm-executor-metrics.patch` makes upstream's `harness/run.py` call
`build()` instead of constructing its own executor. Everything else upstream
— the agent loop, the adapters, the judge — is untouched.

    lab-base   upstream's six tools, prompt byte-for-byte   calibration anchor
    lab-rg     + one structured tool backed by ripgrep      tool-shape control
    lab-gorp   + one structured tool backed by gorp         treatment

Three contrasts: lab-gorp→lab-rg is ranking vs exact under an identical tool
shape (the PRIMARY), lab-rg→lab-base prices having a structured search tool
at all, lab-base→lab-gorp is the product question.

The corpus these arms run over is `md-v1`: every task document converted to
markdown by `build_corpus.py` using upstream's own judge-side extractor, the
original deleted (the operator does not have 3 GB for binaries the agent
would only ever see through the same extractor). Search results therefore
cite the very files the agent reads — `<original name>.md` — and no path
translation layer exists anywhere.

The arm crosses into this vendored code as env (`LABBENCH_ARM`), not a CLI
flag, for the same reason swexplore steers with `SWEXPLORE_*`: the driver
already must set per-cell env for the shim (ToolExecutor runs in-process
inside `harness.run`), and a flag would grow the git patch into upstream's
most-edited argparse block. The arm is echoed into `telemetry.json`, and the
driver cross-checks it against what it set — an env var that didn't arrive
is caught, not silent.

This file is import-clean outside the vendored tree: the upstream-dependent
half (ArmToolExecutor, build) appears only when `harness.tools` is
importable. Both of its homes — `harness/labbench/patches/` where it is
checked in, and `data/labbench/upstream/` where it runs — sit exactly three
levels below the bench root, so one `parents[3]` serves tests and runs alike.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve()
BENCH_ROOT = HERE.parents[3]
LOCBENCH = BENCH_ROOT / "harness" / "locbench"
SHIM = BENCH_ROOT / "harness" / "common" / "shim.py"
# The engine is a sibling checkout, not part of this repo. Deliberately not
# imported from harness/common: this file runs from inside the vendored
# upstream, where that package is not on the path.
GORP_REPO = Path(os.environ.get("GORP_REPO") or BENCH_ROOT.parent / "gorp")
GORP_BIN = Path(os.environ.get("GORP_BIN", GORP_REPO / "target/release/gorp"))

CORPUS = "md-v1"

# --------------------------------------------------------------------------
# Naming contract
# --------------------------------------------------------------------------
# One rule, used by build_corpus.py, preflight, and the tests: the markdown
# conversion of `documents/<rel>` is `documents/<rel>.md` — suffix appended
# always, even to `.txt` and `.md` originals, so the name still shows what
# the document was. There is no reverse mapping because nothing needs one:
# the original is gone and the `.md` file IS the corpus.


def md_name(rel: str) -> str:
    """Corpus filename for an original document's docroot-relative path."""
    return rel + ".md"


def agent_path(rel_md: str) -> str:
    """The path the agent reads and search results cite."""
    return "/workspace/documents/" + rel_md


# --------------------------------------------------------------------------
# Tool lines
# --------------------------------------------------------------------------
# Both descriptions are built to the same shape — opener, mechanism, worked
# example, caveat — because §7.3 and §19 measured that description quality
# moves agent behaviour. An rg tool described worse than the gorp tool would
# turn the lab-rg→lab-gorp contrast into a description contrast, so the two
# share their capability claims and their worked-example result line
# verbatim (asserted by tests/test_labbench_descriptions.py). The mechanism
# clause and the caveat in GORP_DESC are desc-v9's own words, asserted
# against locbench by `tools_lines_track_descv9` below; the example is
# legal-domain because the corpus is.

_EXAMPLE_HIT = (
    "/workspace/documents/spa-execution-copy.docx.md:412: the Indemnification "
    "Cap shall not exceed fifteen percent (15%) of the Purchase Price."
)

GORP_DESC = (
    "Ranked search across all task documents at once. "
    "Give it anything — an identifier, a phrase, or a question: it returns "
    "the most relevant passages as path:line: text (top 10; `k` for more). "
    'Example: query "indemnification cap survival period" → '
    f"{_EXAMPLE_HIT} "
    "Ranked, not exhaustive — if the answer isn't there, rephrase."
)

RG_DESC = (
    "Exact regex search (ripgrep) across all task documents at once. "
    "The pattern is a regex; every match prints as path:line: text. "
    'Example: pattern "[Ii]ndemnification [Cc]ap" → '
    f"{_EXAMPLE_HIT} "
    "Exhaustive, not ranked — if it floods, narrow the pattern or add a path."
)

# Registered description versions. A rewrite is a NEW version selected by
# env, never an edit: an edited description is a different arm reported
# under the old name (CLAUDE.md, "never edit a frozen arm").
DESCS = {"v1": {"gorp": GORP_DESC, "rg": RG_DESC}}
DESC_VERSION = os.environ.get("LABBENCH_DESC", "v1")
if DESC_VERSION not in DESCS:
    raise RuntimeError(f"unknown LABBENCH_DESC {DESC_VERSION!r}; "
                       f"registered: {sorted(DESCS)}")

GORP_TOOL = {
    "name": "gorp",
    "description": DESCS[DESC_VERSION]["gorp"],
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "An identifier, a phrase, or a question",
            },
            "path": {
                "type": "string",
                "description": ("File or directory under /workspace/documents "
                                "to narrow the search. Defaults to all "
                                "documents."),
            },
            "k": {
                "type": "integer",
                "description": "Number of ranked results (default 10, max 50)",
            },
        },
        "required": ["query"],
    },
}

RG_TOOL = {
    "name": "rg",
    "description": DESCS[DESC_VERSION]["rg"],
    "parameters": {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Regex pattern to search for",
            },
            "path": {
                "type": "string",
                "description": ("File or directory under /workspace/documents "
                                "to narrow the search. Defaults to all "
                                "documents."),
            },
            "ignore_case": {
                "type": "boolean",
                "description": "Case-insensitive matching (default false)",
            },
        },
        "required": ["pattern"],
    },
}

# arm -> extra structured tool, or None. ADDITIVE by construction: upstream's
# six tools always stay, so lab-base is upstream verbatim and the treatment
# is exactly one added tool.
ARMS = {
    "lab-base": None,
    "lab-rg": RG_TOOL,
    "lab-gorp": GORP_TOOL,
}
ARM_TOOL = {"lab-rg": "rg", "lab-gorp": "gorp"}

ARM = os.environ.get("LABBENCH_ARM", "lab-base")
if ARM not in ARMS:
    raise RuntimeError(f"unknown LABBENCH_ARM {ARM!r}; "
                       f"registered: {sorted(ARMS)}")

# Output caps. rg inherits upstream grep's own 250-line cap so the two
# lexical surfaces flood identically; gorp is capped by k.
RG_LINE_CAP = 250
GORP_K_DEFAULT = 10
GORP_K_MAX = 50

# --------------------------------------------------------------------------
# The one clause of upstream's prompt we extend, and why
# --------------------------------------------------------------------------
# swexplore's first smoke measured the consequence of announcing a tool only
# in its description: bash_calls was 0 across every arm and the treatment was
# never delivered ("availability is not use", §25). So the treatment arms
# append one line to the system prompt's tool conventions, anchored on the
# `read` bullet. The anchor is asserted: if upstream rewrites the prompt, the
# arm dies loudly rather than quietly running an undelivered treatment.
# lab-base keeps the prompt byte-for-byte — the calibration anchor.
PROMPT_CLAUSE = (
    "- Use `read` to consume input files (handles .docx, .xlsx, .pptx, "
    ".pdf, and\n  plain text)."
)
ARM_CLAUSE = {
    "lab-base": None,
    "lab-rg": (
        "- Use the `rg` tool to locate relevant passages across all "
        "documents before deciding what to read."
    ),
    "lab-gorp": (
        "- Use the `gorp` tool to locate relevant passages across all "
        "documents before deciding what to read."
    ),
}


def amend_system_prompt(prompt: str, arm: str | None = None) -> str:
    arm = ARM if arm is None else arm
    clause = ARM_CLAUSE[arm]
    if clause is None:
        return prompt
    if PROMPT_CLAUSE not in prompt:
        raise RuntimeError(
            f"{arm}: upstream system prompt no longer contains the clause "
            f"this arm anchors on ({PROMPT_CLAUSE!r}). Re-register the arm "
            f"rather than running a treatment that is never delivered.")
    return prompt.replace(PROMPT_CLAUSE, PROMPT_CLAUSE + "\n" + clause)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


_BIN_SHA: str | None = None


def _binary_sha() -> str | None:
    """sha256 of the gorp binary, computed once per process."""
    global _BIN_SHA
    if _BIN_SHA is None and GORP_BIN.exists():
        h = hashlib.sha256()
        with GORP_BIN.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        _BIN_SHA = h.hexdigest()
    return _BIN_SHA


def tools_lines_track_descv9() -> dict:
    """Assert the gorp line still carries desc-v9's own mechanism sentences.

    Run by `preflight_lab.py`. The two clauses below are the parts §19
    measured; the worked example is deliberately NOT shared (it is
    legal-domain here, code-domain there). If a future desc-vN edits them,
    this fires and the arm is re-registered deliberately rather than
    drifting into a different treatment silently.
    """
    sys.path.insert(0, str(LOCBENCH))
    import run as locbench  # noqa: E402

    v9 = locbench.TOOL_LINES["desc-v9"]
    shared = [
        "Give it anything — an identifier, a phrase, or a question",
        "Ranked, not exhaustive — if the answer isn't there, rephrase.",
    ]
    missing = [s for s in shared if s not in v9 or s not in GORP_DESC]
    return {"ok": not missing, "missing": missing,
            "descv9_sha256": _sha(v9), "gorp_desc_sha256": _sha(GORP_DESC)}


# --------------------------------------------------------------------------
# Upstream-dependent half
# --------------------------------------------------------------------------
try:
    from harness.tools import ToolExecutor, get_all_tool_definitions
    _UPSTREAM = True
except ImportError:  # imported from patches/ by tests — pure half only
    _UPSTREAM = False


if _UPSTREAM:

    class ArmToolExecutor(ToolExecutor):
        """Upstream's executor plus exactly one structured search tool.

        The search runs ON THE HOST through `common/shim.py` — the same
        pass-through/logging/blind-flags contract every gorp-bench search
        rides on — with cwd at the documents dir, so logged argv and result
        paths are docroot-relative and re-rooting a hit is a string prefix.
        The env contract (LOCBENCH_REAL_*, LOCBENCH_SHIM_LOG, GORP_*) is the
        driver's job: this process inherits it.
        """

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.arm = ARM
            self.arm_tool = ARM_TOOL[ARM]
            self.lab_search_count = 0
            self.lab_search_empty = 0
            self.warm = self._warm_cache()

        # ------------------------------------------------------------ warm
        def _warm_cache(self) -> dict:
            """Pay the corpus embedding/index cost at cell start, not inside
            the agent's first turn.

            A throwaway ranked search against the real binary (not the shim:
            a harness-issued search must not appear in the per-session shim
            log the analysis reads). rg needs no warm. Never repo-local
            `gorp index`: that writes `.gorp/` INSIDE the docroot, where it
            would be mounted into the sandbox and counted as documents.
            """
            if self.arm_tool != "gorp":
                return {"warmed": False, "reason": "arm has no gorp"}
            t0 = time.time()
            p = subprocess.run(
                [str(GORP_BIN), "--json", "-k", "1", "warm cache", "."],
                cwd=str(self.documents_dir), capture_output=True, timeout=3600)
            return {"warmed": p.returncode == 0, "warm_s": round(time.time() - t0, 2),
                    "returncode": p.returncode,
                    "stderr": p.stderr.decode(errors="ignore")[-400:]
                    if p.returncode else ""}

        # --------------------------------------------------------- dispatch
        def execute(self, tool_name: str, arguments: str | dict) -> str:
            if tool_name == self.arm_tool:
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except json.JSONDecodeError:
                        return f"Error: invalid JSON arguments: {arguments}"
                try:
                    return self._search(arguments)
                except FileNotFoundError as e:  # upstream grep's error shape
                    return f"Error: {e}"
                except Exception as e:  # upstream contract: strings, never raises
                    return f"Error: {type(e).__name__}: {e}"
            return super().execute(tool_name, arguments)

        # ----------------------------------------------------------- search
        def _scope(self, path_str: str | None) -> str | None:
            """Docroot-relative scope argv, or None ⇒ whole corpus.

            Accepts what an agent will actually type: a relative path, or an
            absolute /workspace/documents/... one. Anything outside the
            documents mount is refused in upstream grep's own error shape —
            search is a documents-only capability by construction (the
            rubric in task.json sits above the docroot and is unreachable).
            """
            if not path_str:
                return None
            rel = path_str
            if path_str.startswith("/"):
                prefix = "/workspace/documents"
                if path_str != prefix and not path_str.startswith(prefix + "/"):
                    raise FileNotFoundError(
                        f"path does not exist: {path_str} "
                        f"(search is scoped to /workspace/documents)")
                rel = path_str[len(prefix):].lstrip("/")
            if not rel:
                return None
            host = (self.documents_dir / rel)
            if not host.exists():
                raise FileNotFoundError(f"path does not exist: {path_str}")
            return rel

        def _search(self, arguments: dict) -> str:
            self.lab_search_count += 1
            if self.arm_tool == "gorp":
                query = arguments.get("query", "")
                if not query:
                    return "Error: query is required"
                k = arguments.get("k") or GORP_K_DEFAULT
                k = max(1, min(int(k), GORP_K_MAX))
                scope = self._scope(arguments.get("path")) or "."
                argv = ["--json", "-k", str(k), query, scope]
            else:
                pattern = arguments.get("pattern", "")
                if not pattern:
                    return "Error: pattern is required"
                # Explicit "." scope always: with a piped stdin and no path,
                # ripgrep searches STDIN — every query "matched nothing".
                scope = self._scope(arguments.get("path")) or "."
                argv = ["-n", "--no-heading", "--with-filename"]
                if arguments.get("ignore_case"):
                    argv.append("-i")
                argv += [pattern, scope]

            p = subprocess.run(
                [sys.executable, str(SHIM), self.arm_tool, *argv],
                cwd=str(self.documents_dir), capture_output=True,
                stdin=subprocess.DEVNULL, timeout=600)
            out = p.stdout.decode(errors="replace")

            # Exit 1 with empty stdout is "no hits" for both tools; anything
            # else nonzero is a real error the agent should see the tail of.
            if p.returncode not in (0, 1):
                tail = p.stderr.decode(errors="replace").strip()[-400:]
                return f"Error: {self.arm_tool} failed: {tail or 'no stderr'}"
            hits = self._format_hits(out)
            if not hits:
                self.lab_search_empty += 1
                if self.arm_tool == "gorp":
                    return f"No results for '{query}' — rephrase."
                return f"No matches for '{pattern}'"
            return hits

        def _format_hits(self, out: str) -> str:
            """Re-root docroot-relative hits onto /workspace/documents."""
            lines = []
            for raw in out.splitlines():
                if not raw.strip():
                    continue
                if self.arm_tool == "gorp":
                    try:
                        h = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    rel = h.get("path", "")
                    if rel.startswith("./"):  # the explicit "." scope's prefix
                        rel = rel[2:]
                    lines.append(f"{agent_path(rel)}:{h.get('line')}: "
                                 f"{(h.get('text') or '').rstrip()}")
                else:
                    rel, sep, rest = raw.partition(":")
                    if not sep:
                        continue
                    if rel.startswith("./"):
                        rel = rel[2:]
                    lines.append(f"{agent_path(rel)}:{rest}")
            if self.arm_tool == "rg" and len(lines) > RG_LINE_CAP:
                lines = lines[:RG_LINE_CAP]
                lines.append(f"... ({RG_LINE_CAP}-line cap; narrow the pattern)")
            return "\n".join(lines)

        # ---------------------------------------------------------- metrics
        def get_metrics(self) -> dict:
            return {
                **super().get_metrics(),
                "lab_tool": self.arm_tool,
                "lab_searches": self.lab_search_count,
                "lab_searches_empty": self.lab_search_empty,
            }

    def build(sandbox, shell_timeout: int):
        """What the patched `harness/run.py` calls in place of its own
        constructor pair. lab-base returns upstream's classes verbatim, so
        the anchor arm is upstream by construction, not by re-implementation.
        """
        tools = get_all_tool_definitions()
        extra = ARMS[ARM]
        if extra is None:
            return (ToolExecutor(sandbox=sandbox, shell_timeout=shell_timeout),
                    tools)
        return (ArmToolExecutor(sandbox=sandbox, shell_timeout=shell_timeout),
                tools + [extra])

    def write_telemetry(results_dir, tool_executor) -> None:
        """Provenance for the cell, next to upstream's metrics.json. The
        driver cross-checks `arm` against the env it set."""
        d = DESCS[DESC_VERSION]
        tel = {
            "arm": ARM,
            "corpus": CORPUS,
            "desc_version": DESC_VERSION,
            "gorp_desc_sha256": _sha(d["gorp"]),
            "rg_desc_sha256": _sha(d["rg"]),
            "gorp_sha256": _binary_sha(),
            "warm": getattr(tool_executor, "warm", None),
            "lab_searches": getattr(tool_executor, "lab_search_count", 0),
            "lab_searches_empty": getattr(tool_executor, "lab_search_empty", 0),
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        (Path(results_dir) / "telemetry.json").write_text(
            json.dumps(tel, indent=1) + "\n")
