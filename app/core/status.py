"""The one place that decides a rule's status, with its reasons."""

READY, CLARIFY, REVIEW, INVALID = "ready", "needs clarification", "needs review", "invalid"
_FROM_VERDICT = {"invalid": INVALID, "needs_review": REVIEW, "needs_clarification": CLARIFY}


def decide(u, problems=(), validation=None, examples=(), saved=()):
    """validation is (ok, output) from dogwood validate; examples and saved are cases with their Dogwood result."""
    understood = _FROM_VERDICT.get(u["verdict"])
    if understood == INVALID:
        return {"status": INVALID, "reasons": [i["reason"] for i in u["issues"] if i["verdict"] == "invalid"]}
    if validation and not validation[0]:
        return {"status": INVALID, "reasons": [f"Dogwood validation failed: {validation[1]}"]}
    disagreeing = [
        f"{e['says']} Dogwood said {e['got']}, the summary says {e['expect']}." for e in examples if not e["agrees"]
    ]
    failed = [
        f"Confirmed case: {c['says']} It should be {c['expect']}; Dogwood said {c['got']}."
        for c in saved
        if not c["passed"]
    ]
    if understood == REVIEW or problems or disagreeing or failed:
        reasons = [i["reason"] for i in u["issues"] if i["verdict"] == "needs_review"]
        return {"status": REVIEW, "reasons": reasons + list(problems) + disagreeing + failed}
    if understood == CLARIFY:
        return {
            "status": CLARIFY,
            "reasons": [i["reason"] for i in u["issues"] if i["verdict"] == "needs_clarification"],
        }
    return {"status": READY, "reasons": ["every value has a source, Dogwood validated it, and the examples agree"]}
