"""Numbers and comparison words, read from text with regex. The model never supplies them."""

import re
from decimal import Decimal

MINUTE, HOUR, DAY = 60, 3600, 86400

_NUM = r"(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?"
_END = r"(?!\w)(?![.,]\d)"
AMOUNT = re.compile(
    rf"\$\s?{_NUM}{_END}"
    rf"|(?<![\w.,$]){_NUM}\s*(?:dollars?|usd|bucks)\b",
    re.IGNORECASE,
)

_WORDS = {
    "a": 1,
    "an": 1,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "twelve": 12,
}
_UNITS = [("days?|d", DAY), ("hours?|hrs?|h", HOUR), ("minutes?|mins?|m", MINUTE), ("seconds?|secs?|s", 1)]
_ALL_UNITS = "|".join(u for u, _ in _UNITS)
_WORD_UNITS = "|".join(u.rsplit("|", 1)[0] for u, _ in _UNITS)  # no one-letter units after a word: "an d" is not a day
DURATION = re.compile(
    rf"(?<![\w.])(?:(\d+(?:\.\d+)?)\s*-?\s*({_ALL_UNITS})"
    rf"|({'|'.join(_WORDS)})[\s-]+({_WORD_UNITS})"
    rf"|(?:the\s+)?(?:past|last)\s+({_WORD_UNITS}))\b",
    re.IGNORECASE,
)

STRICT, INCLUSIVE, ALLOWED_MAX, ALLOWED_UNDER = "strict", "inclusive", "allowed_max", "allowed_under"
# Digits glued to letters ("5k", "6pm") count too; digits after a letter ("A100") are a name.
_BARE_DIGITS = re.compile(r"(?<!\w)\d+(?:[.,]\d+)*[a-z]*", re.IGNORECASE)
_COUNT_WORDS = (
    "one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen "
    "eighteen nineteen twenty thirty forty fifty sixty seventy eighty ninety hundred thousand million dozen"
)
_BARE_WORDS = re.compile(rf"(?<![\w-])(?:{'|'.join(_COUNT_WORDS.split())})(?![\w-])", re.IGNORECASE)


def find_amounts(text):
    """Every dollar amount in text: [{text, start, end, value}] where value is an exact Decimal."""
    out = []
    for m in AMOUNT.finditer(text):
        whole, frac = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        value = Decimal(whole.replace(",", "") + ("." + frac if frac else ""))
        out.append({"text": m.group(0), "start": m.start(), "end": m.end(), "value": value})
    return out


def find_durations(text):
    """Every duration in text: [{text, start, end, seconds}]. Fractions that are not whole seconds are skipped."""
    out = []
    for m in DURATION.finditer(text):
        # "the last hour" has no count of its own: it is one of that unit.
        n, unit = (m.group(1) or m.group(3) or "one").lower(), (m.group(2) or m.group(4) or m.group(5)).lower()
        count = Decimal(_WORDS[n]) if n in _WORDS else Decimal(n)
        size = next(s for u, s in _UNITS if re.fullmatch(u, unit))
        seconds = count * size
        if seconds != seconds.to_integral_value() or seconds <= 0:
            continue
        out.append({"text": m.group(0), "start": m.start(), "end": m.end(), "seconds": int(seconds)})
    return out


