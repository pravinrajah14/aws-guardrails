"""Score policy decisions against scripted action histories."""
import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from enforce import DogwoodGate, build_policy, find_form
from tools import STAFF, TOOL_SPECS, MockWorld, check_args

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
    """Turn one case's events into Dogwood trace lines. Returns (lines, scored requests).

    The store's records for the context engine come from the case itself: each request_approval
    response is an approval record, orders and staff are the store's defaults. An event with
    kind "message" is free text (e.g. a claimed approval); it adds no trace line."""
    lines, scored, last_ts = [], [], -1
    records = {"approvals": [], "orders": MockWorld().orders, "staff": STAFF}
    steps = []
    for i, ev in enumerate(case["events"]):
        ts = to_seconds(ev["at"]) + 1          # +1 so "0m" is a valid first timestamp
        if ts <= last_ts:
            raise ValueError(f"case {case['id']}: event {i} is not later than the one before it")
        last_ts = ts
        if ev.get("kind") == "message":
            steps.append({"ts": ts, "kind": "message", "who": ev.get("who", "user"), "text": ev["text"]})
            continue
        if ev["tool"] not in TOOL_SPECS:
            raise ValueError(f"case {case['id']}: unknown tool {ev['tool']}")
        action = TOOL_SPECS[ev["tool"]]["action"]
        args = check_args(ev["tool"], ev["input"])
        kind = ev.get("kind", "request")
        rid = f"{case['id']}-{i}"
        if kind == "request":
            ctx = gate.context_for(action, args, ts, records=records, steps=list(steps))
            lines.append(gate.event_line(ts, action, "request", args, request_id=rid, with_context=True,
                                         facts=ctx["facts"]))
            if "expect" in ev:
                scored.append({"ts": ts, "event": i, "call": f"{ev['tool']}({args})", "expect": ev["expect"].upper(),
                               "facts_status": ctx["status"], "facts_reason": ctx["reason"],
                               "facts_sources": ctx["sources"], "needs_review": ctx["review"],
                               "ignored_claims": ctx["ignored_claims"]})
        else:
            lines.append(gate.event_line(ts, action, kind, args, output=ev.get("output"), request_id=rid))
            if action == "RequestApproval" and kind == "response":
                records["approvals"].append({"order_id": args["order_id"], "asked": args["amount"],
                                             **ev["output"], "at": ts, "source": f"case event {i}"})
        steps.append({"ts": ts, "action": action, "kind": kind, "input": args, "output": ev.get("output")})
    return lines, scored


def main():
    p = argparse.ArgumentParser()
    p.add_argument("actions", help="action list file (JSON)")
    p.add_argument("--rules", required=True, help="rule file under test (.dw)")
    p.add_argument("--base", default="policies/base.dw")
    p.add_argument("--output-dir", help="Save this evaluation's scorecard and traces together")
    p.add_argument("--form", help="rule form for the context engine (default: updated_form.json next to --rules)")
    args = p.parse_args()

    cases = json.loads(Path(args.actions).read_text(encoding="utf-8"))["cases"]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    output_root = Path(args.output_dir) if args.output_dir else None
    work = (output_root / "traces" if output_root else Path(".provenance")) / f"score-{stamp}"
    work.mkdir(parents=True, exist_ok=True)
    policy = build_policy(args.base, args.rules, work / "policy.dw")
    form, form_path = find_form(args.rules, args.form)
    gate = DogwoodGate(work, policy, SCHEMA, form=form)

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
            rows.append({"case": case["id"], **s, "got": got, "rules": rules, "correct": got == s["expect"],
                         "wrong": "" if got == s["expect"] else ("wrong allow" if got == "ALLOW" else "wrong block")})

    right = sum(r["correct"] for r in rows)
    wrong_allows = sum(r["wrong"] == "wrong allow" for r in rows)
    wrong_blocks = sum(r["wrong"] == "wrong block" for r in rows)
    out = output_root or Path("results")
    out.mkdir(parents=True, exist_ok=True)
    (out / f"rule_scorecard_{stamp}.json").write_text(
        json.dumps({"rules": args.rules, "actions": args.actions, "form": str(form_path) if form_path else None,
                    "right": right, "total": len(rows), "wrong_allows": wrong_allows, "wrong_blocks": wrong_blocks,
                    "decisions": rows}, indent=2))
    md = [f"# Rule scorecard, no agent ({stamp})", "",
          f"- Rule file: `{args.rules}`  |  Action lists: `{args.actions}`  |  Rule form: `{form_path or 'none'}`",
          f"- **{right} of {len(rows)} decisions correct.** Any wrong decision is the rule's fault.",
          f"- Wrong allows (unsafe): {wrong_allows}  |  Wrong blocks (annoying): {wrong_blocks}", "",
          "| Case | Call | Expected | Got | Rule(s) | Facts | Correct |", "|---|---|---|---|---|---|---|"]
    for r in rows:
        facts = f"{r['facts_status']}" + (" (review)" if r["needs_review"] else "") if r["facts_status"] else "-"
        md.append(f"| {r['case']} | `{r['call']}` | {r['expect']} | {r['got']} | {r['rules'] or '-'} | {facts} | "
                  f"{'yes' if r['correct'] else '**NO** (' + r['wrong'] + ')'} |")
    (out / f"rule_scorecard_{stamp}.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md))
    print(f"\nSaved {out / f'rule_scorecard_{stamp}.md'} (traces in {work})")


if __name__ == "__main__":
    main()
