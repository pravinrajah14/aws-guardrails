"""The glossary and example library data files, checked against the files they cite."""

import json

import pytest

from app.core import config, examples, glossary, rule_types
from app.web import server

FORMS_DIR = config.REPO / "converter" / "forms"
POLICIES = (config.REPO / "4_rules" / "policies.dw").read_text(encoding="utf-8")
RULE_IDS = {"R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8", "R9", "R10"}
BY_ID = {e["id"]: e for e in examples.LIBRARY}
NO_FORM = "unsupported: no converter form kind"


def _comments_removed(text):
    return "\n".join(line.removeprefix("//").strip() for line in text.splitlines())


def test_every_rule_in_the_policy_file_is_in_the_library():
    assert RULE_IDS <= set(BY_ID)
    for rule_id in ("allow_lookup", "allow_approval_requests", "allow_tests", "allow_rollback"):
        assert f'@id("{rule_id}")' in BY_ID[rule_id]["dogwood"]


@pytest.mark.parametrize("example", [e for e in examples.LIBRARY if e["source"].endswith(".dw")], ids=lambda e: e["id"])
def test_library_text_is_jacks_own_words(example):
    # The file holds each rule and its sentence. Refused rules and the longer notes live only in the library.
    flat = " ".join(_comments_removed(POLICIES).split())
    refused = example["type"] == "unsupported"
    assert (" ".join(example["sentence"].split()) in flat) != refused
    if example["dogwood"] and not refused:
        assert " ".join(example["dogwood"].split()) in flat


@pytest.mark.parametrize(
    "rule_id,tag",
    [
        ("R1", "amount_cap"),
        ("R3", "deploy_allowlist"),
        ("R4", "approval_required"),
        ("R6", "deploy_needs_tests"),
        ("R8", NO_FORM),
        ("R9", NO_FORM),
        ("R10", NO_FORM),
        ("R2", "unsupported"),
        ("R5", "unsupported"),
        ("R7", "unsupported"),
    ],
)
def test_type_tags(rule_id, tag):
    assert BY_ID[rule_id]["type"] == tag


@pytest.mark.parametrize("rule_id", ["R2", "R5", "R7"])
def test_refused_rules_keep_jacks_reason(rule_id):
    assert BY_ID[rule_id]["reason"].startswith("Needs")


def test_forms_match_the_converter_files():
    forms = [e for e in examples.LIBRARY if "form" in e]
    assert len(forms) == len(list((config.REPO / "converter" / "forms").glob("*.json")))
    for e in forms:
        on_disk = json.loads((config.REPO / e["source"]).read_text(encoding="utf-8"))
        assert e["form"] == on_disk and e["sentence"] == on_disk["sentence"]


def test_most_similar_ranks_by_word_overlap_without_repeats():
    picked = examples.most_similar("Production deploys are only allowed for payments, search and web.")
    assert picked[0]["id"] == "R3" and picked[0]["score"] == 1.0
    assert len({e["sentence"] for e in picked}) == len(picked) == examples.PICK_COUNT
    assert [e["score"] for e in picked] == sorted((e["score"] for e in picked), reverse=True)


def test_most_similar_never_picks_baseline_rules_and_can_find_nothing():
    assert examples.most_similar("xylophone") == []
    assert all(e["type"] != examples.BASELINE for e in examples.most_similar("tools are always allowed"))


def test_rule_words_do_not_count_and_rare_words_count_more():
    assert examples.words("No, never, only if you may not, unless you must or can or need.") == {"you"}
    [top, *_] = examples.most_similar("Refunds may not exceed the approval.")
    assert top["id"] == "R1" and top["shared_words"] == ["exceed", "refund"]


DEMO_TYPES = {
    "No refunds over $5,000.": {rule_types.AMOUNT_CAP},
    "Only refund if a manager approved it recently.": {rule_types.APPROVAL_REQUIRED},
    "No production refunds over $500.50.": {rule_types.AMOUNT_CAP},
    "Refund up to $40 for order A100, subject to approval.": {
        rule_types.CAPPED_WITH_APPROVAL,
        rule_types.AMOUNT_CAP,
        rule_types.APPROVAL_REQUIRED,
    },
    "Only email customers who opted in.": None,
}


