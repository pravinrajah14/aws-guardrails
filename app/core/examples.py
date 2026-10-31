"""The example library, and picking the examples closest to a sentence by plain word overlap."""

import json
import re
from pathlib import Path

from app.core import glossary
from app.core.numbers import amount_json, find_amounts, find_durations, window

LIBRARY = json.loads((Path(__file__).parent / "data" / "examples.json").read_text(encoding="utf-8"))
BASELINE = "baseline"
PICK_COUNT = 3
WEIGHT_DIGITS = 3
NOT_IN_SENTENCE = "chosen by a person for that rule, not stated in its sentence"
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


def _sentence_key(sentence):
    return _PUNCTUATION.sub("", sentence).casefold().split()


def same_sentence(a, b):
    return _sentence_key(a) == _sentence_key(b)


def most_similar(sentence, count=PICK_COUNT):
    """The closest examples with their score (shared words / all words of both), best first, no repeated sentences."""
    ours = words(sentence)
    scored = []
    for example in LIBRARY:
        theirs = words(example["sentence"])
        shared = ours & theirs
        if example["type"] != BASELINE and shared:
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


def stating(sentence, type_id, slot, count=PICK_COUNT):
    """Other library examples of this rule type that state this slot, closest first, with their value and weight."""
    ours = words(sentence)
    found = {}
    # Forms first, so a sentence that also has a form takes its value from the form.
    for example in sorted(LIBRARY, key=lambda e: "form" not in e):
        if example["type"] != type_id or example["sentence"] in found or same_sentence(example["sentence"], sentence):
            continue
        form = example.get("form")
        value = _value_in_form(form, slot) if form else _value_in_sentence(example["sentence"], slot)
        theirs = words(example["sentence"])
        score = len(ours & theirs) / len(ours | theirs)
        if value is not None and score > 0:
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
