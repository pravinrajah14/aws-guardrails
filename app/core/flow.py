"""Sentence to status, one named step at a time, so a caller can show each step as it happens."""

import json
from pathlib import Path

from app.core import edge_examples, generate, saved_cases
from app.core.resolve import AnswerError, resolve
from app.core.status import decide

UNDERSTAND, CONTEXT, FILL, SUMMARY, GENERATE, VALIDATE, EXPLAIN, STATUS = (
    "Understand",
    "Context",
    "Fill or ask",
    "Summary",
    "Generate",
    "Validate",
    "Explain",
    "Status",
)
STEPS = [UNDERSTAND, CONTEXT, FILL, SUMMARY, GENERATE, VALIDATE, EXPLAIN, STATUS]
RUNNING, DONE, WAITING, SKIPPED, FAILED = "running", "done", "waiting", "skipped", "failed"


def _step(name, state, **data):
    return {"type": "step", "step": name, "state": state, **data}


def _skip_rest(after):
    """Later steps never ran; say so instead of leaving them looking pending."""
    return [_step(name, SKIPPED) for name in STEPS[STEPS.index(after) + 1 : -1]]


def _understood_view(u):
    return {
        "rule_type": u["rule_type"],
        "description": u.get("description"),
        "action": u["action"],
        "slots": {
            name: {k: s.get(k) for k in ("value", "source", "required", "direction_text", "evidence")}
            for name, s in u["slots"].items()
        },
        "qualifiers": u["qualifiers"],
        "readings": u.get("readings"),
        "rejected": u["rejected"],
        "model": u["model"],
    }


def _final(u, problems=(), validation=None, examples=(), policy=None):
    decided = decide(u, problems, validation, examples)
    return _step(STATUS, DONE, **decided, policy=policy)


def _stop_at(u, after, problems=()):
    yield from _skip_rest(after)
    yield _final(u, problems)


def write_policy(sentence, model=None, given=None, picks=None):
    """Yields one event per step: {type, step, state, ...}. The last event is always the status."""
    yield _step(UNDERSTAND, RUNNING)
    try:
        result = resolve(sentence, model, given, picks)
    except AnswerError as e:
        yield {"type": "answer_error", "message": str(e)}
        result = resolve(sentence, model)
    u = result["understanding"]
    yield _step(UNDERSTAND, DONE, understood=_understood_view(u), highlights=result["highlights"])
    yield _step(CONTEXT, DONE, context=result["context"])
    conflicts = u.get("conflicts", [])
    fill_state = WAITING if result["questions"] or conflicts else DONE
    yield _step(FILL, fill_state, questions=result["questions"], conflicts=conflicts, limit_scope=u.get("limit_scope"))
    if u["verdict"] != "ok_so_far":
        yield from _stop_at(u, FILL)
        return
    problems = generate.readiness_problems(u)
    if problems:
        yield from _stop_at(u, FILL, problems)
        return
    yield from _generate_steps(u)


def _save_record(out, sentence, summary, status, examples, saved=()):
    record = {
        "sentence": sentence,
        "summary": summary,
        "status": status,
        "examples": examples,
        "saved_cases": list(saved),
    }
    (out / "record.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")


def _generate_steps(u):
    forms = generate.build_forms(u)
    summary = generate.forms_summary(forms)
    yield _step(SUMMARY, DONE, summary=summary)
    out = generate.new_policy_dir()
    yield _step(GENERATE, RUNNING)
    policy_path = generate.write_policy(forms, out)
    rule_text = (out / generate.RULE_FILE).read_text(encoding="utf-8")
    yield _step(GENERATE, DONE, forms=forms, dogwood=rule_text, policy_id=out.name)
    validation = generate.validate(policy_path, out)
    yield _step(VALIDATE, DONE if validation[0] else FAILED, output=validation[1])
    if not validation[0]:
        yield from _skip_rest(VALIDATE)
        decided = decide(u, validation=validation)
        _save_record(out, u["sentence"], summary, decided, [])
        yield _step(STATUS, DONE, **decided, policy=out.name)
        return
    yield _step(EXPLAIN, RUNNING)
    literal = generate.explain(out / generate.RULE_FILE)
    (out / "explain.txt").write_text(literal + "\n", encoding="utf-8")
    gate = generate.gate_for(forms, out, policy_path)
    examples = edge_examples.run_all(u, gate)
    saved = saved_cases.rerun(u["sentence"], gate)
    yield _step(EXPLAIN, DONE, summary=summary, literal=literal, examples=examples, saved_cases=saved)
    decided = decide(u, validation=validation, examples=examples, saved=saved)
    _save_record(out, u["sentence"], summary, decided, examples, saved)
    yield _step(STATUS, DONE, **decided, policy=out.name)


def load_policy(policy_id):
    """A saved policy's folder and record. policy_id is a folder name, never a path."""
    out = generate.POLICIES_ROOT / Path(policy_id).name
    return out, json.loads((out / "record.json").read_text(encoding="utf-8"))
