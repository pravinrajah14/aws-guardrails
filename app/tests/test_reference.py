"""The form-driven search, known bugs, the reference check and saved cases, all proven with the real dogwood CLI."""

import json

import pytest
from fastapi.testclient import TestClient

from app.core import config, flow, generate, known_bugs, planted, reference, saved_cases
from app.tests.test_flow import FakeModel
from app.web import server
from question.compare import describe, find_difference

REFERENCES = sorted((config.REPO / "data" / "references").glob("*.json"))
WITH_FORMS = [p.stem for p in REFERENCES if json.loads(p.read_text())["forms"]]
A100 = "Refund up to $40 for order A100, subject to approval."


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ANSWERS_PATH", tmp_path / "answers.sqlite")
    monkeypatch.setattr(generate, "POLICIES_ROOT", tmp_path / "policies")
    monkeypatch.setattr(saved_cases, "CASES_DIR", tmp_path / "confirmed")
    monkeypatch.setattr(server, "model", FakeModel())


def load(name):
    return json.loads((config.REPO / "data" / "references" / f"{name}.json").read_text())


def policy_for(sentence, given=None):
    return list(flow.write_policy(sentence, FakeModel(), given))[-1]["policy"]


def test_references_cover_the_demo_sentences_and_jacks_rules():
    assert len(REFERENCES) == 8
    for sentence in server.DEMO_SENTENCES:
        assert reference.find_reference(sentence), sentence
    approval = load("manager_approved_recently")["forms"][0]["requires"]
    assert approval["at_most"] == {"amount": "approved_amount"}


@pytest.mark.parametrize("name", WITH_FORMS)
def test_a_reference_never_differs_from_itself_and_generates_valid_policy(name, tmp_path):
    forms = load(name)["forms"]
    assert find_difference(forms, forms) is None
    policy = generate.write_policy(forms, tmp_path / "out")
    assert generate.validate(policy, tmp_path / "out")[0]


BUG_CASES = [
    (name, bug)
    for name in WITH_FORMS
    for bug in known_bugs.BUGS
    if known_bugs.plant(load(name)["forms"], bug) is not None
]


@pytest.mark.parametrize("name,bug", BUG_CASES)
def test_every_adapted_bug_is_found_and_dogwood_agrees_with_the_search(name, bug, tmp_path):
    forms = load(name)["forms"]
    found = reference.compare_forms(known_bugs.plant(forms, bug), forms, load(name)["sentence"], tmp_path)
    assert found is not None
    assert found["policy"]["verdict"] != found["reference"]["verdict"]


def test_each_demo_bug_applies_to_some_reference():
    applied = {bug for _, bug in BUG_CASES}
    assert set(known_bugs.DEMO_BUGS) <= applied


def test_the_example_from_the_brief():
    forms = load("capped_a100")["forms"]
    case = find_difference(known_bugs.plant(forms, "missing_amount_check"), forms)
    assert "asked for $40 on order A100 and approved $25" in describe(case) and "refund $40" in describe(case)
    assert case["predicted"] == ["ALLOW", "DENY"]


def test_cap_difference_needs_no_history():
    ref = load("no_refunds_over_5000")["forms"]
    case = find_difference(known_bugs.plant(ref, "boundary"), ref)
    assert len(case["events"]) == 1 and case["events"][0]["input"]["amount"] == 5000


def test_different_windows_are_found_at_their_edge():
    a = load("manager_approved_recently")["forms"]
    b = json.loads(json.dumps(a))
    b[0]["requires"]["within"] = "30m"
    case = find_difference(a, b)
    assert case["predicted"] == ["ALLOW", "DENY"] and case["events"][-1]["at"] - case["events"][-2]["at"] > 1800


def test_every_planted_error_in_the_checker_is_caught_by_dogwood():
    rows = planted.replay_all()
    assert len(rows) == 13 and all(r["caught"] for r in rows)


def test_every_named_checker_adaptation_exists():
    known = {f"{r['policy']}_{r['error']}" for r in planted.replay_all()}
    named = {name for names in known_bugs.CHECKER.values() for name in names}
    assert named <= known
    for bug in known_bugs.CHECKER:
        assert bug in known_bugs.BUGS


