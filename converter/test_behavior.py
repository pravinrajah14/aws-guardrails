#!/usr/bin/env python3
"""Check generated-policy decisions using real Dogwood replay."""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from convert import convert, json

HERE = Path(__file__).parent
SCHEMA = HERE.parent / "kylie-test-bed-upload" / "policies" / "schema.cedarschema"
DOGWOOD = os.environ.get("DOGWOOD", "dogwood")
# Same principal/resource/event shape as kylie-test-bed/enforce.py.
P = 'scope(principal: Shop::OAuthUser::"support-agent", resource: Shop::Gateway::"testbed")'
WHO = 'callerPrincipal: Shop::OAuthUser::"support-agent", callerResource: Shop::Gateway::"testbed"'
_rid = [0]


def rec(d):
    return "{ " + ", ".join(f"{k}: {json.dumps(v)}" for k, v in d.items()) + " }"


def ev(t, kind, action, inp, out=None):
    _rid[0] += 1
    o = f", output: {rec(out)}" if out is not None else ""
    return (f'@{t} {P} request_context(input: {rec(inp)}) Shop::Action::"{action}"::{kind}'
            f'(input: {rec(inp)}{o}, {WHO}, requestId: "r{_rid[0]}")')


def approval(t, order="A200", amount=300, approved=True, approved_amount=None):
    i = {"order_id": order, "amount": amount, "reason": "damaged"}
    return [ev(t, "request", "RequestApproval", i),
            ev(t + 1, "response", "RequestApproval", i,
               {"approved": approved, "approver": "manager.jordan",
                "approved_amount": amount if approved_amount is None else approved_amount})]


def refund(t, order="A200", amount=300):
    return [ev(t, "request", "IssueRefund", {"order_id": order, "amount": amount})]


H = 3600
CASES = [  # (name, history, expected verdict on the final Refund)
    ("requested 300 but only 100 approved: block 300", approval(0, approved_amount=100) + refund(600), "DENY"),
    ("requested 300 but only 100 approved: allow 100", approval(0, approved_amount=100) + refund(600, amount=100), "ALLOW"),
    ("approval is exact, not a ceiling: block 90", approval(0, approved_amount=100) + refund(600, amount=90), "DENY"),
    ("manager approved, same order+amount, 10 min ago", approval(0) + refund(600), "ALLOW"),
    ("no approval at all", refund(0), "DENY"),
    ("approved for a different amount", approval(0, amount=100) + refund(600, amount=300), "DENY"),
    ("approved for a different order", approval(0, order="A100") + refund(600), "DENY"),
    ("approval was denied (approved=false)", approval(0, approved=False) + refund(600), "DENY"),
    ("approval 59 min ago (inside window)", approval(0) + refund(59 * 60), "ALLOW"),
    ("approval 61 min ago (stale)", approval(0) + refund(61 * 60), "DENY"),
]


def main():
    forms = [("manager_approval.json", json.loads((HERE / "forms/manager_approval.json").read_text()))]
    policy = convert(forms, SCHEMA, baseline=True)
    bad = 0
    with tempfile.TemporaryDirectory() as d:
        pol = Path(d, "p.dw")
        pol.write_text(policy)
        for name, lines, want in CASES:
            tr = Path(d, "t.log")
            tr.write_text("\n".join(lines) + "\n")
            r = subprocess.run([DOGWOOD, "replay", str(pol), "--policy-schema", str(SCHEMA),
                                "--trace", str(tr)], capture_output=True, text=True)
            rows = [l for l in (r.stdout + r.stderr).splitlines() if "time point" in l]
            got = "ERROR" if r.returncode or not rows else ("ALLOW" if "ALLOW" in rows[-1] else "DENY")
            bad += got != want
            print(f"{'ok  ' if got == want else 'FAIL'} {name}: expected {want}, got {got}")
            if got == "ERROR":
                print(r.stdout + r.stderr)
    print(f"\n{len(CASES) - bad}/{len(CASES)} correct")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
