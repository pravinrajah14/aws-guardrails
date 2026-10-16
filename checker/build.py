"""Planted-error harness for Dogwood policies.

For each base policy, write the intended policy and one mutant per planted
error, then (1) run `dogwood validate` on every file and (2) replay one
hand-built trace per mutant through both the intended policy and the mutant.
A mutant that validates but gives a different verdict on its trace is an
error in meaning, not syntax.
"""
import json, os, subprocess, sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DOGWOOD = os.environ.get("DOGWOOD", os.path.expanduser("~/a3/dogwood/target/release/dogwood"))
SCHEMA = os.path.join(ROOT, "schema.cedarschema")
POL = os.path.join(ROOT, "policies")
TR = os.path.join(ROOT, "traces")

# ---------------------------------------------------------------- policies
P1 = '''// P1 (prerequisite): refunds of $500 or more need a manager's approval
// for this same refund (same order, same amount) within the last hour.
@id("refund_small")
permit (principal, action == Shop::Action::"IssueRefund", resource)
when { context.input.amount < 500 };

@id("refund_large_approved")
permit (principal, action == Shop::Action::"IssueRefund", resource)
when { context.input.amount >= 500 }
when temporal {
    formerly within 1h Shop::Action::"RequestApproval"::response{
        input.order_id: context.input.order_id,
        input.amount: context.input.amount,
        output.approved: true,
        output.approver_role: "manager"
    }
};
'''

P2 = '''// P2 (rate limit): an agent may issue at most 5 refunds per hour.
// The 6th completed refund inside one hour is blocked.
def temporal count_formerly(?w, ?s) {
    count for ($t: Timepoint). where (formerly within ?w (?s && tp($t)))
};

@id("refund_allowed")
permit (principal, action == Shop::Action::"IssueRefund", resource);

@id("refund_rate_limit")
forbid (principal, action == Shop::Action::"IssueRefund", resource)
when temporal {
    exists (n: Long). (
        (count_formerly(1h, Shop::Action::"IssueRefund"::response{
            input.order_id: _, input.customer: _
        })) == n
        && n >= 5
    )
};
'''

P3 = '''// P3 (prerequisite, deploys): a production deploy needs a passing test run
// on the same commit, with coverage of at least 80%, in the last 30 minutes.
@id("deploy_nonprod")
permit (principal, action == Shop::Action::"Deploy", resource)
when { context.input.env != "prod" };

@id("deploy_prod_tested")
permit (principal, action == Shop::Action::"Deploy", resource)
when { context.input.env == "prod" }
when temporal {
    exists (c: Long). (
        formerly within 30m Shop::Action::"RunTests"::response{
            input.commit: context.input.commit,
            output.passed: true,
            output.coverage: c
        }
        && c >= 80
    )
};
'''

# (policy id, error id, label, list of (old, new) replacements)
MUTANTS = [
    ("P1", "window", "Wrong time window (1h -> 24h)",
        [("formerly within 1h", "formerly within 24h")]),
    ("P1", "boundary", "> vs >= ($500 itself skips approval)",
        [("amount < 500", "amount <= 500"), ("amount >= 500", "amount > 500")]),
    ("P1", "attempt", "Checks the attempt, not the outcome (approval asked, not granted)",
        [('"RequestApproval"::response{', '"RequestApproval"::request{'),
         (',\n        output.approved: true,\n        output.approver_role: "manager"', '')]),
    ("P1", "person", "Wrong person (any approver role accepted)",
        [('output.approver_role: "manager"', 'output.approver_role: _')]),
    ("P1", "other_refund", "Approval that belongs to a different refund (order not pinned)",
        [("input.order_id: context.input.order_id", "input.order_id: _")]),

    ("P2", "window", "Wrong time window (per hour -> per day)",
        [("count_formerly(1h,", "count_formerly(24h,")]),
    ("P2", "boundary", "> vs >= (6th refund slips through)",
        [("n >= 5", "n > 5")]),
    ("P2", "attempt", "Counts attempts, not completed refunds",
        [('"IssueRefund"::response{', '"IssueRefund"::request{')]),
    ("P2", "person", "Wrong scope (limit counted per customer, not per agent)",
        [("input.customer: _", "input.customer: context.input.customer")]),

    ("P3", "window", "Wrong time window (30m -> 24h)",
        [("formerly within 30m", "formerly within 24h")]),
    ("P3", "boundary", "> vs >= (exactly 80% coverage rejected)",
        [("c >= 80", "c > 80")]),
    ("P3", "attempt", "Checks the attempt, not the outcome (tests started, not passed)",
        [('''exists (c: Long). (
        formerly within 30m Shop::Action::"RunTests"::response{
            input.commit: context.input.commit,
            output.passed: true,
            output.coverage: c
        }
        && c >= 80
    )''', '''formerly within 30m Shop::Action::"RunTests"::request{
        input.commit: context.input.commit
    }''')]),
    ("P3", "other_refund", "Approval/evidence for a different item (tests on any commit)",
        [("input.commit: context.input.commit", "input.commit: _")]),
]

