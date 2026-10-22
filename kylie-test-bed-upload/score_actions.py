"""Score a rule WITHOUT the agent: replay scripted action lists through Dogwood and compare
every decision to its known right answer. Any wrong decision is the rule's fault.

  python score_actions.py tests/refund_recent_approval_actions.json --rules rules/refund_recent_approval.dw

Action list format (draft, agree with the team):
{
  "cases": [
    {
      "id": "approved_30m_ago",
      "events": [
        {"at": "0m",  "tool": "request_approval", "kind": "response",
         "input": {"order_id": "A100", "amount": 40, "reason": "damaged"},
         "output": {"approved": true, "approver": "manager.jordan", "approved_amount": 40}},
        {"at": "30m", "tool": "issue_refund", "input": {"order_id": "A100", "amount": 40}, "expect": "ALLOW"}
      ]
    }
  ]
}
- "at" is time since the case started: seconds as a number, or "45s", "30m", "3h", "1d". Must increase.
- "kind" is "request" (default: the agent asks to do something, Dogwood decides) or
  "response" / "error" (history: something that already happened).
- Only requests with "expect" are scored. Each case is its own fresh history.
"""
import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from enforce import DogwoodGate, build_policy
from tools import TOOL_SPECS, check_args

SCHEMA = "policies/schema.cedarschema"
UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def to_seconds(at):
    if isinstance(at, (int, float)):
        return int(at)
    m = re.fullmatch(r"\s*(\d+)\s*([smhd])\s*", str(at))
    if not m:
        raise ValueError(f"bad time {at!r}: use a number of seconds or e.g. '30m', '3h'")
    return int(m.group(1)) * UNITS[m.group(2)]


def case_lines(gate, case):
    """Turn one case's events into Dogwood trace lines. Returns (lines, scored requests)."""
    lines, scored, last_ts = [], [], -1
    for i, ev in enumerate(case["events"]):
        ts = to_seconds(ev["at"]) + 1          # +1 so "0m" is a valid first timestamp
        if ts <= last_ts:
            raise ValueError(f"case {case['id']}: event {i} is not later than the one before it")
        last_ts = ts
        if ev["tool"] not in TOOL_SPECS:
            raise ValueError(f"case {case['id']}: unknown tool {ev['tool']}")
        action = TOOL_SPECS[ev["tool"]]["action"]
        args = check_args(ev["tool"], ev["input"])
        kind = ev.get("kind", "request")
        rid = f"{case['id']}-{i}"
        if kind == "request":
            lines.append(gate.event_line(ts, action, "request", args, request_id=rid, with_context=True))
            if "expect" in ev:
                scored.append({"ts": ts, "event": i, "call": f"{ev['tool']}({args})", "expect": ev["expect"].upper()})
        else:
            lines.append(gate.event_line(ts, action, kind, args, output=ev.get("output"), request_id=rid))
    return lines, scored


def main():
    p = argparse.ArgumentParser()
    p.add_argument("actions", help="action list file (JSON)")
    p.add_argument("--rules", required=True, help="rule file under test (.dw)")
    p.add_argument("--base", default="policies/base.dw")
    args = p.parse_args()

    cases = json.loads(Path(args.actions).read_text(encoding="utf-8"))["cases"]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    work = Path(".provenance") / f"score-{stamp}"
    work.mkdir(parents=True, exist_ok=True)
    policy = build_policy(args.base, args.rules, work / "policy.dw")
    gate = DogwoodGate(work, policy, SCHEMA)

    ok, msg = gate.validate()
    if not ok:
        sys.exit(f"Rule file did not validate:\n{msg}")

    rows = []
    for case in cases:
        lines, scored = case_lines(gate, case)
        (work / f"{case['id']}.trace.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
        ran, verdicts, problem = gate.replay(lines)
        by_ts = {v["ts"]: v for v in verdicts}
        for s in scored:
            v = by_ts.get(s["ts"])
            got = v["decision"] if (ran and v) else "ERROR"
            rules = ", ".join(gate.rule_name(i) for i in v["rules"]) if v else problem[:120]
            rows.append({"case": case["id"], **s, "got": got, "rules": rules, "correct": got == s["expect"]})

    right = sum(r["correct"] for r in rows)
    out = Path("results")
    out.mkdir(exist_ok=True)
    (out / f"rule_scorecard_{stamp}.json").write_text(
        json.dumps({"rules": args.rules, "actions": args.actions, "right": right, "total": len(rows), "decisions": rows}, indent=2))
    md = [f"# Rule scorecard, no agent ({stamp})", "",
          f"- Rule file: `{args.rules}`  |  Action lists: `{args.actions}`",
          f"- **{right} of {len(rows)} decisions correct.** Any wrong decision is the rule's fault.", "",
          "| Case | Call | Expected | Got | Rule(s) | Correct |", "|---|---|---|---|---|---|"]
    for r in rows:
        md.append(f"| {r['case']} | `{r['call']}` | {r['expect']} | {r['got']} | {r['rules'] or '-'} | {'yes' if r['correct'] else '**NO**'} |")
    (out / f"rule_scorecard_{stamp}.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md))
    print(f"\nSaved results/rule_scorecard_{stamp}.md (traces in {work})")


if __name__ == "__main__":
    main()
