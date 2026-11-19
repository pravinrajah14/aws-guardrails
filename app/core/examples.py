"""The example library, and picking the examples closest to a sentence.

Two searches use the library, each with its own score:
- the example score picks the past rules shown to the model (most_similar);
- the evidence score picks the past rules that may suggest a missing value (stating).
Both come from the active ranking, plain word overlap unless use_ranking swaps it (the ranking
evaluation in ranking_eval/ does). Keeping them apart lets one change without the other.
"""

import json
import re
from contextlib import contextmanager
from pathlib import Path

from app.core import glossary, rule_types
from app.core.numbers import amount_json, find_amounts, find_durations, window

LIBRARY = json.loads((Path(__file__).parent / "data" / "examples.json").read_text(encoding="utf-8"))
BASELINE = "baseline"
PICK_COUNT = 3
WEIGHT_DIGITS = 3
NOT_IN_SENTENCE = "chosen by a person for that rule, not stated in its sentence"
_NOTE_KEYS = ("note", "approximation", "other_comments")
_WORD = re.compile(r"[a-z]+")
_PUNCTUATION = re.compile(r"[^\w\s]")
_STOP_WORDS = frozenset(
    {"a", "an", "the", "of", "to", "in", "on", "for", "at", "by", "as", "is", "are", "be", "it", "its", "and", "or"}
    | {"that", "this", "s", "from", "with", "their"}
)


def _stem(word):
    return word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") else word


def words(text):
    return {_stem(w) for w in _WORD.findall(text.lower())} - _STOP_WORDS


def word_spans(text, wanted):
    """[start, end] of each word in text that is one of the wanted words once stemmed."""
    return [[m.start(), m.end()] for m in _WORD.finditer(text.lower()) if _stem(m.group()) in wanted]


def _sentence_key(sentence):
    return _PUNCTUATION.sub("", sentence).casefold().split()


def same_sentence(a, b):
    return _sentence_key(a) == _sentence_key(b)


def overlap(a, b):
    """Shared words / all words of both (Jaccard), and the shared words."""
    ours, theirs = words(a), words(b)
    shared = ours & theirs
    return (len(shared) / len(ours | theirs) if shared else 0.0), sorted(shared)


class WordOverlap:
    """Today's ranking: both scores are plain word overlap, and evidence comes from the same rule type."""

    name = "jaccard"

    def example_scores(self, sentence, candidates):
        """(score, example) for the candidates worth showing the model; any order."""
        scored = []
        for example in candidates:
            score, shared = overlap(sentence, example["sentence"])
            if shared:
                scored.append((score, example))
        return scored

    def in_evidence_pool(self, example, query):
        return example["type"] == query["rule_type"]

    def evidence_scores(self, sentence, candidates, query):
        """(score, example) for evidence candidates that already state the slot; a score of 0 drops one."""
        return [(overlap(sentence, example["sentence"])[0], example) for example in candidates]


_active = WordOverlap()


@contextmanager
def use_ranking(ranking):
    """Rank with this object instead of word overlap, for the length of the block."""
    global _active
    before, _active = _active, ranking
    try:
        yield ranking
    finally:
        _active = before


def active_ranking():
    return _active


def most_similar(sentence, count=PICK_COUNT):
    """The closest examples with their score, best first, no repeated sentences."""
    candidates = [e for e in LIBRARY if e["type"] != BASELINE]
    scored = [
        (score, overlap(sentence, e["sentence"])[1], e) for score, e in _active.example_scores(sentence, candidates)
    ]
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


def is_supported(example):
    return example["type"] in rule_types.supported_ids()


def unsupported_reason(example):
    """Why the converter can't build this example's rule; None when it can."""
    if is_supported(example):
        return None
    return example["reason"] or example["type"].partition(": ")[2] or None


def policy_of(example_id):
    """The example's own Dogwood text, or its rule form for a converter form, and the notes kept with it."""
    example = next(e for e in LIBRARY if e["id"] == example_id)
    form = example.get("form")
    return {
        "policy": json.dumps(form, indent=2) if form else example.get("dogwood"),
        "policy_kind": "rule form" if form else "Dogwood",
        "notes": [example[k] for k in _NOTE_KEYS if example.get(k)],
    }


def similar_others(sentence, count=PICK_COUNT):
    """most_similar without the sentence itself: an example can't be evidence for its own wording."""
    others = [e for e in most_similar(sentence, len(LIBRARY)) if not same_sentence(e["sentence"], sentence)]
    return others[:count]


def with_weights(picked):
    """The same examples with weights: their scores scaled to sum to 1."""
    total = sum(e["score"] for e in picked)
    if not total:
        return []
    weighted = [{**e, "weight": round(e["score"] / total, WEIGHT_DIGITS)} for e in picked]
    weighted[-1]["weight"] = round(1 - sum(e["weight"] for e in weighted[:-1]), WEIGHT_DIGITS)
    return weighted


def _value_in_form(form, slot):
    requires = form.get("requires", {})
    return {
        "window": requires.get("within"),
        "approver_role": requires.get("approver_role"),
        "max_amount": form.get("max"),
        "services": form.get("allowed"),
    }.get(slot)


def _value_in_sentence(sentence, slot):
    if slot == "window":
        found = find_durations(sentence)
        return window(found[0]["seconds"]) if len(found) == 1 else None
    if slot == "max_amount":
        found = find_amounts(sentence)
        return amount_json(found[0]["value"]) if len(found) == 1 else None
    if slot == "approver_role":
        return next((h["value"] for h in glossary.find_terms(sentence) if h["field"] == slot), None)
    return None


def _stated_in_sentence(sentence, slot, value):
    if isinstance(value, list):
        return all(item.casefold() in sentence.casefold() for item in value)
    return _value_in_sentence(sentence, slot) == value


def stating(sentence, type_id, slot, count=PICK_COUNT, understanding=None):
    """Other library examples in the evidence pool that state this slot, closest first, with their value and weight.

    The pool is the active ranking's: today, the same rule type.
    """
    query = {"sentence": sentence, "rule_type": type_id, "slot": slot, "understanding": understanding}
    stated, seen = [], set()
    # Forms first, so a sentence that also has a form takes its value from the form.
    for example in sorted(LIBRARY, key=lambda e: "form" not in e):
        if example["sentence"] in seen or same_sentence(example["sentence"], sentence):
            continue
        if not _active.in_evidence_pool(example, query):
            continue
        form = example.get("form")
        value = _value_in_form(form, slot) if form else _value_in_sentence(example["sentence"], slot)
        if value is not None:
            seen.add(example["sentence"])
            stated.append((example, value))
    values = {id(e): v for e, v in stated}
    found = {}
    for score, example in _active.evidence_scores(sentence, [e for e, _ in stated], query):
        value = values[id(example)]
        if score > 0:
            found[example["sentence"]] = {
                "id": example["id"],
                "source": example["source"],
                "sentence": example["sentence"],
                "value": value,
                "note": None if _stated_in_sentence(example["sentence"], slot, value) else NOT_IN_SENTENCE,
                "score": round(score, 3),
            }
    closest = sorted(found.values(), key=lambda e: -e["score"])[:count]
    return with_weights(closest)
