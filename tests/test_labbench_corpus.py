"""Guards the md-v1 naming contract — the one rule that makes a search hit
a readable file.

There is no path-translation layer in labbench: the corpus file IS the
document the agent reads, and every consumer (build_corpus.py writing it,
ArmToolExecutor re-rooting hits onto /workspace/documents, the descriptions'
worked example, preflight's spot checks) assumes the same rule: original
relpath + ".md", appended always. A one-off suffix rule broken in any of
them reports hits on files that do not exist, which an agent experiences as
a tool that lies. These tests pin the rule and the frame geometry that
depends on it.

`lab_arms` and `lab_frame` are imported from the checked-in tree — no
vendored checkout, no upstream venv — so CI runs them cold.
"""
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BENCH / "harness" / "labbench" / "patches"))
sys.path.insert(0, str(BENCH / "harness" / "labbench"))
sys.path.insert(0, str(BENCH / "harness"))

import lab_arms  # noqa: E402
import lab_frame  # noqa: E402


# ------------------------------------------------------------ naming rule

def test_md_name_appends_the_suffix_to_every_extension():
    assert lab_arms.md_name("psa.docx") == "psa.docx.md"
    assert lab_arms.md_name("schedule.xlsx") == "schedule.xlsx.md"
    assert lab_arms.md_name("mail/thread-01.eml") == "mail/thread-01.eml.md"


def test_md_name_double_suffixes_txt_and_md_originals():
    # `memo.txt` -> `memo.txt.md`, `notes.md` -> `notes.md.md`: appending
    # unconditionally is what keeps "strip one .md" unambiguous and keeps a
    # genuine upstream .md document distinguishable from its conversion.
    assert lab_arms.md_name("memo.txt") == "memo.txt.md"
    assert lab_arms.md_name("notes.md") == "notes.md.md"


def test_agent_path_roots_a_relative_hit_at_the_documents_mount():
    assert (lab_arms.agent_path("sub/psa.docx.md")
            == "/workspace/documents/sub/psa.docx.md")


def test_the_descriptions_worked_example_obeys_the_naming_rule():
    cited = lab_arms._EXAMPLE_HIT.split(":", 1)[0]
    rel = cited[len("/workspace/documents/"):]
    original = rel[:-len(".md")]
    assert lab_arms.md_name(original) == rel


# --------------------------------------------------------- frame geometry

def _fake_side():
    """A miniature population exercising every filter branch."""
    rows = [
        # eligible: retrieval work_type, big corpus
        ("corporate-ma/analyze-big-deal", "analyze", 12, None),
        # eligible: no work_type, retrieval verb, big corpus
        ("contracts/extract-key-terms", None, 30, None),
        # excluded: draft-shaped
        ("contracts/draft-msa", "draft", 40, None),
        # excluded: retrieval-shaped but corpus below the floor
        ("real-estate/review-lease", "review", 3, None),
        # eligible: shared corpus, no work_type, numbered slug — the
        # firm-knowledge shape; in regardless of anything else
        ("firm-knowledge/tasks/001", None, 9000, "../../dms"),
        ("firm-knowledge/tasks/002", None, 9000, "../../dms"),
    ]
    return {
        tid: {"task": tid, "area": lab_frame.area_of(tid), "work_type": wt,
              "slug_verb": lab_frame.verb_of(tid), "n_docs": n,
              "shared_corpus": shared, "n_criteria": 5, "difficulty": None,
              "usable": True}
        for tid, wt, n, shared in rows
    }


def test_draft_tasks_and_thin_corpora_are_excluded_shared_corpora_are_in():
    frame, order, _ = lab_frame.build(_fake_side())
    assert "contracts/draft-msa" not in order
    assert "real-estate/review-lease" not in order
    assert "corporate-ma/analyze-big-deal" in order
    assert any(t.startswith("firm-knowledge/") for t in order)


def test_the_shared_corpus_group_is_capped_not_proportional():
    side = _fake_side()
    # 30 shared tasks against 8 ordinary ones: uncapped they would be ~79%
    # of the order; the cap holds them to SHARED_MAX_SHARE.
    for i in range(6):
        side[f"contracts/extract-terms-{i}"] = {
            "task": f"contracts/extract-terms-{i}", "area": "contracts",
            "work_type": None, "slug_verb": "extract", "n_docs": 20,
            "shared_corpus": None, "n_criteria": 5, "difficulty": None,
            "usable": True}
    for i in range(3, 31):
        side[f"firm-knowledge/tasks/{i:03d}"] = {
            "task": f"firm-knowledge/tasks/{i:03d}", "area": "firm-knowledge",
            "work_type": None, "slug_verb": f"{i:03d}", "n_docs": 9000,
            "shared_corpus": "../../dms", "n_criteria": 5, "difficulty": None,
            "usable": True}
    frame, order, _ = lab_frame.build(side)
    fk = sum(1 for t in order if t.startswith("firm-knowledge/"))
    assert fk / len(order) <= lab_frame.SHARED_MAX_SHARE + 0.05


def test_the_order_is_deterministic_from_the_seed():
    a = lab_frame.build(_fake_side())[1]
    b = lab_frame.build(_fake_side())[1]
    assert a == b


def test_rungs_are_prefixes_of_one_another():
    frame, order, _ = lab_frame.build(_fake_side())
    for a, b in zip(lab_frame.RUNGS, lab_frame.RUNGS[1:]):
        assert frame[:a] == frame[:b][:a]


def test_scenario_leaves_take_their_parents_slug_verb():
    assert lab_frame.verb_of("funds/compare-lpa-terms/scenario-02") == "compare"
    assert lab_frame.slug_of("funds/compare-lpa-terms/scenario-02") == "compare-lpa-terms"


def test_a_task_json_missing_criteria_fields_is_unusable():
    assert lab_frame.task_json_usable(
        {"title": "t", "criteria": [{"id": "C1", "title": "x",
                                     "match_criteria": "PASS if"}]})
    assert not lab_frame.task_json_usable({"title": "t", "criteria": []})
    assert not lab_frame.task_json_usable(
        {"title": "t", "criteria": [{"id": "C1", "title": "x"}]})
