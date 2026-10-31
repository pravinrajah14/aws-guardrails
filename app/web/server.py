"""The demo's backend: a thin layer over app.core that streams each step as one line of JSON."""

import json
import queue
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.core import agent_run, answers, flow, generate, known_bugs, planted, reference, rule_types, saved_cases
from app.core.model import ModelError, OllamaModel
from app.core.store import MockWorld

STATIC = Path(__file__).parent / "static"
TICK_SECONDS = 1
DEMO_SENTENCES = [
    "No refunds over $5,000.",
    "Only refund if a manager approved it recently.",
    "No production refunds over $500.50.",
    "Refund up to $40 for order A100, subject to approval.",
    "Only email customers who opted in.",
]
FINISHED = object()
# The model handles one request well and many badly, so requests queue here.
model_lock = threading.Lock()
model = OllamaModel()
app = FastAPI(title="Guardrails demo")


class PolicyRequest(BaseModel):
    sentence: str
    answers: dict[str, str] = {}
    picks: dict[str, str] = {}


class CheckRequest(BaseModel):
    policy_id: str
    bug: str | None = None


class ConfirmRequest(BaseModel):
    policy_id: str
    says: str
    expect: str
    events: list[dict]


class RunRequest(BaseModel):
    policy_id: str
    task_id: str
    approval: str = "yes"
    different_amount: int | None = None


def _work(events, produce):
    """Runs in a thread, holding the model, so the response can keep sending ticks while the model thinks."""
    if not model_lock.acquire(blocking=False):
        events.put({"type": "waiting"})
        model_lock.acquire()
    try:
        produce(events.put)
    except (ModelError, RuntimeError, OSError) as e:
        events.put({"type": "error", "message": str(e)})
    finally:
        model_lock.release()
        events.put(FINISHED)


def _stream(produce):
    events = queue.Queue()
    threading.Thread(target=_work, args=(events, produce), daemon=True).start()
    seconds = 0
    while True:
        try:
            event = events.get(timeout=TICK_SECONDS)
        except queue.Empty:
            seconds += TICK_SECONDS
            yield json.dumps({"type": "tick", "seconds": seconds}) + "\n"
            continue
        if event is FINISHED:
            return
        yield json.dumps(event) + "\n"


def _ndjson(produce):
    return StreamingResponse(_stream(produce), media_type="application/x-ndjson")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/setup")
def setup():
    return {
        "demo_sentences": DEMO_SENTENCES,
        "steps": flow.STEPS,
        "supported_types": rule_types.supported_ids(),
        "orders": MockWorld().orders,
        "tasks": [
            {k: t.get(k) for k in ("id", "prompt", "approval", "different_amount", "file")} for t in agent_run.tasks()
        ],
    }


@app.post("/api/policy")
def write_policy(request: PolicyRequest):
    def produce(emit):
        for event in flow.write_policy(request.sentence, model, request.answers, request.picks):
            emit(event)

    return _ndjson(produce)


@app.get("/api/policy/{policy_id}")
def get_policy(policy_id: str):
    out, record = _policy_or_404(policy_id)
    forms = json.loads((out / generate.FORM_FILE).read_text(encoding="utf-8"))
    return {
        **record,
        "id": out.name,
        "dogwood": (out / generate.RULE_FILE).read_text(encoding="utf-8"),
        "bugs": known_bugs.applicable(forms),
        "has_reference": bool((found := reference.find_reference(record["sentence"])) and found["forms"]),
        "runs": agent_run.runs_of(out.name),
    }


def _policy_or_404(policy_id):
    try:
        return flow.load_policy(policy_id)
    except FileNotFoundError:
        raise HTTPException(404, f"no policy {policy_id}") from None


@app.post("/api/reference")
def reference_check(request: CheckRequest):
    _policy_or_404(request.policy_id)
    try:
        return reference.check(request.policy_id, request.bug)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


@app.post("/api/fix")
def fix_from_reference(request: CheckRequest):
    _policy_or_404(request.policy_id)
    try:
        return reference.fix(request.policy_id, request.bug)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


@app.post("/api/confirm")
def confirm_case(request: ConfirmRequest):
    out, record = _policy_or_404(request.policy_id)
    saved_cases.save(record["sentence"], request.says, request.expect, request.events)
    forms = json.loads((out / generate.FORM_FILE).read_text(encoding="utf-8"))
    gate = generate.gate_for(forms, out, out / "policy.dw")
    return {"saved_cases": saved_cases.rerun(record["sentence"], gate)}


@app.post("/api/answers/reset")
def reset_answers():
    backup, cleared = answers.backup_and_clear()
    return {"cleared": cleared, "backup": backup.name if backup else None}


@app.get("/api/planted")
def planted_errors():
    return {"rows": planted.replay_all()}


@app.post("/api/run")
def run_agent(request: RunRequest):
    _policy_or_404(request.policy_id)
    if request.task_id not in {t["id"] for t in agent_run.tasks()}:
        raise HTTPException(404, f"no task {request.task_id}")

    def produce(emit):
        result = agent_run.run(request.policy_id, request.task_id, request.approval, request.different_amount, emit)
        emit({"type": "finished", "policy_id": request.policy_id, **result})

    return _ndjson(produce)


@app.get("/logs/{policy_id}/{session}/{name}")
def run_log(policy_id: str, session: str, name: str):
    path = agent_run.log_path(policy_id, session, name)
    if path is None:
        raise HTTPException(404, "no such log")
    return PlainTextResponse(path.read_text(encoding="utf-8"))


app.mount("/static", StaticFiles(directory=STATIC), name="static")
