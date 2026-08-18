"""provaudit: the mixture gate, driven by fabricated cells.

The gate's whole value is that it FAILS on data that looks fine. Every test
here builds a run directory by hand and asserts the gate's verdict, because
the real thing it guards against — a rung that half-ran on a rebuilt binary,
or picked up a new CLI overnight — is by construction something you cannot
produce on purpose in a real campaign.

The motivating case is real: s27..s33 all recorded `"model": "sonnet"` and all
in fact ran claude-sonnet-5, which RESEARCH.md §32.3 then compared against the
paper's Sonnet-4.5 row. The data was on disk; no gate read it.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

BENCH = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BENCH / "harness"))


def load(tmp_path):
    """Load provaudit with DATA pointed at a scratch tree."""
    spec = importlib.util.spec_from_file_location(
        "_t_provaudit", BENCH / "harness" / "swexplore" / "provaudit.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["_t_provaudit"] = m
    spec.loader.exec_module(m)
    m.DATA = tmp_path
    return m


GOOD = {
    "model_requested": "claude-sonnet-4-5-20250929",
    "model_resolved": "claude-sonnet-4-5-20250929",
    "claude_version": "2.1.234 (Claude Code)",
    "gorp_sha256": "abc123",
    "gorp_version": {"commit": "db4ef4a", "compat_key": "v2-d256-dac6",
                     "git_dirty": False},
    "desc_version": "v12",
    "sg_search_flags": "",
    "sg_index_flags": "",
    "unblock_grep": False,
    "tools": "Read,Glob,Grep,Bash",
    "index": {"index_flags": ""},
}

RUN_PROV = {
    "run_id": "r1",
    "gorp_version": {"commit": "db4ef4a", "git_dirty": False},
    "upstream": {"sha": "3c12dc5a5519",
                 "delta": ["M eval_runner.py", "?? _sg_repos.py",
                           "?? explorers/sg_arms.py", "?? explorers/sg_static.py"]},
}


def build(tmp_path, cells, run_prov=RUN_PROV, run_id="r1"):
    """cells: {arm: [meta-dict, ...]} -> runs/<run>/<instance>/<arm>/meta.json"""
    root = tmp_path / "runs" / run_id
    for arm, metas in cells.items():
        for i, meta in enumerate(metas):
            d = root / f"inst{i}" / arm
            d.mkdir(parents=True, exist_ok=True)
            (d / "meta.json").write_text(json.dumps({**meta, "arm": arm}))
    if run_prov is not None:
        root.mkdir(parents=True, exist_ok=True)
        (root / "provenance.json").write_text(json.dumps(run_prov))
    return root


def test_a_clean_run_passes(tmp_path):
    m = load(tmp_path)
    build(tmp_path, {"cc": [GOOD] * 3, "cc-gorp": [GOOD] * 3})
    failures, summary = m.audit("r1", {"cc", "cc-gorp"})
    assert failures == []
    assert summary["cc"]["n_cells"] == 3
    assert summary["cc-gorp"]["model_resolved"] == "claude-sonnet-4-5-20250929"


def test_a_model_that_changed_mid_run_fails(tmp_path):
    """The motivating case, inverted: two resolved models inside one arm."""
    m = load(tmp_path)
    drifted = {**GOOD, "model_resolved": "claude-sonnet-5"}
    build(tmp_path, {"cc": [GOOD, GOOD, drifted]})
    failures, _ = m.audit("r1", {"cc"})
    assert any("model_resolved is not constant" in f for f in failures)


def test_a_rebuilt_binary_mid_run_fails(tmp_path):
    m = load(tmp_path)
    rebuilt = {**GOOD, "gorp_sha256": "def456",
               "gorp_version": {**GOOD["gorp_version"], "commit": "1e48592"}}
    build(tmp_path, {"cc-gorp": [GOOD, rebuilt]})
    failures, _ = m.audit("r1", {"cc-gorp"})
    assert any("gorp_sha256 is not constant" in f for f in failures)
    assert any("gorp_version.commit is not constant" in f for f in failures)


def test_a_description_that_changed_mid_run_fails(tmp_path):
    """An env var set in one shell and not the next leaves no other trace."""
    m = load(tmp_path)
    build(tmp_path, {"cc-gorp": [GOOD, {**GOOD, "desc_version": "v11"}]})
    failures, _ = m.audit("r1", {"cc-gorp"})
    assert any("desc_version is not constant" in f for f in failures)


def test_hidden_engine_flags_that_changed_mid_run_fail(tmp_path):
    """The agent never sees these, so nothing else could ever show them."""
    m = load(tmp_path)
    build(tmp_path, {"cc-gorp": [GOOD, {**GOOD, "sg_search_flags": "--bridge-expand 8"}]})
    failures, _ = m.audit("r1", {"cc-gorp"})
    assert any("sg_search_flags is not constant" in f for f in failures)


def test_a_cli_upgrade_mid_run_fails(tmp_path):
    m = load(tmp_path)
    build(tmp_path, {"cc": [GOOD, {**GOOD, "claude_version": "2.2.0 (Claude Code)"}]})
    failures, _ = m.audit("r1", {"cc"})
    assert any("claude_version is not constant" in f for f in failures)


def test_arms_on_different_toolchains_fail(tmp_path):
    """Each arm internally consistent, but the contrast is between toolchains
    rather than between arms — the subtlest form, and per-arm checks miss it."""
    m = load(tmp_path)
    other = {**GOOD, "model_resolved": "claude-sonnet-5"}
    build(tmp_path, {"cc": [GOOD] * 2, "cc-gorp": [other] * 2})
    failures, _ = m.audit("r1", {"cc", "cc-gorp"})
    assert any("arms disagree on model_resolved" in f for f in failures)


def test_an_unresolved_model_fails(tmp_path):
    """A cell that ran but recorded no resolution is the pre-§36 hole itself."""
    m = load(tmp_path)
    build(tmp_path, {"cc": [GOOD, {**GOOD, "model_resolved": None}]})
    failures, _ = m.audit("r1", {"cc"})
    assert any("recorded no resolved model" in f for f in failures)


def test_pre_36_cells_are_rejected(tmp_path):
    m = load(tmp_path)
    old = {k: v for k, v in GOOD.items() if k != "model_requested"}
    build(tmp_path, {"cc": [old]})
    failures, _ = m.audit("r1", {"cc"})
    assert any("predate the §36 provenance fields" in f for f in failures)


def test_a_dirty_binary_fails(tmp_path):
    """A binary built from a dirty tree corresponds to no commit, so the run
    cannot be reproduced even in principle."""
    m = load(tmp_path)
    prov = {**RUN_PROV, "gorp_version": {"commit": "c9d5424", "git_dirty": True}}
    build(tmp_path, {"cc": [GOOD]}, run_prov=prov)
    failures, _ = m.audit("r1", {"cc"})
    assert any("dirty tree" in f for f in failures)


def test_an_unregistered_vendor_delta_fails(tmp_path):
    m = load(tmp_path)
    prov = {**RUN_PROV, "upstream": {
        **RUN_PROV["upstream"],
        "delta": RUN_PROV["upstream"]["delta"] + ["M eval.py"]}}
    build(tmp_path, {"cc": [GOOD]}, run_prov=prov)
    failures, _ = m.audit("r1", {"cc"})
    assert any("unregistered delta" in f for f in failures)


def test_a_missing_run_provenance_fails(tmp_path):
    m = load(tmp_path)
    build(tmp_path, {"cc": [GOOD]}, run_prov=None)
    failures, _ = m.audit("r1", {"cc"})
    assert any("no provenance.json" in f for f in failures)


def test_a_registered_arm_with_no_cells_fails(tmp_path):
    """0/0 passing vacuously is the failure mode §18 exists to end."""
    m = load(tmp_path)
    build(tmp_path, {"cc": [GOOD]})
    failures, _ = m.audit("r1", {"cc", "cc-gorp"})
    assert any("cc-gorp: registered arm has no cells" in f for f in failures)


def test_unreadable_meta_is_a_failure_not_a_skip(tmp_path):
    m = load(tmp_path)
    root = build(tmp_path, {"cc": [GOOD]})
    (root / "inst9" / "cc").mkdir(parents=True)
    (root / "inst9" / "cc" / "meta.json").write_text("{not json")
    failures, _ = m.audit("r1", {"cc"})
    assert any("unreadable meta.json" in f for f in failures)
