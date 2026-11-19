"""Quiz check #2: does the rule the tool wrote make the same allow/block calls as the correct rule?

For one quiz test, run the same scenarios through two rule files:
  - the CORRECT rule (from the answer key)
  - the CANDIDATE rule (what the tool wrote)
Every request in every scenario is decided by Dogwood twice, once per rule, and the
two answers are compared. The test passes only if they never disagree.

  python compare_rules.py tests/refund_recent_approval_actions.json \
      --correct rules/refund_recent_approval.dw \
      --candidate rules/practice/wrong_window_24h.dw

Disagreements are counted two ways, because they are not equally bad:
  - wrong allow: the candidate ALLOWS something the correct rule blocks (the costly mistake)
  - wrong block: the candidate BLOCKS something the correct rule allows
If the candidate rule does not even pass `dogwood validate`, the test fails right away.

Scenarios use the same format as score_actions.py. "expect" labels are not needed here:
the correct rule is the answer key. Set DOGWOOD_CMD the same way as for the other scripts.
"""
import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from enforce import DogwoodGate, build_policy
from score_actions import SCHEMA, case_lines

TS = re.compile(r"^@(\d+) ")


def decisions(gate, cases):
    """Run every case through one rule. Returns {(case_id, ts): "ALLOW" | "DENY" | "ERROR"}."""
    out, calls = {}, {}
    for case in cases:
        lines, _ = case_lines(gate, case)
        requests = [l for l in lines if "::request(" in l]
        ran, verdicts, problem = gate.replay(lines)
        by_ts = {v["ts"]: v["decision"] for v in verdicts}
        for line in requests:
            ts = int(TS.match(line).group(1))
            out[(case["id"], ts)] = by_ts.get(ts, "ERROR") if ran else "ERROR"
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("scenarios", help="scenario file (JSON, same format as score_actions.py)")
    p.add_argument("--correct", required=True, help="answer-key rule file (.dw)")
    p.add_argument("--candidate", required=True, help="rule file the tool wrote (.dw)")
    p.add_argument("--base", default="policies/base.dw")
    p.add_argument("--name", default=None, help="test name for the report (default: candidate file name)")
    args = p.parse_args()

    name = args.name or Path(args.candidate).stem
    cases = json.loads(Path(args.scenarios).read_text(encoding="utf-8"))["cases"]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    work = Path(".provenance") / f"compare-{stamp}"

    gates = {}
    for role, rule in (("correct", args.correct), ("candidate", args.candidate)):
        d = work / role
        d.mkdir(parents=True, exist_ok=True)
        policy = build_policy(args.base, rule, d / "policy.dw")
        gate = DogwoodGate(d, policy, SCHEMA)
        ok, msg = gate.validate()
        if not ok and role == "correct":
            sys.exit(f"The CORRECT rule does not validate, so the answer key needs fixing:\n{msg}")
        gates[role] = (gate, ok, msg)

    rows, wrong_allows, wrong_blocks, errors = [], 0, 0, 0
    cand_gate, cand_valid, cand_msg = gates["candidate"]
    correct = decisions(gates["correct"][0], cases)
    cand = decisions(cand_gate, cases) if cand_valid else {k: "INVALID" for k in correct}

    for (case_id, ts), want in correct.items():
        got = cand[(case_id, ts)]
        if got == want:
            result = "same"
        elif want == "DENY" and got == "ALLOW":
            result, wrong_allows = "WRONG ALLOW", wrong_allows + 1
        elif want == "ALLOW" and got == "DENY":
            result, wrong_blocks = "WRONG BLOCK", wrong_blocks + 1
        else:
            result, errors = "ERROR", errors + 1
        rows.append({"case": case_id, "ts": ts, "correct": want, "candidate": got, "result": result})

    passed = cand_valid and wrong_allows == wrong_blocks == errors == 0
    summary = {
        "test": name, "scenarios": args.scenarios, "correct_rule": args.correct,
        "candidate_rule": args.candidate, "candidate_validates": cand_valid,
        "decisions": len(rows), "wrong_allows": wrong_allows, "wrong_blocks": wrong_blocks,
        "errors": errors, "passed": passed, "rows": rows,
    }

    md = [f"# Rule comparison: {name} ({stamp})", "",
          f"- Correct rule: `{args.correct}`  |  Candidate rule: `{args.candidate}`",
          f"- Scenarios: `{args.scenarios}` ({len(cases)} cases, {len(rows)} decisions)",
          f"- Candidate passes dogwood validate: {'yes' if cand_valid else '**NO**'}",
          f"- Wrong allows: **{wrong_allows}**  |  Wrong blocks: **{wrong_blocks}**  |  Errors: {errors}",
          f"- **Result: {'PASS' if passed else 'FAIL'}**", ""]
    if not cand_valid:
        md += ["Validate output:", "```", cand_msg[:1500], "```", ""]
    md += ["| Case | Time | Correct rule | Tool's rule | Result |", "|---|---|---|---|---|"]
    for r in rows:
        flag = r["result"] if r["result"] == "same" else f"**{r['result']}**"
        md.append(f"| {r['case']} | @{r['ts']} | {r['correct']} | {r['candidate']} | {flag} |")

    out = Path("results")
    out.mkdir(exist_ok=True)
    (out / f"compare_{name}_{stamp}.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (out / f"compare_{name}_{stamp}.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md))
    print(f"\nSaved results/compare_{name}_{stamp}.md and .json (traces in {work})")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