# Only these words decide a direction; any other wording around an amount needs review.
_BEFORE_AMOUNT = {
    "over": STRICT,
    "above": STRICT,
    "more than": STRICT,
    "greater than": STRICT,
    "exceed": STRICT,
    "exceeds": STRICT,
    "exceeding": STRICT,
    "at least": INCLUSIVE,
    "up to": ALLOWED_MAX,
    "at most": ALLOWED_MAX,
    "no more than": ALLOWED_MAX,
    "not more than": ALLOWED_MAX,
    "may not exceed": ALLOWED_MAX,
    "must not exceed": ALLOWED_MAX,
    "capped at": ALLOWED_MAX,
    "limited to": ALLOWED_MAX,
    "maximum of": ALLOWED_MAX,
    "maximum": ALLOWED_MAX,
    "max": ALLOWED_MAX,
    "under": ALLOWED_UNDER,
    "below": ALLOWED_UNDER,
    "less than": ALLOWED_UNDER,
}
_AFTER_AMOUNT = {
    "or more": INCLUSIVE,
    "and up": INCLUSIVE,
    "and above": INCLUSIVE,
    "or higher": INCLUSIVE,
    "or greater": INCLUSIVE,
    "maximum": ALLOWED_MAX,
    "max": ALLOWED_MAX,
}
_BEFORE_REGEX = re.compile(rf"(?<!\w)({'|'.join(_BEFORE_AMOUNT)})\s+$", re.IGNORECASE)
_AFTER_REGEX = re.compile(rf"\s+({'|'.join(_AFTER_AMOUNT)})(?!\w)", re.IGNORECASE)
_NEGATION = re.compile(r"\b(?:no|not|never|don't|do not|cannot|can't)\b", re.IGNORECASE)
_ALLOWED_STATEMENT = re.compile(r"\b(?:is|are)\s+(?:allowed|permitted|ok|fine)\b", re.IGNORECASE)
DIRECTION_TEXT = {
    STRICT: "blocked when amount > {}",
    INCLUSIVE: "blocked when amount >= {}",
    ALLOWED_MAX: "allowed when amount <= {}",
    ALLOWED_UNDER: "allowed when amount < {}",
}
BLOCK_OPERATOR = {STRICT: ">", ALLOWED_MAX: ">", INCLUSIVE: ">=", ALLOWED_UNDER: ">="}


def find_direction(text, start, end):
    """The direction stated right next to the amount at text[start:end]: {kind, text, start, end}, or None.

    Words on both sides are a contradiction or a repeat, so they count as no answer.
    """
    before = _BEFORE_REGEX.search(text[:start])
    after = _AFTER_REGEX.match(text, end)
    if bool(before) == bool(after):
        return None
    if before:
        return {
            "kind": _BEFORE_AMOUNT[before.group(1).lower()],
            "text": before.group(1),
            "start": before.start(1),
            "end": before.end(1),
        }
    return {
        "kind": _AFTER_AMOUNT[after.group(1).lower()],
        "text": after.group(1),
        "start": after.start(1),
        "end": after.end(1),
    }


def contradicts_direction(text, found):
    """The rest of the sentence points the other way: "No refunds under $5", "Refunds over $5 are allowed"."""
    if found["kind"] in (ALLOWED_MAX, ALLOWED_UNDER):
        return bool(_NEGATION.search(text[: found["start"]]))
    return bool(_ALLOWED_STATEMENT.search(text))


def find_bare_numbers(text):
    """Numbers in text that are neither a dollar amount nor a duration: [{text, start, end}]."""
    taken = find_amounts(text) + find_durations(text)
    found = [m for regex in (_BARE_DIGITS, _BARE_WORDS) for m in regex.finditer(text)]
    free = [m for m in found if not any(t["start"] < m.end() and m.start() < t["end"] for t in taken)]
    return [{"text": m.group(0), "start": m.start(), "end": m.end()} for m in sorted(free, key=lambda m: m.start())]


def window(seconds):
    """Converter window string in the largest unit that divides exactly: 86400 -> '24h'."""
    for suffix, size in (("h", HOUR), ("m", MINUTE)):
        if seconds % size == 0:
            return f"{seconds // size}{suffix}"
    return f"{seconds}s"


def how_long(seconds):
    """Plain words for a length of time: 5400 -> '1 hour and 30 minutes'."""
    parts = []
    for name, size in (("hour", HOUR), ("minute", MINUTE), ("second", 1)):
        n, seconds = divmod(seconds, size)
        if n:
            parts.append(f"{n} {name}{'s' if n != 1 else ''}")
    if not parts:
        return "just now"
    return (", ".join(parts[:-1]) + " and " + parts[-1]) if len(parts) > 1 else parts[0]


def amount_json(value):
    """Whole dollars as an int; anything with cents as the exact decimal string."""
    return int(value) if value == value.to_integral_value() else str(value)
