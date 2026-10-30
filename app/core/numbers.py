"""Dollar amounts and durations, read from text with regex. The model never supplies numbers.

Amounts keep their cents exactly (Decimal, never float, never rounded).
Durations are whole seconds plus the converter's window form ("24h", "30m", "90s").
"""
import re
from decimal import Decimal

_NUM = r"(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?"
_END = r"(?!\w)(?![.,]\d)"
AMOUNT = re.compile(
    rf"\$\s?{_NUM}{_END}"
    rf"|(?<![\w.,$]){_NUM}\s*(?:dollars?|usd|bucks)\b",
    re.I,
)

_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
          "seven": 7, "eight": 8, "nine": 9, "ten": 10, "twelve": 12}
_UNITS = [("days?|d", 86400), ("hours?|hrs?|h", 3600), ("minutes?|mins?|m", 60), ("seconds?|secs?|s", 1)]
_ALL_UNITS = "|".join(u for u, _ in _UNITS)
_WORD_UNITS = "|".join(u.rsplit("|", 1)[0] for u, _ in _UNITS)  # no one-letter units after a word: "an d" is not a day
DURATION = re.compile(
    rf"(?<![\w.])(?:(\d+(?:\.\d+)?)\s*-?\s*({_ALL_UNITS})"
    rf"|({'|'.join(_WORDS)})[\s-]+({_WORD_UNITS}))\b",
    re.I,
)


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
        n, unit = (m.group(1) or m.group(3)).lower(), (m.group(2) or m.group(4)).lower()
        count = Decimal(_WORDS[n]) if n in _WORDS else Decimal(n)
        size = next(s for u, s in _UNITS if re.fullmatch(u, unit))
        seconds = count * size
        if seconds != seconds.to_integral_value() or seconds <= 0:
            continue
        out.append({"text": m.group(0), "start": m.start(), "end": m.end(), "seconds": int(seconds)})
    return out


def window(seconds):
    """Converter window string in the largest unit that divides exactly: 86400 -> '24h'."""
    for suffix, size in (("h", 3600), ("m", 60)):
        if seconds % size == 0:
            return f"{seconds // size}{suffix}"
    return f"{seconds}s"


def amount_json(value):
    """Whole dollars as an int; anything with cents as the exact decimal string."""
    return int(value) if value == value.to_integral_value() else str(value)
