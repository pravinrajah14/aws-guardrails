"""Concrete examples at a rule's edges, each replayed through Dogwood and compared with what the summary says."""

from app.core import glossary, rule_types
from app.core.generate import replay_case, values_of
from app.core.numbers import MINUTE, how_long
from app.core.store import STAFF, MockWorld

ALLOW, DENY = "ALLOW", "DENY"
# An order the store doesn't hold: its total can't decide the example, so only the rule under test does.
UNKNOWN_ORDER = "Z900"
UNLISTED_SERVICE = "unlisted-service"
VERSION = "1.5.0"


def _example(says, expect, events):
    return {"says": says, "expect": expect, "events": events}


def _refund(order_id, amount, at=0):
    return {"at": at, "tool": "issue_refund", "input": {"order_id": order_id, "amount": amount}, "expect": "?"}


def _approval(order_id, amount, at=0, approver="manager.jordan"):
    return {
        "at": at,
        "tool": "request_approval",
        "kind": "response",
        "input": {"order_id": order_id, "amount": amount, "reason": "edge example"},
        "output": {"approved": True, "approver": approver, "approved_amount": amount},
    }


def _tests(service, passed, at=0):
    return {
        "at": at,
        "tool": "run_tests",
        "kind": "response",
        "input": {"service": service, "version": VERSION},
        "output": {"passed": passed},
    }


def _deploy(service, env, at=0, version=VERSION):
    return {
        "at": at,
        "tool": "deploy_service",
        "input": {"service": service, "version": version, "env": env},
        "expect": "?",
    }


def _just_past(seconds):
    """One step past the window, in the rule's own unit so the example reads naturally."""
    return seconds + MINUTE if seconds % MINUTE == 0 else seconds + 1


def _edges(values):
    """(highest amount allowed, lowest amount blocked) for a cap."""
    cap = values["max_amount"]
    return (cap, cap + 1) if values["block_op"] == ">" else (cap - 1, cap)


def _scope_order(u):
    scope = next((q for q in u["qualifiers"] if q["mapped"] and q["field"] == "order_id"), None)
    return scope["value"] if scope else None


def _cap_examples(u, values):
    allowed, blocked = _edges(values)
    order = _scope_order(u) or UNKNOWN_ORDER
    # The store-wide rule also caps a refund at the order's total, so stay inside it to show this rule's edge.
    total = MockWorld().orders.get(order, {}).get("total")
    allowed = min(allowed, total) if total else allowed
    far = blocked * 2
    examples = [
        _example(f"A ${allowed:,} refund is allowed.", ALLOW, [_refund(order, allowed)]),
        _example(f"A ${blocked:,} refund is blocked.", DENY, [_refund(order, blocked)]),
        _example(f"A ${far:,} refund is blocked.", DENY, [_refund(order, far)]),
        _example("A $1 refund is allowed.", ALLOW, [_refund(order, 1)]),
    ]
    if _scope_order(u):
        examples.append(
            _example(
                f"A ${blocked:,} refund on another order isn't limited by this rule.",
                ALLOW,
                [_refund(UNKNOWN_ORDER, blocked)],
            )
        )
    return examples


def _staff_with_other_role(role):
    return next(name for name, person in STAFF.items() if person["role"] != role)


def _staff_with_role(role):
    return next(name for name, person in STAFF.items() if person["role"] == role)


