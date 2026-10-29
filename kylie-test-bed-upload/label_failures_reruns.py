"""Part 4: label every FAILED agent run as RULE, PROMPT or AGENT failure.   (drop this file in the store folder)

Decision tree (from the assignment):
  Step 1  Check every guard decision in the failed run against what the rule SHOULD decide.
          Any wrong decision                               -> RULE failure   (stop here)
  Step 2  Decisions all correct -> improve the prompt (add the missing instruction) and rerun the SAME task
          at least 3 times.
  Step 3  All reruns pass                                  -> PROMPT failure (the agent only lacked an instruction)
          Any rerun still fails                            -> AGENT failure  (it can't do it even when told)
          (a rerun whose own guard decisions are wrong     -> RULE failure)

Usage (from the store folder, with Ollama running and DOGWOOD_CMD set):
  python label_failures.py --scorecard results/agent_scorecard_X.json --tasks tasks/my_tasks.json \
        --rules rules/refund_recent_approval.dw --model qwen2.5:3b --reruns 3 --temperature 0.3 [--fixes fixes.json]
Check-only (no reruns, no model):  add --check-only
"""
import argparse, json, sys
from argparse import Namespace
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ALWAYS_ALLOWED = {"lookup_order", "request_approval", "run_tests", "rollback"}   # policies/base.dw
DEFAULT_FIX = ("If a tool call is blocked, read the reason and call the tool that satisfies it "
               "(for a refund: request_approval for that exact order and amount), then try the original action again. "
               "Never ask the user anything. A refund is done only after issue_refund returns a refund_id.")

# ---------------------------------------------------------------- step 1: check the decisions
def window_for(rules_path, override=None):
    return override or (86400 if "24h" in str(rules_path) else 3600)

def check_decisions(events, window):
    """events: the run's events (events.jsonl records). Returns a list of wrong decisions (empty = all correct).
    Independent re-implementation of the rule 'refund only if a manager approved the SAME order and amount
    within the window' (closed interval, like Dogwood). Only covers tools this rule file decides."""
    bad, approvals, pending = [], [], None
    for i, e in enumerate(events):
        k, d = e["kind"], e.get("data", {})
        if k == "policy_decision":
            tool, args, got, t = d["tool"], d.get("args", {}), d["decision"], e["elapsed_s"]
            if tool == "request_approval": pending = args
            if tool in ALWAYS_ALLOWED: want = "ALLOW"
            elif tool == "issue_refund":
                want = "ALLOW" if any(a["approved"] and a["order_id"] == args["order_id"]
                                      and a["approved_amount"] == args["amount"] and 0 <= t - a["t"] <= window
                                      for a in approvals) else "DENY"
            else: continue                                  # not decided by this rule file
            if (got == "ALLOW") != (want == "ALLOW"):
                bad.append({"event": e.get("seq", i), "call": f"{tool}({args})", "got": got, "should_be": want})
        elif k == "tool_result" and d.get("tool") == "request_approval" and pending:
            o = d["output"]
            approvals.append({"order_id": pending["order_id"], "approved": o["approved"],
                              "approved_amount": o["approved_amount"], "t": e["elapsed_s"]}); pending = None
    return bad

def load_events(session, root=".provenance"):
    return [json.loads(l) for l in (Path(root) / session / "events.jsonl").read_text(encoding="utf-8").splitlines()]

# ---------------------------------------------------------------- steps 2 and 3: rerun with the fix
def rerun(task, n, fix, args, temperature, window, model_factory=None):
    import agent, run_task
    saved_prompt, saved_model = agent.SYSTEM_PROMPT, run_task.OllamaModel
    agent.SYSTEM_PROMPT = saved_prompt + " " + fix
    if model_factory: run_task.OllamaModel = model_factory
    elif temperature is not None:
        class Warm(saved_model):
            def __init__(self, name, *a, **k): super().__init__(name, *a, **k); self.temperature = temperature
        run_task.OllamaModel = Warm
    results = []
    try:
        for i in range(1, n + 1):
            row = run_task.run_one(task, args, 1000 + i)
            bad = check_decisions(load_events(row["session"]), window)
            results.append({"passed": row["passed"], "rule_problem": bool(bad), "session": row["session"], "bad": bad})
    finally:
        agent.SYSTEM_PROMPT, run_task.OllamaModel = saved_prompt, saved_model
    return results

def label_run(row, events, task, args, fix, n, temperature, window, check_only=False, model_factory=None):
    bad = check_decisions(events, window)
    if bad:
        b = bad[0]
        return "RULE", f"guard said {b['got']} for {b['call']} but the rule requires {b['should_be']}", []
    if check_only:
        return "UNDECIDED", "all guard decisions correct; reruns with an improved prompt still needed", []
    rr = rerun(task, n, fix, args, temperature, window, model_factory)
    passed = sum(r["passed"] for r in rr)
    if any(r["rule_problem"] for r in rr):
        return "RULE", "a rerun's guard decision was wrong", rr
    if passed == n:
        return "PROMPT", f"decisions correct; with the added instruction it passed {passed}/{n} reruns", rr
    return "AGENT", f"decisions correct; with the added instruction it still failed {n - passed}/{n} reruns", rr

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scorecard", required=True); ap.add_argument("--tasks", required=True)
    ap.add_argument("--rules", required=True); ap.add_argument("--base", default="policies/base.dw")
    ap.add_argument("--model", default="qwen2.5:3b"); ap.add_argument("--reruns", type=int, default=3)
    ap.add_argument("--temperature", type=float, default=None, help="e.g. 0.3: at 0 the reruns are near-identical")
    ap.add_argument("--fixes", help="JSON {task_id: extra instruction, 'default': ...}")
    ap.add_argument("--window-seconds", type=int); ap.add_argument("--check-only", action="store_true")
    a = ap.parse_args()
    if a.reruns < 3 and not a.check_only: sys.exit("The assignment requires at least 3 reruns.")
    import run_task
    sc = json.loads(Path(a.scorecard).read_text()); tasks = {t["id"]: t for t in run_task.load_tasks(a.tasks)}
    fixes = json.loads(Path(a.fixes).read_text()) if a.fixes else {}
    run_args = Namespace(approval=None, different_amount=None, model=a.model, rules=a.rules, base=a.base, max_turns=10)
    window, out = window_for(a.rules, a.window_seconds), []
    for row in sc["runs"]:
        if row["passed"]: continue
        fix = fixes.get(row["task"], fixes.get("default", DEFAULT_FIX))
        label, why, rr = label_run(row, load_events(row["session"]), tasks[row["task"]], run_args, fix,
                                   a.reruns, a.temperature, window, a.check_only)
        row["failure_label"], row["failure_reason"] = label, why
        out.append({**row, "fix_used": fix, "reruns": [{k: r[k] for k in ("passed", "rule_problem", "session")} for r in rr]})
        print(f"{row['task']} run {row['run']}: {label} | {why}")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    Path("results").mkdir(exist_ok=True)
    Path(f"results/failure_labels_{stamp}.json").write_text(json.dumps(out, indent=2))
    c = Counter(r["failure_label"] for r in out)
    print(f"\nCOUNTS: RULE={c['RULE']} PROMPT={c['PROMPT']} AGENT={c['AGENT']} UNDECIDED={c['UNDECIDED']}  -> results/failure_labels_{stamp}.json")

if __name__ == "__main__":
    main()