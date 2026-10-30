"""The understanding checks, driven by a fake model with canned answers (no Ollama needed)."""
import pytest

from app.core import rule_types
from app.core.understand import understand


class FakeModel:
    def __init__(self, answer):
        self.answer = answer
        self.prompts = []

    def info(self):
        return {"name": "fake", "temperature": 0, "seed": 0}

    def complete(self, prompt):
        self.prompts.append(prompt)
        return self.answer


def answer(rule_type, action, slots=(), vague=(), qualifiers=(), reason=""):
    return {"rule_type": rule_type, "action": action, "unsupported_reason": reason,
            "slots": [{"slot": s, "span": sp, "value": v} for s, sp, v in slots],
            "vague_terms": [{"span": sp, "slot": s} for sp, s in vague],
            "qualifiers": [{"span": sp, "field": f} for sp, f in qualifiers]}


def run(sentence, ans):
    return understand(sentence, FakeModel(ans))


def stated(u):
    return {k: s["value"] for k, s in u["slots"].items() if s["source"] == "stated"}


# ------------------------------------------------------------------ the four demo sentences

def test_amount_cap():
    s = "No refunds over $5,000."
    u = run(s, answer("amount_cap", "IssueRefund", [("max_amount", "$5,000", "$5000")]))
    assert u["verdict"] == "ok_so_far"
    assert stated(u) == {"max_amount": 5000}
    slot = u["slots"]["max_amount"]
    assert slot["source"] == "stated"
    assert s[slot["spans"][0]["start"]:slot["spans"][0]["end"]] == "$5,000"


def test_approval_recently_is_vague():
    s = "Only refund if a manager approved it recently."
    # What qwen2.5:7b actually answers: "recently" put in the window slot, not in vague_terms.
    u = run(s, answer("approval_required", "IssueRefund",
                      [("approver_role", "a manager", "manager"), ("window", "recently", "recently")]))
    assert u["verdict"] == "needs_clarification"
    assert stated(u) == {"approver_role": "manager"}
    assert u["slots"]["window"]["source"] == "missing"
    [v] = u["vague_terms"]
    assert (v["text"], v["slot"], v["start"], v["end"]) == ("recently", "window", 37, 45)


def test_approval_vague_flagged_directly():
    u = run("Only refund if a manager approved it recently.",
            answer("approval_required", "IssueRefund", [("approver_role", "a manager", "manager")],
                   vague=[("recently", "window")]))
    assert u["verdict"] == "needs_clarification"
    assert [v["text"] for v in u["vague_terms"]] == ["recently"]


def test_production_cents_needs_review():
    u = run("No production refunds over $500.50.",
            answer("amount_cap", "IssueRefund", [("max_amount", "$500.50", "$500.50")],
                   qualifiers=[("production", "env")]))
    assert u["verdict"] == "needs_review"
    assert stated(u) == {"max_amount": "500.50"}       # exact, not rounded
    [q] = u["qualifiers"]
    assert q["text"] == "production" and q["mapped"] is False and q["field"] is None
    reasons = " ".join(i["reason"] for i in u["issues"])
    assert "whole dollars" in reasons and "production" in reasons


def test_production_kept_when_model_span_swallows_it():
    # The model once gave the whole sentence as the amount span and no qualifier.
    u = run("No production refunds over $500.50.",
            answer("amount_cap", "IssueRefund", [("max_amount", "No production refunds over $500.50", ".50")],
                   qualifiers=[("production", "none")]))
    assert u["verdict"] == "needs_review"
    assert stated(u) == {"max_amount": "500.50"}
    assert [q["text"] for q in u["qualifiers"]] == ["production"]


def test_capped_with_approval():
    u = run("Refund up to $40 for order A100, subject to approval.",
            answer("capped_with_approval", "IssueRefund",
                   [("max_amount", "$40", "$40"), ("order_id", "order A100", "A100")],
                   vague=[("subject to approval", "window")]))
    assert u["rule_type"] == "capped_with_approval"
    assert stated(u) == {"max_amount": 40, "order_id": "A100"}
    # The approval window is not stated, and there is no default.
    assert u["slots"]["window"]["source"] == "missing"
    assert u["verdict"] == "needs_clarification"


def test_capped_with_approval_window_missing_without_vague():
    u = run("Refund up to $40 for order A100, subject to approval.",
            answer("capped_with_approval", "IssueRefund",
                   [("max_amount", "$40", ""), ("order_id", "order A100", "A100")]))
    assert u["verdict"] == "needs_clarification"
    assert "window is required" in u["verdict_reason"]


# ------------------------------------------------------------------ unsupported

UNSUPPORTED = [
    "Don't let the agent read customers' SSNs",
    "Block deploys after 6pm on Fridays",
    "Only email customers who opted in",
    "Never copy private data to a public repo",
    "At most 5 refunds per hour",
]


