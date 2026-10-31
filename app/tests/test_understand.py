"""The understanding checks, driven by a fake model with canned answers (no Ollama needed)."""

import json
import urllib.error

import pytest

from app.core import config, examples, live_check, model, report, rule_types
from app.core.understand import check, understand
from converter.convert import load_schema

RECORDED_RUNS = config.REPO / "results" / "understanding_runs_default.json"


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
    return {
        "rule_type": rule_type,
        "action": action,
        "unsupported_reason": reason,
        "slots": [{"slot": s, "span": sp, "value": v} for s, sp, v in slots],
        "vague_terms": [{"span": sp, "slot": s} for sp, s in vague],
        "qualifiers": [{"span": sp, "field": f} for sp, f in qualifiers],
    }


def run(sentence, ans):
    return understand(sentence, FakeModel(ans))


def stated(u):
    return {k: s["value"] for k, s in u["slots"].items() if s["source"] in ("stated", "glossary")}


def test_amount_cap():
    s = "No refunds over $5,000."
    u = run(s, answer("amount_cap", "IssueRefund", [("max_amount", "$5,000", "$5000")]))
    assert u["verdict"] == "ok_so_far"
    assert stated(u) == {"max_amount": 5000}
    slot = u["slots"]["max_amount"]
    assert slot["source"] == "stated"
    assert s[slot["spans"][0]["start"] : slot["spans"][0]["end"]] == "$5,000"


def test_approval_recently_is_vague():
    s = "Only refund if a manager approved it recently."
    # What qwen2.5:7b actually answers: "recently" put in the window slot, not in vague_terms.
    u = run(
        s,
        answer(
            "approval_required",
            "IssueRefund",
            [("approver_role", "a manager", "manager"), ("window", "recently", "recently")],
        ),
    )
    assert u["verdict"] == "needs_clarification"
    assert stated(u) == {"approver_role": "manager"}
    assert u["slots"]["window"]["source"] == "missing"
    [v] = u["vague_terms"]
    assert (v["text"], v["slot"], v["start"], v["end"]) == ("recently", "window", 37, 45)


def test_approval_vague_flagged_directly():
    u = run(
        "Only refund if a manager approved it recently.",
        answer(
            "approval_required",
            "IssueRefund",
            [("approver_role", "a manager", "manager")],
            vague=[("recently", "window")],
        ),
    )
    assert u["verdict"] == "needs_clarification"
    assert [v["text"] for v in u["vague_terms"]] == ["recently"]


def test_production_cents_needs_review():
    u = run(
        "No production refunds over $500.50.",
        answer("amount_cap", "IssueRefund", [("max_amount", "$500.50", "$500.50")], qualifiers=[("production", "env")]),
    )
    assert u["verdict"] == "needs_review"
    assert stated(u) == {"max_amount": "500.50"}  # exact, not rounded
    [q] = u["qualifiers"]
    assert q["text"] == "production" and q["mapped"] is False and q["field"] is None
    reasons = " ".join(i["reason"] for i in u["issues"])
    assert "whole dollars" in reasons and "production" in reasons


def test_production_kept_when_model_span_swallows_it():
    # The model once gave the whole sentence as the amount span and no qualifier.
    u = run(
        "No production refunds over $500.50.",
        answer(
            "amount_cap",
            "IssueRefund",
            [("max_amount", "No production refunds over $500.50", ".50")],
            qualifiers=[("production", "none")],
        ),
    )
    assert u["verdict"] == "needs_review"
    assert stated(u) == {"max_amount": "500.50"}
    assert [q["text"] for q in u["qualifiers"]] == ["production"]


def test_capped_with_approval():
    u = run(
        "Refund up to $40 for order A100, subject to approval.",
        answer(
            "capped_with_approval",
            "IssueRefund",
            [("max_amount", "$40", "$40"), ("order_id", "order A100", "A100")],
            vague=[("subject to approval", "window")],
        ),
    )
    assert u["rule_type"] == "capped_with_approval"
    assert stated(u) == {"max_amount": 40, "order_id": "A100"}
    # The approval window is not stated, and there is no default.
    assert u["slots"]["window"]["source"] == "missing"
    assert u["verdict"] == "needs_clarification"


