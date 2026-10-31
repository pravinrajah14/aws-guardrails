"""Run sentences against the real model, one call at a time, and save every run as JSON."""

import json
import subprocess
import time
from pathlib import Path

from app.core import config, report, rule_types
from app.core.model import ModelError, ModelTimeout, OllamaModel
from app.core.numbers import ALLOWED_MAX
from app.core.understand import CLARIFY, INVALID, OK, REVIEW, understand

DEMO = [
    {
        "sentence": "No refunds over $5,000.",
        "expect": {
            "rule_type": rule_types.AMOUNT_CAP,
            "values": {"max_amount": 5000},
            "verdict": OK,
        },
        "variants": {
            "no period": "No refunds over $5,000",
            "lowercase": "no refunds over $5,000.",
            "rephrase": "Refunds over $5,000 are not allowed.",
        },
    },
    {
        "sentence": "Only refund if a manager approved it recently.",
        "expect": {
            "rule_type": rule_types.APPROVAL_REQUIRED,
            "values": {"approver_role": "manager"},
            "verdict": CLARIFY,
            "vague": "recently",
        },
        "variants": {
            "no period": "Only refund if a manager approved it recently",
            "lowercase": "only refund if a manager approved it recently.",
            "rephrase": "Don't refund unless a manager recently approved it.",
        },
    },
    {
        "sentence": "No production refunds over $500.50.",
        "expect": {"rule_type": rule_types.AMOUNT_CAP, "values": {"max_amount": "500.50"}, "verdict": REVIEW},
        "variants": {
            "no period": "No production refunds over $500.50",
            "lowercase": "no production refunds over $500.50.",
            "rephrase": "Production refunds over $500.50 are not allowed.",
        },
    },
    {
        "sentence": "Refund up to $40 for order A100, subject to approval.",
        # No verdict is expected for this one: the approval window is not stated.
        "expect": {
            "rule_type": rule_types.CAPPED_WITH_APPROVAL,
            "values": {"max_amount": 40, "order_id": "A100"},
            "directions": {"max_amount": ALLOWED_MAX},
        },
        "variants": {
            "no period": "Refund up to $40 for order A100, subject to approval",
            "lowercase": "refund up to $40 for order a100, subject to approval.",
            "rephrase": "Refunds for order A100 are capped at $40 and need approval.",
        },
    },
]
UNSUPPORTED = [
    "Don't let the agent read customers' SSNs",
    "Block deploys after 6pm on Fridays",
    "Only email customers who opted in",
    "Never copy private data to a public repo",
    "At most 5 refunds per hour",
    "Refunds are only allowed for orders placed within the last 30 days.",
]
REFUSED = {"verdict": INVALID, "rule_type": None}
FULL_REPEATS = 3
FULL_SEEDS = [1, 2, 3]
FULL_TEMPERATURE = 0.3
PS_TIMEOUT_SECONDS = 10
CASE_FILES = [
    config.REPO / "app" / "tests" / "understanding_cases.json",
    config.REPO / "app" / "tests" / "regression_cases.json",
]


def ollama_ps():
    try:
        done = subprocess.run(["ollama", "ps"], check=False, capture_output=True, text=True, timeout=PS_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError) as e:
        return f"could not run ollama ps: {e}"
    return (done.stdout or done.stderr).strip()


def _call(label, sentence, expect, temperature=config.TEMPERATURE, seed=config.SEED):
    return {"label": label, "sentence": sentence, "expect": expect, "temperature": temperature, "seed": seed}


def cases():
    """The sentences with fixed expected results, and the supported sentences in the library and forms."""
    return [c for path in CASE_FILES for c in json.loads(path.read_text(encoding="utf-8"))]


def default_calls():
    originals = [_call("original", d["sentence"], d["expect"]) for d in DEMO]
    refused = [_call("unsupported", s, REFUSED) for s in UNSUPPORTED]
    seen = {d["sentence"] for d in DEMO}
    return (
        originals + refused + [_call("case", c["sentence"], c["expect"]) for c in cases() if c["sentence"] not in seen]
    )


def full_calls():
    calls = []
    for d in DEMO:
        calls.append(_call("original", d["sentence"], d["expect"]))
        calls += [
            _call(label, s, {k: v for k, v in d["expect"].items() if k != "directions"})
            for label, s in d["variants"].items()
        ]
    calls += [_call("unsupported", s, REFUSED) for s in UNSUPPORTED]
    repeats = [c for c in calls for _ in range(FULL_REPEATS)]
    seeded = [
        {**c, "temperature": FULL_TEMPERATURE, "seed": seed}
        for c in calls
        if c["label"] in ("original", "unsupported")
        for seed in FULL_SEEDS
    ]
    return repeats + seeded


def _outcome(u):
    return {
        "answer": u["model"]["answer"],
        "timing": u["model"]["timing"],
        "shown": report.shown(u),
        "vague": [v["text"] for v in u["vague_terms"]],
        "verdict_reason": u["verdict_reason"],
        "examples_used": u["examples_used"],
    }


def run_calls(calls, model_name):
    """Make the calls in order. Returns the runs and why the run stopped early, or None if it finished."""
    runs, timeouts = [], 0
    for call in calls:
        model = OllamaModel(model_name, temperature=call["temperature"], seed=call["seed"])
        try:
            run = {**call, **_outcome(understand(call["sentence"], model))}
        except ModelTimeout as e:
            timeouts += 1
            runs.append({**call, "error": str(e)})
            print(f"  timeout {timeouts}/{config.MAX_TIMEOUTS}  {call['sentence']}", flush=True)
            if timeouts >= config.MAX_TIMEOUTS:
                return runs, f"stopped after {timeouts} timeouts: {e}"
            continue
        except ModelError as e:
            runs.append({**call, "error": str(e)})
            return runs, f"stopped: {e}"
        runs.append(run)
        t = run["timing"]
        print(
            f"  t={call['temperature']} seed={call['seed']} {t['total']:>5}s "
            f"prompt_tokens={t['prompt_tokens']} {report.summary(run['shown'])}  {call['sentence']}",
            flush=True,
        )
    return runs, None


def session(calls, model_name, mode):
    start = time.monotonic()
    ps_start = ollama_ps()
    print(f"ollama ps at start:\n{ps_start}\n", flush=True)
    runs, stopped = run_calls(calls, model_name)
    ps_end = ollama_ps()
    print(f"\nollama ps at end:\n{ps_end}", flush=True)
    return {
        "mode": mode,
        "model": model_name,
        "num_ctx": config.NUM_CTX,
        "keep_alive": config.KEEP_ALIVE,
        "call_timeout_seconds": config.CALL_TIMEOUT_SECONDS,
        "seconds": round(time.monotonic() - start, 1),
        "ollama_ps_start": ps_start,
        "ollama_ps_end": ps_end,
        "stopped": stopped,
        "runs": runs,
    }


def run(model_name=config.MODEL, out=None, full=False):
    """Returns why the run stopped early, or None."""
    mode = "full" if full else "default"
    out = Path(out or config.REPO / "results" / ("understanding_full.md" if full else "understanding.md"))
    calls = full_calls() if full else default_calls()
    data = session(calls, model_name, mode)
    runs_path = out.parent / f"understanding_runs_{mode}.json"
    runs_path.parent.mkdir(parents=True, exist_ok=True)
    runs_path.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
    report.write_understanding(runs_path, out)
    print(f"wrote {runs_path} and {out}")
    return data["stopped"]
