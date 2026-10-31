"""Sentence to status with a fake model and the real dogwood CLI."""

import json

import pytest

from app.core import config, edge_examples, flow, generate
from app.core.status import READY
from converter.convert import convert

DEMO_ANSWERS = {
    "No refunds over $5,000.": {
        "rule_type": "amount_cap",
        "action": "IssueRefund",
        "slots": [{"slot": "max_amount", "span": "$5,000", "value": "$5,000"}],
    },
    "Only refund if a manager approved it recently.": {
        "rule_type": "approval_required",
        "action": "IssueRefund",
        "slots": [{"slot": "approver_role", "span": "a manager", "value": "manager"}],
        "vague_terms": [{"span": "recently", "slot": "window"}],
    },
    "No production refunds over $500.50.": {
        "rule_type": "amount_cap",
        "action": "IssueRefund",
        "slots": [{"slot": "max_amount", "span": "$500.50", "value": "$500.50"}],
    },
    "Refund up to $40 for order A100, subject to approval.": {
        "rule_type": "capped_with_approval",
        "action": "IssueRefund",
        "slots": [
            {"slot": "max_amount", "span": "$40", "value": "$40"},
            {"slot": "order_id", "span": "order A100", "value": "A100"},
        ],
        "vague_terms": [{"span": "subject to approval", "slot": "window"}],
    },
    "Only email customers who opted in.": {"rule_type": "unsupported", "action": "none", "unsupported_reason": "email"},
    "Refunds of $1,200 or more are blocked.": {
        "rule_type": "amount_cap",
        "action": "IssueRefund",
        "slots": [{"slot": "max_amount", "span": "$1,200", "value": "$1,200"}],
    },
    "Refund up to $9,000 for order A999, subject to approval.": {
        "rule_type": "capped_with_approval",
        "action": "IssueRefund",
        "slots": [{"slot": "max_amount", "span": "$9,000", "value": "$9,000"}],
    },
    "Production deploys are only allowed for payments and web.": {
        "rule_type": "deploy_allowlist",
        "action": "DeployService",
        "slots": [{"slot": "services", "span": "payments and web", "value": "payments, web"}],
    },
    "Don't deploy to production unless tests passed in the past day.": {
        "rule_type": "deploy_needs_tests",
        "action": "DeployService",
        "slots": [{"slot": "window", "span": "the past day", "value": "the past day"}],
    },
    "Refunds over $750 for order A200 are blocked.": {
        "rule_type": "amount_cap",
        "action": "IssueRefund",
        "slots": [{"slot": "max_amount", "span": "$750", "value": "$750"}],
    },
}
PASSED_EDGE = "Refund up to $40 for order A100, subject to approval."


class FakeModel:
    def info(self):
        return {"name": "fake", "temperature": 0, "seed": 0}

    def complete(self, prompt):
        sentence = prompt["user"].removeprefix("Rule sentence: ")
        return {"unsupported_reason": "", "slots": [], "vague_terms": [], "qualifiers": [], **DEMO_ANSWERS[sentence]}


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ANSWERS_PATH", tmp_path / "answers.sqlite")
    monkeypatch.setattr(generate, "POLICIES_ROOT", tmp_path / "policies")


def run(sentence, given=None):
    events = list(flow.write_policy(sentence, FakeModel(), given))
    return events, events[-1]


def step(events, name, state=None):
    return next(e for e in reversed(events) if e.get("step") == name and (state is None or e["state"] == state))


def test_cap_is_ready_with_summary_before_code_and_edge_examples():
    events, final = run("No refunds over $5,000.")
    assert final["status"] == READY
    names = [e["step"] for e in events if e["state"] == "done"]
    assert names.index("Summary") < names.index("Generate") < names.index("Validate") < names.index("Explain")
    assert step(events, "Summary")["summary"] == "A refund over $5,000 is blocked."
    assert "context.input.amount > 5000" in step(events, "Generate", "done")["dogwood"]
    says = {e["says"]: e["got"] for e in step(events, "Explain", "done")["examples"]}
    assert says["A $5,000 refund is allowed."] == "ALLOW"
    assert says["A $5,001 refund is blocked."] == "DENY"
    assert (
        "PERMIT" in step(events, "Explain", "done")["literal"] or "FORBID" in step(events, "Explain", "done")["literal"]
    )
    assert "OK" in step(events, "Validate")["output"]


def test_inclusive_limit_follows_the_wording():
    events, final = run("Refunds of $1,200 or more are blocked.")
    assert final["status"] == READY
    assert "context.input.amount >= 1200" in step(events, "Generate", "done")["dogwood"]
    says = {e["says"]: e["got"] for e in step(events, "Explain", "done")["examples"]}
    assert says["A $1,199 refund is allowed."] == "ALLOW" and says["A $1,200 refund is blocked."] == "DENY"


