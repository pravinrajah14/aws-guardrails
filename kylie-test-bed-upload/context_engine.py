"""The context engine: before an action is checked, look up checked facts about it in the store.

Dogwood still makes every decision. The engine only reads records and reports what they say,
so a rule can test those facts. It never fills a gap with a guess: when the records are missing,
stale or disagree, it attaches no approval facts (only the status and the order's facts), and
Dogwood's deny-by-default blocks.

Order facts (order_total, refunded_on_order) are attached on every IssueRefund whose order is in the
store, with or without a rule form, so the store-wide invariant in policies/base.dw can always check them.

Inputs (all plain Python, so the scripted scorer and the live agent use the same code):
  action, args   the request Dogwood is about to check
  records        {"approvals": [...], "refunds": [...], "orders": {...}, "staff": {...}}; each approval is
                 {order_id, asked, approved, approver, approved_amount, at, source}, each refund is
                 {refund_id, order_id, amount, at}
  steps          the session's earlier steps, oldest first: {ts, action, kind, input, output}
                 for tool events, {ts, kind: "message", who, text} for free text
  now            Dogwood timestamp of the request, in seconds (same clock as the approvals' "at")
  window         the rule form's "within", in seconds; None if no form was given

Output: {"facts", "status", "reason", "sources", "details", "review", "ignored_claims"}
  facts    what goes into Dogwood's request_context (schema type RefundFacts), or None when the
           order is not in the store and there is no approval to report
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
        # Every completed refund for this order, whichever approval it used, checked against the trace.
        store_all = sum(r["amount"] for r in records.get("refunds", []) if r["order_id"] == order_id)
        trace_all = sum((s.get("output") or {}).get("amount", 0) for s in steps
                        if s.get("action") == "IssueRefund" and s["kind"] == "response"
                        and s["input"].get("order_id") == order_id)
        details.update({"refunded_on_order_store": store_all, "refunded_on_order_trace": trace_all})
        if store_all != trace_all:
            return result("conflict", f"Refunded on order {order_id}: store says {store_all}, this session's "
                          f"trace says {trace_all}. A person must review.",
                          {"status": "conflict", **base}, sources, details, review=True)
        base["refunded_on_order"] = store_all
        sources.append(f"refunded_on_order: store refunds for {order_id}, checked against the trace")

    def order_only(status):
        # No approval facts to report: only the status, plus the order's facts if the order is known.
        return {"status": status, **base} if base else None

    approvals = sorted((a for a in records.get("approvals", []) if a["order_id"] == order_id and "at" in a),
                       key=lambda a: a["at"])
    if not approvals:
        ignored = free_text_claims(steps, order_id)
        reason = f"No approval record for order {order_id}."
        if ignored:
            reason += f" Ignored {len(ignored)} unverified approval claim(s) in free text."
        return result("missing", reason, order_only("missing"), sources, details, ignored=ignored)
    if window is None:
        # Without the rule's window the engine cannot tell ok from expired, so it says nothing.
        for a in approvals:
            sources.append(f"approval: {a.get('source', 'store approval')} at @{a['at']}")
        return result("missing", "No rule form given, so the approval window is unknown; attaching only the "
                      "order's facts.", order_only("missing"), sources, details)

    # Only approvals inside the rule's window count. Older ones are listed but never cause a conflict.
    current = [a for a in approvals if now - a["at"] <= window]   # inclusive, like Dogwood's 'formerly within'
    for a in approvals:
        sources.append(f"approval: {a.get('source', 'store approval')} at @{a['at']}"
                       + ("" if a in current else " (expired, ignored)"))
    if not current:
        age = now - approvals[-1]["at"]
        details.update({"approver": approvals[-1]["approver"], "approved_at": approvals[-1]["at"], "age_seconds": age})
        return result("expired", f"Newest approval for order {order_id} is {age}s old; the rule allows {window}s.",
                      {"status": "expired", **base}, sources, details)

    answers = {(a["approved"], a["approved_amount"] if a["approved"] else None) for a in current}
    if len(answers) > 1:
        shown = "; ".join(f"{'approved ' + str(a['approved_amount']) if a['approved'] else 'refused'} by "
                          f"{a['approver']} @{a['at']}" for a in current)
        return result("conflict", f"{len(current)} approval records for order {order_id} inside the window disagree "
                      f"({shown}). A person must review.", {"status": "conflict", **base}, sources, details,
                      review=True)

    newest = current[-1]
    age = now - newest["at"]
    details.update({"approver": newest["approver"], "approved_at": newest["at"], "age_seconds": age})
    if not newest["approved"]:
        return result("missing", f"The manager refused the refund for order {order_id}; no valid approval on file.",
                      order_only("missing"), sources, details)

    # What has already been refunded against this approval: the store's refund records, checked
    # against this session's trace. If they disagree the engine cannot say which is right.
    store_refunds = [r for r in records.get("refunds", []) if r["order_id"] == order_id]
    undated = [r for r in store_refunds if "at" not in r]
    if undated:
        return result("conflict", f"Store refund record(s) {[r.get('refund_id') for r in undated]} for order "
                      f"{order_id} have no time, so it is unknown whether they used this approval. "
                      "A person must review.", {"status": "conflict", **base}, sources, details, review=True)
    store_total = sum(r["amount"] for r in store_refunds if r["at"] > newest["at"])
    trace_total = sum((s.get("output") or {}).get("amount", 0) for s in steps
                      if s.get("action") == "IssueRefund" and s["kind"] == "response"
                      and s["input"].get("order_id") == order_id and s["ts"] > newest["at"])
    details.update({"refunded_so_far_store": store_total, "refunded_so_far_trace": trace_total})
    sources.append(f"refunded_so_far: store refunds for {order_id} since @{newest['at']}, checked against the trace")
    if store_total != trace_total:
        return result("conflict", f"Refunded since the approval for order {order_id}: store says {store_total}, "
                      f"this session's trace says {trace_total}. A person must review.",
                      {"status": "conflict", **base}, sources, details, review=True)

    facts = {"status": "ok", "approved_amount": newest["approved_amount"], "refunded_so_far": store_total, **base}
    person = records.get("staff", {}).get(newest["approver"])
    if person and person.get("role"):
        facts["approver_role"] = person["role"]
        details["approver_role"] = person["role"]
        sources.append(f"approver_role: staff[{newest['approver']}]")
    role = facts.get("approver_role", "unknown role")
    return result("ok", f"Approved {newest['approved_amount']} for order {order_id} by {newest['approver']} "
                  f"({role}) {age}s ago; {store_total} refunded since.", facts, sources, details)


def get_facts(action, args, records, steps, now, window):
    """Facts for one request. Only IssueRefund has facts today; other actions get none."""
    if action != "IssueRefund":
        return result(None, f"No facts are looked up for {action}.")
    return refund_facts(args, records, steps, now, window)
