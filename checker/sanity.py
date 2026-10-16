"""Happy-path checks: the intended policies must ALLOW legitimate work,
so the DENY verdicts in build.py are not from a policy that denies everything."""
import os, build
from build import approval, refund_req, refund_done, tests, deploy, last_verdict, POL, TR
cases = [
 ("P1", "Manager approves A1 $600, refund 100s later", approval(0,"A1",600,True,"manager")+refund_req(100,"A1",600), "ALLOW"),
 ("P1", "Refund $499, no approval", refund_req(0,"A2",499), "ALLOW"),
 ("P2", "5th refund in an hour", sum((refund_done(i*60,f"B{i}",20) for i in range(4)),[])+refund_req(400,"B5",20), "ALLOW"),
 ("P3", "Tests pass 90%, deploy 60s later", tests(0,"abc",True,90)+deploy(60,"abc"), "ALLOW"),
 ("P3", "Staging deploy, no tests", deploy(0,"abc","staging"), "ALLOW"),
]
for pid, story, evs, want in cases:
    tp = os.path.join(TR, f"sanity_{pid}_{abs(hash(story))%1000}.log"); open(tp,"w").write("\n".join(evs)+"\n")
    got,_ = last_verdict(os.path.join(POL,f"{pid}_intended.dw"), tp)
    print(f"{pid} {'OK ' if got==want else 'BAD'} {got:5}  {story}")
# off-by-one probe: does the attempt-counting mutant block the 5th refund?
tp = os.path.join(TR,"probe_attempt_5th.log")
open(tp,"w").write("\n".join(sum((refund_done(i*60,f"F{i}",20) for i in range(4)),[])+refund_req(400,"F5",20))+"\n")
print("P2_attempt mutant on 5th refund:", last_verdict(os.path.join(POL,"P2_attempt.dw"), tp)[0])
