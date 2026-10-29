"""The context engine: before an action is checked, look up checked facts about it in the store.

Dogwood still makes every decision. The engine only reads records and reports what they say,
so a rule can test those facts. It never fills a gap with a guess: when the records are missing,
stale or disagree, it attaches nothing (or only the status), and Dogwood's deny-by-default blocks.

Inputs (all plain Python, so the scripted scorer and the live agent use the same code):
  action, args   the request Dogwood is about to check
  records        {"approvals": [...], "orders": {...}, "staff": {...}}; each approval is
                 {order_id, asked, approved, approver, approved_amount, at, source}
  steps          the session's earlier steps, oldest first: {ts, action, kind, input, output}
                 for tool events, {ts, kind: "message", who, text} for free text
  now            Dogwood timestamp of the request, in seconds (same clock as the approvals' "at")
  window         the rule form's "within", in seconds; None if no form was given

Output: {"facts", "status", "reason", "sources", "details", "review", "ignored_claims"}
  facts    what goes into Dogwood's request_context (schema type RefundFacts), or None
  status   ok | missing | expired | conflict (None for actions the engine has no facts for)
"""
import re

UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
CLAIM = re.compile(r"\b(approv\w*|okay(ed)?|ok'?d|signed off|authori[sz]ed)\b", re.I)
RECENT_STEPS = 6


def window_seconds(within):
    """'1h' -> 3600. The form's own format; anything else is an error, not a guess."""
    m = re.fullmatch(r"(\d+)([smhd])", str(within))
    if not m:
        raise ValueError(f"bad window {within!r}: use e.g. '1h' or '30m'")
    return int(m.group(1)) * UNITS[m.group(2)]


def form_window(form, action):
    """The window a rule form sets for this action, or None if the form does not gate it."""
    if not form or form.get("action") != action or "requires" not in form:
        return None
    return window_seconds(form["requires"]["within"])


def step_text(step):
    if step["kind"] == "message":
        return f"@{step['ts']} {step['who']} said: {step['text'][:60]}"
    shown = ", ".join(f"{k}={v}" for k, v in step.get("input", {}).items() if k != "reason")
    return f"@{step['ts']} {step['action']} {step['kind']} ({shown})"


def free_text_claims(steps, order_id):
    """Free-text mentions of an approval: user messages, and 'reason' fields the agent wrote.
    These are never evidence; they are only reported so the log shows they were ignored."""
    found = []
    for s in steps:
        if s["kind"] == "message":
            texts = [s["text"]]
        else:
            texts = [v for k, v in s.get("input", {}).items() if k == "reason" and isinstance(v, str)]
        for t in texts:
            if CLAIM.search(t) and (order_id in t or not re.search(r"\b[A-Z]\d{3}\b", t)):
                found.append(f"@{s['ts']}: {t[:80]}")
    return found


def result(status, reason, facts=None, sources=(), details=None, review=False, ignored=()):
    return {"facts": facts, "status": status, "reason": reason, "sources": list(sources),
            "details": details or {}, "review": review, "ignored_claims": list(ignored)}


def refund_facts(args, records, steps, now, window):
    order_id = args["order_id"]
    sources, details = [], {"earlier_steps": [step_text(s) for s in steps[-RECENT_STEPS:]]}
    base = {}
    order = records.get("orders", {}).get(order_id)
    if order is not None and isinstance(order.get("total"), int):
        base["order_total"] = order["total"]
        sources.append(f"order_total: store orders[{order_id}]")

    approvals = sorted((a for a in records.get("approvals", []) if a["order_id"] == order_id and "at" in a),
                       key=lambda a: a["at"])
    if not approvals:
        ignored = free_text_claims(steps, order_id)
        reason = f"No approval record for order {order_id}."
        if ignored:
            reason += f" Ignored {len(ignored)} unverified approval claim(s) in free text."
        return result("missing", reason, None, sources, details, ignored=ignored)

    for a in approvals:
        sources.append(f"approval: {a.get('source', 'store approval')} at @{a['at']}")
    answers = {(a["approved"], a["approved_amount"] if a["approved"] else None) for a in approvals}
    if len(answers) > 1:
        shown = "; ".join(f"{'approved ' + str(a['approved_amount']) if a['approved'] else 'refused'} by "
                          f"{a['approver']} @{a['at']}" for a in approvals)
        return result("conflict", f"{len(approvals)} approval records for order {order_id} disagree ({shown}). "
                      "A person must review.", {"status": "conflict", **base}, sources, details, review=True)

    newest = approvals[-1]
    age = now - newest["at"]
    details.update({"approver": newest["approver"], "approved_at": newest["at"], "age_seconds": age})
    if not newest["approved"]:
        return result("missing", f"The manager refused the refund for order {order_id}; no valid approval on file.",
                      None, sources, details)
    if window is None:
        # Without the rule's window the engine cannot tell ok from expired, so it says nothing.
        return result("missing", "No rule form given, so the approval window is unknown; attaching nothing.",
                      None, sources, details)
    if age > window:   # inclusive at the limit, like Dogwood's 'formerly within'
        return result("expired", f"Newest approval for order {order_id} is {age}s old; the rule allows {window}s.",
                      {"status": "expired", **base}, sources, details)

    facts = {"status": "ok", "approved_amount": newest["approved_amount"], **base}
    person = records.get("staff", {}).get(newest["approver"])
    if person and person.get("role"):
        facts["approver_role"] = person["role"]
        details["approver_role"] = person["role"]
        sources.append(f"approver_role: staff[{newest['approver']}]")
    role = facts.get("approver_role", "unknown role")
    return result("ok", f"Approved {newest['approved_amount']} for order {order_id} by {newest['approver']} "
                  f"({role}) {age}s ago.", facts, sources, details)


def get_facts(action, args, records, steps, now, window):
    """Facts for one request. Only IssueRefund has facts today; other actions get none."""
    if action != "IssueRefund":
        return result(None, f"No facts are looked up for {action}.")
    return refund_facts(args, records, steps, now, window)
