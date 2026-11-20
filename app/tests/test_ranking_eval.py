"""The ranking evaluation (ranking_eval/): its answer keys, rankings, modes and grading, with a fake model."""

import json

import pytest

from app.core import config, examples, glossary
from ranking_eval import keys, quiz, report, structure
from ranking_eval.rankings import BY_NAME, CANDIDATES, Pair

CASES = keys.load()
LIBRARY_IDS = {e["id"] for e in examples.LIBRARY}


class FakeModel:
    """Answers every sentence with one fixed reading."""

    def __init__(self, answer):
        self.answer = answer

    def info(self):
        return {"name": "fake", "temperature": 0, "seed": 0}

    def complete(self, prompt):
        return json.loads(json.dumps(self.answer))


APPROVAL = FakeModel(
    {
        "rule_type": "approval_required",
        "action": "IssueRefund",
        "unsupported_reason": "",
        "slots": [{"slot": "approver_role", "span": "a manager", "value": "manager"}],
        "vague_terms": [{"span": "recently", "slot": "window"}],
        "qualifiers": [],
    }
)


def case(case_id):
    return next(c for c in CASES if c["id"] == case_id)


def test_every_key_is_well_formed():
    ids = [c["id"] for c in CASES]
    assert len(ids) == len(set(ids))
    assert {c["kind"] for c in CASES} == set(report.KINDS)
    for c in CASES:
        assert set(c["leave_out"]) <= LIBRARY_IDS and set(c["relevant"]) <= LIBRARY_IDS, c["id"]
        for vk in c["values"].values():
            for mode in keys.MODES:
                assert keys.source(vk, mode) in (keys.SENTENCE, keys.GLOSSARY, keys.SAVED, keys.ASK, keys.UNFILLED)
        assert c["expect"].get("refuse") or c["reference"], c["id"]


def test_about_a_third_is_hidden_in_every_kind():
    for kind in report.KINDS:
        of_kind = [c for c in CASES if c["kind"] == kind]
        assert abs(sum(c["hidden"] for c in of_kind) - len(of_kind) / 3) <= 1


def test_every_library_entry_has_a_profile():
    assert LIBRARY_IDS <= set(structure.PROFILES)


def test_isolated_mode_removes_the_case_policy_glossary_gaps_and_saved_answers(tmp_path):
    c = case("masked-manager-approval")
    real_answers, real_gaps = config.ANSWERS_PATH, list(glossary.GLOSSARY["gaps"])
    with quiz.environment(c, "isolated", tmp_path) as library:
        assert "form_manager_approval" not in {e["id"] for e in library}
        assert examples.LIBRARY is library
        assert glossary.GLOSSARY["gaps"] == []
        assert config.ANSWERS_PATH == tmp_path / "answers.sqlite"
    assert config.ANSWERS_PATH == real_answers and glossary.GLOSSARY["gaps"] == real_gaps
    assert "form_manager_approval" in {e["id"] for e in examples.LIBRARY}


def test_realistic_mode_keeps_everything(tmp_path):
    with quiz.environment(case("masked-manager-approval"), "realistic", tmp_path) as library:
        assert {e["id"] for e in library} == LIBRARY_IDS
        assert glossary.GLOSSARY["gaps"]


@pytest.mark.parametrize("name", [r.name for r in CANDIDATES] + ["oracle", "random", "none"])
def test_every_ranking_runs_both_searches(name, monkeypatch):
    ranking = BY_NAME[name]()
    if name == "embeddings":
        monkeypatch.setattr(ranking, "_embed", lambda texts: [[1.0, float(len(t) % 7)] for t in texts])
    ranking.set_case(case("para-signoff"), examples.LIBRARY, "isolated")
    with examples.use_ranking(Pair(BY_NAME["jaccard"]() if name == "behavioral" else ranking, ranking)):
        shown = examples.most_similar("Refunds need a manager's sign-off, given recently.")
        evidence = examples.stating("Refunds need a manager's sign-off, given recently.", "approval_required", "window")
    assert all(e["type"] != examples.BASELINE for e in shown)
    if name != "none":
        assert {e["value"] for e in evidence} <= {"1h"}


def test_oracle_suggests_nothing_when_the_key_says_no_suggestion_is_right():
    oracle = BY_NAME["oracle"]()
    oracle.set_case(case("near-staging-tests"), examples.LIBRARY, "isolated")
    with examples.use_ranking(Pair(oracle, oracle)):
        sentence = "A version may only go to staging if its tests passed recently."
        assert examples.stating(sentence, "deploy_needs_tests", "window") == []


def test_structure_filter_keeps_deploy_approval_out_of_refund_evidence():
    query = structure.type_profile("approval_required")
    assert structure.same_frame(query, structure.library_profile("R4"))
    assert not structure.same_frame(query, structure.library_profile("R5"))
    assert not structure.same_frame(query, structure.library_profile("R2"))


