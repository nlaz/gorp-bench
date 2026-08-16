"""Guards §16.6/§19 for the LAB arms: description text is a treatment, and
drift is an unmeasured arm.

Three past failures shape these tests. §19 measured description quality
moving agent behaviour, so the rg and gorp tools must make the same
capability claims with the same worked example or the PRIMARY contrast
becomes a description contrast. §16.10's footer incident showed a single
uncontrolled sentence moving ranked share from 7% to 98%, so the shared
desc-v9 clauses are asserted against locbench's live copy, not a paraphrase.
And swexplore's first smoke showed an availability-only treatment is never
delivered, so the system-prompt amendment is tested to differ between arms
in exactly the tool name and to die loudly when its anchor clause vanishes.

`lab_arms` is imported from `harness/labbench/patches/` — the checked-in
copy — so CI needs no vendored checkout. The pure half of the module must
stay importable without upstream, and that property is itself asserted here.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]
                       / "harness" / "labbench" / "patches"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]
                       / "harness" / "locbench"))

import pytest  # noqa: E402

import lab_arms  # noqa: E402


# ---------------------------------------------------------------- imports

def test_the_pure_half_imports_without_the_vendored_upstream():
    # The module was imported from patches/, where harness.tools does not
    # exist. If this attribute is True the guard broke and CI is silently
    # depending on a vendored tree it does not have.
    assert lab_arms._UPSTREAM is False


# ------------------------------------------------------- shared clauses

def test_gorp_desc_carries_descv9_mechanism_and_caveat_verbatim():
    import run as locbench

    v9 = locbench.TOOL_LINES["desc-v9"]
    for clause in (
        "Give it anything — an identifier, a phrase, or a question",
        "Ranked, not exhaustive — if the answer isn't there, rephrase.",
    ):
        assert clause in v9, f"desc-v9 lost its own clause: {clause!r}"
        assert clause in lab_arms.GORP_DESC, f"GORP_DESC lost: {clause!r}"


def test_tools_lines_track_descv9_agrees_with_the_direct_assertion():
    assert lab_arms.tools_lines_track_descv9()["ok"]


# ------------------------------------------- rg/gorp description parity

def test_both_descriptions_share_the_worked_example_result_line():
    assert lab_arms._EXAMPLE_HIT in lab_arms.GORP_DESC
    assert lab_arms._EXAMPLE_HIT in lab_arms.RG_DESC


def test_the_example_hit_cites_a_converted_markdown_document():
    # The corpus is md-v1: every path an agent will ever see ends in .md and
    # keeps the original extension visible before it. An example citing a
    # bare .docx would teach agents to search for files that do not exist.
    assert "/workspace/documents/" in lab_arms._EXAMPLE_HIT
    cited = lab_arms._EXAMPLE_HIT.split(":", 1)[0]
    assert cited.endswith(".docx.md")


def test_descriptions_are_registered_versions_not_loose_strings():
    d = lab_arms.DESCS["v1"]
    assert d["gorp"] == lab_arms.GORP_DESC
    assert d["rg"] == lab_arms.RG_DESC
    assert lab_arms.ARMS["lab-gorp"]["description"] == lab_arms.GORP_DESC
    assert lab_arms.ARMS["lab-rg"]["description"] == lab_arms.RG_DESC


# ------------------------------------------------------ prompt amendment

def test_treatment_clauses_differ_only_in_the_tool_name():
    rg = lab_arms.ARM_CLAUSE["lab-rg"]
    gorp = lab_arms.ARM_CLAUSE["lab-gorp"]
    assert rg.replace("`rg`", "`gorp`") == gorp
    assert lab_arms.ARM_CLAUSE["lab-base"] is None


def test_lab_base_prompt_is_untouched_byte_for_byte():
    prompt = "header\n" + lab_arms.PROMPT_CLAUSE + "\nfooter"
    assert lab_arms.amend_system_prompt(prompt, "lab-base") == prompt


def test_treatment_amendment_appends_one_line_after_the_anchor():
    prompt = "header\n" + lab_arms.PROMPT_CLAUSE + "\nfooter"
    out = lab_arms.amend_system_prompt(prompt, "lab-gorp")
    expected = ("header\n" + lab_arms.PROMPT_CLAUSE + "\n"
                + lab_arms.ARM_CLAUSE["lab-gorp"] + "\nfooter")
    assert out == expected


def test_a_missing_anchor_clause_raises_instead_of_running_untreated():
    with pytest.raises(RuntimeError, match="never delivered"):
        lab_arms.amend_system_prompt("a rewritten upstream prompt", "lab-gorp")


# ---------------------------------------------------------------- arms

def test_arm_registry_is_additive_one_tool_per_treatment():
    assert lab_arms.ARMS["lab-base"] is None
    assert lab_arms.ARM_TOOL == {"lab-rg": "rg", "lab-gorp": "gorp",
                                 "lab-cc-rg": "rg", "lab-cc-gorp": "gorp"}
    assert lab_arms.ARMS["lab-rg"]["name"] == "rg"
    assert lab_arms.ARMS["lab-gorp"]["name"] == "gorp"


def test_the_cc_family_differs_from_api_in_the_loop_only():
    # The family axis is who drives the agent; every prompt-visible surface
    # must be IDENTICAL between lab-X and lab-cc-X, or a family contrast
    # smuggles in a second variable.
    for base, cc in (("lab-base", "lab-cc-base"), ("lab-rg", "lab-cc-rg"),
                     ("lab-gorp", "lab-cc-gorp")):
        assert lab_arms.ARMS[base] is lab_arms.ARMS[cc]
        assert lab_arms.ARM_CLAUSE[base] == lab_arms.ARM_CLAUSE[cc]
        assert lab_arms.ARM_TOOL.get(base) == lab_arms.ARM_TOOL.get(cc)