def test_check_finds_nothing_on_a_correct_policy_and_a_loaded_bug_otherwise():
    policy_id = policy_for(A100, {"window": "1h"})
    assert reference.check(policy_id)["same"] is True
    broken = reference.check(policy_id, "any_approver")
    assert broken["same"] is False and "support" in broken["difference"]["story"]


def test_a_user_window_that_differs_from_the_reference_is_found():
    result = reference.check(policy_for(A100, {"window": "2h"}))
    assert result["same"] is False and result["difference"]["policy"]["verdict"] == "ALLOW"


def test_fix_shows_before_and_after_and_passes_its_recheck():
    policy_id = policy_for(A100, {"window": "1h"})
    fixed = reference.fix(policy_id, "missing_amount_check")
    assert fixed["before"]["verdict"] == "ALLOW" and fixed["after"]["verdict"] == "DENY"
    assert fixed["after"]["verdict"] == fixed["reference_verdict"]
    assert fixed["validated"] and fixed["recheck_same"] and fixed["status"]["status"] == "ready"
    assert "isn't checked" in fixed["before"]["summary"] and "at most the approved amount" in fixed["after"]["summary"]
    assert "approved_amount" not in fixed["before"]["dogwood"] and "approved_amount" in fixed["after"]["dogwood"]
    assert reference.check(fixed["policy_id"])["same"] is True


def test_no_reference_says_so():
    result = reference.check(policy_for("Refunds over $750 for order A200 are blocked."))
    assert result["has_reference"] is False and "Confirm the edge examples" in result["message"]


def test_confirmed_case_reruns_when_the_policy_changes():
    sentence = "No refunds over $5,000."
    policy_id = policy_for(sentence)
    client = TestClient(server.app)
    policy = client.get(f"/api/policy/{policy_id}").json()
    example = policy["examples"][1]
    response = client.post(
        "/api/confirm",
        json={"policy_id": policy_id, "says": example["says"], "expect": "DENY", "events": example["events"]},
    ).json()
    assert response["saved_cases"][0]["passed"]
    events = list(flow.write_policy(sentence, FakeModel()))
    assert events[-1]["status"] == "ready"
    assert next(e for e in events if e.get("step") == "Explain" and e["state"] == "done")["saved_cases"][0]["passed"]


def test_a_confirmed_case_the_new_policy_breaks_means_review():
    sentence = "No refunds over $5,000."
    events = [{"at": 0, "tool": "issue_refund", "input": {"order_id": "Z900", "amount": 5000}, "expect": "?"}]
    saved_cases.save(sentence, "$5,000 should be blocked.", "DENY", events)
    final = list(flow.write_policy(sentence, FakeModel()))[-1]
    assert final["status"] == "needs review" and "Confirmed case" in final["reasons"][0]


def test_saving_the_same_case_twice_keeps_one_file():
    events = [{"at": 0, "tool": "issue_refund", "input": {"order_id": "Z900", "amount": 1}, "expect": "?"}]
    saved_cases.save("X.", "says", "ALLOW", events)
    saved_cases.save("X.", "says", "ALLOW", events)
    assert len(saved_cases.for_sentence("x.")) == 1


def test_api_reference_fix_planted_and_policy_fields():
    client = TestClient(server.app)
    policy_id = policy_for(A100, {"window": "1h"})
    policy = client.get(f"/api/policy/{policy_id}").json()
    assert policy["has_reference"] and {b["id"] for b in policy["bugs"]} == set(known_bugs.DEMO_BUGS)
    body = {"policy_id": policy_id, "bug": "wrong_window"}
    assert client.post("/api/reference", json=body).json()["same"] is False
    fixed = client.post("/api/fix", json=body).json()
    assert client.get(f"/api/policy/{fixed['policy_id']}").status_code == 200
    assert all(r["caught"] for r in client.get("/api/planted").json()["rows"])
    assert client.post("/api/reference", json={"policy_id": policy_id, "bug": "boundary"}).status_code == 200
    capless = policy_for("Only refund if a manager approved it recently.", {"window": "1h"})
    assert client.post("/api/reference", json={"policy_id": capless, "bug": "boundary"}).status_code == 400
