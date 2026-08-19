"""The §36 arm registration: cc-gorp, and the six registries that must agree.

Two failure modes have real history behind them, and both are silent:

  * an arm registered in some registries but not all. Adding `sub-sgb` cost
    three consecutive fix commits (42c192a, d2e2b90, 27f3c08), each finding the
    next copy the last had missed, and the symptom is a campaign that dies at
    analysis — after the money is spent.
  * a description that moved more than one variable. §19.9 is the case:
    desc-v9 changed the tool's name AND a clause, so neither could be
    attributed. v12 must be v11 plus the rename and nothing else, which is
    asserted here rather than proofread.
"""

import importlib.util
import re
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1]
SWEX = BENCH / "harness" / "swexplore"
sys.path.insert(0, str(BENCH / "harness"))
# sg_arms runs from the vendored checkout and resolves paths relative to it, so
# the deployed copy is the one that can be imported.
UPSTREAM = BENCH / "data" / "swexplore" / "upstream"

import pytest  # noqa: E402

ARMS_FILE = UPSTREAM / "explorers" / "sg_arms.py"
needs_vendor = pytest.mark.skipif(
    not ARMS_FILE.exists(), reason="vendored upstream not fetched (fetch.sh)")


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def sg_arms():
    sys.path.insert(0, str(UPSTREAM))
    import explorers.sg_arms as m
    return m


# ---------------------------------------------------------------- the rename
@needs_vendor
def test_v12_is_v11_with_only_the_tool_renamed():
    m = sg_arms()
    assert m.SG_LINE_V12 == re.sub(r"\bsg\b", "gorp", m.SG_LINE_V11)
    # and the rename actually happened
    assert not re.search(r"\bsg\b", m.SG_LINE_V12)
    assert m.SG_LINE_V12.count("gorp") == m.SG_LINE_V11.count("sg")


@needs_vendor
def test_v12_keeps_the_clauses_v11_was_measured_on():
    """The three spans §19/§30.4 attribute effects to. If a future edit moves
    one, the arm is re-registered deliberately instead of drifting."""
    m = sg_arms()
    for clause in (
        "Give it anything — an identifier, a phrase, or a question",
        "Start wide: add a path argument only to narrow further",
        "Default to ranked search; use -e only to verify or count",
        "Ranked, not exhaustive — if the answer isn't there, rephrase.",
    ):
        assert clause in m.SG_LINE_V12, clause


@needs_vendor
def test_v12_is_the_description_gorp_actually_ships():
    """The join that makes the shipped wording a measured one.

    gorp's README is the substitutive framing ("The only code search tool
    available"); swexplore's arms are additive, so the shared span is the
    mechanism sentence through the caveat. Those must match exactly, or the
    product ships a description no campaign has measured.
    """
    readme = (BENCH.parent / "gorp" / "README.md")
    if not readme.exists():
        pytest.skip("no sibling gorp checkout")
    md = re.sub(r"\s+", " ", readme.read_text())
    m = sg_arms()
    for clause in ('`gorp "query"` searches the whole repository',
                   "Start wide: add a path argument only to narrow further",
                   "Ranked, not exhaustive"):
        assert clause in md, f"gorp's README no longer says: {clause}"
        assert clause in re.sub(r"\s+", " ", m.SG_LINE_V12)


# ------------------------------------------------------------- the registries
@needs_vendor
def test_cc_gorp_is_in_every_registry():
    m = sg_arms()
    triage = _load(SWEX / "triage_swex.py", "_t_triage_swex")
    analyze = _load(SWEX / "analyze.py", "_t_analyze")
    viewer = _load(SWEX / "viewer.py", "_t_viewer")

    assert "cc-gorp" in m.ARMS
    assert m.ARM_TOOL["cc-gorp"] == "gorp"
    for name, reg in (("triage_swex", triage.ALL_ARM_TOOL),
                      ("analyze", analyze.ALL_ARM_TOOL),
                      ("viewer", viewer.ALL_ARM_TOOL)):
        assert reg.get("cc-gorp") == "gorp", f"{name} does not know cc-gorp"

    patch = (SWEX / "patches" / "0001-eval_runner-arms-cost-rolling.patch").read_text()
    assert '"cc-gorp"' in patch.split("SG_ARMS = {")[1].split("}")[0]
    assert '"cc-gorp": lambda rec: _sg_arm_method(rec, "cc-gorp")' in patch


