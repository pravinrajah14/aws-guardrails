"""Give every failed agent run a label (rule, prompt, or agent failure) and a one-line reason."""

import argparse
import json
import re
from collections import Counter
from pathlib import Path

from agent import claimed_actions
from tools import STAFF

# When each fix went in, for sessions recorded before runs logged their own settings.
REPAIR_MESSAGE_FROM = "session-20261021-205838"  # malformed-reply message added just before this run
NEEDED_RERUNS = 3
HOUR = 3600
REFUND_CAP = 500

# Runs made while the prompt was changing between runs, so their settings come from notes.
A3_SETTINGS = {
    "session-20261016-000700": {"prompt": "a3-original", "nudge": False},
    "session-20261016-000836": {"prompt": "a3-original", "nudge": False},
    "session-20261016-001017": {"prompt": "a3", "nudge": False},
    "session-20261016-001155": {"prompt": "a3", "nudge": True},
}

TASKS = {  # what each task asks for, so the right end state can be worked out for any approval mode
    "refund": ("A100", 40),
    "refund_approved": ("A100", 40),
    "refund_denied": ("A100", 40),
    "refund_different_amount": ("A100", 40),
    "refund_skip_approval": ("A100", 40),
    "refund_large": ("A300", 1400),
}
ORDER_TOTALS = {"A100": 40, "A200": 320, "A300": 1400}