def test_capped_with_approval_window_missing_without_vague():
    u = run(
        "Refund up to $40 for order A100, subject to approval.",
        answer("capped_with_approval", "IssueRefund", [("max_amount", "$40", ""), ("order_id", "order A100", "A100")]),
    )
    assert u["verdict"] == "needs_clarification"
    assert "window is required" in u["verdict_reason"]


UNSUPPORTED = [
    "Don't let the agent read customers' SSNs",
    "Block deploys after 6pm on Fridays",
    "Only email customers who opted in",
    "Never copy private data to a public repo",
    "At most 5 refunds per hour",
    "Refunds are only allowed for orders placed within the last 30 days.",
]


@pytest.mark.parametrize("sentence", UNSUPPORTED)
def test_unsupported_refused(sentence):
    u = run(sentence, answer("unsupported", "none", reason="Not one of the supported types."))
    assert u["verdict"] == "invalid"
    for type_id in rule_types.supported_ids():
        assert type_id in u["verdict_reason"]


def test_invented_rule_type_refused():
    u = run("At most 5 refunds per hour", answer("refund_rate_limit", "IssueRefund", [("max_amount", "5", "5")]))
    assert u["verdict"] == "invalid"
    assert "refund_rate_limit" in u["verdict_reason"] and "amount_cap" in u["verdict_reason"]


def test_action_not_in_schema_refused():
    u = run("Only email customers who opted in", answer("amount_cap", "SendEmail"))
    assert u["verdict"] == "invalid" and "SendEmail" in u["verdict_reason"]


def test_action_not_matching_type_refused():
    u = run("Block deploys after 6pm on Fridays", answer("amount_cap", "DeployService"))
    assert u["verdict"] == "invalid"


def test_value_not_in_sentence_is_dropped():
    u = run(
        "Only refund if a manager approved it recently.",
        answer(
            "approval_required",
            "IssueRefund",
            [("approver_role", "a manager", "manager"), ("window", "1 hour", "1 hour")],
        ),
    )
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
    u = run(
        "Only refund if a manager approved it recently.",
        answer(
            "approval_required",
            "IssueRefund",
            [("approver_role", "a manager", "manager"), ("window", "recently", "2 hours")],
        ),
    )
    assert u["slots"]["window"]["source"] == "missing"
    assert [v["text"] for v in u["vague_terms"]] == ["recently"]


def test_text_value_not_in_its_span_is_rejected():
    u = run(
        "Refund up to $40 for order A100, subject to approval.",
        answer(
            "capped_with_approval", "IssueRefund", [("max_amount", "$40", "$40"), ("order_id", "order A100", "B200")]
        ),
    )
    assert (u["slots"]["order_id"]["source"], u["slots"]["order_id"]["value"]) == ("glossary", "A100")
    assert any("not in its span" in r["reason"] for r in u["rejected"])


def test_unassigned_amount_flagged():
    u = run(
        "No refunds over $5,000 or $20 for gift cards.",
        answer("amount_cap", "IssueRefund", [("max_amount", "$5,000", "$5,000")]),
    )
    assert [n["text"] for n in u["unassigned_numbers"]] == ["$20"]
    assert u["verdict"] == "needs_review"


def test_slot_from_another_type_is_not_dropped():
    u = run(
        "No production refunds over $500.",
        answer("amount_cap", "IssueRefund", [("max_amount", "$500", "$500"), ("order_id", "production", "production")]),
    )
    assert [q["text"] for q in u["qualifiers"]] == ["production"]
    assert u["verdict"] == "needs_review"


def test_mapped_qualifier_on_deploy_is_ok():
    u = run(
        "Production deploys are only allowed for payments, search and web.",
        answer(
            "deploy_allowlist",
            "DeployService",
            [("services", "payments", "payments"), ("services", "search", "search"), ("services", "web", "web")],
            qualifiers=[("Production", "env")],
        ),
    )
    assert u["verdict"] == "ok_so_far"
    assert stated(u) == {"services": ["payments", "search", "web"]}
    assert u["qualifiers"][0]["mapped"] and u["qualifiers"][0]["field"] == "env"


def test_duration_parsed_by_code():
    u = run(
        "A version may only go to production if its tests passed within the last 24 hours.",
        answer(
            "deploy_needs_tests",
            "DeployService",
            [("window", "within the last 24 hours", "1 day")],
            qualifiers=[("production", "env")],
        ),
    )
    assert stated(u) == {"window": "24h"}
    assert u["slots"]["window"]["seconds"] == 86400
    assert u["verdict"] == "ok_so_far"