@pytest.mark.parametrize("sentence", server.DEMO_SENTENCES)
def test_demo_sentences_rank_supported_examples_first_and_their_own_type_in_the_top_three(sentence):
    picked = examples.similar_others(sentence)
    supported = [examples.is_supported(e) for e in picked]
    assert supported == sorted(supported, reverse=True)
    if DEMO_TYPES[sentence]:
        assert DEMO_TYPES[sentence] & {e["type"] for e in picked}


def test_unsupported_examples_only_fill_in_when_few_supported_ones_share_a_word():
    assert all(examples.is_supported(e) for e in examples.similar_others("No refunds over $5,000."))
    assert [e["id"] for e in examples.similar_others("Only email customers who opted in.")] == ["R7"]


SLOTS = ("window", "approver_role", "max_amount", "services")
FORM_SENTENCES = [json.loads(p.read_text(encoding="utf-8"))["sentence"] for p in FORMS_DIR.glob("*.json")]


def _spelled_differently(sentence):
    return sentence.upper().rstrip(".") + " !"


@pytest.mark.parametrize("sentence", [*server.DEMO_SENTENCES, *FORM_SENTENCES])
@pytest.mark.parametrize("spell", [str, _spelled_differently], ids=["as written", "case and punctuation"])
def test_an_example_matching_the_input_is_never_evidence_for_it(sentence, spell):
    text = spell(sentence)
    evidence = examples.similar_others(text, len(examples.LIBRARY))
    types = {e["type"] for e in examples.LIBRARY}
    evidence += [e for t in types for slot in SLOTS for e in examples.stating(text, t, slot, len(examples.LIBRARY))]
    assert sentence not in {e["sentence"] for e in evidence}


def test_every_copy_of_a_matching_example_is_left_out():
    sentence = "A single refund may not exceed $500."
    assert {"R1", "form_refund_cap"} == {e["id"] for e in examples.LIBRARY if e["sentence"] == sentence}
    assert not {"R1", "form_refund_cap"} & {e["id"] for e in examples.similar_others(sentence)}


def test_the_recently_sentence_is_proposed_from_jacks_r4_only():
    sentence = "Only refund if a manager approved it recently."
    assert [(e["id"], e["value"]) for e in examples.stating(sentence, "approval_required", "window")] == [("R4", "1h")]


def test_a_value_its_sentence_does_not_state_is_labelled_and_keeps_its_weight():
    evidence = examples.stating("Refund only if a manager approved it lately.", "approval_required", "window")
    by_id = {e["id"]: e for e in evidence}
    assert by_id["form_manager_approval"]["value"] == "1h"
    assert by_id["form_manager_approval"]["note"] == examples.NOT_IN_SENTENCE
    assert by_id["R4"]["note"] is None
    total = sum(e["score"] for e in evidence)
    assert by_id["form_manager_approval"]["weight"] == pytest.approx(
        by_id["form_manager_approval"]["score"] / total, abs=0.001
    )


@pytest.mark.parametrize(
    "type_id, slot, example_id",
    [
        ("amount_cap", "max_amount", "form_refund_cap"),
        ("approval_required", "approver_role", "form_manager_approval"),
        ("deploy_allowlist", "services", "form_prod_allowlist"),
        ("deploy_needs_tests", "window", "form_prod_needs_tests"),
    ],
)
def test_a_value_its_sentence_states_has_no_label(type_id, slot, example_id):
    evidence = examples.stating(
        "refund production deploy version tests manager services", type_id, slot, len(examples.LIBRARY)
    )
    assert {e["id"]: e["note"] for e in evidence}[example_id] is None


@pytest.mark.parametrize("term", glossary.GLOSSARY["terms"], ids=lambda t: t["words"][0])
def test_glossary_values_appear_in_their_source_file(term):
    assert f'"{term["value"]}"' in (config.REPO / term["source"]).read_text(encoding="utf-8")


def test_order_ids_in_the_store_match_the_glossary_pattern():
    assert [h["text"] for h in glossary.find_terms("order A100 or a200 but not BA100 or A100x")] == ["A100", "a200"]


def test_find_terms_keeps_the_longest_word():
    [hit] = glossary.find_terms("A manager's approval")
    assert (hit["text"], hit["value"]) == ("manager's", "manager")
