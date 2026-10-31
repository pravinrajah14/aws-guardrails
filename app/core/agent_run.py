"""Run one agent task against a saved policy, handing each recorded step to a callback as it happens."""

import json

from app.core import config, generate
from app.core.store import (
    STORE_DIR,
    AgentModel,
    DogwoodGate,
    MockWorld,
    Recorder,
    build_policy,
    find_form,
    load_tasks,
    run_agent,
)

TASK_FILES = ["tasks/refund_tasks.json", "tasks/recent_approval_tasks.json", "tasks/camillia_tasks.json"]
LOG_FILES = {"timeline.txt", "events.jsonl", "trace.log", "policy.dw"}
SHOWN_KINDS = {
    "user_task",
    "policy_validate",
    "model_turn",
    "empty_reply",
    "nudge",
    "claim_nudge",
    "policy_decision",
    "facts",
    "claim_ignored",
    "needs_review",
    "blocked",
    "bad_call",
    "tool_result",
    "tool_error",
    "agent_final",
}
RUNS_FOLDER = "runs"


class StreamingRecorder(Recorder):
    def __init__(self, root, on_event):
        super().__init__(root=root)
        self.on_event = on_event

    def log(self, kind, summary, **data):
        record = super().log(kind, summary, **data)
        if kind in SHOWN_KINDS:
            self.on_event({"type": "agent", "kind": kind, "summary": summary, "data": _shown_data(kind, data)})
        return record


def _shown_data(kind, data):
    if kind == "policy_decision":
        keys = ("tool", "args", "decision", "rules", "note", "facts", "facts_status", "facts_reason")
        return {k: data.get(k) for k in keys}
    if kind == "facts":
        return {k: data.get(k) for k in ("facts", "status", "sources")}
    return {}


def tasks():
    """Every agent task once, from the store's task files."""
    seen = {}
    for name in TASK_FILES:
        for task in load_tasks(STORE_DIR / name):
            seen.setdefault(task["id"], {**task, "file": name})
    return list(seen.values())


def end_state(world):
    return {
        "refunds": world.refunds,
        "approvals": world.approvals,
        "deployed": {f"{service}/{env}": version for (service, env), version in world.deployed.items()},
    }


def run(policy_id, task_id, approval, different_amount, on_event):
    """approval is the pretend manager's switch. Returns the store's end state and where the log is."""
    task = next(t for t in tasks() if t["id"] == task_id)
    out = generate.POLICIES_ROOT / policy_id
    rule = out / generate.RULE_FILE
    world = MockWorld(approval=approval, different_amount=different_amount)
    model = AgentModel()
    rec = StreamingRecorder(out / RUNS_FOLDER, on_event)
    policy = build_policy(generate.BASE, rule, rec.dir / "policy.dw")
    rec.start_session(task["id"], task["prompt"], model.name, policy, config.SCHEMA_PATH)
    form, _ = find_form(rule)
    gate = DogwoodGate(rec.dir, policy, config.SCHEMA_PATH, form=form, store=world)
    ok, output = gate.validate()
    rec.log("policy_validate", "OK" if ok else "FAILED", output=output)
    if not ok:
        return {"validated": False, "output": output, "session": rec.dir.name}
    world, final = run_agent(model, task["prompt"], gate, rec, world=world)
    state = end_state(world)
    rec.log("session_end", f"{rec.seq + 1} events recorded", **state)
    (rec.dir / "end_state.json").write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    return {"validated": True, "state": state, "final": final, "session": rec.dir.name}


def runs_of(policy_id):
    """A policy's agent runs, newest first, each with what it left in the store."""
    root = generate.POLICIES_ROOT / policy_id / RUNS_FOLDER
    sessions = sorted(root.glob("session-*"), reverse=True) if root.is_dir() else []
    return [{"session": s.name, "finished": (s / "end_state.json").is_file()} for s in sessions]


def log_path(policy_id, session, name):
    """A run's log file, or None. Only known file names inside a known run folder."""
    if name not in LOG_FILES or "/" in policy_id or "/" in session or ".." in policy_id + session:
        return None
    path = generate.POLICIES_ROOT / policy_id / RUNS_FOLDER / session / name
    return path if path.is_file() else None
