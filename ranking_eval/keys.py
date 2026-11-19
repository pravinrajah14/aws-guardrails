"""Reading the answer keys (cases.json): what each case expects in each mode, and when two values are the same."""

import json
from pathlib import Path

from app.core.numbers import find_durations
from ranking_eval import structure

CASES_PATH = Path(__file__).parent / "cases.json"
MODES = ("isolated", "realistic")
SENTENCE, GLOSSARY, SAVED, ASK, UNFILLED = "sentence", "glossary", "saved_answer", "ask", "unfilled"


def load(path=CASES_PATH):
    return json.loads(Path(path).read_text(encoding="utf-8"))["cases"]


def per_mode(spec, mode):
    return spec[mode] if isinstance(spec, dict) and set(spec) <= set(MODES) else spec


def source(value_key, mode):
    return per_mode(value_key["source"], mode)


def suggestion(value_key, mode):
    """The suggestion the question should carry; None means none is right."""
    return per_mode(value_key["suggest"], mode) if "suggest" in value_key else value_key["value"]


def needed_questions(case, mode):
    return sorted(f for f, vk in case["values"].items() if source(vk, mode) == ASK)


def _seconds(value):
    found = find_durations(str(value)) or find_durations("".join(str(value).split()))
    return found[0]["seconds"] if len(found) == 1 else None


def normalize(field, value):
    if value is None:
        return None
    if field == "window":
        return _seconds(value)
    if field == "services" or isinstance(value, list):
        return tuple(sorted(str(v).strip().casefold() for v in value))
    if field == "max_amount":
        try:
            return int(value)
        except (TypeError, ValueError):
            return str(value)
    return str(value).strip().casefold()


def same_value(field, a, b):
    return normalize(field, a) == normalize(field, b)


def raw_answer(field, value):
    """The key's value as a manager would type it into the tool's question."""
    if isinstance(value, list):
        return ", ".join(value)
    return str(value)


def evidence_relevant(case, field, library, mode):
    """Library entries that would be right evidence for this field: same action and prior action, and they set
    the value the key says the question should suggest. None when the key says no suggestion is right."""
    expect = case["expect"]
    frame = {"action": expect.get("action"), "prior": expect.get("prior")}
    value_key = case["values"].get(field)
    want = suggestion(value_key, mode) if value_key else None
    if want is None:
        return []
    out = []
    for e in library:
        profile = structure.library_profile(e["id"])
        if (
            profile
            and structure.same_frame(frame, profile)
            and field in profile["sets"]
            and same_value(field, profile["sets"][field], want)
        ):
            out.append(e["id"])
    return out
