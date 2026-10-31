"""Create a generated policy or evaluate it with the original guarded agent."""

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from app.core import config

ROOT = Path(__file__).resolve().parent
STORE = ROOT / "kylie-test-bed-upload"
SCHEMA = STORE / "policies/schema.cedarschema"
BASE = STORE / "policies/base.dw"
POINTER = ROOT / "pipeline-runs/latest-policy.json"
OLLAMA_TAGS_URL = "http://127.0.0.1:11434/api/tags"
OLLAMA_CHECK_TIMEOUT_SECONDS = 5
STDOUT_TAIL_CHARS = 2500
STDERR_TAIL_CHARS = 1500
CODE_FILES = [
    ROOT / "run_pipeline.py",
    ROOT / "converter/convert.py",
    ROOT / "question/find_question.py",
    ROOT / "question/answer.py",
    STORE / "agent.py",
    STORE / "run_task.py",
    STORE / "enforce.py",
    STORE / "score_actions.py",
    STORE / "tools.py",
    STORE / "recorder.py",
    STORE / "context_engine.py",
]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json_atomically(path, data):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2) + "\n")
    temp.replace(path)


def say(text):
    print(text, flush=True)


class PipelineRun:
    """One output folder: its manifest, kept up to date on disk, and the commands run for it."""

    def __init__(self, out, manifest):
        self.out = out
        self.manifest = manifest

    def save(self):
        write_json_atomically(self.out / "manifest.json", self.manifest)

    def run(self, name, command, cwd=ROOT):
        argv = list(map(str, command))
        say(f"\n{name}: {' '.join(argv)}")
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)
        (self.out / f"{name}.log").write_text(proc.stdout + proc.stderr)
        self.manifest["commands"].append({"stage": name, "argv": argv, "cwd": str(cwd), "exit_code": proc.returncode})
        self.save()
        say(proc.stdout[-STDOUT_TAIL_CHARS:])
        if proc.returncode:
            raise RuntimeError(f"{name} failed: {proc.stderr[-STDERR_TAIL_CHARS:]} (see {self.out / (name + '.log')})")


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=["all", "create", "test"], default="all")
    parser.add_argument("--from-policy", type=Path, help="Creation directory; defaults to the latest created policy")
    parser.add_argument(
        "--answer", choices=["allow", "block"], help="Operator's answer to the solver question; block selects 1h"
    )
    parser.add_argument("--model", default=config.MODEL)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--tasks", type=Path, default=STORE / "tasks/recent_approval_tasks.json")
    parser.add_argument("--out", type=Path, help="New output directory; never overwrites an earlier run")
    return parser


def check_model_installed(parser, model):
    """Fail before running the solver if the requested real model is unavailable.

    There is deliberately no fallback to the scripted model.
    """
    try:
        with urllib.request.urlopen(OLLAMA_TAGS_URL, timeout=OLLAMA_CHECK_TIMEOUT_SECONDS) as response:
            models = {m["name"] for m in json.load(response)["models"]}
        if model not in models:
            parser.error(f"model {model} is not installed; available: {sorted(models)}")
    except OSError as error:
        parser.error(f"start Ollama first (ollama serve): {error}")


def load_creation(parser, from_policy):
    """The creation folder to test and its manifest, refused if anything changed since it was created."""
    source = from_policy
    if source is None:
        if not POINTER.is_file():
            parser.error("create a policy first: ./policy create")
        source = Path(json.loads(POINTER.read_text())["directory"])
    source = source.resolve()
    original = json.loads((source / "manifest.json").read_text())
    if digest(source / "selected/updated_rule.dw") != original["inputs"]["policy"]["sha256"]:
        parser.error("the created policy was edited; create a new policy before testing")
    for name, path in {"schema": SCHEMA, "base": BASE}.items():
        if digest(path) != original["inputs"][name]["sha256"]:
            parser.error(f"{name} changed since creation; create a new policy before testing")
    return source, original


def choose_answer(args, question_data):
    if args.answer is not None:
        return args.answer
    if args.stage != "create":
        return "block"
    say(question_data["question"])
    answer = None
    while answer not in ("allow", "block"):
        answer = input("Answer allow or block: ").strip().lower()
    return answer


def create_policy(run, args):
    """Ask the solver question, apply the answer, and return the folder holding the selected policy."""
    run.run("question", [sys.executable, ROOT / "question/find_question.py", "--out", run.out / "question"])
    question_data = json.loads((run.out / "question/question.json").read_text())
    for version in question_data["versions"].values():
        form_path = ROOT / "question/forms" / version["form"]
        say(f"\nCandidate form: {form_path}")
        say(json.dumps(json.loads(form_path.read_text()), indent=2))
    answer = choose_answer(args, question_data)
    run.manifest["answer"] = answer
    selected = run.out / "selected"
    run.run(
        "selection",
        [
            sys.executable,
            ROOT / "question/answer.py",
            "--answer",
            answer,
            "--question",
            run.out / "question/question.json",
            "--out",
            selected,
        ],
    )
    say("\nSelected form:")
    say(json.dumps(json.loads((selected / "updated_form.json").read_text()), indent=2))
    say("\nGenerated Dogwood policy:")
    say((selected / "updated_rule.dw").read_text())
    return selected


