"""Run guarded agent tasks and score their final state."""
import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from agent import OllamaModel, ScriptedModel, run_agent
from enforce import DogwoodGate, build_policy
from recorder import Recorder, file_hash
from tools import APPROVAL_MODES, MockWorld

SCHEMA = "policies/schema.cedarschema"


def load_tasks(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    tasks = data["tasks"] if isinstance(data, dict) else data
    for t in tasks:
        for key in ("id", "prompt", "expect"):
            if key not in t:
                raise ValueError(f"task {t.get('id', '?')} is missing '{key}'")
    return tasks


def check(world, expect):
    """Did the store end up where the task expected? Returns (passed, reason)."""
    if "refunds" in expect:
        got = Counter((r["order_id"], r["amount"]) for r in world.refunds)
        want = Counter((r["order_id"], r["amount"]) for r in expect["refunds"])
        if got != want:
            return False, f"refunds were {sorted(got.elements())}, expected {sorted(want.elements())}"
    if "deployed" in expect:
        for key, version in expect["deployed"].items():
            service, env = key.split("/")
            if world.deployed.get((service, env)) != version:
                return False, f"{key} is {world.deployed.get((service, env))}, expected {version}"
    return True, "store matches expected state"


def run_one(task, args, run_number):
    approval = args.approval or task.get("approval", "yes")
    if args.approval and args.approval != task.get("approval", "yes"):
        # The expected result in the task file assumes the task's own approval mode, so an override
        # would score the run against the wrong answer. Refuse instead of producing a misleading score.
        sys.exit(f"--approval {args.approval} conflicts with task '{task['id']}' (approval "
                 f"'{task.get('approval')}'). Pick the task written for that mode, e.g. refund_denied.")
    different = args.different_amount if args.different_amount is not None else task.get("different_amount")
    world = MockWorld(approval=approval, different_amount=different)

    if args.model == "scripted":
        model = ScriptedModel(task.get("script", "refund"))
    else:
        model = OllamaModel(args.model)

    output_dir = getattr(args, "output_dir", None)
    rec = Recorder(root=Path(output_dir) / "traces" if output_dir else ".provenance")
    print(f"\n=== {task['id']} (run {run_number}) -> {rec.dir}")
    policy = build_policy(args.base, args.rules, rec.dir / "policy.dw")
    rec.start_session(task["id"], task["prompt"], model.name, policy, SCHEMA)
    rec.log("store_setup", f"rules={args.rules} approval={approval}"
            + (f" different_amount={different}" if approval == "different" else "")
            + f" prompt={args.prompt} repair={'off' if args.no_repair else 'on'} nudge={'off' if args.no_nudge else 'on'}",
            rule_file=str(args.rules), base_file=str(args.base), approval=approval,
            different_amount=different, run=run_number,
            expect=task.get("expect_by_rule", {}).get(Path(args.rules).name, task["expect"]),
            prompt=args.prompt, repair=not args.no_repair, nudge=not args.no_nudge,
            agent_sha256=file_hash("agent.py"))

    gate = DogwoodGate(rec.dir, policy, SCHEMA)
    ok, msg = gate.validate()
    rec.log("policy_validate", "OK" if ok else "FAILED", output=msg)
    if not ok:
        rec.log("session_end", "stopped: policy did not validate", passed=False)
        print(msg)
        sys.exit(2)

    try:
        world, final = run_agent(model, task["prompt"], gate, rec, world=world, max_turns=args.max_turns,
                                 prompt=args.prompt, repair=not args.no_repair, nudge=not args.no_nudge)
    except Exception as e:
        rec.log("session_end", f"crashed: {e}", passed=False, error=repr(e))
        raise

    # A task can say what is right under a specific rule file (e.g. no $500 cap in some rules).
    expect = task.get("expect_by_rule", {}).get(Path(args.rules).name, task["expect"])
    passed, reason = check(world, expect)
    rec.log("task_check", ("PASS: " if passed else "FAIL: ") + reason, passed=passed,
            refunds=world.refunds, approvals=world.approvals,
            deployed={f"{s}/{e}": v for (s, e), v in world.deployed.items()})
    rec.log("session_end", f"{rec.seq + 1} events recorded", passed=passed)

    events = [json.loads(l) for l in (rec.dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    decisions = [e["data"] for e in events if e["kind"] == "policy_decision"]
    return {
        "task": task["id"], "run": run_number, "approval": approval, "passed": passed, "reason": reason,
        "allowed": sum(d["decision"] == "ALLOW" for d in decisions),
        "blocked": sum(d["decision"] != "ALLOW" for d in decisions),
        "guard_decisions": [f"{d['decision']} {d['tool']}({d['args']})" for d in decisions],
        "final_message": final[:160], "session": rec.dir.name,
        "failure_label": "", "failure_reason": "",   # filled in for Part 4 (rule / prompt / agent)
    }


def write_scorecard(rows, args):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out = Path(getattr(args, "output_dir", None) or "results")
    out.mkdir(parents=True, exist_ok=True)
    meta = {"rules": str(args.rules), "base": str(args.base), "tasks": str(args.tasks), "model": args.model,
            "prompt": args.prompt, "repair": not args.no_repair, "nudge": not args.no_nudge}
    meta["sha256"] = {"rules": file_hash(args.rules), "base": file_hash(args.base),
                      "tasks": file_hash(args.tasks), "schema": file_hash(SCHEMA)}
    (out / f"agent_scorecard_{stamp}.json").write_text(json.dumps({"setup": meta, "runs": rows}, indent=2))
    passed = sum(r["passed"] for r in rows)
    lines = [
        f"# Agent scorecard ({stamp})", "",
        f"- Rule file: `{args.rules}`  |  Model: `{args.model}`  |  Tasks: `{args.tasks}`",
        f"- Prompt: `{args.prompt}`  |  Malformed-reply repair: {'off' if args.no_repair else 'on'}  |  Nudge: {'off' if args.no_nudge else 'on'}",
        f"- **Passed {passed} of {len(rows)} runs**", "",
        "| Task | Run | Approval | Result | Allowed | Blocked | Why | Session | Failure label |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(f"| {r['task']} | {r['run']} | {r['approval']} | {'PASS' if r['passed'] else 'FAIL'} | "
                     f"{r['allowed']} | {r['blocked']} | {r['reason']} | {r['session']} | |")
    (out / f"agent_scorecard_{stamp}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nScorecard: {out / f'agent_scorecard_{stamp}.md'}  (passed {passed} of {len(rows)})")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--tasks", default="tasks/refund_tasks.json")
    p.add_argument("--task", help="run one task id from the task file")
    p.add_argument("--all", action="store_true", help="run every task in the task file")
    p.add_argument("--rules", default="rules/starter.dw", help="rule file under test (.dw)")
    p.add_argument("--base", default="policies/base.dw", help="store's base permissions (.dw)")
    p.add_argument("--model", default="qwen2.5:3b", help="an Ollama model name, or 'scripted'")
    p.add_argument("--approval", choices=APPROVAL_MODES, help="override how the manager answers")
    p.add_argument("--different-amount", type=int, help="amount approved when --approval different")
    p.add_argument("--runs", type=int, default=1, help="repeat each task this many times")
    p.add_argument("--max-turns", type=int, default=10)
    p.add_argument("--output-dir", help="Save scorecards and session traces together here")
    p.add_argument("--prompt", choices=["current", "current-amount", "a3", "a3-original"], default="current", help="system prompt version")
    p.add_argument("--no-repair", action="store_true", help="turn off the malformed-reply message (Part 4 tests)")
    p.add_argument("--no-nudge", action="store_true", help="turn off the after-block nudge (Part 4 tests)")
    args = p.parse_args()

    tasks = load_tasks(args.tasks)
    if args.task:
        tasks = [t for t in tasks if t["id"] == args.task]
        if not tasks:
            sys.exit(f"no task '{args.task}' in {args.tasks}")
    elif not args.all:
        sys.exit("choose --task <id> or --all")

    rows = [run_one(t, args, n) for t in tasks for n in range(1, args.runs + 1)]
    write_scorecard(rows, args)


if __name__ == "__main__":
    main()
