"""Known words in a rule sentence, found by code so that they never depend on the model."""

import json
import re
from pathlib import Path

GLOSSARY = json.loads((Path(__file__).parent / "data" / "glossary.json").read_text(encoding="utf-8"))


def _word_regex(word):
    return re.compile(rf"(?<!\w){re.escape(word).replace(chr(39), '[’' + chr(39) + ']')}(?!\w)", re.IGNORECASE)


# Longest first, so "manager's" is one term and not "manager" plus a leftover.
_WORDS = sorted(
    ((word, term) for term in GLOSSARY["terms"] for word in term["words"]),
    key=lambda pair: -len(pair[0]),
)
_WORD_MATCHERS = [(_word_regex(word), term) for word, term in _WORDS]
_PATTERN_MATCHERS = [(re.compile(rf"(?<!\w){p['regex']}(?!\w)", re.IGNORECASE), p) for p in GLOSSARY["patterns"]]


def _hit(match, field, value, source):
    return {
        "text": match.group(0),
        "start": match.start(),
        "end": match.end(),
        "field": field,
        "value": value,
        "source": source,
    }


def find_terms(sentence):
    """Every glossary term and order id in the sentence, in reading order, without overlaps."""
    hits = []
    for regex, term in _WORD_MATCHERS:
        for m in regex.finditer(sentence):
            if not any(h["start"] < m.end() and m.start() < h["end"] for h in hits):
                hits.append(_hit(m, term["field"], term["value"], term["source"]))
    for regex, pattern in _PATTERN_MATCHERS:
        hits += [_hit(m, pattern["field"], m.group(0), pattern["source"]) for m in regex.finditer(sentence)]
    return sorted(hits, key=lambda h: h["start"])


def is_order_id(text):
    return any(regex.fullmatch(text) for regex, pattern in _PATTERN_MATCHERS if pattern["field"] == "order_id")


def known_values(field):
    return sorted({term["value"] for term in GLOSSARY["terms"] if term["field"] == field})


def gap_entry(field, term):
    """The cited entry that gives a value to a slot the sentence leaves out or leaves vague, or None."""
    return next((g for g in GLOSSARY["gaps"] if g["field"] == field and g["term"] == term), None)


def mask_order_ids(text, mask):
    for regex, pattern in _PATTERN_MATCHERS:
        if pattern["field"] == "order_id":
            text = regex.sub(mask, text)
    return text