@needs_vendor
def test_every_arm_is_in_every_registry():
    """Not just cc-gorp: the invariant is that the registries agree, so the
    next arm cannot repeat sub-sgb's three-commit discovery process."""
    m = sg_arms()
    triage = _load(SWEX / "triage_swex.py", "_t2_triage_swex")
    analyze = _load(SWEX / "analyze.py", "_t2_analyze")
    viewer = _load(SWEX / "viewer.py", "_t2_viewer")
    arms = set(m.ARMS)
    for name, reg in (("triage_swex", triage.ALL_ARM_TOOL),
                      ("analyze", analyze.ALL_ARM_TOOL),
                      ("viewer", viewer.ALL_ARM_TOOL)):
        assert set(reg) == arms, f"{name} registry differs: {set(reg) ^ arms}"


@needs_vendor
def test_every_named_tool_has_a_shim():
    """A tool named in an arm but missing from SEARCH_TOOLS gets no shim, so it
    reaches the real binary on PATH and escapes the harness entirely — no log,
    no block, no injected flags."""
    m = sg_arms()
    for arm, tool in m.ARM_TOOL.items():
        assert tool in m.SEARCH_TOOLS, f"{arm} types {tool!r}, which has no shim"


@needs_vendor
def test_cc_gorp_is_additive_and_names_its_tool_everywhere():
    m = sg_arms()
    tools, allowed, sysline = m.ARMS["cc-gorp"]
    assert "Grep" in tools and "Bash" in tools      # additive: Grep stays
    assert allowed == ["Bash(gorp *)"]
    assert "`gorp`" in sysline
    assert "`gorp`" in m.ARM_CLAUSE["cc-gorp"]
    assert "Grep" in m.ARM_CLAUSE["cc-gorp"]


@needs_vendor
def test_cc_remains_the_untouched_control():
    """cc is the calibration anchor against the published row. If it ever gains
    a clause or an allowlist it stops being comparable to the paper."""
    m = sg_arms()
    tools, allowed, sysline = m.ARMS["cc"]
    assert allowed == [] and sysline == ""
    assert m.ARM_CLAUSE["cc"] is None
    assert m.ARM_TOOL.get("cc") is None


# ------------------------------------------------------------------ the split
@needs_vendor
def test_cross_repo_paths_resolve():
    """The §36 split check. BENCH_ROOT was one level too high after the repo
    split, which made SHIM a path that does not exist — every shim would have
    exec'd a missing file and every search in every arm would have failed,
    while still producing rows that read as a clean null."""
    m = sg_arms()
    assert m.BENCH_ROOT == BENCH
    assert m.SHIM.exists()
    assert m.LOCBENCH.is_dir()


# ----------------------------------------------------------- §37: cc-gorp-route
@needs_vendor
def test_v13_lineage_is_preserved():
    """v13 (superseded before any cell ran) stays defined and derived: the
    lineage v12 -> +route clause -> v14 rewrite is part of the record."""
    m = sg_arms()
    assert m.SG_LINE_V13 == m.SG_LINE_V12 + m.ROUTE_CLAUSE_V13


@needs_vendor
def test_v14_routes_names_and_calibrates_verification():
    """The three treatment ideas v14 carries; a rewrite that drops one is a
    different arm. Checked as spans, §19.9-style."""
    m = sg_arms()
    for clause in (
        "ranked semantic search",                       # not "code search"
        "use grep, not gorp",                           # routing
        "pasted as-is",                                 # query steering
        "widen with -k 20 before rephrasing",           # escalate, then rephrase
        "candidates, not conclusions",                  # trust calibration
        "Verify what you keep, not every search.",      # the anti-redundancy brake
        "list candidate spellings in one query",        # kept from v11: best shape
    ):
        assert clause in m.SG_LINE_V14, clause