def reuse_policy(run, source, original):
    """Copy an earlier creation's policy here and rescore it."""
    selected = run.out / "selected"
    selected.mkdir()
    for name in ("updated_form.json", "updated_rule.dw", "actions_with_answer.json"):
        shutil.copy2(source / "selected" / name, selected / name)
    run.manifest["creation_directory"] = str(source)
    run.manifest["answer"] = original["answer"]
    run.run(
        "scripted",
        [
            sys.executable,
            STORE / "score_actions.py",
            selected / "actions_with_answer.json",
            "--rules",
            selected / "updated_rule.dw",
            "--output-dir",
            run.out / "scripted",
        ],
        cwd=STORE,
    )
    (scorecard,) = (run.out / "scripted").glob("rule_scorecard_*.json")
    shutil.copy2(scorecard, selected / "rule_scorecard.json")
    return selected


def record_inputs(run, selected, tasks):
    policy = selected / "updated_rule.dw"
    run.manifest["inputs"] = {
        "policy": {"path": str(policy), "sha256": digest(policy)},
        "schema": {"path": str(SCHEMA), "sha256": digest(SCHEMA)},
        "base": {"path": str(BASE), "sha256": digest(BASE)},
        "tasks": {"path": str(tasks), "sha256": digest(tasks)},
        "form": json.loads((selected / "updated_form.json").read_text()),
    }
    # Snapshot every live input so subsequent edits cannot change what a run means.
    (run.out / "schema.cedarschema").write_bytes(SCHEMA.read_bytes())
    (run.out / "base.dw").write_bytes(BASE.read_bytes())
    (run.out / "tasks.json").write_bytes(tasks.read_bytes())


def finish_creation(run, policy):
    run.manifest["status"] = "created"
    run.save()
    POINTER.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomically(POINTER, {"directory": str(run.out), "policy_sha256": digest(policy)})
    say(f"\nPolicy created: {policy}\nNext: ./policy test")


def write_summary(run, args, card, rule_card, policy):
    manifest = run.manifest
    lines = [
        "# Generated-policy agent integration",
        "",
        f"Model: {args.model}; execution: {manifest['execution']}",
        f"Answer: {manifest['answer']}; approval window: {manifest['inputs']['form']['requires']['within']}",
        f"Scripted checks: {rule_card['right']}/{rule_card['total']}",
        f"Agent tasks: {manifest['agent_score']['passed']}/{len(card['runs'])}",
        f"Selected policy SHA256: {digest(policy)}",
        "",
        "The agent scorecard confirms the same generated-policy hash.",
        "Session traces and per-session combined policies are under agent/traces/.",
        "Failed tasks are retained; a successful process launch is not a passing evaluation.",
    ]
    (run.out / "SUMMARY.md").write_text("\n".join(lines) + "\n")
    say(f"\nResults: {run.out / 'SUMMARY.md'}")


def test_with_agent(run, args, selected):
    """Run the agent tasks against the policy. Returns the process exit code."""
    policy = selected / "updated_rule.dw"
    run.run(
        "agent",
        [
            sys.executable,
            STORE / "run_task.py",
            "--rules",
            policy,
            "--tasks",
            run.out / "tasks.json",
            "--all",
            "--runs",
            args.runs,
            "--model",
            args.model,
            "--output-dir",
            run.out / "agent",
        ],
        cwd=STORE,
    )
    (card_path,) = (run.out / "agent").glob("agent_scorecard_*.json")
    card = json.loads(card_path.read_text())
    if card["setup"]["sha256"]["rules"] != run.manifest["inputs"]["policy"]["sha256"]:
        raise RuntimeError("agent did not load the selected generated policy")
    rule_card = json.loads((selected / "rule_scorecard.json").read_text())
    run.manifest["scripted_score"] = {"right": rule_card["right"], "total": rule_card["total"]}
    run.manifest["agent_score"] = {
        "passed": sum(r["passed"] for r in card["runs"]),
        "total": len(card["runs"]),
        "scorecard": str(card_path),
    }
    clean = rule_card["right"] == rule_card["total"] and all(r["passed"] for r in card["runs"])
    run.manifest["status"] = "passed" if clean else "completed_with_failures"
    run.save()
    write_summary(run, args, card, rule_card, policy)
    return 0 if clean else 1


def main():
    parser = build_parser()
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be positive")
    tasks = args.tasks.resolve()
    if not tasks.is_file():
        parser.error(f"task file does not exist: {tasks}")
    if args.stage != "create" and args.model != "scripted":
        check_model_installed(parser, args.model)
    source, original = load_creation(parser, args.from_policy) if args.stage == "test" else (None, None)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
    out = (args.out or ROOT / "pipeline-runs" / stamp).resolve()
    out.mkdir(parents=True, exist_ok=False)
    manifest = {
        "started_utc": stamp,
        "model": args.model,
        "runs_per_task": args.runs,
        "answer": args.answer,
        "execution": "scripted" if args.model == "scripted" else "ollama",
        "commands": [],
        "stage": args.stage,
        "status": "running",
    }
    run = PipelineRun(out, manifest)
    run.save()
    try:
        manifest["code_sha256"] = {str(p.relative_to(ROOT)): digest(p) for p in CODE_FILES}
        selected = create_policy(run, args) if source is None else reuse_policy(run, source, original)
        record_inputs(run, selected, tasks)
        if args.stage == "create":
            finish_creation(run, selected / "updated_rule.dw")
            return 0
        return test_with_agent(run, args, selected)
    except Exception as error:
        manifest["status"], manifest["error"] = "error", str(error)
        run.save()
        raise


if __name__ == "__main__":
    sys.exit(main())