def test_approval_asks_then_is_ready_with_windows_and_amounts_at_their_edges():
    sentence = "Only refund if a manager approved it recently."
    events, final = run(sentence)
    assert final["status"] == "needs clarification"
    assert step(events, "Fill or ask")["state"] == "waiting"
    assert step(events, "Summary")["state"] == "skipped"
    events, final = run(sentence, {"window": "1h"})
    assert final["status"] == READY, final["reasons"]
    examples = {e["says"]: e for e in step(events, "Explain", "done")["examples"]}
    assert 4 <= len(examples) <= 6
    assert examples["Approved $40 by a manager 1 hour ago: a $40 refund is allowed."]["got"] == "ALLOW"
    assert examples["Approved $40 by a manager 1 hour and 1 minute ago: a $40 refund is blocked."]["got"] == "DENY"
    assert examples["Approved $25 with a $40 refund: blocked."]["got"] == "DENY"
    assert examples["Approved $25 with a $25 refund: allowed."]["got"] == "ALLOW"


def test_production_cents_needs_review_and_generates_nothing():
    events, final = run("No production refunds over $500.50.")
    assert final["status"] == "needs review"
    assert step(events, "Generate")["state"] == "skipped"
    assert any("whole dollars" in r for r in final["reasons"]) and any("production" in r for r in final["reasons"])


def test_capped_with_approval_generates_both_rules_scoped_to_the_order():
    events, final = run(PASSED_EDGE, {"window": "1h"})
    assert final["status"] == READY, final["reasons"]
    rule = step(events, "Generate", "done")["dogwood"]
    assert 'context.input.order_id == "A100" && context.input.amount > 40' in rule
    assert 'when { context.input.order_id == "A100" }' in rule
    assert 'context.facts.approver_role == "manager"' in rule
    assert "context.input.amount + context.facts.refunded_so_far <= context.facts.approved_amount" in rule
    assert all(e["agrees"] for e in step(events, "Explain", "done")["examples"])


def test_unsupported_is_invalid_and_names_the_supported_types():
    events, final = run("Only email customers who opted in.")
    assert final["status"] == "invalid" and "amount_cap" in final["reasons"][0]
    assert step(events, "Validate")["state"] == "skipped"


def test_order_the_store_does_not_have_needs_review():
    _, final = run("Refund up to $9,000 for order A999, subject to approval.", {"window": "1h"})
    assert final["status"] == "needs review" and "A999 is not in the store" in final["reasons"][0]


@pytest.mark.parametrize(
    "sentence,given",
    [
        ("Production deploys are only allowed for payments and web.", None),
        ("Don't deploy to production unless tests passed in the past day.", None),
        ("Refunds over $750 for order A200 are blocked.", None),
    ],
)
def test_other_rule_types_are_ready_and_their_examples_agree(sentence, given):
    events, final = run(sentence, given)
    assert final["status"] == READY, final["reasons"]
    examples = step(events, "Explain", "done")["examples"]
    assert 4 <= len(examples) <= 6 and all(e["agrees"] for e in examples)


def test_policy_folder_keeps_what_dogwood_said():
    _, final = run("No refunds over $5,000.")
    out = generate.POLICIES_ROOT / final["policy"]
    assert "OK" in (out / "validate.txt").read_text()
    assert "FORBID" in (out / "explain.txt").read_text()
    assert json.loads((out / "record.json").read_text())["status"]["status"] == READY
    assert len(json.loads((out / "forms.json").read_text())) == 1


def test_an_example_that_disagrees_with_the_summary_means_review(monkeypatch):
    real = edge_examples.build

    def wrong(u):
        return [{**e, "expect": "DENY"} if e["expect"] == "ALLOW" else e for e in real(u)]

    monkeypatch.setattr(edge_examples, "build", wrong)
    _, final = run("No refunds over $5,000.")
    assert final["status"] == "needs review" and "Dogwood said ALLOW" in final["reasons"][0]


def test_validation_failure_is_invalid(monkeypatch):
    monkeypatch.setattr(generate, "validate", lambda policy, out: (False, "bad policy"))
    events, final = run("No refunds over $5,000.")
    assert final["status"] == "invalid" and "bad policy" in final["reasons"][0]
    assert step(events, "Explain")["state"] == "skipped"


def test_limit_op_defaults_to_strict_and_refuses_others():
    form = {"id": "x", "kind": "limit", "action": "IssueRefund", "field": "amount", "max": 5}
    assert "amount > 5" in convert([("x", form)], config.SCHEMA_PATH)
    assert "amount >= 5" in convert([("x", {**form, "op": ">="})], config.SCHEMA_PATH)
    with pytest.raises(ValueError, match="'op'"):
        convert([("x", {**form, "op": "<"})], config.SCHEMA_PATH)
