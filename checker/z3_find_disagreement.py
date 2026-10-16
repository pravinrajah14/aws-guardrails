"""Proof of concept for the offline checker: ask Z3 for a short history where
the intended P1 policy and a buggy variant disagree, then replay that history
through the real Dogwood CLI to confirm the disagreement.

The P1 semantics are hand-encoded here (bounded: up to K earlier approval
events, then one refund decision). A real checker would generate this
encoding from the Dogwood policy instead of writing it by hand.
"""
import os, sys
from z3 import Int, Bool, Solver, And, Or, Not, If, sat
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build import approval, refund_req, last_verdict, POL, TR

K = 2                      # earlier approval events to consider
MANAGER, SUPPORT = 0, 1
ROLE = {MANAGER: "manager", SUPPORT: "support"}

def p1(dec, evs, window=3600, small=lambda a: a < 500, large=lambda a: a >= 500,
       pin_order=True, need_outcome=True, need_manager=True):
    order, amount, now = dec
    def matches(e):
        o, a, ok, role, t = e
        t_resp = t + 10            # the approval *response* lands 10s after the request
        c = [now - t_resp >= 0, now - t_resp <= window, a == amount]
        if pin_order: c.append(o == order)
        if need_outcome: c.append(ok)
        if need_manager: c.append(role == MANAGER)
        return And(*c)
    return Or(small(amount), And(large(amount), Or(*[matches(e) for e in evs])))

MUTANTS = {
    "window":       dict(window=86400),
    "boundary":     dict(small=lambda a: a <= 500, large=lambda a: a > 500),
    "attempt":      dict(need_outcome=False, need_manager=False),
    "person":       dict(need_manager=False),
    "other_refund": dict(pin_order=False),
}

for name, kw in MUTANTS.items():
    s = Solver()
    evs = [(Int(f"o{i}"), Int(f"a{i}"), Bool(f"ok{i}"), Int(f"r{i}"), Int(f"t{i}")) for i in range(K)]
    dec = (Int("order"), Int("amount"), Int("now"))
    for o, a, ok, r, t in evs:
        s.add(o >= 1, o <= 9, a >= 1, a <= 2000, Or(r == MANAGER, r == SUPPORT), t >= 0)
    s.add(dec[0] >= 1, dec[0] <= 9, dec[1] >= 1, dec[1] <= 2000)
    s.add(*[dec[2] > t + 10 for *_, t in evs])            # decision comes after each approval
    s.add(*[evs[i][4] + 20 <= evs[i + 1][4] for i in range(K - 1)])
    s.add(p1(dec, evs) != p1(dec, evs, **kw))              # the two policies disagree
    if s.check() != sat:
        print(f"{name:13} no disagreement within K={K}")
        continue
    m = s.model()
    v = lambda x: m.eval(x, model_completion=True).as_long()
    b = lambda x: bool(m.eval(x, model_completion=True))
    lines, desc = [], []
    for o, a, ok, r, t in evs:
        lines += approval(v(t), f"A{v(o)}", v(a), b(ok), ROLE[v(r)])
        desc.append(f"t={v(t)}s approval A{v(o)} ${v(a)} {'granted' if b(ok) else 'declined'} by {ROLE[v(r)]}")
    lines += refund_req(v(dec[2]), f"A{v(dec[0])}", v(dec[1]))
    desc.append(f"t={v(dec[2])}s refund A{v(dec[0])} ${v(dec[1])}")
    tp = os.path.join(TR, f"z3_P1_{name}.log")
    open(tp, "w").write("\n".join(lines) + "\n")
    want, _ = last_verdict(os.path.join(POL, "P1_intended.dw"), tp)
    got, _ = last_verdict(os.path.join(POL, f"P1_{name}.dw"), tp)
    status = "confirmed by Dogwood" if want != got else "NOT confirmed"
    print(f"{name:13} intended={want:5} buggy={got:5} [{status}]  " + " ; ".join(desc))