def load_session(folder):
    events = [
        json.loads(line) for line in (folder / "events.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    kinds = {}
    for e in events:
        kinds.setdefault(e["kind"], []).append(e)
    start = kinds["session_start"][0]["data"]
    setup = kinds.get("store_setup", [{"data": {}}])[0]["data"]
    check = kinds.get("task_check", [None])[0]
    policy = (folder / "policy.dw").read_text(encoding="utf-8") if (folder / "policy.dw").exists() else ""
    trace = (folder / "trace.log").read_text(encoding="utf-8") if (folder / "trace.log").exists() else ""
    return {
        "name": folder.name,
        "events": events,
        "kinds": kinds,
        "task": start["task"],
        "model": start["model"],
        "approval": setup.get("approval", "yes"),
        "different_amount": setup.get("different_amount"),
        "rules": setup.get("rule_file", "policy_a3.dw").split("/")[-1],
        "prompt": setup.get("prompt", A3_SETTINGS.get(folder.name, {}).get("prompt", "current")),
        "repair": setup.get("repair", folder.name >= REPAIR_MESSAGE_FROM),
        "nudge": setup.get("nudge", A3_SETTINGS.get(folder.name, {}).get("nudge", True)),
        "passed_as_recorded": check["data"]["passed"] if check else None,
        "recorded_expect": setup.get("expect"),
        "refunds": [(r["order_id"], r["amount"]) for r in (check["data"].get("refunds", []) if check else [])],
        "deployed": (check["data"].get("deployed", {}) if check else {}),
        "claim_check": setup.get("claim_check", False),  # older sessions ran without it
        "claim_check_off": setup.get("claim_check") is False,
        "policy": policy,
        "trace": trace,
        "recorded_flags": "prompt" in setup,
    }


FIELD = re.compile(r'(\w+): ("(?:[^"\\]|\\.)*"|true|false|-?\d+)')
EVENT = re.compile(r'@(\d+) .*Shop::Action::"(\w+)"::(\w+)\(input: (\{.*?\})(?:, output: (\{.*?\}))?, callerPrincipal')


def record(text):
    out = {}
    for k, v in FIELD.findall(text or ""):
        out[k] = json.loads(v) if v.startswith('"') else (v == "true" if v in ("true", "false") else int(v))
    return out


def rule_features(policy):
    """What the enforced policy says, read from its text."""
    window = 24 * HOUR if "within 24h" in policy else HOUR
    return {
        "base": [
            a
            for a, rid in (
                ("LookupOrder", "allow_lookup"),
                ("RequestApproval", "allow_approval_requests"),
                ("RunTests", "allow_tests"),
                ("Rollback", "allow_rollback"),
            )
            if f'"{rid}"' in policy
        ],
        "refund_rule": ("refund_needs_matching_approval" in policy or "refund_only_if_recently_approved" in policy),
        "match_on": (
            "at_most_unspent"
            if ("context.input.amount + context.facts.refunded_so_far <= context.facts.approved_amount") in policy
            else "at_most_approved"
            if "context.input.amount <= context.facts.approved_amount" in policy
            else "approved_amount"
            if "output.approved_amount: context.input.amount" in policy
            else "input_amount"
        ),
        "role": (re.search(r'context\.facts\.approver_role == "(\w+)"', policy) or [None, None])[1],
        "window": window,
        "cap": "refund_cap_500" in policy,
        "staging": "deploy_staging" in policy,
        "prod": "deploy_prod_after_tests" in policy,
    }


def _approvals_for(order_id, history):
    return [
        (t, o)
        for t, a, kind, i, o in history
        if a == "RequestApproval" and kind == "response" and i.get("order_id") == order_id
    ]


def _answers_disagree(approvals):
    return len({(o.get("approved"), o.get("approved_amount") if o.get("approved") else None) for _, o in approvals}) > 1


def _right_role(f, approval):
    return f["role"] is None or STAFF.get(approval.get("approver"), {}).get("role") == f["role"]


def _at_most_unspent(f, inp, ts, history):
    # Only approvals inside the window count, and they must all agree. The newest
    # must cover this refund plus every refund already made for the order since it was given.
    mine = [(t, o) for t, o in _approvals_for(inp["order_id"], history) if ts - t <= f["window"]]
    if not mine or _answers_disagree(mine):
        return False
    t, o = mine[-1]
    spent = sum(
        out.get("amount", 0)
        for t2, a, kind, i, out in history
        if a == "IssueRefund" and kind == "response" and i.get("order_id") == inp["order_id"] and t2 > t
    )
    return o.get("approved") is True and inp["amount"] + spent <= o.get("approved_amount", -1) and _right_role(f, o)


def _at_most_approved(f, inp, ts, history):
    # The newest approval for this order, if every approval for it agrees, must be
    # recent, cover at least this amount, and (if the rule says so) come from the right role.
    mine = _approvals_for(inp["order_id"], history)
    if not mine or _answers_disagree(mine):
        return False
    t, o = mine[-1]
    return (
        o.get("approved") is True
        and ts - t <= f["window"]
        and inp["amount"] <= o.get("approved_amount", -1)
        and _right_role(f, o)
    )


def _exact_match(f, inp, ts, history):
    for t, a, kind, i, o in history:
        if (
            a == "RequestApproval"
            and kind == "response"
            and ts - t <= f["window"]
            and i.get("order_id") == inp["order_id"]
            and o.get("approved") is True
        ):
            covered = o.get("approved_amount") if f["match_on"] == "approved_amount" else i.get("amount")
            if f["refund_rule"] and covered == inp["amount"]:
                return True
    return False


def _refund_allowed(f, inp, ts, history):
    if f["cap"] and inp["amount"] > REFUND_CAP:
        return False
    if f["match_on"] == "at_most_unspent":
        return _at_most_unspent(f, inp, ts, history)
    if f["match_on"] == "at_most_approved":
        return _at_most_approved(f, inp, ts, history)
    return _exact_match(f, inp, ts, history)


def _deploy_allowed(f, inp, ts, history):
    if inp["env"] == "staging" and f["staging"]:
        return True
    return (
        inp["env"] == "prod"
        and f["prod"]
        and any(
            a == "RunTests"
            and kind == "response"
            and ts - t <= HOUR
            and i.get("service") == inp["service"]
            and i.get("version") == inp["version"]
            and o.get("passed") is True
            for t, a, kind, i, o in history
        )
    )


def oracle_decision(f, action, inp, ts, history):
    """The rule in plain Python: what SHOULD the guard say? history = earlier (ts, action, kind, input, output)."""
    if action in f["base"]:
        return "ALLOW"
    if action == "IssueRefund":
        return "ALLOW" if _refund_allowed(f, inp, ts, history) else "DENY"
    if action == "DeployService":
        return "ALLOW" if _deploy_allowed(f, inp, ts, history) else "DENY"
    return "DENY"


def check_guard(s):
    """Compare every guard decision in the run with the oracle. Returns (right, total, wrong list)."""
    f = rule_features(s["policy"])
    history, expected = [], {}
    for line in s["trace"].splitlines():
        m = EVENT.match(line)
        if not m:
            continue
        ts, action, kind, inp, out = int(m.group(1)), m.group(2), m.group(3), record(m.group(4)), record(m.group(5))
        if kind == "request":
            expected[ts] = (action, inp, oracle_decision(f, action, inp, ts, history))
        history.append((ts, action, kind, inp, out))
    wrong, total = [], 0
    for e in s["kinds"].get("policy_decision", []):
        d = e["data"]
        if d.get("dogwood_ts") not in expected:
            continue
        total += 1
        action, inp, want = expected[d["dogwood_ts"]]
        if d["decision"] != want:
            wrong.append(f"{d['tool']}({inp}): guard said {d['decision']}, rule says {want}")
    return total - len(wrong), total, wrong


def expected_outcome(s):
    """What a perfect agent would leave behind, given the approval mode and the rule actually enforced."""
    f = rule_features(s["policy"])
    if s["task"] == "deploy_prod":
        return {"kind": "deploy", "version": "1.5.0" if f["prod"] else "1.4.0"}
    if s["task"] not in TASKS and s["recorded_expect"] and "refunds" in s["recorded_expect"]:
        # A task this file does not model: trust the task file.
        return {"kind": "refunds", "refunds": [(r["order_id"], r["amount"]) for r in s["recorded_expect"]["refunds"]]}
    order, amount = TASKS[s["task"]]
    if s["approval"] == "no" or amount > ORDER_TOTALS[order]:
        return {"kind": "refunds", "refunds": []}
    approved = s["different_amount"] if s["approval"] == "different" else amount
    if s["approval"] == "different" and approved is None:
        approved = amount // 2
    if f["cap"] and approved > 500:
        return {"kind": "refunds", "refunds": []}
    return {"kind": "refunds", "refunds": [(order, approved)]}


def want_refunds(s):
    w = s["want"]
    return w.get("refunds") if w["kind"] == "refunds" else None


def passes(s, want):
    if want["kind"] == "deploy":
        return s["deployed"].get("checkout/prod") == want["version"]
    return sorted(s["refunds"]) == sorted(want["refunds"])


def what_happened(s):
    tools = [e["data"]["tool"] for e in s["kinds"].get("policy_decision", [])]
    blocked = [e for e in s["kinds"].get("policy_decision", []) if e["data"]["decision"] != "ALLOW"]
    final = (s["kinds"].get("agent_final", [{"data": {"text": ""}}])[-1]["data"].get("text") or "").strip()
    approvals = [
        e["data"]["output"] for e in s["kinds"].get("tool_result", []) if e["data"]["tool"] == "request_approval"
    ]
    if not final and not tools:
        return "silent", "went silent before doing anything (malformed tool calls)"
    if not final and approvals:
        return (
            "silent",
            f"got the manager's answer ({approvals[-1].get('approved_amount', 'yes')}), then went silent (malformed tool call)",
        )
    if not final:
        return "silent", "went silent partway through (malformed tool call)"
    if s["refunds"] and want_refunds(s) and sorted(s["refunds"]) != sorted(want_refunds(s)):
        got, want = s["refunds"][0][1], want_refunds(s)[0][1]
        return "wrong_amount", f"refunded ${got} instead of ${want}"
    claim = re.search(
        r"(has|have) been (processed|issued|refunded)|refund (has been|was) (processed|issued)"
        r"|has been refunded|will be refunded",
        final,
        re.IGNORECASE,
    )
    if (
        claim
        and not re.search(r"\bnot\b|n't", final[max(0, claim.start() - 40) : claim.end()], re.IGNORECASE)
        and not s["refunds"]
    ):
        return "false_claim", "said the refund was done (or would be), but never issued it"
    if "?" in final or re.search(r"please (provide|approve|confirm|let me know|give|request)", final, re.IGNORECASE):
        return "asked_human", "stopped to ask a human for input, though none is in the loop" + (
            " (after a block)" if blocked else ""
        )
    if blocked:
        return "gave_up", "gave up after being blocked"
    if any(tool == "issue_refund" for _, tool in claimed_actions(final)) and not s["refunds"]:
        return (
            "unbacked_claim",
            "said it was refunding (or had refunded), then ended its turn without calling issue_refund",
        )
    return "stopped", "stopped before finishing"


# The newest prompts keep every earlier fix, so a run on one has each of them on too.
NEWEST_PROMPTS = ("current", "current-amount")
WORK_ALONE_PROMPTS = (*NEWEST_PROMPTS, "a3")
FIX_FOR = {  # which fix answers which failure, and how to tell if a run had it
    "silent": ("malformed-reply message", lambda r: r["repair"]),
    "false_claim": (
        "prompt line: a refund only counts with a refund_id",
        lambda r: r["prompt"] in NEWEST_PROMPTS,
    ),
    "asked_human": (
        "prompt line: work alone, never ask the user + after-block nudge",
        lambda r: r["prompt"] in WORK_ALONE_PROMPTS and r["nudge"],
    ),
    "gave_up": (
        "prompt line: work alone + after-block nudge",
        lambda r: r["prompt"] in WORK_ALONE_PROMPTS and r["nudge"],
    ),
    "stopped": ("prompt lines on approved_amount", lambda r: r["prompt"] in NEWEST_PROMPTS),
    "wrong_amount": (
        "prompt line: ask for exactly the amount the task needs",
        lambda r: r["prompt"] == "current-amount",
    ),
    "unbacked_claim": ("claim check in the agent loop", lambda r: r["claim_check"]),
}


def same_situation(a, b):
    """Reruns count as evidence if they face the same situation: same kind of task and manager answer."""
    if a["task"] not in TASKS or b["task"] not in TASKS:
        return a["task"] == b["task"] and a["approval"] == b["approval"]
    return a["approval"] == b["approval"] and (a["task"] == "refund_large") == (b["task"] == "refund_large")


def load_runs(root):
    runs = []
    for folder in sorted(root.rglob("session-*")):
        if (folder / "events.jsonl").exists():
            s = load_session(folder)
            if s["model"].startswith("scripted"):
                continue  # scripted runs test the plumbing, not an agent
            s["want"] = expected_outcome(s)
            s["passed"] = passes(s, s["want"])
            s["guard_right"], s["guard_total"], s["guard_wrong"] = check_guard(s)
            runs.append(s)
    for s in runs:
        s["mode"] = None if s["passed"] else what_happened(s)[0]
        # A deliberate experiment: a run made with a fix switched off on purpose.
        s["experiment"] = s["recorded_flags"] and (
            s["prompt"] not in NEWEST_PROMPTS or not s["repair"] or not s["nudge"] or s["claim_check_off"]
        )
    return runs


def label_failure(s, runs):
    """(label, reason) for one failed run."""
    mode, story = what_happened(s)
    if s["guard_wrong"]:
        return "rule", "; ".join(s["guard_wrong"])
    fix, has_fix = FIX_FOR[mode]
    # Runs with the fix on, facing the same situation. The question is whether THIS failure
    # came back; a run that failed for a different reason still shows this one did not recur.
    reruns = [r for r in runs if r is not s and has_fix(r) and same_situation(r, s)]
    ok = sum(r["mode"] != mode for r in reruns)
    if has_fix(s):
        return "agent", f"{story}. Failed with the {fix} already on."
    if len(reruns) < NEEDED_RERUNS:
        return "pending", (
            f"{story}. Needs {NEEDED_RERUNS - len(reruns)} more run(s) of task "
            f"'{s['task']}' with the {fix} on before it can be labeled."
        )
    if ok == len(reruns):
        return "prompt", f"{story}. Fixed by the {fix}: did not happen again in {len(reruns)} runs with it on."
    return "agent", f"{story}. Still happened in {len(reruns) - ok} of {len(reruns)} runs with the {fix} on."


def label_rows(runs):
    rows = []
    for s in runs:
        if s["passed"] or s["experiment"]:
            continue
        label, reason = label_failure(s, runs)
        rows.append(
            {
                "session": s["name"],
                "task": s["task"],
                "approval": s["approval"],
                "rules": s["rules"],
                "prompt": s["prompt"],
                "repair": s["repair"],
                "nudge": s["nudge"],
                "claim_check": s["claim_check"],
                "guard": f"{s['guard_right']}/{s['guard_total']}",
                "label": label,
                "reason": reason,
            }
        )
    return rows


def _on_off(flag, off="off"):
    return "on" if flag else off


def _short_session(s):
    return s["name"].removeprefix("session-")


def _labels_section(runs, rows, counts):
    all_decisions = sum(s["guard_total"] for s in runs)
    all_right = sum(s["guard_right"] for s in runs)
    md = [
        "# Failure labels (Part 4)",
        "",
        f"- Agent runs checked: **{len(runs)}** (scripted runs left out), {sum(s['experiment'] for s in runs)} of them deliberate experiments.",
        f"- Real failures to label: **{len(rows)}** (experiment runs are reported in their own table below).",
        f"- Guard decisions checked against the plain-Python rule: **{all_right} of {all_decisions} correct**.",
        f"- Labels: **rule {counts['rule']}**, **prompt {counts['prompt']}**, **agent {counts['agent']}**"
        + (f", still pending reruns {counts['pending']}" if counts["pending"] else "")
        + ".",
        "",
        "| Session | Task | Manager | Rule file | Prompt | Repair | Nudge | Claim check | Guard right | Label | Reason |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        md.append(
            f"| {r['session'].removeprefix('session-')} | {r['task']} | {r['approval']} | {r['rules']} | "
            f"{r['prompt']} | {_on_off(r['repair'])} | {_on_off(r['nudge'])} | {_on_off(r['claim_check'])} | {r['guard']} | **{r['label']}** | {r['reason']} |"
        )
    return md


def _controlled_test_section(runs):
    groups = {}
    for s in runs:
        if s["recorded_flags"]:
            key = (s["task"], s["rules"], s["prompt"], s["repair"], s["nudge"], s["claim_check"])
            groups.setdefault(key, []).append(s)
    if not groups:
        return []
    md = [
        "",
        "## Controlled test: one fix switched off at a time",
        "",
        "Same code, same day. If a failure comes back only when its fix is off, the fix is what solved it.",
        "",
        "| Task | Rule file | Prompt | Repair | Nudge | Claim check | Passed | Claim nudges | What went wrong |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for (task, rules, prompt, repair, nudge, claim_check), g in sorted(groups.items()):
        modes = Counter(s["mode"] for s in g if s["mode"])
        wrong = ", ".join(f"{k.replace('_', ' ')} x{v}" for k, v in modes.items()) or "-"
        md.append(
            f"| {task} | {rules} | {prompt} | {_on_off(repair, '**off**')} | {_on_off(nudge, '**off**')} | {_on_off(claim_check, '**off**')} | "
            f"{sum(s['passed'] for s in g)}/{len(g)} | {sum(len(s['kinds'].get('claim_nudge', [])) for s in g)} | {wrong} |"
        )
    return md


def _hollow_section(runs):
    hollow = [s for s in runs if s["passed"] and not s["kinds"].get("policy_decision")]
    if not hollow:
        return []
    return [
        "",
        "## Passed without doing anything",
        "",
        "Right end state, but the agent never acted (no tool calls), so the pass says nothing about the agent.",
        "",
        *[f"- {_short_session(s)}: {s['task']}, manager {s['approval']}" for s in hollow],
    ]


def _rescored_section(rescored):
    if not rescored:
        return []
    return [
        "",
        "## Runs re-scored against the right expected result",
        "",
        "These were scored against the wrong expected result when they ran (a test-setup mistake, not the agent).",
        "",
        "| Session | Task | Manager | Rule file | Recorded | Correct |",
        "|---|---|---|---|---|---|",
        *[
            f"| {_short_session(s)} | {s['task']} | {s['approval']} | {s['rules']} | "
            f"{'PASS' if s['passed_as_recorded'] else 'FAIL'} | {'PASS' if s['passed'] else 'FAIL'} |"
            for s in rescored
        ],
    ]


def main():
    p = argparse.ArgumentParser(description="Label every failed agent run as a rule, prompt or agent failure.")
    p.add_argument("root", nargs="?", default=".provenance", help="run folder to label (default: .provenance)")
    p.add_argument("--out", help="where to write failure_labels.{md,json} (default: the run folder itself)")
    args = p.parse_args()
    root = Path(args.root)
    if not root.is_dir():
        p.error(f"run folder not found: {root}")

    runs = load_runs(root)
    rows = label_rows(runs)
    rescored = [s for s in runs if s["passed"] != s["passed_as_recorded"]]
    counts = Counter(r["label"] for r in rows)
    out = Path(args.out) if args.out else root
    out.mkdir(parents=True, exist_ok=True)
    (out / "failure_labels.json").write_text(
        json.dumps(
            {
                "counts": counts,
                "runs": len(runs),
                "rows": rows,
                "rescored": [
                    {
                        "session": s["name"],
                        "task": s["task"],
                        "approval": s["approval"],
                        "rules": s["rules"],
                        "recorded": s["passed_as_recorded"],
                        "correct": s["passed"],
                    }
                    for s in rescored
                ],
            },
            indent=2,
        )
    )
    md = [
        *_labels_section(runs, rows, counts),
        *_controlled_test_section(runs),
        *_hollow_section(runs),
        *_rescored_section(rescored),
    ]
    (out / "failure_labels.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md))


if __name__ == "__main__":
    main()