# ---------------------------------------------------------------- traces
def val(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    return json.dumps(v)

def rec(d):
    return "{ " + ", ".join(f"{k}: {val(v)}" for k, v in d.items()) + " }"

_rid = [0]
def ev(t, action, kind, inp, out=None, who="agent1"):
    if kind == "request":
        _rid[0] += 1
    rid = f"r{_rid[0]}"
    p = f'Shop::OAuthUser::"{who}"'
    g = 'Shop::Gateway::"gw1"'
    outpart = f", output: {rec(out)}" if out is not None else ""
    return (f"@{t} scope(principal: {p}, resource: {g}) request_context(input: {rec(inp)}) "
            f'Shop::Action::"{action}"::{kind}(input: {rec(inp)}{outpart}, '
            f'callerPrincipal: {p}, callerResource: {g}, requestId: "{rid}")')

def approval(t, order, amount, approved, role):
    i = {"order_id": order, "amount": amount}
    return [ev(t, "RequestApproval", "request", i),
            ev(t + 10, "RequestApproval", "response", i, {"approved": approved, "approver_role": role})]

def refund_req(t, order, amount, cust="c1"):
    return [ev(t, "IssueRefund", "request", {"order_id": order, "customer": cust, "amount": amount})]

def refund_done(t, order, amount, cust="c1"):
    i = {"order_id": order, "customer": cust, "amount": amount}
    return [ev(t, "IssueRefund", "request", i),
            ev(t + 5, "IssueRefund", "response", i, {"refund_id": "x" + order})]

def tests(t, commit, passed, cov, response=True):
    i = {"commit": commit}
    out = [ev(t, "RunTests", "request", i)]
    if response:
        out.append(ev(t + 20, "RunTests", "response", i, {"passed": passed, "coverage": cov}))
    return out

def deploy(t, commit, env="prod"):
    return [ev(t, "Deploy", "request", {"commit": commit, "env": env})]

# Each trace: (story, intended verdict on the LAST decision) -- the last line
# is always the decision we compare.
TRACES = {
    ("P1", "window"): ("Manager approves A1 $600, refund issued 2 hours later",
        approval(0, "A1", 600, True, "manager") + refund_req(7200, "A1", 600)),
    ("P1", "boundary"): ("Refund of exactly $500 with no approval",
        refund_req(0, "A2", 500)),
    ("P1", "attempt"): ("Approval requested but declined, then refund",
        approval(0, "A3", 600, False, "manager") + refund_req(100, "A3", 600)),
    ("P1", "person"): ("Support rep (not a manager) approves, then refund",
        approval(0, "A4", 600, True, "support") + refund_req(100, "A4", 600)),
    ("P1", "other_refund"): ("Manager approves order A9, agent refunds order A5",
        approval(0, "A9", 600, True, "manager") + refund_req(100, "A5", 600)),

    ("P2", "window"): ("5 refunds early, 6th refund 70 minutes later",
        sum((refund_done(i * 60, f"B{i}", 20) for i in range(5)), []) + refund_req(4200, "B6", 20)),
    ("P2", "boundary"): ("6th refund within 10 minutes",
        sum((refund_done(i * 60, f"C{i}", 20) for i in range(5)), []) + refund_req(600, "C6", 20)),
    ("P2", "attempt"): ("3 completed refunds plus 2 blocked attempts, then another refund",
        sum((refund_done(i * 60, f"D{i}", 20) for i in range(3)), [])
        + refund_req(200, "D8", 20) + refund_req(260, "D9", 20) + refund_req(400, "D10", 20)),
    ("P2", "person"): ("5 refunds for customer c1, 6th refund for customer c2",
        sum((refund_done(i * 60, f"E{i}", 20, "c1") for i in range(5)), []) + refund_req(400, "E6", 20, "c2")),

    ("P3", "window"): ("Tests pass, prod deploy 1 hour later",
        tests(0, "abc", True, 90) + deploy(3600, "abc")),
    ("P3", "boundary"): ("Tests pass with exactly 80% coverage, then deploy",
        tests(0, "abc", True, 80) + deploy(60, "abc")),
    ("P3", "attempt"): ("Tests run and fail, then deploy",
        tests(0, "abc", False, 90) + deploy(60, "abc")),
    ("P3", "other_refund"): ("Tests pass on commit abc, agent deploys commit def",
        tests(0, "abc", True, 90) + deploy(60, "def")),
}

# ---------------------------------------------------------------- run
def run(*args):
    r = subprocess.run([DOGWOOD, *args], capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()

def last_verdict(policy_path, trace_path):
    code, out = run("replay", policy_path, "--policy-schema", SCHEMA, "--trace", trace_path)
    if code != 0:
        return "ERROR", out
    lines = [l for l in out.splitlines() if "time point" in l]
    return ("ALLOW" if "ALLOW" in lines[-1] else "DENY"), out

def main():
    base = {"P1": P1, "P2": P2, "P3": P3}
    rows, ok_all = [], True
    for pid, src in base.items():
        p = os.path.join(POL, f"{pid}_intended.dw")
        open(p, "w").write(src)
        code, out = run("validate", p, "--policy-schema", SCHEMA)
        print(f"[validate] {pid}_intended.dw -> {'PASS' if code == 0 else 'FAIL'}")
        if code != 0:
            print(out); ok_all = False
    for pid, eid, label, reps in MUTANTS:
        src = base[pid]
        for old, new in reps:
            if old not in src:
                sys.exit(f"replacement not found in {pid}/{eid}: {old!r}")
            src = src.replace(old, new)
        mp = os.path.join(POL, f"{pid}_{eid}.dw")
        open(mp, "w").write(f"// PLANTED ERROR: {label}\n" + src)
        vcode, vout = run("validate", mp, "--policy-schema", SCHEMA)
        story, events = TRACES[(pid, eid)]
        tp = os.path.join(TR, f"{pid}_{eid}.log")
        open(tp, "w").write("\n".join(events) + "\n")
        want, _ = last_verdict(os.path.join(POL, f"{pid}_intended.dw"), tp)
        got, gout = last_verdict(mp, tp)
        rows.append((pid, eid, label, "PASS" if vcode == 0 else "FAIL", story, want, got))
        if vcode != 0 or want == got or "ERROR" in (want, got):
            ok_all = False
            print(vout or gout)
    print()
    print(f"{'policy':6} {'error':13} {'validate':9} {'intended':9} {'mutant':7}  scenario")
    for pid, eid, label, v, story, want, got in rows:
        flag = "" if want != got else "   <-- NOT DISTINGUISHED"
        print(f"{pid:6} {eid:13} {v:9} {want:9} {got:7}  {story}{flag}")
    print("\nALL MUTANTS VALIDATE AND DIFFER IN MEANING" if ok_all else "\nSOME CHECKS FAILED")

if __name__ == "__main__":
    main()
