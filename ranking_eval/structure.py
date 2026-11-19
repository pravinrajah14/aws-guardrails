"""Structure profiles: what a rule controls, what must happen first, which fields must match, which values it sets.

Three ways to get one:
- library_profile: written by hand for the 18 library policies (profiles.json);
- sentence_profile: the partial profile code can read from a new sentence before the model runs;
- understanding_profile: the full profile of the rule the model read (its rule type's forms plus its slots).
None of the scores below look at rule types; understanding_profile only uses the type's forms to know the shape.
"""

import json
import re
from pathlib import Path

from app.core import glossary, rule_types
from app.core.numbers import find_amounts, find_durations

PROFILES = json.loads((Path(__file__).parent / "profiles.json").read_text(encoding="utf-8"))["profiles"]

# What each part of a profile is worth when two profiles are compared. Few weights, on purpose.
WEIGHTS = {"action": 0.35, "prior": 0.30, "kinds": 0.15, "same": 0.10, "sets": 0.10}

_REFUND = re.compile(r"\b(?:refund\w*|reimburs\w*|money back)\b", re.IGNORECASE)
_DEPLOY = re.compile(
    r"\b(?:deploy\w*|releas\w*|ship\w*|roll ?out\w*)\b|\bgo(?:es)? (?:to|into) (?:production|prod|staging|live)\b",
    re.IGNORECASE,
)
_APPROVAL = re.compile(r"\b(?:approv\w+|sign(?:ed)?[- ]?off|authori[sz]\w+|ok'?a?y?ed)\b", re.IGNORECASE)
_TESTS = re.compile(r"\b(?:tests?|test suite|ci|checks?)\b", re.IGNORECASE)
_ROLLBACK = re.compile(r"\broll ?back\w*\b", re.IGNORECASE)
_REPEAT = re.compile(r"\b(?:twice|again|more than \d+ refunds?|in a single day|per day|in total)\b", re.IGNORECASE)
_ONLY_FOR = re.compile(r"\bonly\b.*\bfor\b|\bonly\b.*\bmay (?:go|be deployed)\b", re.IGNORECASE)


def library_profile(example_id):
    return PROFILES.get(example_id)


def sentence_profile(sentence):
    """What code can read before the model: action and prior action from wording, values from the parsers.

    A part code can't see is None (unknown), not empty, so a comparison skips it instead of counting it as a miss.
    """
    refund, deploy = bool(_REFUND.search(sentence)), bool(_DEPLOY.search(sentence))
    action = "IssueRefund" if refund and not deploy else "DeployService" if deploy and not refund else None
    prior = None
    if _APPROVAL.search(sentence):
        prior = "RequestApproval"
    elif action == "DeployService" and _ROLLBACK.search(sentence):
        prior = "Rollback"
    elif action == "DeployService" and _TESTS.search(sentence):
        prior = "RunTests"
    elif action == "IssueRefund" and _REPEAT.search(sentence):
        prior = "IssueRefund"
    sets = {}
    durations = find_durations(sentence)
    if durations:
        sets["window"] = durations[0]["seconds"]
    amounts = find_amounts(sentence)
    if amounts:
        sets["max_amount"] = int(amounts[0]["value"])
    scope = {}
    for hit in glossary.find_terms(sentence):
        if hit["field"] == "approver_role":
            sets["approver_role"] = hit["value"]
        elif hit["field"] == "env":
            scope["env"] = hit["value"]
    kinds = None
    if prior and action:
        kinds = ["requires_prior"]
    elif action == "DeployService" and _ONLY_FOR.search(sentence):
        kinds = ["allowlist"]
    elif action and amounts:
        kinds = ["limit"]
    return {
        "action": action,
        "prior": prior,
        "prior_known": action is not None or prior is not None,
        "kinds": kinds,
        "same": None,
        "sets": sets,
        "scope": scope,
    }


def understanding_profile(u):
    """The full profile of the rule the model read, from its rule type's form templates and its slots."""
    if not u or not u.get("rule_type") or u["rule_type"] not in rule_types.BY_ID:
        return None
    forms = rule_types.BY_ID[u["rule_type"]]["forms"]
    prior, same = None, set()
    for form in forms:
        requires = form.get("requires")
        if requires:
            prior = requires["action"]
            same |= set(requires.get("same", [])) | set(requires.get("at_most", {}))
    sets = {}
    for name, slot in u.get("slots", {}).items():
        sets[name] = slot.get("value") if slot.get("source") != "missing" else None
    scope = {q["field"]: q["value"] for q in u.get("qualifiers", []) if q.get("mapped") and "value" in q}
    return {
        "action": u["action"] or rule_types.BY_ID[u["rule_type"]]["action"],
        "prior": prior,
        "prior_known": True,
        "kinds": sorted({f["kind"] for f in forms}),
        "same": sorted(same),
        "sets": sets,
        "scope": scope,
    }


def type_profile(type_id):
    """The profile of a rule type with no values, for callers that have only the type."""
    if type_id not in rule_types.BY_ID:
        return None
    t = rule_types.BY_ID[type_id]
    return understanding_profile({"rule_type": type_id, "action": t["action"], "slots": {}, "qualifiers": []}) | {
        "sets": {s["name"]: None for s in t["slots"]}
    }


def _jaccard(a, b):
    a, b = set(a), set(b)
    return len(a & b) / len(a | b) if a | b else 1.0


def same_frame(query, profile):
    """Same action and same prior action: the evidence filter that replaces 'same rule type'."""
    if query is None or profile is None:
        return False
    return query["action"] == profile["action"] and query["prior"] == profile["prior"]


def similarity(query, profile):
    """0..1. Parts the query doesn't know are left out and the rest re-weighted."""
    if query is None or profile is None:
        return 0.0
    total, got = 0.0, 0.0
    if query["action"] is not None:
        total += WEIGHTS["action"]
        got += WEIGHTS["action"] * (query["action"] == profile["action"])
    if query.get("prior_known"):
        total += WEIGHTS["prior"]
        got += WEIGHTS["prior"] * (query["prior"] == profile["prior"])
    if query["kinds"] is not None:
        total += WEIGHTS["kinds"]
        got += WEIGHTS["kinds"] * _jaccard(query["kinds"], profile["kinds"])
    if query["same"] is not None:
        total += WEIGHTS["same"]
        got += WEIGHTS["same"] * _jaccard(query["same"], profile["same"])
    if query["sets"]:
        total += WEIGHTS["sets"]
        got += WEIGHTS["sets"] * _jaccard(query["sets"], profile["sets"])
    return got / total if total else 0.0
