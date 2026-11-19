"""Policies through Dogwood on a fixed set of histories, so two policies can be compared decision by decision.

The histories sit on the boundaries a wrong value would cross (59 vs 61 minutes, $500 vs $501, the
approver's role, staging vs production), because a 24-hour and a 1-hour window agree on any history
that doesn't fall between them. This is the same comparison as kylie-test-bed-upload/compare_rules.py,
on the app's own gate.
"""

import tempfile
from pathlib import Path

from app.core import config, generate
from app.core.store import BASE, DogwoodGate, build_policy

MINUTE, HOUR, DAY = 60, 3600, 86400
ORDER, OTHER_ORDER = "A300", "A200"  # A300's total (1400) leaves room for every amount below
MANAGER, SUPPORT = "manager.jordan", "support.sam"
VERSION, OTHER_VERSION = "1.5.0", "1.4.0"
REFUND_TIMES = [30, 30 * MINUTE, 59 * MINUTE, 61 * MINUTE, 119 * MINUTE, 121 * MINUTE, 23 * HOUR, 25 * HOUR, 47 * HOUR]
REFUND_AMOUNTS = [40, 100, 101, 250, 499, 500, 501, 1000, 1400]
DEPLOY_TIMES = [
    MINUTE,
    59 * MINUTE,
    61 * MINUTE,
    119 * MINUTE,
    121 * MINUTE,
    23 * HOUR,
    25 * HOUR,
    47 * HOUR,
    49 * HOUR,
]
SERVICES = ["payments", "search", "web", "checkout"]


def _approval(order, amount, at=0, approver=MANAGER, approved=True):
    output = {"approved": approved, "approver": approver, "approved_amount": amount if approved else 0}
    return {
        "at": at,
        "tool": "request_approval",
        "kind": "response",
        "input": {"order_id": order, "amount": amount, "reason": "ranking evaluation"},
        "output": output,
    }


def _refund(order, amount, at):
    return {"at": at, "tool": "issue_refund", "input": {"order_id": order, "amount": amount}, "expect": "?"}


def _tests(service, passed, at=0, version=VERSION):
    return {
        "at": at,
        "tool": "run_tests",
        "kind": "response",
        "input": {"service": service, "version": version},
        "output": {"passed": passed},
    }


def _deploy(service, env, at, version=VERSION):
    return {
        "at": at,
        "tool": "deploy_service",
        "input": {"service": service, "version": version, "env": env},
        "expect": "?",
    }


def _h(hid, events, timed=False):
    return {"id": hid, "events": events, "timed": timed}


def refund_histories():
    out = [_h(f"no approval, ${a}", [_refund(ORDER, a, 0)]) for a in REFUND_AMOUNTS]
    out += [
        _h(
            f"manager approved $1000 {t}s before a $1000 refund",
            [_approval(ORDER, 1000), _refund(ORDER, 1000, t)],
            t > MINUTE,
        )
        for t in REFUND_TIMES
    ]
    out += [
        _h(f"manager approved ${a} a minute before", [_approval(ORDER, a), _refund(ORDER, a, MINUTE)])
        for a in REFUND_AMOUNTS
    ]
    out += [
        _h("approval was for another order", [_approval(OTHER_ORDER, 300), _refund(ORDER, 300, MINUTE)]),
        _h("approved $300, refund $1000", [_approval(ORDER, 300), _refund(ORDER, 1000, MINUTE)]),
        _h("approved $1000, refund $300", [_approval(ORDER, 1000), _refund(ORDER, 300, MINUTE)]),
        _h("manager said no", [_approval(ORDER, 1000, approved=False), _refund(ORDER, 1000, MINUTE)]),
        _h("support approved, not a manager", [_approval(ORDER, 1000, approver=SUPPORT), _refund(ORDER, 1000, MINUTE)]),
    ]
    return out


def deploy_histories():
    out = [
        _h(
            f"tests passed {t}s before a prod deploy",
            [_tests("payments", True), _deploy("payments", "prod", t)],
            t > MINUTE,
        )
        for t in DEPLOY_TIMES
    ]
    out += [_h(f"tests passed, prod deploy of {s}", [_tests(s, True), _deploy(s, "prod", MINUTE)]) for s in SERVICES]
    out += [_h(f"no tests, prod deploy of {s}", [_deploy(s, "prod", 0)]) for s in SERVICES]
    out += [_h(f"no tests, staging deploy of {s}", [_deploy(s, "staging", 0)]) for s in SERVICES]
    out += [
        _h("tests failed", [_tests("payments", False), _deploy("payments", "prod", MINUTE)]),
        _h(
            "tests passed for another version",
            [_tests("payments", True, version=OTHER_VERSION), _deploy("payments", "prod", MINUTE)],
        ),
        _h("tests passed, staging deploy", [_tests("payments", True), _deploy("payments", "staging", MINUTE)]),
    ]
    return out


def histories(action, gap_free=False):
    """The fixed histories for an action. gap_free drops the ones that depend on how long ago something happened."""
    found = refund_histories() if action == "IssueRefund" else deploy_histories() if action == "DeployService" else []
    return [h for h in found if not (gap_free and h["timed"])]


class Policy:
    """One policy ready for Dogwood: from forms (the converter) or from Dogwood text."""

    def __init__(self, forms=None, dogwood=None, workdir=None):
        self.dir = Path(workdir or tempfile.mkdtemp(prefix="policy-", dir=_scratch()))
        if forms is not None:
            out = self.dir / "forms"
            policy = generate.write_policy(forms, out)
            self.gate = DogwoodGate(out, policy, config.SCHEMA_PATH, form=generate.engine_form(forms))
        else:
            self.dir.mkdir(parents=True, exist_ok=True)
            rule = self.dir / "rule.dw"
            rule.write_text(dogwood, encoding="utf-8")
            policy = build_policy(BASE, rule, self.dir / "policy.dw")
            self.gate = DogwoodGate(self.dir, policy, config.SCHEMA_PATH)
        self.valid, self.validate_output = self.gate.validate()

    def decide(self, history):
        if not self.valid:
            return "INVALID"
        try:
            return generate.replay_case(self.gate, {"id": "h", "events": [dict(e) for e in history["events"]]})[
                "verdict"
            ]
        except (RuntimeError, ValueError) as e:
            return f"ERROR: {e}"[:80]


def _scratch():
    root = config.REPO / "pipeline-runs" / "ranking-eval-policies"
    root.mkdir(parents=True, exist_ok=True)
    return root


def compare(reference, candidate, found):
    """Each history under both policies. wrong allow: the candidate allows what the reference blocks."""
    rows = []
    for h in found:
        want, got = reference.decide(h), candidate.decide(h)
        if got == want:
            result = "same"
        elif want == "DENY" and got == "ALLOW":
            result = "wrong allow"
        elif want == "ALLOW" and got == "DENY":
            result = "wrong block"
        else:
            result = "error"
        rows.append({"history": h["id"], "reference": want, "candidate": got, "result": result})
    return rows


def agreement(a, b, found):
    """Share of histories two policies decide the same way, or None if either can't run."""
    if not found or not a.valid or not b.valid:
        return None
    same = sum(a.decide(h) == b.decide(h) for h in found)
    return same / len(found)
