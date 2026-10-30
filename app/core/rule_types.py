"""The only rule types we generate policies for.

Each type names the tool it guards, its slots (the values a sentence must or may state),
example sentences, and the converter forms it becomes. A form template value written as
"{slot}" is replaced by that slot's value; a key whose optional slot is missing is dropped.
A qualifier that maps to a field on the action (e.g. "production" -> env) becomes the
form's only_when; that is added by the generator, not written in the templates.
Anything that is not one of these types is refused.
"""

RULE_TYPES = [
    {
        "id": "amount_cap",
        "description": "A single refund may not be larger than a fixed dollar amount.",
        "action": "IssueRefund",
        "slots": [
            {"name": "max_amount", "kind": "amount", "required": True, "field": "amount",
             "description": "the largest refund allowed, in dollars"},
        ],
        "examples": ["No refunds over $5,000.", "A single refund may not exceed $500."],
        "forms": [
            {"kind": "limit", "action": "IssueRefund", "field": "amount", "max": "{max_amount}"},
        ],
    },
    {
        "id": "approval_required",
        "description": "A refund needs an earlier approval, given within a time window, for at least the refund amount.",
        "action": "IssueRefund",
        "slots": [
            {"name": "window", "kind": "duration", "required": True, "field": None,
             "description": "how long ago the approval may have been given"},
            {"name": "approver_role", "kind": "text", "required": False, "field": None,
             "description": "the role of the person who must approve, e.g. a job title"},
        ],
        "examples": ["Only refund if a manager approved it recently.",
                     "Refunds need approval from a supervisor in the last 2 hours."],
        "forms": [
            {"kind": "requires_prior", "action": "IssueRefund",
             "requires": {"action": "RequestApproval", "within": "{window}", "same": ["order_id"],
                          "output": {"approved": True}, "at_most": {"amount": "approved_amount"},
                          "spent_so_far": {"amount": "refunded_so_far"},
                          "approver_role": "{approver_role}"}},
        ],
    },
    {
        "id": "capped_with_approval",
        "description": "Refunds for one specific order are capped at a dollar amount and also need an approval.",
        "action": "IssueRefund",
        "slots": [
            {"name": "max_amount", "kind": "amount", "required": True, "field": "amount",
             "description": "the largest refund allowed for that order, in dollars"},
            {"name": "order_id", "kind": "text", "required": True, "field": "order_id",
             "description": "the order the rule is about, e.g. an order number"},
            {"name": "window", "kind": "duration", "required": True, "field": None,
             "description": "how long ago the approval may have been given"},
            {"name": "approver_role", "kind": "text", "required": False, "field": None,
             "description": "the role of the person who must approve, e.g. a job title"},
        ],
        "examples": ["Refund up to $40 for order A100, subject to approval."],
        "forms": [
            {"kind": "limit", "action": "IssueRefund", "field": "amount", "max": "{max_amount}",
             "only_when": {"field": "order_id", "op": "==", "value": "{order_id}"}},
            {"kind": "requires_prior", "action": "IssueRefund",
             "only_when": {"field": "order_id", "op": "==", "value": "{order_id}"},
             "requires": {"action": "RequestApproval", "within": "{window}", "same": ["order_id"],
                          "output": {"approved": True}, "at_most": {"amount": "approved_amount"},
                          "spent_so_far": {"amount": "refunded_so_far"},
                          "approver_role": "{approver_role}"}},
        ],
    },
    {
        "id": "deploy_allowlist",
        "description": "Only the listed services may be deployed.",
        "action": "DeployService",
        "slots": [
            {"name": "services", "kind": "list", "required": True, "field": "service",
             "description": "the service names allowed; one slot entry per name"},
        ],
        "examples": ["Production deploys are only allowed for payments, search and web."],
        "forms": [
            {"kind": "allowlist", "action": "DeployService", "field": "service", "allowed": "{services}"},
        ],
    },
    {
        "id": "deploy_needs_tests",
        "description": "A service version may only be deployed if its tests passed within a time window.",
        "action": "DeployService",
        "slots": [
            {"name": "window", "kind": "duration", "required": True, "field": None,
             "description": "how long ago the tests may have passed"},
        ],
        "examples": ["A version may only go to production if its tests passed within the last 24 hours."],
        "forms": [
            {"kind": "requires_prior", "action": "DeployService",
             "requires": {"action": "RunTests", "within": "{window}", "same": ["service", "version"],
                          "output": {"passed": True}}},
        ],
    },
]

BY_ID = {t["id"]: t for t in RULE_TYPES}
SLOT_NAMES = sorted({s["name"] for t in RULE_TYPES for s in t["slots"]})


def supported_ids():
    return [t["id"] for t in RULE_TYPES]


def slots_of(type_id):
    return {s["name"]: s for s in BY_ID[type_id]["slots"]}


def refusal_text():
    return "We only support these rule types: " + "; ".join(
        f"{t['id']} ({t['description'].rstrip('.')})" for t in RULE_TYPES) + "."


def fill(template, values):
    """Put slot values into a form template. Optional slots with no value drop their key."""
    if isinstance(template, str) and template.startswith("{") and template.endswith("}"):
        return values.get(template[1:-1], _MISSING)
    if isinstance(template, dict):
        out = {}
        for k, v in template.items():
            v = fill(v, values)
            if v is not _MISSING:
                out[k] = v
        return out
    if isinstance(template, list):
        return [fill(v, values) for v in template]
    return template


_MISSING = object()


def schema_problems(actions):
    """Actions and fields the rule types use that the action schema does not have."""
    problems = []
    for t in RULE_TYPES:
        if t["action"] not in actions:
            problems.append(f"{t['id']}: no action {t['action']!r} in the schema")
            continue
        inp = actions[t["action"]]["input"]
        for s in t["slots"]:
            if s["field"] and s["field"] not in inp:
                problems.append(f"{t['id']}: {t['action']} has no input field {s['field']!r}")
        for f in t["forms"]:
            if f["action"] != t["action"]:
                problems.append(f"{t['id']}: form guards {f['action']}, type says {t['action']}")
    return problems
