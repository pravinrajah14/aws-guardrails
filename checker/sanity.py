"""Happy-path checks: the intended policies must ALLOW legitimate work, so build.py's DENYs mean something."""

import os
from pathlib import Path

from build import POL, TR, approval, deploy, last_verdict, refund_req, refunds_done, tests

cases = [
    (
        "P1",
        "Manager approves A1 $600, refund 100s later",
        approval(0, "A1", 600, True, "manager") + refund_req(100, "A1", 600),
        "ALLOW",
    ),
    ("P1", "Refund $499, no approval", refund_req(0, "A2", 499), "ALLOW"),
    (
        "P2",
        "5th refund in an hour",
        refunds_done("B", 4) + refund_req(400, "B5", 20),
        "ALLOW",
    ),
    ("P3", "Tests pass 90%, deploy 60s later", tests(0, "abc", True, 90) + deploy(60, "abc"), "ALLOW"),
    ("P3", "Staging deploy, no tests", deploy(0, "abc", "staging"), "ALLOW"),
]
for pid, story, evs, want in cases:
    tp = os.path.join(TR, f"sanity_{pid}_{abs(hash(story)) % 1000}.log")
    Path(tp).write_text("\n".join(evs) + "\n")
    got, _ = last_verdict(os.path.join(POL, f"{pid}_intended.dw"), tp)
    print(f"{pid} {'OK ' if got == want else 'BAD'} {got:5}  {story}")
# off-by-one probe: does the attempt-counting mutant block the 5th refund?
tp = os.path.join(TR, "probe_attempt_5th.log")
Path(tp).write_text("\n".join(refunds_done("F", 4) + refund_req(400, "F5", 20)) + "\n")
print("P2_attempt mutant on 5th refund:", last_verdict(os.path.join(POL, "P2_attempt.dw"), tp)[0])