def _approval_examples(u, values, order, full, seconds):
    """Refunds on an order worth at least `full`, around the approval's window, amount and role."""
    role = values["approver_role"]
    manager = _staff_with_role(role)
    late = _just_past(seconds)
    part = max(1, full * 5 // 8)
    return [
        _example(
            f"Approved ${full:,} by a {role} {how_long(seconds)} ago: a ${full:,} refund is allowed.",
            ALLOW,
            [_approval(order, full, 0, manager), _refund(order, full, seconds)],
        ),
        _example(
            f"Approved ${full:,} by a {role} {how_long(late)} ago: a ${full:,} refund is blocked.",
            DENY,
            [_approval(order, full, 0, manager), _refund(order, full, late)],
        ),
        _example(
            f"Approved ${part:,} with a ${full:,} refund: blocked.",
            DENY,
            [_approval(order, part, 0, manager), _refund(order, full, MINUTE)],
        ),
        _example(
            f"Approved ${part:,} with a ${part:,} refund: allowed.",
            ALLOW,
            [_approval(order, part, 0, manager), _refund(order, part, MINUTE)],
        ),
        _example(
            f"Approved by someone who isn't a {role}: blocked.",
            DENY,
            [_approval(order, full, 0, _staff_with_other_role(role)), _refund(order, full, MINUTE)],
        ),
    ]


def _window_seconds(u):
    return u["slots"]["window"]["seconds"]


def _approval_required_examples(u, values):
    order = _scope_order(u) or "A100"
    full = MockWorld().orders[order]["total"]
    examples = _approval_examples(u, values, order, full, _window_seconds(u))
    examples.append(_example("No approval at all: blocked.", DENY, [_refund(order, full)]))
    return examples


def _capped_examples(u, values):
    order = values["order_id"]
    total = MockWorld().orders[order]["total"]
    allowed, blocked = _edges(values)
    full = min(allowed, total)
    examples = _approval_examples(u, values, order, full, _window_seconds(u))
    manager = _staff_with_role(values["approver_role"])
    examples.append(
        _example(
            f"Approved ${blocked:,}: a ${blocked:,} refund is over the cap, so it's blocked.",
            DENY,
            [_approval(order, blocked, 0, manager), _refund(order, blocked, MINUTE)],
        )
    )
    return examples


def _env(u):
    return next((q["value"] for q in u["qualifiers"] if q["mapped"] and q["field"] == "env"), None)


def _other_env(env):
    return next(v for v in glossary.known_values("env") if v != env)


def _allowlist_examples(u, values):
    services = values["services"]
    env = _env(u) or "prod"
    examples = [
        _example(f"Deploying {services[0]} to {env}: allowed.", ALLOW, [_deploy(services[0], env)]),
        _example(f"Deploying {services[-1]} to {env}: allowed.", ALLOW, [_deploy(services[-1], env)]),
        _example(f"Deploying {UNLISTED_SERVICE} to {env}: blocked.", DENY, [_deploy(UNLISTED_SERVICE, env)]),
    ]
    other = _other_env(env)
    if _env(u):
        examples.append(
            _example(f"Deploying {UNLISTED_SERVICE} to {other}: allowed.", ALLOW, [_deploy(UNLISTED_SERVICE, other)])
        )
    else:
        examples.append(
            _example(f"Deploying {UNLISTED_SERVICE} to {other}: blocked.", DENY, [_deploy(UNLISTED_SERVICE, other)])
        )
    return examples


def _needs_tests_examples(u, values):
    env = _env(u) or "prod"
    seconds = _window_seconds(u)
    late = _just_past(seconds)
    service = "checkout"
    examples = [
        _example(
            f"Tests passed {how_long(seconds)} ago: deploying to {env} is allowed.",
            ALLOW,
            [_tests(service, True), _deploy(service, env, seconds)],
        ),
        _example(
            f"Tests passed {how_long(late)} ago: deploying to {env} is blocked.",
            DENY,
            [_tests(service, True), _deploy(service, env, late)],
        ),
        _example(
            f"Tests failed: deploying to {env} is blocked.",
            DENY,
            [_tests(service, False), _deploy(service, env, MINUTE)],
        ),
        _example(f"No tests at all: deploying to {env} is blocked.", DENY, [_deploy(service, env)]),
        _example(
            "Tests passed for another version: blocked.",
            DENY,
            [_tests(service, True), _deploy(service, env, MINUTE, version="9.9.9")],
        ),
    ]
    if _env(u):
        other = _other_env(env)
        examples.append(_example(f"No tests, deploying to {other}: allowed.", ALLOW, [_deploy(service, other)]))
    return examples


BUILDERS = {
    rule_types.AMOUNT_CAP: _cap_examples,
    rule_types.APPROVAL_REQUIRED: _approval_required_examples,
    rule_types.CAPPED_WITH_APPROVAL: _capped_examples,
    rule_types.DEPLOY_ALLOWLIST: _allowlist_examples,
    rule_types.DEPLOY_NEEDS_TESTS: _needs_tests_examples,
}


def build(u):
    return BUILDERS[u["rule_type"]](u, values_of(u))


def replay(gate, example):
    """One example through Dogwood. The last event is the decision being shown."""
    decided = replay_case(gate, {"id": "edge", "events": [dict(e) for e in example["events"]]})
    return {
        **example,
        "got": decided["verdict"],
        "rules": decided["rules"],
        "agrees": decided["verdict"] == example["expect"],
    }


def run_all(u, gate):
    return [replay(gate, example) for example in build(u)]