@needs_vendor
def test_v14_does_not_advertise_what_is_dead_or_demoted():
    """-e is grep's job in this arm, and the §31 pipe syntax failed its
    registered gate (§31.2: merged ranking covered 68.9% of the sequential
    union against a >=95% bar) — neither may appear in the description."""
    m = sg_arms()
    assert "-e" not in m.SG_LINE_V14
    assert "|" not in m.SG_LINE_V14
    assert not re.search(r"\bsg\b", m.SG_LINE_V14)


@needs_vendor
def test_cc_gorp_route_is_registered_everywhere():
    m = sg_arms()
    triage = _load(SWEX / "triage_swex.py", "_t3_triage_swex")
    analyze = _load(SWEX / "analyze.py", "_t3_analyze")
    viewer = _load(SWEX / "viewer.py", "_t3_viewer")
    assert m.ARM_TOOL["cc-gorp-route"] == "gorp"
    for name, reg in (("triage_swex", triage.ALL_ARM_TOOL),
                      ("analyze", analyze.ALL_ARM_TOOL),
                      ("viewer", viewer.ALL_ARM_TOOL)):
        assert reg.get("cc-gorp-route") == "gorp", f"{name} missing cc-gorp-route"
    patch = (SWEX / "patches" / "0001-eval_runner-arms-cost-rolling.patch").read_text()
    assert '"cc-gorp-route"' in patch.split("SG_ARMS = {")[1].split("}")[0]
    assert '"cc-gorp-route": lambda rec: _sg_arm_method(rec, "cc-gorp-route")' in patch


@needs_vendor
def test_cc_gorp_route_is_additive_and_grep_open():
    """The arm's registration IS the grep-open state: not an env switch."""
    m = sg_arms()
    tools, allowed, sysline = m.ARMS["cc-gorp-route"]
    assert "Grep" in tools and "Bash" in tools
    assert allowed == ["Bash(gorp *)", "Bash(grep *)"]
    assert sysline == m.SG_LINE_V14
    assert "cc-gorp-route" in m.GREP_OPEN_ARMS
    assert "`grep`" in m.ARM_CLAUSE["cc-gorp-route"]


@needs_vendor
def test_frozen_arms_are_not_grep_open():
    """GREP_OPEN_ARMS must never grow a frozen arm: cc-gorp's s36 cells were
    measured with shell grep blocked, and an arm's environment is part of its
    recorded identity."""
    m = sg_arms()
    assert m.GREP_OPEN_ARMS == frozenset({"cc-gorp-route", "cc-bash",
                                          "cc-gorp-route2"})


# ------------------------------------------------------------ §37.2: cc-bash
@needs_vendor
def test_cc_bash_is_the_shell_and_nothing_else():
    """The Bash-toll control decomposes route − cc. Its whole point is purity:
    a shell and open grep, with no engine and no description. Any sysline or
    gorp binding here and the decomposition is void."""
    m = sg_arms()
    tools, allowed, sysline = m.ARMS["cc-bash"]
    assert "Bash" in tools and "Grep" in tools
    assert allowed == ["Bash(grep *)"]
    assert sysline == ""                       # no description at all
    assert m.ARM_TOOL.get("cc-bash") is None   # no engine
    assert "cc-bash" in m.GREP_OPEN_ARMS
    assert "`grep`" in m.ARM_CLAUSE["cc-bash"]
    assert "gorp" not in m.ARM_CLAUSE["cc-bash"]


# ---------------------------------------------------------- §37.3: v15
@needs_vendor
def test_v15_is_v14_plus_the_when_it_shines_clause():
    """One clause moved, mechanically: v15 differs from v14 only inside the
    when-to-use bullet, and names the boundary the s37 trajectories measured
    (gorp pays off where lexical search has no anchor)."""
    m = sg_arms()
    assert m.SG_LINE_V15 == m.SG_LINE_V14.replace(m._V15_OLD, m._V15_NEW)
    for span in ("lexical search has no anchor", "unfamiliar naming",
                 "cross-language", "prose"):
        assert span in m.SG_LINE_V15, span
    assert span not in m.SG_LINE_V14
    tools, allowed, sysline = m.ARMS["cc-gorp-route2"]
    assert (tools, allowed) == (m.ARMS["cc-gorp-route"][0],
                                m.ARMS["cc-gorp-route"][1])
    assert sysline == m.SG_LINE_V15