@pytest.mark.parametrize("sentence", UNSUPPORTED)
def test_unsupported_refused(sentence):
    u = run(sentence, answer("unsupported", "none", reason="Not one of the supported types."))
    assert u["verdict"] == "invalid"
    for type_id in rule_types.supported_ids():
        assert type_id in u["verdict_reason"]


def test_invented_rule_type_refused():
    u = run("At most 5 refunds per hour",
            answer("refund_rate_limit", "IssueRefund", [("max_amount", "5", "5")]))
    assert u["verdict"] == "invalid"
    assert "refund_rate_limit" in u["verdict_reason"] and "amount_cap" in u["verdict_reason"]


def test_action_not_in_schema_refused():
    u = run("Only email customers who opted in", answer("amount_cap", "SendEmail"))
    assert u["verdict"] == "invalid" and "SendEmail" in u["verdict_reason"]


def test_action_not_matching_type_refused():
    u = run("Block deploys after 6pm on Fridays", answer("amount_cap", "DeployService"))
    assert u["verdict"] == "invalid"


# ------------------------------------------------------------------ bad model outputs

def test_value_not_in_sentence_is_dropped():
    u = run("Only refund if a manager approved it recently.",
            answer("approval_required", "IssueRefund",
                   [("approver_role", "a manager", "manager"), ("window", "1 hour", "1 hour")]))
    assert u["slots"]["window"]["source"] == "missing"
    assert {"what": "slot window", "span": "1 hour", "reason": "rejected: not in the sentence"} in u["rejected"]
    assert u["verdict"] == "needs_clarification"


def test_wrong_number_for_correct_span_uses_code():
    u = run("No refunds over $5,000.", answer("amount_cap", "IssueRefund", [("max_amount", "$5,000", "500")]))
    assert stated(u) == {"max_amount": 5000}
    assert "model said '500'" in u["slots"]["max_amount"]["note"]
    assert u["verdict"] == "ok_so_far"


def test_invented_number_inside_vague_span_is_rejected():
    # Seen live: the model put "recently" in the window slot with value "2 hours".
    u = run("Only refund if a manager approved it recently.",
            answer("approval_required", "IssueRefund",
                   [("approver_role", "a manager", "manager"), ("window", "recently", "2 hours")]))
    assert u["slots"]["window"]["source"] == "missing"
    assert [v["text"] for v in u["vague_terms"]] == ["recently"]


def test_text_value_not_in_its_span_is_rejected():
    u = run("Refund up to $40 for order A100, subject to approval.",
            answer("capped_with_approval", "IssueRefund",
                   [("max_amount", "$40", "$40"), ("order_id", "order A100", "B200")]))
    assert u["slots"]["order_id"]["source"] == "missing"
    assert any("not in its span" in r["reason"] for r in u["rejected"])


def test_unassigned_amount_flagged():
    u = run("No refunds over $5,000 or $20 for gift cards.",
            answer("amount_cap", "IssueRefund", [("max_amount", "$5,000", "$5,000")]))
    assert [n["text"] for n in u["unassigned_numbers"]] == ["$20"]
    assert u["verdict"] == "needs_review"


def test_slot_from_another_type_is_not_dropped():
    u = run("No production refunds over $500.", answer(
        "amount_cap", "IssueRefund", [("max_amount", "$500", "$500"), ("order_id", "production", "production")]))
    assert [q["text"] for q in u["qualifiers"]] == ["production"]
    assert u["verdict"] == "needs_review"


def test_mapped_qualifier_on_deploy_is_ok():
    u = run("Production deploys are only allowed for payments, search and web.", answer(
        "deploy_allowlist", "DeployService",
        [("services", "payments", "payments"), ("services", "search", "search"), ("services", "web", "web")],
        qualifiers=[("Production", "env")]))
    assert u["verdict"] == "ok_so_far"
    assert stated(u) == {"services": ["payments", "search", "web"]}
    assert u["qualifiers"][0]["mapped"] and u["qualifiers"][0]["field"] == "env"


def test_duration_parsed_by_code():
    u = run("A version may only go to production if its tests passed within the last 24 hours.", answer(
        "deploy_needs_tests", "DeployService", [("window", "within the last 24 hours", "1 day")],
        qualifiers=[("production", "env")]))
    assert stated(u) == {"window": "24h"}
    assert u["slots"]["window"]["seconds"] == 86400
    assert u["verdict"] == "ok_so_far"


def test_prompt_context_recorded():
    fake = FakeModel(answer("amount_cap", "IssueRefund", [("max_amount", "$5,000", "")]))
    u = understand("No refunds over $5,000.", fake)
    ctx = u["prompt_context"]
    assert ctx["rule_types"] == rule_types.supported_ids()
    assert ctx["tools"]["IssueRefund"] == ["amount", "order_id"]
    assert "No refunds over $5,000." in fake.prompts[0]["user"]
    for type_id in rule_types.supported_ids():
        assert type_id in fake.prompts[0]["system"]