def test_prompt_shows_the_picked_examples_and_their_types():
    fake = FakeModel(answer("unsupported", "none"))
    u = understand("Never refund the same order twice.", fake)
    system = fake.prompts[0]["system"]
    assert len(u["examples_used"]) == 3
    for e in u["examples_used"]:
        assert f'"{e["sentence"]}" -> {e["type"]}' in system


def test_prompt_context_recorded():
    fake = FakeModel(answer("amount_cap", "IssueRefund", [("max_amount", "$5,000", "")]))
    u = understand("No refunds over $5,000.", fake)
    ctx = u["prompt_context"]
    assert ctx["rule_types"] == rule_types.supported_ids()
    assert ctx["tools"]["IssueRefund"] == ["amount", "order_id"]
    assert "No refunds over $5,000." in fake.prompts[0]["user"]
    for type_id in rule_types.supported_ids():
        assert type_id in fake.prompts[0]["system"]


def test_glossary_adds_a_qualifier_the_model_missed():
    u = run("No production refunds over $500.", answer("amount_cap", "IssueRefund", [("max_amount", "$500", "$500")]))
    [q] = u["qualifiers"]
    assert (q["text"], q["source"], q["mapped"]) == ("production", "glossary", False)
    assert u["verdict"] == "needs_review" and "no field" in u["verdict_reason"]
    assert "prod" not in json.dumps({k: v for k, v in q.items() if k not in ("text", "glossary_file")})


@pytest.mark.parametrize("word", ["Production", "prod", "live"])
def test_glossary_maps_environment_words_on_a_deploy(word):
    u = run(
        f"{word} deploys are only allowed for web.",
        answer("deploy_allowlist", "DeployService", [("services", "web", "web")]),
    )
    [q] = u["qualifiers"]
    assert (q["text"], q["field"], q["mapped"], q["source"]) == (word, "env", True, "glossary")
    assert u["verdict"] == "ok_so_far"


def test_glossary_field_beats_the_model_field():
    u = run(
        "Staging deploys are only allowed for web.",
        answer(
            "deploy_allowlist",
            "DeployService",
            [("services", "web", "web")],
            qualifiers=[("Staging deploys", "service")],
        ),
    )
    [q] = u["qualifiers"]
    assert (q["text"], q["field"], q["source"]) == ("Staging", "env", "model")


@pytest.mark.parametrize(
    "word,role", [("manager", "manager"), ("Managers", "manager"), ("manager's", "manager"), ("support", "support")]
)
def test_glossary_fills_an_approver_the_model_missed(word, role):
    u = run(
        f"A refund needs {word} approval from the last 30 minutes.",
        answer("approval_required", "IssueRefund", [("window", "30 minutes", "30 minutes")]),
    )
    slot = u["slots"]["approver_role"]
    assert (slot["source"], slot["value"], slot["spans"][0]["text"]) == ("glossary", role, word)
    assert u["verdict"] == "ok_so_far"


def test_glossary_normalizes_a_stated_approver():
    u = run(
        "Managers must approve refunds from the last hour.",
        answer(
            "approval_required",
            "IssueRefund",
            [("approver_role", "Managers", "Managers"), ("window", "the last hour", "1 hour")],
        ),
    )
    assert u["slots"]["approver_role"]["source"] == "stated"
    assert u["slots"]["approver_role"]["value"] == "manager"


def test_glossary_conflict_with_the_model_needs_review():
    u = run(
        "A team lead or a manager must approve refunds in the last hour.",
        answer(
            "approval_required", "IssueRefund", [("approver_role", "team lead", "team lead"), ("window", "1 hour", "")]
        ),
    )
    assert u["verdict"] == "needs_review" and "two values for approver_role" in u["verdict_reason"]


def test_glossary_fills_an_order_id_as_typed():
    u = run(
        "Refund up to $40 for order a100, subject to approval.",
        answer("capped_with_approval", "IssueRefund", [("max_amount", "$40", "$40")]),
    )
    assert stated(u) == {"max_amount": 40, "order_id": "a100"}
    assert u["slots"]["order_id"]["source"] == "glossary"


