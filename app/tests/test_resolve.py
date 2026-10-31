"""Filling gaps from saved answers, the glossary and examples, driven by a fake model (no Ollama needed)."""

import pytest

from app.core import answers, config, examples
from app.core import resolve as resolver
from app.core.resolve import AnswerError, resolve

APPROVAL = "Only refund if a manager approved it recently."
CAPPED = "Refund up to $40 for order A100, subject to approval."


class FakeModel:
    def __init__(self, answer):
        self.answer = answer

    def info(self):
        return {"name": "fake", "temperature": 0, "seed": 0}

    def complete(self, prompt):
        return self.answer


def answer(rule_type, action, slots=(), vague=(), qualifiers=()):
    return {
        "rule_type": rule_type,
        "action": action,
        "unsupported_reason": "",
        "slots": [{"slot": s, "span": sp, "value": v} for s, sp, v in slots],
        "vague_terms": [{"span": sp, "slot": s} for sp, s in vague],
        "qualifiers": [{"span": sp, "field": f} for sp, f in qualifiers],
    }


APPROVAL_MODEL = FakeModel(
    answer("approval_required", "IssueRefund", [("approver_role", "a manager", "manager")], [("recently", "window")])
)
CAPPED_MODEL = FakeModel(
    answer(
        "capped_with_approval",
        "IssueRefund",
        [("max_amount", "$40", "$40"), ("order_id", "order A100", "A100")],
        [("subject to approval", "window")],
    )
)
SCOPE_SENTENCE = "Never refund more than $750 on a single order."
SCOPE_MODEL = FakeModel(answer("amount_cap", "IssueRefund", [("max_amount", "$750", "$750")]))


