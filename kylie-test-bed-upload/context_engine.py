"""Before an action is checked, look up checked facts about it in the store. Dogwood still decides."""

import re

UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
CLAIM = re.compile(r"\b(approv\w*|okay(ed)?|ok'?d|signed off|authori[sz]ed)\b", re.IGNORECASE)
RECENT_STEPS = 6
OK, MISSING, EXPIRED, CONFLICT = "ok", "missing", "expired", "conflict"


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
    return {
        "facts": facts,
        "status": status,
        "reason": reason,
        "sources": list(sources),
        "details": details or {},
        "review": review,
        "ignored_claims": list(ignored),
    }


def _conflict(reason, sources, details):
    return result(CONFLICT, reason, {"status": CONFLICT}, sources, details, review=True)


def _store_refunds(records, order_id):
    return [r for r in records.get("refunds", []) if r["order_id"] == order_id]


def _trace_refunded(steps, order_id, since=None):
    return sum(
        (s.get("output") or {}).get("amount", 0)
        for s in steps
        if s.get("action") == "IssueRefund"
        and s["kind"] == "response"
        and s["input"].get("order_id") == order_id
        and (since is None or s["ts"] > since)
    )


def _order_facts(order_id, records, steps, sources, details):
    """The order's facts, empty if it is not in the store. Returns (facts, conflict result or None)."""
    order = records.get("orders", {}).get(order_id)
    if order is None or not isinstance(order.get("total"), int):
        return {}, None
    sources.append(f"order_total: store orders[{order_id}]")
    # Every completed refund for this order, whichever approval it used, checked against the trace.
    store_all = sum(r["amount"] for r in _store_refunds(records, order_id))
    trace_all = _trace_refunded(steps, order_id)
    details.update({"refunded_on_order_store": store_all, "refunded_on_order_trace": trace_all})
    if store_all != trace_all:
        reason = (
            f"Refunded on order {order_id}: store says {store_all}, this session's "
            f"trace says {trace_all}. A person must review."
        )
        return {}, _conflict(reason, sources, details)
    sources.append(f"refunded_on_order: store refunds for {order_id}, checked against the trace")
    return {"order_total": order["total"], "refunded_on_order": store_all}, None


def _order_only(status, base):
    # No approval facts to report: only the status, plus the order's facts if the order is known.
    return {"status": status, **base} if base else None


def _approval_source(approval):
    return f"approval: {approval.get('source', 'store approval')} at @{approval['at']}"


def _no_approval(order_id, steps, base, sources, details):
    ignored = free_text_claims(steps, order_id)
    reason = f"No approval record for order {order_id}."
    if ignored:
        reason += f" Ignored {len(ignored)} unverified approval claim(s) in free text."
    return result(MISSING, reason, _order_only(MISSING, base), sources, details, ignored=ignored)


def _approved_facts(order_id, newest, age, records, steps, base, sources, details):
    """Facts for an approval that holds, or a conflict if the refunds made against it cannot be counted."""
    # What has already been refunded against this approval: the store's refund records, checked
    # against this session's trace. If they disagree the engine cannot say which is right.
    store_refunds = _store_refunds(records, order_id)
    undated = [r for r in store_refunds if "at" not in r]
    if undated:
        reason = (
            f"Store refund record(s) {[r.get('refund_id') for r in undated]} for order "
            f"{order_id} have no time, so it is unknown whether they used this approval. "
            "A person must review."
        )
        return _conflict(reason, sources, details)
    store_total = sum(r["amount"] for r in store_refunds if r["at"] > newest["at"])
    trace_total = _trace_refunded(steps, order_id, since=newest["at"])
    details.update({"refunded_so_far_store": store_total, "refunded_so_far_trace": trace_total})
    sources.append(f"refunded_so_far: store refunds for {order_id} since @{newest['at']}, checked against the trace")
    if store_total != trace_total:
        reason = (
            f"Refunded since the approval for order {order_id}: store says {store_total}, "
            f"this session's trace says {trace_total}. A person must review."
        )
        return _conflict(reason, sources, details)

    facts = {"status": OK, "approved_amount": newest["approved_amount"], "refunded_so_far": store_total, **base}
    person = records.get("staff", {}).get(newest["approver"])
    if person and person.get("role"):
        facts["approver_role"] = person["role"]
        details["approver_role"] = person["role"]
        sources.append(f"approver_role: staff[{newest['approver']}]")
    role = facts.get("approver_role", "unknown role")
    reason = (
        f"Approved {newest['approved_amount']} for order {order_id} by {newest['approver']} "
        f"({role}) {age}s ago; {store_total} refunded since."
    )
    return result(OK, reason, facts, sources, details)


