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
OVERRIDES = Path(__file__).resolve().parent / "tests" / "expectation_overrides.json"
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
    response is an approval record, each issue_refund response a refund record, orders and staff
    are the store's defaults. An event with
    kind "message" is free text (e.g. a claimed approval); it adds no trace line."""
    lines, scored, last_ts = [], [], -1
    records = {"approvals": [], "refunds": [], "orders": MockWorld().orders, "staff": STAFF}
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
                               "ignored_claims": ctx["ignored_claims"], "expect_status": ev.get("expect_status")})
        else:
            lines.append(gate.event_line(ts, action, kind, args, output=ev.get("output"), request_id=rid))
            if action == "RequestApproval" and kind == "response":
                records["approvals"].append({"order_id": args["order_id"], "asked": args["amount"],
                                             **ev["output"], "at": ts, "source": f"case event {i}"})
            if action == "IssueRefund" and kind == "response":
                records["refunds"].append({"order_id": args["order_id"], **ev["output"], "at": ts})
        steps.append({"ts": ts, "action": action, "kind": kind, "input": args, "output": ev.get("output")})
    return lines, scored


def load_overrides(path, actions_path):
    """The overrides for this action list, keyed by case id."""
    if not path or not Path(path).is_file():
        return {}
    found = {}
    for o in json.loads(Path(path).read_text(encoding="utf-8"))["overrides"]:
        if o["actions"] == Path(actions_path).name:
            found[o["case"]] = {**o, "old_expected": o["old_expected"].upper(), "new_expected": o["new_expected"].upper()}
    return found


def tally(rows, expect_key):
    wrong = [r for r in rows if r["got"] != r[expect_key]]
    return {"right": len(rows) - len(wrong), "total": len(rows),
            "wrong_allows": sum(r["got"] == "ALLOW" for r in wrong),
            "wrong_blocks": sum(r["got"] != "ALLOW" for r in wrong)}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("actions", help="action list file (JSON)")
    p.add_argument("--rules", required=True, help="rule file under test (.dw)")
    p.add_argument("--base", default="policies/base.dw")
    p.add_argument("--output-dir", help="Save this evaluation's scorecard and traces together")
    p.add_argument("--form", help="rule form for the context engine (default: updated_form.json next to --rules)")
    p.add_argument("--overrides", default=str(OVERRIDES),
                   help="deliberate expectation changes (default: tests/expectation_overrides.json)")
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

    overrides = load_overrides(args.overrides, args.actions)
    applied = []
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
                         "wrong": "" if got == s["expect"] else ("wrong allow" if got == "ALLOW" else "wrong block"),
                         "expect_current": s["expect"], "override": None})
        o = overrides.get(case["id"])
        if o:
            mine = [r for r in rows if r["case"] == case["id"] and r["expect"] == o["old_expected"]]
            if len(mine) != 1:
                sys.exit(f"override for {case['id']}: expected one decision with expectation {o['old_expected']}, "
                         f"found {len(mine)}; the action list changed, so check the override")
            mine[0]["expect_current"], mine[0]["override"] = o["new_expected"], o
            applied.append(o)
    missing = sorted(set(overrides) - {o["case"] for o in applied})
    if missing:
        sys.exit(f"overrides name cases not in {args.actions}: {missing}")

    right = sum(r["correct"] for r in rows)
    wrong_allows = sum(r["wrong"] == "wrong allow" for r in rows)
    wrong_blocks = sum(r["wrong"] == "wrong block" for r in rows)
    current = tally(rows, "expect_current")
    override_list = [{k: o[k] for k in ("case", "old_expected", "new_expected", "reason", "date", "decided_by")}
                     for o in applied]
    status_wrong = [r for r in rows if r["expect_status"] and r["expect_status"] != r["facts_status"]]
    out = output_root or Path("results")
    out.mkdir(parents=True, exist_ok=True)
    (out / f"rule_scorecard_{stamp}.json").write_text(
        json.dumps({"rules": args.rules, "actions": args.actions, "form": str(form_path) if form_path else None,
                    "right": right, "total": len(rows), "wrong_allows": wrong_allows, "wrong_blocks": wrong_blocks,
                    "status_checked": sum(bool(r["expect_status"]) for r in rows), "status_wrong": len(status_wrong),
                    "current": current, "overrides_file": args.overrides if applied else None,
                    "overrides": override_list, "decisions": rows}, indent=2))
    md = [f"# Rule scorecard, no agent ({stamp})", "",
          f"- Rule file: `{args.rules}`  |  Action lists: `{args.actions}`  |  Rule form: `{form_path or 'none'}`",
          f"- Against the original expectations: **{right} of {len(rows)} decisions correct** "
          f"(wrong allows (unsafe) {wrong_allows}, wrong blocks (annoying) {wrong_blocks})",
          f"- Against current expectations ({len(applied)} override(s)): **{current['right']} of {current['total']}** "
          f"(wrong allows {current['wrong_allows']}, wrong blocks {current['wrong_blocks']})"]
    if any(r["expect_status"] for r in rows):
        md.append(f"- Engine status wrong: {len(status_wrong)} of {sum(bool(r['expect_status']) for r in rows)} checked")
    md.append("")
    if applied:
        md += [f"Overrides (`{args.overrides}`):", ""]
        md += [f"- `{o['case']}`: {o['old_expected']} -> {o['new_expected']}, decided by {o['decided_by']} on "
               f"{o['date']}. {o['reason']}" for o in applied]
        md.append("")
    md += ["| Case | Call | Expected | Got | Rule(s) | Facts | Correct |", "|---|---|---|---|---|---|---|"]
    for r in rows:
        facts = f"{r['facts_status']}" + (" (review)" if r["needs_review"] else "") if r["facts_status"] else "-"
        if r["expect_status"] and r["expect_status"] != r["facts_status"]:
            facts += f" **(expected {r['expect_status']})**"
        shown = r["expect"] if not r["override"] else f"{r['expect']} (now {r['expect_current']}, override)"
        md.append(f"| {r['case']} | `{r['call']}` | {shown} | {r['got']} | {r['rules'] or '-'} | {facts} | "
                  f"{'yes' if r['correct'] else '**NO** (' + r['wrong'] + ')'}"
                  + (f"; current: {'yes' if r['got'] == r['expect_current'] else '**NO**'}" if r["override"] else "")
                  + " |")
    (out / f"rule_scorecard_{stamp}.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md))
    print(f"\nSaved {out / f'rule_scorecard_{stamp}.md'} (traces in {work})")


if __name__ == "__main__":
    main()