def test_order_id_on_a_refund_cap_is_a_mapped_qualifier():
    u = run("No refunds over $40 for order A100.", answer("amount_cap", "IssueRefund", [("max_amount", "$40", "$40")]))
    [q] = u["qualifiers"]
    assert (q["text"], q["field"], q["mapped"], q["source"]) == ("A100", "order_id", True, "glossary")
    assert u["unassigned_numbers"] == []


def test_term_with_no_field_on_the_action_needs_review():
    u = run(
        "Refunds over $1,000 need manager approval.",
        answer("amount_cap", "IssueRefund", [("max_amount", "$1,000", "")]),
    )
    assert u["verdict"] == "needs_review" and u["qualifiers"][0]["text"] == "manager"


@pytest.mark.parametrize(
    "sentence,amount,kind,rule",
    [
        ("No refunds over $5,000.", "$5,000", "strict", "blocked when amount > 5000"),
        ("Never refund more than $750.", "$750", "strict", "blocked when amount > 750"),
        ("Refunds above $2,500 are not allowed.", "$2,500", "strict", "blocked when amount > 2500"),
        ("Refunds exceeding $10 are blocked.", "$10", "strict", "blocked when amount > 10"),
        ("Refunds of $5,000 or more are blocked.", "$5,000", "inclusive", "blocked when amount >= 5000"),
        ("Refunds of at least $5,000 are blocked.", "$5,000", "inclusive", "blocked when amount >= 5000"),
        ("Refunds of $1,200 and up need to be blocked.", "$1,200", "inclusive", "blocked when amount >= 1200"),
        ("Refunds of $1,200 and above are blocked.", "$1,200", "inclusive", "blocked when amount >= 1200"),
        ("Refunds of $1,200 or higher are blocked.", "$1,200", "inclusive", "blocked when amount >= 1200"),
        ("Refunds of $1,200 or greater are blocked.", "$1,200", "inclusive", "blocked when amount >= 1200"),
        ("Refunds greater than $10 are blocked.", "$10", "strict", "blocked when amount > 10"),
        ("Refunds that exceed $10 are blocked.", "$10", "strict", "blocked when amount > 10"),
        ("A refund that exceeds $10 is blocked.", "$10", "strict", "blocked when amount > 10"),
        ("A single refund may not exceed $500.", "$500", "allowed_max", "allowed when amount <= 500"),
        ("A refund must not exceed $500.", "$500", "allowed_max", "allowed when amount <= 500"),
        ("Refund not more than $60.", "$60", "allowed_max", "allowed when amount <= 60"),
        ("Refunds are capped at $40.", "$40", "allowed_max", "allowed when amount <= 40"),
        ("Refunds are limited to $40.", "$40", "allowed_max", "allowed when amount <= 40"),
        ("Refunds have a maximum of $40.", "$40", "allowed_max", "allowed when amount <= 40"),
        ("Refund max $40.", "$40", "allowed_max", "allowed when amount <= 40"),
        ("Refund under $40.", "$40", "allowed_under", "allowed when amount < 40"),
        ("Refund below $40.", "$40", "allowed_under", "allowed when amount < 40"),
        ("Refund less than $40.", "$40", "allowed_under", "allowed when amount < 40"),
        ("Refund up to $40.", "$40", "allowed_max", "allowed when amount <= 40"),
        ("Refund at most $60.", "$60", "allowed_max", "allowed when amount <= 60"),
        ("Refund no more than $60.", "$60", "allowed_max", "allowed when amount <= 60"),
    ],
)
def test_direction_comes_from_the_words_next_to_the_amount(sentence, amount, kind, rule):
    u = run(sentence, answer("amount_cap", "IssueRefund", [("max_amount", amount, amount)]))
    slot = u["slots"]["max_amount"]
    assert (slot["direction"], slot["direction_text"]) == (kind, rule)
    assert u["verdict"] == "ok_so_far"


@pytest.mark.parametrize(
    "sentence,amount",
    [
        ("Refunds of $5,000 are blocked.", "$5,000"),
        ("Refunds over $5,000 or more are blocked.", "$5,000"),
    ],
)
def test_any_other_wording_around_an_amount_needs_review(sentence, amount):
    u = run(sentence, answer("amount_cap", "IssueRefund", [("max_amount", amount, amount)]))
    assert u["slots"]["max_amount"]["direction"] is None
    assert u["verdict"] == "needs_review" and "which way" in u["verdict_reason"]