def test_a_masked_case_is_graded_on_all_five_checks(tmp_path):
    c = case("masked-manager-approval")
    pair = Pair(BY_NAME["jaccard"](), BY_NAME["jaccard"]())
    record = quiz.run_case(c, "isolated", pair, APPROVAL, tmp_path)
    g = quiz.grade(c, record)
    assert record["questions"][0]["field"] == "window"
    assert record["questions"][0]["evidence"] == ["R4"]
    assert g["suggestions"] == {"window": quiz.CORRECT}
    assert g["understood"] and g["right_source"] and g["asks_right"]
    assert g["works"], g["works_detail"]
    assert g["recall_evidence"] == 1.0


def test_with_no_evidence_a_suggestion_is_absent_and_none_is_right(tmp_path):
    c = case("masked-manager-approval-no-evidence")
    record = quiz.run_case(c, "isolated", Pair(BY_NAME["jaccard"](), BY_NAME["jaccard"]()), APPROVAL, tmp_path)
    assert quiz.grade(c, record)["suggestions"] == {"window": quiz.CORRECT_NONE}


def test_the_none_reference_never_suggests(tmp_path):
    c = case("masked-manager-approval")
    none = BY_NAME["none"]()
    g = quiz.grade(c, quiz.run_case(c, "isolated", Pair(none, none), APPROVAL, tmp_path))
    assert g["suggestions"] == {"window": quiz.ABSENT}


def test_a_wrong_window_is_a_wrong_allow_against_the_reference(tmp_path, monkeypatch):
    c = case("masked-manager-approval")
    monkeypatch.setitem(c["values"]["window"], "value", "24h")  # the manager answers 24h; the reference says 1h
    record = quiz.run_case(c, "isolated", Pair(BY_NAME["jaccard"](), BY_NAME["jaccard"]()), APPROVAL, tmp_path)
    g = quiz.grade(c, record)
    assert g["works"] is False and g["wrong_allows"] > 0 and g["wrong_blocks"] == 0


def test_report_writes_a_leaderboard(tmp_path):
    c = case("masked-manager-approval")
    with (tmp_path / "records.jsonl").open("w") as f:
        for name in ("jaccard", "structural"):
            pair = Pair(BY_NAME["jaccard"](), BY_NAME[name]())
            record = quiz.run_case(c, "isolated", pair, APPROVAL, tmp_path / name)
            record["grade"] = quiz.grade(c, record)
            f.write(json.dumps(record, default=str) + "\n")
    meta = {"stamp": "t", "model": APPROVAL.info(), "embedding_model": "x", "cases_file": "c", "modes": ["isolated"]}
    (tmp_path / "meta.json").write_text(json.dumps({**meta, "runs": [], "argv": [], "final": False}))
    report.write(tmp_path, CASES)
    text = (tmp_path / "leaderboard.md").read_text()
    assert "**jaccard+jaccard** (baseline)" in text and "jaccard+structural" in text
    assert "1/1 (100%)" in text


def test_an_approval_with_no_role_in_isolated_mode_fails_check_2_without_crashing(tmp_path):
    c = case("ask-needs-approval")
    no_role = FakeModel({**APPROVAL.answer, "slots": [], "vague_terms": []})
    record = quiz.run_case(c, "isolated", Pair(BY_NAME["jaccard"](), BY_NAME["jaccard"]()), no_role, tmp_path)
    g = quiz.grade(c, record)
    assert g["works"] is False
    assert "edge examples could not be built" in g["works_detail"]


def test_only_the_baseline_keeps_the_same_rule_type_pool():
    from ranking_eval.rankings import CANDIDATES, SAME_ACTION, SAME_TYPE

    assert {r.name: r.pool for r in CANDIDATES if r.pool == SAME_TYPE} == {"jaccard": SAME_TYPE}
    assert all(r.pool == SAME_ACTION for r in CANDIDATES if r.name != "jaccard")


def test_the_same_action_pool_finds_evidence_a_rule_type_has_none_of():
    sentence = "Refund up to $40 for order A100, subject to approval."
    found = {}
    for name in ("jaccard", "jaccard-action"):
        ranking = BY_NAME[name]()
        with examples.use_ranking(Pair(BY_NAME["jaccard"](), ranking)):
            found[name] = {e["id"]: e["value"] for e in examples.stating(sentence, "capped_with_approval", "window")}
    assert found["jaccard"] == {}
    assert found["jaccard-action"] == {"form_manager_approval": "1h", "R4": "1h"}


def test_jaccard_action_only_runs_in_the_evidence_score():
    args = quiz.argparse.Namespace(pass_="example", example=None, evidence="jaccard")
    assert "jaccard-action" not in {ex for ex, _ in quiz.pairs_for(args)}
    args = quiz.argparse.Namespace(pass_="evidence", example=None, evidence=None)
    assert ("jaccard", "jaccard-action") in quiz.pairs_for(args)