@pytest.fixture(autouse=True)
def fresh_answers(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ANSWERS_PATH", tmp_path / "data" / "answers.sqlite")
    monkeypatch.setattr(config, "ANSWERED_BY", "tester")


def test_first_time_proposes_a_window_with_its_examples_and_weights():
    result = resolve(APPROVAL, APPROVAL_MODEL)
    assert result["understanding"]["verdict"] == "needs_clarification"
    [q] = result["questions"]
    assert (q["field"], q["term"], q["proposal"]) == ("window", "recently", "1h")
    assert [(e["id"], e["value"], e["weight"]) for e in q["evidence"]] == [("R4", "1h", 1)]
    assert result["understanding"]["slots"]["window"]["source"] == "missing"


def test_with_no_other_example_stating_a_window_it_asks_with_no_proposal(monkeypatch):
    library = [e for e in examples.LIBRARY if e["id"] != "R4"]
    monkeypatch.setattr(examples, "LIBRARY", library)
    [q] = resolve(APPROVAL, APPROVAL_MODEL)["questions"]
    assert (q["proposal"], q["evidence"]) == (None, [])


def test_context_weights_leave_out_the_sentence_itself():
    weighted = resolve(APPROVAL, APPROVAL_MODEL)["context"]["examples"]
    assert weighted and APPROVAL not in {e["sentence"] for e in weighted}


def test_answering_saves_it_and_the_next_run_fills_from_it():
    resolve(APPROVAL, APPROVAL_MODEL, {"window": "2 hours"})
    result = resolve(APPROVAL, APPROVAL_MODEL)
    u = result["understanding"]
    assert result["questions"] == [] and u["verdict"] == "ok_so_far"
    slot = u["slots"]["window"]
    assert (slot["value"], slot["seconds"], slot["source"]) == ("2h", 7200, "saved_answer")
    assert slot["evidence"].startswith("from saved answer (tester, ")


def test_a_different_later_answer_flags_a_conflict_until_someone_picks():
    resolve(APPROVAL, APPROVAL_MODEL, {"window": "1h"})
    result = resolve(APPROVAL, APPROVAL_MODEL, {"window": "2h"})
    u = result["understanding"]
    assert u["verdict"] == "needs_review" and "disagree" in u["verdict_reason"]
    assert [r["value"] for r in u["conflicts"][0]["rows"]] == ["1h", "2h"]
    assert u["slots"]["window"]["source"] == "missing"
    assert resolve(APPROVAL, APPROVAL_MODEL)["understanding"]["verdict"] == "needs_review"
    picked = resolve(APPROVAL, APPROVAL_MODEL, picks={"window": "2h"})["understanding"]
    assert picked["verdict"] == "ok_so_far" and picked["slots"]["window"]["value"] == "2h"


def test_same_answer_twice_is_no_conflict():
    resolve(APPROVAL, APPROVAL_MODEL, {"window": "1h"})
    resolve(APPROVAL, APPROVAL_MODEL, {"window": "60 minutes"})
    assert len(answers.everything()) == 1


def test_saved_answer_only_counts_for_the_same_rule_type_field_and_term():
    resolve(APPROVAL, APPROVAL_MODEL, {"window": "1h"})
    assert resolve(CAPPED, CAPPED_MODEL)["understanding"]["verdict"] == "needs_clarification"
    lately = FakeModel(
        answer("approval_required", "IssueRefund", [("approver_role", "a manager", "manager")], [("lately", "window")])
    )
    assert resolve("Only refund if a manager approved it lately.", lately)["questions"]


def test_no_examples_of_the_type_asks_with_no_proposal():
    [q] = resolve(CAPPED, CAPPED_MODEL)["questions"]
    assert (q["field"], q["proposal"], q["evidence"]) == ("window", None, [])


def test_disagreeing_examples_are_all_shown_and_there_is_no_proposal(monkeypatch):
    library = [
        {
            "id": "x",
            "source": "a",
            "sentence": "Refund with manager approval in the last hour.",
            "type": "approval_required",
        },
        {"id": "y", "source": "b", "sentence": "Refund with manager approval in 2 hours.", "type": "approval_required"},
    ]
    monkeypatch.setattr(examples, "LIBRARY", library)
    [q] = resolve(APPROVAL, APPROVAL_MODEL)["questions"]
    assert q["proposal"] is None and q["examples_disagree"]
    assert {(e["id"], e["value"]) for e in q["evidence"]} == {("x", "1h"), ("y", "2h")}
    assert sum(e["weight"] for e in q["evidence"]) == pytest.approx(1)


def test_unstated_approver_is_filled_from_the_glossary_with_its_source():
    resolve(CAPPED, CAPPED_MODEL, {"window": "1h"})
    u = resolve(CAPPED, CAPPED_MODEL)["understanding"]
    slot = u["slots"]["approver_role"]
    assert (slot["value"], slot["evidence"]) == ("manager", "from glossary (kylie-test-bed-upload/tools.py)")
    assert u["verdict"] == "ok_so_far"


def test_production_on_a_deploy_cites_the_glossary_and_stays_review_on_a_refund():
    deploy = "Production deploys are only allowed for web."
    model = FakeModel(answer("deploy_allowlist", "DeployService", [("services", "web", "web")]))
    result = resolve(deploy, model)
    [q] = result["understanding"]["qualifiers"]
    assert (q["value"], q["glossary_file"]) == ("prod", "converter/forms/prod_allowlist.json")
    assert {"text": "Production", "field": "env", "value": "prod", "source": q["glossary_file"]} in result["context"][
        "glossary"
    ]
    refund = "No production refunds over $500."
    u = resolve(refund, FakeModel(answer("amount_cap", "IssueRefund", [("max_amount", "$500", "$500")])))[
        "understanding"
    ]
    assert u["verdict"] == "needs_review" and "value" not in u["qualifiers"][0]


def test_each_refund_answer_makes_it_a_cap_and_is_saved():
    [q] = resolve(SCOPE_SENTENCE, SCOPE_MODEL)["questions"]
    assert q["options"] == ["each refund", "a total"] and q["proposal"] is None
    u = resolve(SCOPE_SENTENCE, SCOPE_MODEL, {"limit_scope": "each refund"})["understanding"]
    assert u["verdict"] == "ok_so_far" and u["rule_type"] == "amount_cap"
    assert resolve(SCOPE_SENTENCE, SCOPE_MODEL)["understanding"]["verdict"] == "ok_so_far"


def test_a_total_answer_is_refused_for_now():
    u = resolve(SCOPE_SENTENCE, SCOPE_MODEL, {"limit_scope": "a total"})["understanding"]
    assert u["verdict"] == "invalid" and "can't be enforced" in u["verdict_reason"]


@pytest.mark.parametrize(
    "field,raw",
    [("window", "soon"), ("approver_role", "director"), ("max_amount", "5k"), ("limit_scope", "both")],
)
def test_answers_that_cannot_be_used_are_refused(field, raw):
    sentence, model = {
        "window": (APPROVAL, APPROVAL_MODEL),
        "limit_scope": (SCOPE_SENTENCE, SCOPE_MODEL),
    }.get(field, (CAPPED, FakeModel(answer("capped_with_approval", "IssueRefund", [("order_id", "A100", "A100")]))))
    with pytest.raises(AnswerError):
        resolve(sentence, model, {field: raw})
    assert answers.everything() == []


def test_an_answer_to_no_question_is_refused():
    with pytest.raises(AnswerError, match="no open question"):
        resolve("No refunds over $5,000.", SCOPE_MODEL, {"window": "1h"})


def test_order_data_never_reaches_the_answers_file():
    resolve(CAPPED, CAPPED_MODEL, {"window": "1h"})
    [row] = answers.everything()
    assert "A100" not in row["sentence"] and "[order id]" in row["sentence"]
    model = FakeModel(answer("capped_with_approval", "IssueRefund", [("max_amount", "$40", "$40")]))
    sentence = "Refund up to $40, subject to approval."
    u = resolve(sentence, model, {"order_id": "A100", "window": "1h"})["understanding"]
    assert u["slots"]["order_id"]["value"] == "A100"
    assert all("A100" not in str(row) for row in answers.everything())


def test_only_the_agreed_columns_are_stored():
    resolve(APPROVAL, APPROVAL_MODEL, {"window": "1h"})
    [row] = answers.everything()
    assert set(row) == {"id", "rule_type", "field", "term", "value", "answered_by", "answered_at", "sentence"}


def test_context_lists_schema_glossary_examples_and_saved_answers_with_sources():
    resolve(APPROVAL, APPROVAL_MODEL, {"window": "1h"})
    context = resolve(APPROVAL, APPROVAL_MODEL)["context"]
    assert {t["tool"] for t in context["schema"]} == {"IssueRefund", "RequestApproval"}
    assert all(t["source"].endswith("schema.cedarschema") for t in context["schema"])
    assert context["glossary"][0]["source"] == "kylie-test-bed-upload/tools.py"
    assert len(context["examples"]) == 3 and all(e["source"] for e in context["examples"])
    assert sum(e["weight"] for e in context["examples"]) == pytest.approx(1)
    assert context["saved_answers"][0]["value"] == "1h"


def test_highlights_mark_each_source_without_overlap():
    first = resolve(APPROVAL, APPROVAL_MODEL)["highlights"]
    assert {(h["text"], h["source"]) for h in first} == {("recently", "asked"), ("manager", "stated")}
    resolve(APPROVAL, APPROVAL_MODEL, {"window": "1h"})
    later = resolve(APPROVAL, APPROVAL_MODEL)["highlights"]
    assert ("recently", "filled") in {(h["text"], h["source"]) for h in later}
    review = resolve("no refunds over 5k", FakeModel(answer("amount_cap", "IssueRefund")))["highlights"]
    assert ("5k", "review") in {(h["text"], h["source"]) for h in review}


def test_refused_sentence_has_no_questions_or_marks():
    model = FakeModel(answer("unsupported", "none"))
    result = resolve("Only email customers who opted in", model)
    assert result["understanding"]["verdict"] == "invalid" and result["questions"] == []


def test_answers_cli_lists_exports_and_deletes(tmp_path, capsys):
    resolve(APPROVAL, APPROVAL_MODEL, {"window": "1h"})
    answers.main(["list"])
    assert "window 'recently' = '1h' (tester," in capsys.readouterr().out
    out = tmp_path / "export.json"
    answers.main(["export", "--out", str(out)])
    assert '"value": "1h"' in out.read_text()
    answers.main(["delete", "1"])
    assert answers.everything() == []
    resolve(APPROVAL, APPROVAL_MODEL, {"window": "1h"})
    answers.main(["delete", "--all"])
    assert answers.everything() == []


def test_resolve_cli_prints_the_proposal(capsys, monkeypatch):
    monkeypatch.setattr(resolver, "OllamaModel", lambda: APPROVAL_MODEL)
    resolver.main([APPROVAL])
    out = capsys.readouterr().out
    assert "proposed: 1h (confirm with --answer window=1h)" in out and "weight" in out