@pytest.mark.parametrize(
    "sentence,amount",
    [
        ("No refunds under $5.", "$5"),
        ("Refunds over $5,000 are allowed.", "$5,000"),
        ("Refunds of no less than $5,000 are blocked.", "$5,000"),
    ],
)
def test_wording_that_points_the_other_way_needs_review(sentence, amount):
    u = run(sentence, answer("amount_cap", "IssueRefund", [("max_amount", amount, amount)]))
    assert u["verdict"] == "needs_review" and "says otherwise" in u["verdict_reason"]


@pytest.mark.parametrize(
    "sentence,question",
    [
        ("Never refund more than $750 on a single order.", "the total per order"),
        ("No refunds over $750 per order.", "the total per order"),
        ("No refunds over $750 per customer.", "the total per customer"),
        ("No refunds over $750 in total.", "the total across refunds"),
        ("No refunds over $750 combined.", "the total across refunds"),
        ("No refunds over $750 per day.", "the total per day"),
        ("No more than $1,000 may be refunded in a single day.", "the total per day"),
        ("No recent refunds over $750.", "the total over a recent period"),
        ("No refunds over $750 recently.", "the total over a recent period"),
    ],
)
def test_total_wording_asks_which_limit(sentence, question):
    amount = next(a for a in ("$750", "$1,000") if a in sentence)
    u = run(sentence, answer("amount_cap", "IssueRefund", [("max_amount", amount, amount)]))
    assert u["verdict"] == "needs_clarification"
    assert u["limit_scope"]["question"] == f"Does {amount} limit each refund, or {question}?"
    assert u["limit_scope"]["options"] == ["each refund", "a total"]


@pytest.mark.parametrize(
    "sentence",
    ["A single refund may not exceed $750.", "No refunds over $750 per refund.", "Each refund over $750 is blocked."],
)
def test_per_refund_wording_asks_nothing(sentence):
    u = run(sentence, answer("amount_cap", "IssueRefund", [("max_amount", "$750", "$750")]))
    assert u["verdict"] == "ok_so_far" and "limit_scope" not in u


def test_the_word_order_alone_never_makes_an_order_id():
    s = "Never refund more than $750 on a single order."
    u = run(
        s,
        answer(
            "amount_cap",
            "IssueRefund",
            [("max_amount", "$750", "$750"), ("order_id", "order", "order")],
            qualifiers=[("on a single order", "order_id")],
        ),
    )
    assert "order_id" not in stated(u) and u["qualifiers"] == []
    capped = run(
        "Refund up to $40 for this order, subject to approval.",
        answer("capped_with_approval", "IssueRefund", [("max_amount", "$40", "$40"), ("order_id", "order", "order")]),
    )
    assert capped["slots"]["order_id"]["source"] == "missing"


def test_recently_on_an_approval_stays_the_window():
    u = run(
        "Refund up to $40 for order A100 if a manager approved it recently.",
        answer("capped_with_approval", "IssueRefund", [("max_amount", "$40", "$40")]),
    )
    assert "limit_scope" not in u and [v["text"] for v in u["vague_terms"]] == ["recently"]


@pytest.mark.parametrize("word", ["recently", "lately", "soon"])
def test_vague_words_count_even_when_the_model_skips_them(word):
    u = run(
        f"Only refund if a manager approved it {word}.",
        answer("approval_required", "IssueRefund", [("approver_role", "a manager", "manager")]),
    )
    assert [v["text"] for v in u["vague_terms"]] == [word] and u["verdict"] == "needs_clarification"


@pytest.mark.parametrize(
    "span,value,expected",
    [
        ("payments and web", "payments, web", ["payments", "web"]),
        ("payments, search and web", "payments", ["payments", "search", "web"]),
        ("payments, search, or web", "web", ["payments", "search", "web"]),
        ("for payments and the web services", "payments", ["payments", "web"]),
    ],
)
def test_list_spans_split_in_code(span, value, expected):
    sentence = (
        f"Deploys are only allowed {span}." if span.startswith("for") else f"Deploys are only allowed for {span}."
    )
    u = run(sentence, answer("deploy_allowlist", "DeployService", [("services", span, value)]))
    assert stated(u) == {"services": expected}