def refund_facts(args, records, steps, now, window):
    order_id = args["order_id"]
    sources, details = [], {"earlier_steps": [step_text(s) for s in steps[-RECENT_STEPS:]]}
    base, conflict = _order_facts(order_id, records, steps, sources, details)
    if conflict:
        return conflict

    approvals = sorted(
        (a for a in records.get("approvals", []) if a["order_id"] == order_id and "at" in a), key=lambda a: a["at"]
    )
    if not approvals:
        return _no_approval(order_id, steps, base, sources, details)
    if window is None:
        # Without the rule's window the engine cannot tell ok from expired, so it says nothing.
        sources += [_approval_source(a) for a in approvals]
        reason = "No rule form given, so the approval window is unknown; attaching only the order's facts."
        return result(MISSING, reason, _order_only(MISSING, base), sources, details)

    # Only approvals inside the rule's window count. Older ones are listed but never cause a conflict.
    current = [a for a in approvals if now - a["at"] <= window]  # inclusive, like Dogwood's 'formerly within'
    sources += [_approval_source(a) + ("" if a in current else " (expired, ignored)") for a in approvals]
    if not current:
        age = now - approvals[-1]["at"]
        details.update({"approver": approvals[-1]["approver"], "approved_at": approvals[-1]["at"], "age_seconds": age})
        reason = f"Newest approval for order {order_id} is {age}s old; the rule allows {window}s."
        return result(EXPIRED, reason, {"status": EXPIRED, **base}, sources, details)

    answers = {(a["approved"], a["approved_amount"] if a["approved"] else None) for a in current}
    if len(answers) > 1:
        shown = "; ".join(
            f"{'approved ' + str(a['approved_amount']) if a['approved'] else 'refused'} by {a['approver']} @{a['at']}"
            for a in current
        )
        reason = f"{len(current)} approval records for order {order_id} inside the window disagree ({shown}). A person must review."
        return _conflict(reason, sources, details)

    newest = current[-1]
    age = now - newest["at"]
    details.update({"approver": newest["approver"], "approved_at": newest["at"], "age_seconds": age})
    if not newest["approved"]:
        reason = f"The manager refused the refund for order {order_id}; no valid approval on file."
        return result(MISSING, reason, _order_only(MISSING, base), sources, details)
    return _approved_facts(order_id, newest, age, records, steps, base, sources, details)


def get_facts(action, args, records, steps, now, window):
    """Facts for one request. Only IssueRefund has facts today; other actions get none.

    The engine never fills a gap with a guess. When records are missing or stale it attaches no approval
    facts, so deny-by-default blocks. When they disagree it attaches only status "conflict", which
    policies/base.dw forbids. Order facts are attached whenever the order is in the store, with or without
    a rule form, so the store-wide invariant in policies/base.dw can always check them.

    records: {"approvals": [...], "refunds": [...], "orders": {...}, "staff": {...}}; each approval is
        {order_id, asked, approved, approver, approved_amount, at, source}, each refund is
        {refund_id, order_id, amount, at}
    steps: the session's earlier steps, oldest first: {ts, action, kind, input, output} for tool events,
        {ts, kind: "message", who, text} for free text
    now: Dogwood timestamp of the request, in seconds (same clock as the approvals' "at")
    window: the rule form's "within", in seconds; None if no form was given

    Returns {"facts", "status", "reason", "sources", "details", "review", "ignored_claims"}, where facts is
    what goes into Dogwood's request_context (schema type RefundFacts), or None when there is nothing to
    report, and status is ok, missing, expired, conflict, or None for an action with no facts.
    """
    if action != "IssueRefund":
        return result(None, f"No facts are looked up for {action}.")
    return refund_facts(args, records, steps, now, window)
