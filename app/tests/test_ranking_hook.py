"""Splitting the library search into an example score and an evidence score changes nothing by default.

The two functions below are frozen copies of most_similar and stating from before the split
(commit ffa8d8b). The live functions, with no ranking swapped in, must give exactly what they gave.
"""

import json
from pathlib import Path

import pytest

from app.core import examples, rule_types
from app.core.examples import LIBRARY, PICK_COUNT, words

CASES = Path(__file__).parent / "understanding_cases.json"
SLOTS = ["window", "approver_role", "max_amount", "services", "order_id"]


def old_most_similar(sentence, count=PICK_COUNT):
    ours = words(sentence)
    scored = []
    for example in LIBRARY:
        theirs = words(example["sentence"])
        shared = ours & theirs
        if example["type"] != examples.BASELINE and shared:
            scored.append((len(shared) / len(ours | theirs), sorted(shared), example))
    picked, seen = [], set()
    for score, shared, example in sorted(scored, key=lambda s: -s[0]):
        if example["sentence"] not in seen and len(picked) < count:
            seen.add(example["sentence"])
            picked.append(
                {
                    "id": example["id"],
                    "source": example["source"],
                    "type": example["type"],
                    "sentence": example["sentence"],
                    "reason": example.get("reason"),
                    "score": round(score, 3),
                    "shared_words": shared,
                }
            )
    return picked


def old_stating(sentence, type_id, slot, count=PICK_COUNT):
    ours = words(sentence)
    found = {}
    for example in sorted(LIBRARY, key=lambda e: "form" not in e):
        if (
            example["type"] != type_id
            or example["sentence"] in found
            or examples.same_sentence(example["sentence"], sentence)
        ):
            continue
        form = example.get("form")
        value = examples._value_in_form(form, slot) if form else examples._value_in_sentence(example["sentence"], slot)
        theirs = words(example["sentence"])
        score = len(ours & theirs) / len(ours | theirs)
        if value is not None and score > 0:
            found[example["sentence"]] = {
                "id": example["id"],
                "source": example["source"],
                "sentence": example["sentence"],
                "value": value,
                "note": None
                if examples._stated_in_sentence(example["sentence"], slot, value)
                else examples.NOT_IN_SENTENCE,
                "score": round(score, 3),
            }
    closest = sorted(found.values(), key=lambda e: -e["score"])[:count]
    return examples.with_weights(closest)


def sentences():
    held_out = [c["sentence"] for c in json.loads(CASES.read_text(encoding="utf-8"))]
    extra = [
        "Only refund if a manager approved it recently.",
        "Refunds need a manager's sign-off, given recently.",
        "Deploys to prod need passing tests from recently.",
        "Refunds over some amount are blocked.",
        "Only some services may go to production.",
        "",
    ]
    return sorted({e["sentence"] for e in LIBRARY} | set(held_out) | set(extra))


@pytest.mark.parametrize("sentence", sentences())
def test_example_score_unchanged(sentence):
    assert examples.most_similar(sentence) == old_most_similar(sentence)
    assert examples.most_similar(sentence, len(LIBRARY)) == old_most_similar(sentence, len(LIBRARY))


@pytest.mark.parametrize("sentence", sentences())
def test_evidence_score_unchanged(sentence):
    for type_id in rule_types.supported_ids():
        for slot in SLOTS:
            assert examples.stating(sentence, type_id, slot) == old_stating(sentence, type_id, slot)


def test_use_ranking_swaps_and_restores():
    class Nothing(examples.WordOverlap):
        def example_scores(self, sentence, candidates):
            return []

    before = examples.active_ranking()
    with examples.use_ranking(Nothing()):
        assert examples.most_similar("A single refund may not exceed $500.") == []
    assert examples.active_ranking() is before
    assert examples.most_similar("A single refund may not exceed $500.")