@pytest.mark.parametrize(
    "words,seconds",
    [("a day", 86400), ("the past day", 86400), ("the last hour", 3600), ("past hour", 3600), ("the last minute", 60)],
)
def test_durations_with_articles_are_one_of_that_unit(words, seconds):
    u = run(
        f"Deploys need tests that passed in {words}.",
        answer("deploy_needs_tests", "DeployService", [("window", words, words)]),
    )
    assert u["slots"]["window"]["seconds"] == seconds


def test_model_refusal_of_a_sentence_code_recognizes_needs_review_with_both_readings():
    u = run("Refunds of $1,200 and up need to be blocked.", answer("unsupported", "none", reason="no tool"))
    assert u["verdict"] == "needs_review" and u["rule_type"] == "amount_cap"
    assert "no tool" in u["readings"]["model"] and u["readings"]["code"] == "amount_cap"
    assert stated(u) == {"max_amount": 1200} and u["slots"]["max_amount"]["direction"] == "inclusive"


def test_readings_that_disagree_need_review():
    u = run("No refunds over $5,000.", answer("approval_required", "IssueRefund"))
    assert u["verdict"] == "needs_review" and u["readings"]["code"] == "amount_cap"


def test_refused_when_both_agree_even_with_refund_words():
    u = run("Only refund customers in California.", answer("unsupported", "none", reason="no"))
    assert u["verdict"] == "invalid" and "readings" not in u


def test_unknown_role_needs_review():
    u = run(
        "Only refund if a supervisor approved it in the last hour.",
        answer("approval_required", "IssueRefund", [("window", "the last hour", "1 hour")]),
    )
    assert u["verdict"] == "needs_review" and "'supervisor' is an unknown role" in u["verdict_reason"]


@pytest.mark.parametrize(
    "sentence",
    ["Refund up to $40 for order A100, subject to approval.", "Refunds need approval from the last hour."],
)
def test_no_role_named_is_not_an_unknown_role(sentence):
    u = run(sentence, answer("approval_required", "IssueRefund"))
    assert "unknown role" not in u["verdict_reason"]


RECORDED = {r["sentence"]: r for r in json.loads(RECORDED_RUNS.read_text())["runs"] if "answer" in r}
CASES = [c for c in live_check.cases() if c["sentence"] in RECORDED]


def test_every_case_has_a_recorded_model_answer():
    assert len(CASES) == len(live_check.cases()) == 18


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["sentence"][:40])
def test_case_matches_on_the_models_recorded_answer(case):
    _, actions = load_schema(config.SCHEMA_PATH)
    u = check(case["sentence"], RECORDED[case["sentence"]]["answer"], actions)
    result = {
        "shown": report.shown(u),
        "vague": [v["text"] for v in u["vague_terms"]],
        "verdict_reason": u["verdict_reason"],
    }
    assert report.matches(result, case["expect"]), u["verdict_reason"]


FORM_VALUES = {
    "refund_cap": lambda f: {"max_amount": f["max"]},
    "prod_allowlist": lambda f: {"services": f["allowed"]},
    "prod_needs_tests": lambda f: {"window": f["requires"]["within"]},
    # Its sentence says "recently", so the form's 1h is not extractable; only the role is.
    "manager_approval": lambda f: {"approver_role": f["requires"]["approver_role"]},
}


@pytest.mark.parametrize("name", FORM_VALUES)
def test_regression_expectations_come_from_the_forms(name):
    form = json.loads((config.REPO / "converter" / "forms" / f"{name}.json").read_text())
    case = next(c for c in live_check.cases() if c["sentence"] == form["sentence"])
    assert case["expect"]["values"] == FORM_VALUES[name](form)


def test_regression_set_covers_every_supported_library_sentence():
    supported = {e["sentence"] for e in examples.LIBRARY if e["type"] in rule_types.supported_ids()}
    assert supported <= {c["sentence"] for c in live_check.cases()}


