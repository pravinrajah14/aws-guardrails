"""What code can see in a sentence on its own: rule type signals, total-or-single wording, vague words, roles."""

import re

from app.core import glossary, rule_types
from app.core.numbers import find_amounts, find_durations

_REFUND = re.compile(r"\brefund\w*\b", re.IGNORECASE)
_DEPLOY = re.compile(r"\bdeploy\w*\b|\bgo(?:es)? to production\b", re.IGNORECASE)
_APPROVAL = re.compile(r"\bapprov\w+\b|\bsign(?:ed)?[- ]off\b", re.IGNORECASE)
_TESTS = re.compile(r"\btests?\b", re.IGNORECASE)
_ONLY_FOR = re.compile(r"\bonly\b.*\bfor\b", re.IGNORECASE)
_AMOUNT_LIKE = re.compile(
    r"\$\s?\d|(?<![\w.,])\d+(?:\.\d+)?k\b|(?<![\w.,])\d[\d,.]*\s*(?:dollars?|usd|bucks)\b", re.IGNORECASE
)
_LIST_AFTER_FOR = re.compile(r"\bfor\s+(.+?)[.]?$", re.IGNORECASE)

_PER_REFUND = re.compile(r"\b(?:each refund|a single refund|per refund)\b", re.IGNORECASE)
_RECENT = re.compile(r"\brecent(?:ly)?\b", re.IGNORECASE)
TOTAL_WORDINGS = [
    (re.compile(r"\b(?:on a single order|per order)\b", re.IGNORECASE), "the total per order"),
    (re.compile(r"\bper customer\b", re.IGNORECASE), "the total per customer"),
    (re.compile(r"\b(?:in total|combined)\b", re.IGNORECASE), "the total across refunds"),
    (re.compile(r"\b(?:per|in a single|in a)\s+(day|week|month|hour)\b", re.IGNORECASE), "the total per {}"),
]
RECENT_TOTAL = "the total over a recent period"

VAGUE_WINDOW = re.compile(r"\b(?:recently|recent|lately|soon|shortly)\b", re.IGNORECASE)

# A word right before "approval" that is not a role, as in "needs approval" or "subject to approval".
_NOT_A_ROLE = frozenset(
    {"a", "an", "the", "any", "to", "for", "with", "without", "and", "or", "of", "that", "this", "its", "their"}
    | {"need", "needs", "needed", "require", "requires", "required", "get", "gets", "have", "has"}
    | {"prior", "same", "request", "requesting"}
)
_ROLE_BEFORE_APPROVAL = re.compile(r"([\w'’]+)\s+(?:approval|sign[- ]off)\b", re.IGNORECASE)
_ROLE_BEFORE_APPROVED = re.compile(r"\b(?:an?|the)\s+([a-z]+(?:\s[a-z]+)?)\s+approve[sd]?\b", re.IGNORECASE)
_LIST_SEPARATOR = re.compile(r"\s*,\s*(?:(?:and|or)\s+)?|\s+(?:and|or)\s+", re.IGNORECASE)
_LIST_LEAD_IN = re.compile(r"^(?:only\s+)?(?:allowed\s+)?(?:for\s+|to\s+)?(?:the\s+)?", re.IGNORECASE)
_LIST_TAIL = re.compile(r"\s+services?$", re.IGNORECASE)


def type_signals(sentence):
    """Supported types the words point to, best reading first. Empty means code sees none."""
    amount = bool(_AMOUNT_LIKE.search(sentence))
    if _REFUND.search(sentence):
        approval = bool(_APPROVAL.search(sentence))
        has_order = any(h["field"] == "order_id" for h in glossary.find_terms(sentence))
        if amount and approval and has_order:
            return [rule_types.CAPPED_WITH_APPROVAL]
        if amount and approval:
            return [rule_types.APPROVAL_REQUIRED, rule_types.CAPPED_WITH_APPROVAL]
        if approval:
            return [rule_types.APPROVAL_REQUIRED]
        return [rule_types.AMOUNT_CAP] if amount else []
    if _DEPLOY.search(sentence):
        if _TESTS.search(sentence):
            return [rule_types.DEPLOY_NEEDS_TESTS]
        if _ONLY_FOR.search(sentence):
            return [rule_types.DEPLOY_ALLOWLIST]
    return []


def code_answer(sentence, type_id):
    """The answer code alone can give for a type, in the model's answer shape. Code then checks it like any other."""
    slots = []
    defs = rule_types.slots_of(type_id)
    amounts, durations = find_amounts(sentence), find_durations(sentence)
    if "max_amount" in defs and amounts:
        slots.append({"slot": "max_amount", "span": amounts[0]["text"], "value": amounts[0]["text"]})
    if "window" in defs and durations:
        slots.append({"slot": "window", "span": durations[0]["text"], "value": durations[0]["text"]})
    if "services" in defs:
        listed = _LIST_AFTER_FOR.search(sentence)
        if listed:
            slots.append({"slot": "services", "span": listed.group(1), "value": listed.group(1)})
    return {
        "rule_type": type_id,
        "action": rule_types.BY_ID[type_id]["action"],
        "unsupported_reason": "",
        "slots": slots,
        "vague_terms": [],
        "qualifiers": [],
    }


def find_total_wording(sentence, type_id):
    """Wording that may make an amount a total rather than a per-refund limit: {text, start, end, total}, or None."""
    if _PER_REFUND.search(sentence):
        return None
    for regex, total in TOTAL_WORDINGS:
        m = regex.search(sentence)
        if m:
            period = m.group(1).lower() if m.groups() else ""
            return {"text": m.group(0), "start": m.start(), "end": m.end(), "total": total.format(period)}
    # "recently" on a rule with an approval is the approval's window, not a total.
    m = _RECENT.search(sentence) if type_id == rule_types.AMOUNT_CAP else None
    return {"text": m.group(0), "start": m.start(), "end": m.end(), "total": RECENT_TOTAL} if m else None


def unknown_roles(sentence):
    """Words the sentence uses as an approver that the glossary doesn't know: [{text, start, end}]."""
    known = {(h["start"], h["end"]) for h in glossary.find_terms(sentence)}
    found = []
    for regex in (_ROLE_BEFORE_APPROVAL, _ROLE_BEFORE_APPROVED):
        for m in regex.finditer(sentence):
            words = m.group(1).lower().split()
            is_known = (m.start(1), m.end(1)) in known or any(w in _NOT_A_ROLE for w in words)
            if not is_known:
                found.append({"text": m.group(1), "start": m.start(1), "end": m.end(1)})
    return found


def split_list(text):
    """List items in a span, split on commas, "and" and "or": [(item, start, end)], offsets within text."""
    items, position = [], 0
    for piece in _LIST_SEPARATOR.split(text):
        start = text.index(piece, position)
        position = start + len(piece)
        stripped = _LIST_TAIL.sub("", _LIST_LEAD_IN.sub("", piece)).rstrip(" .")
        if stripped:
            begin = start + piece.index(stripped)
            items.append((stripped, begin, begin + len(stripped)))
    return items