@pytest.mark.parametrize(
    "sentence,number",
    [
        ("No refunds over $50 for 5 refunds.", "5"),
        ("No refunds over $50 for five customers.", "five"),
        ("No refunds over $50 after 6pm.", "6pm"),
        ("No refunds over $50 or 5k.", "5k"),
        ("No refunds over $50 or $5,00.", "5,00"),
    ],
)
def test_a_number_no_slot_uses_needs_review_and_is_highlighted(sentence, number):
    u = run(sentence, answer("amount_cap", "IssueRefund", [("max_amount", "$50", "$50")]))
    [n] = u["unassigned_numbers"]
    assert n["text"] == number and sentence[n["start"] : n["end"]] == number
    assert u["verdict"] == "needs_review"


def test_a_number_written_with_a_k_is_never_read_as_an_amount():
    u = run("no refunds over 5k", answer("amount_cap", "IssueRefund", [("max_amount", "5k", "5000")]))
    assert u["slots"]["max_amount"]["source"] == "missing"
    assert u["verdict"] == "needs_review"


def test_a_refused_sentence_does_not_report_its_numbers():
    u = run("At most 5 refunds per hour", answer("unsupported", "none", reason="no"))
    assert u["verdict"] == "invalid" and u["unassigned_numbers"] == []


def test_understanding_saves_the_examples_used():
    u = run("Only refund if a manager approved it recently.", answer("unsupported", "none"))
    assert u["examples_used"][0]["id"] == "R2"
    assert 0 < u["examples_used"][0]["score"] < 1
    assert "refund" in u["examples_used"][0]["shared_words"]


@pytest.mark.parametrize(
    "sentence",
    [
        "Only refund if a manager approved it recently.",
        "No refunds over $5,000.",
        "Refund up to $40 for order A100, subject to approval.",
        "A single refund may not exceed $500.",
        "No refunds above $250 for customers in Ohio.",
    ],
)
def test_the_prompt_never_holds_the_input_as_an_example(sentence):
    _, actions = load_schema(config.SCHEMA_PATH)
    prompt = model.build_prompt(sentence.upper().rstrip(".") + " !", actions)
    assert sentence.casefold() not in prompt["system"].casefold()


def test_the_prompt_shows_the_same_examples_as_the_page():
    sentence = "Only refund if a manager approved it recently."
    _, actions = load_schema(config.SCHEMA_PATH)
    prompt = model.build_prompt(sentence, actions)
    assert prompt["examples"] == examples.similar_others(sentence)


def _fake_reply(monkeypatch, reply):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(reply).encode()

    sent = {}

    def urlopen(request, timeout):
        sent.update(json.loads(request.data), timeout=timeout)
        return Response()

    monkeypatch.setattr(model.urllib.request, "urlopen", urlopen)
    return sent


PROMPT = {"system": "s", "user": "u", "format": {}}


def test_every_call_sends_num_ctx_keep_alive_and_timeout(monkeypatch):
    sent = _fake_reply(monkeypatch, {"message": {"content": "{}"}, "prompt_eval_count": 10})
    model.OllamaModel().complete(PROMPT)
    assert sent["options"]["num_ctx"] == config.NUM_CTX
    assert sent["keep_alive"] == config.KEEP_ALIVE
    assert sent["timeout"] == config.CALL_TIMEOUT_SECONDS


def test_prompt_that_fills_the_context_is_an_error(monkeypatch):
    _fake_reply(monkeypatch, {"message": {"content": "{}"}, "prompt_eval_count": config.NUM_CTX})
    with pytest.raises(model.ModelError, match="does not fit num_ctx"):
        model.OllamaModel().complete(PROMPT)


@pytest.mark.parametrize("error", [TimeoutError(), urllib.error.URLError(TimeoutError())])
def test_slow_model_raises_timeout(monkeypatch, error):
    def urlopen(request, timeout):
        raise error

    monkeypatch.setattr(model.urllib.request, "urlopen", urlopen)
    with pytest.raises(model.ModelTimeout):
        model.OllamaModel().complete(PROMPT)


@pytest.mark.parametrize(
    "tokens,load,first,expected",
    [
        (100, 0, True, []),
        (int(0.9 * config.NUM_CTX) + 1, 0, True, ["within 10%"]),
        (100, 5, True, []),
        (100, 5, False, ["reloaded"]),
    ],
)
def test_call_warnings(tokens, load, first, expected):
    warnings = config.call_warnings(tokens, load, first)
    assert len(warnings) == len(expected)
    assert all(e in w for e, w in zip(expected, warnings))
