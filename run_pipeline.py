#!/usr/bin/env python3
"""Create a generated policy or evaluate it with the original guarded agent."""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STORE = ROOT / "kylie-test-bed-upload"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=["all", "create", "test"], default="all")
    parser.add_argument("--from-policy", type=Path, help="Creation directory; defaults to the latest created policy")
    parser.add_argument("--answer", choices=["allow", "block"],
                        help="Operator's answer to the solver question; block selects 1h")
    parser.add_argument("--model", default="qwen2.5:7b")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--tasks", type=Path, default=STORE / "tasks/recent_approval_tasks.json")
    parser.add_argument("--out", type=Path, help="New output directory; never overwrites an earlier run")
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be positive")
    tasks = args.tasks.resolve()
    if not tasks.is_file():
        parser.error(f"task file does not exist: {tasks}")
    # Fail before running the solver if the requested real model is unavailable.
    # There is deliberately no fallback to the scripted model.
    if args.stage != "create" and args.model != "scripted":
        try:
            with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=5) as response:
                models = {m["name"] for m in json.load(response)["models"]}
            if args.model not in models:
                parser.error(f"model {args.model} is not installed; available: {sorted(models)}")
        except OSError as error:
            parser.error(f"start Ollama first (ollama serve): {error}")
    source = None
    if args.stage == "test":
        source = args.from_policy
        if source is None:
            pointer = ROOT / "pipeline-runs/latest-policy.json"
            if not pointer.is_file():
                parser.error("create a policy first: ./policy create")
            source = Path(json.loads(pointer.read_text())["directory"])
        source = source.resolve()
        original = json.loads((source / "manifest.json").read_text())
        original_policy = source / "selected/updated_rule.dw"
        if digest(original_policy) != original["inputs"]["policy"]["sha256"]:
            parser.error("the created policy was edited; create a new policy before testing")
        for name, path in {"schema": STORE / "policies/schema.cedarschema",
                           "base": STORE / "policies/base.dw"}.items():
            if digest(path) != original["inputs"][name]["sha256"]:
                parser.error(f"{name} changed since creation; create a new policy before testing")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    out = (args.out or ROOT / "pipeline-runs" / stamp).resolve()
    out.mkdir(parents=True, exist_ok=False)
    env = os.environ.copy()
    # Preserve an explicitly configured Dogwood command; otherwise use PATH.
    manifest = {"started_utc": stamp, "model": args.model, "runs_per_task": args.runs,
                "answer": args.answer, "execution": "scripted" if args.model == "scripted" else "ollama",
                "commands": [], "stage": args.stage, "status": "running"}
    manifest_path = out / "manifest.json"

    def save():
        temp = manifest_path.with_suffix(".tmp")
        temp.write_text(json.dumps(manifest, indent=2) + "\n")
        temp.replace(manifest_path)

    def run(name, command, cwd=ROOT):
        print(f"\n{name}: {' '.join(map(str, command))}", flush=True)
        proc = subprocess.run(list(map(str, command)), cwd=cwd, env=env,
                              capture_output=True, text=True)
        (out / f"{name}.log").write_text(proc.stdout + proc.stderr)
        manifest["commands"].append({"stage": name, "argv": list(map(str, command)),
                                     "cwd": str(cwd), "exit_code": proc.returncode})
        save()
        print(proc.stdout[-2500:], flush=True)
        if proc.returncode:
            raise RuntimeError(f"{name} failed: {proc.stderr[-1500:]} (see {out / (name + '.log')})")

    save()
    try:
        manifest["code_sha256"] = {str(p.relative_to(ROOT)): digest(p) for p in [
            ROOT / "run_pipeline.py", ROOT / "converter/convert.py", ROOT / "question/find_question.py",
            ROOT / "question/answer.py", STORE / "agent.py", STORE / "run_task.py", STORE / "enforce.py",
            STORE / "score_actions.py", STORE / "tools.py", STORE / "recorder.py",
            STORE / "context_engine.py"]}
        if source is None:
            run("question", [sys.executable, ROOT / "question/find_question.py", "--out", out / "question"])
            question_data = json.loads((out / "question/question.json").read_text())
            for version in question_data["versions"].values():
                form_path = ROOT / "question/forms" / version["form"]
                form = json.loads(form_path.read_text())
                print(f"\nCandidate form: {form_path}", flush=True)
                print(json.dumps(form, indent=2), flush=True)
            if args.answer is None:
                if args.stage == "create":
                    question = question_data["question"]
                    print(question, flush=True)
                    while args.answer not in ("allow", "block"):
                        args.answer = input("Answer allow or block: ").strip().lower()
                else:
                    args.answer = "block"
            manifest["answer"] = args.answer
            selected = out / "selected"
            run("selection", [sys.executable, ROOT / "question/answer.py", "--answer", args.answer,
                              "--question", out / "question/question.json", "--out", selected])
            selected_form = json.loads((selected / "updated_form.json").read_text())
            print("\nSelected form:", flush=True)
            print(json.dumps(selected_form, indent=2), flush=True)
            print("\nGenerated Dogwood policy:", flush=True)
            print((selected / "updated_rule.dw").read_text(), flush=True)
        else:
            import shutil
            selected = out / "selected"
            selected.mkdir()
            for name in ("updated_form.json", "updated_rule.dw", "actions_with_answer.json"):
                shutil.copy2(source / "selected" / name, selected / name)
            manifest["creation_directory"] = str(source)
            manifest["answer"] = original["answer"]
            run("scripted", [sys.executable, STORE / "score_actions.py",
                             selected / "actions_with_answer.json", "--rules", selected / "updated_rule.dw",
                             "--output-dir", out / "scripted"], cwd=STORE)
            scorecard, = (out / "scripted").glob("rule_scorecard_*.json")
            shutil.copy2(scorecard, selected / "rule_scorecard.json")
        policy = selected / "updated_rule.dw"
        schema = STORE / "policies/schema.cedarschema"
        base = STORE / "policies/base.dw"
        manifest["inputs"] = {
            "policy": {"path": str(policy), "sha256": digest(policy)},
            "schema": {"path": str(schema), "sha256": digest(schema)},
            "base": {"path": str(base), "sha256": digest(base)},
            "tasks": {"path": str(tasks), "sha256": digest(tasks)},
            "form": json.loads((selected / "updated_form.json").read_text()),
        }
        # Snapshot every live input so subsequent edits cannot change what a run means.
        (out / "schema.cedarschema").write_bytes(schema.read_bytes())
        (out / "base.dw").write_bytes(base.read_bytes())
        (out / "tasks.json").write_bytes(tasks.read_bytes())
        if args.stage == "create":
            manifest["status"] = "created"
            save()
            pointer = ROOT / "pipeline-runs/latest-policy.json"
            pointer.parent.mkdir(parents=True, exist_ok=True)
            temp = pointer.with_suffix(".tmp")
            temp.write_text(json.dumps({"directory": str(out), "policy_sha256": digest(policy)}, indent=2) + "\n")
            temp.replace(pointer)
            print(f"\nPolicy created: {policy}\nNext: ./policy test", flush=True)
            return 0
        run("agent", [sys.executable, STORE / "run_task.py", "--rules", policy,
                      "--tasks", out / "tasks.json", "--all", "--runs", args.runs,
                      "--model", args.model, "--output-dir", out / "agent"], cwd=STORE)
        card_path, = (out / "agent").glob("agent_scorecard_*.json")
        card = json.loads(card_path.read_text())
        if card["setup"]["sha256"]["rules"] != manifest["inputs"]["policy"]["sha256"]:
            raise RuntimeError("agent did not load the selected generated policy")
        rule_card = json.loads((selected / "rule_scorecard.json").read_text())
        manifest["scripted_score"] = {"right": rule_card["right"], "total": rule_card["total"]}
        manifest["agent_score"] = {"passed": sum(r["passed"] for r in card["runs"]),
                                   "total": len(card["runs"]), "scorecard": str(card_path)}
        clean = (rule_card["right"] == rule_card["total"] and all(r["passed"] for r in card["runs"]))
        manifest["status"] = "passed" if clean else "completed_with_failures"
        save()
        lines = ["# Generated-policy agent integration", "",
                 f"Model: {args.model}; execution: {manifest['execution']}",
                 f"Answer: {manifest['answer']}; approval window: {manifest['inputs']['form']['requires']['within']}",
                 f"Scripted checks: {rule_card['right']}/{rule_card['total']}",
                 f"Agent tasks: {manifest['agent_score']['passed']}/{len(card['runs'])}",
                 f"Selected policy SHA256: {digest(policy)}", "",
                 "The agent scorecard confirms the same generated-policy hash.",
                 "Session traces and per-session combined policies are under agent/traces/.",
                 "Failed tasks are retained; a successful process launch is not a passing evaluation."]
        (out / "SUMMARY.md").write_text("\n".join(lines) + "\n")
        print(f"\nResults: {out / 'SUMMARY.md'}", flush=True)
        return 0 if clean else 1
    except Exception as error:
        manifest["status"], manifest["error"] = "error", str(error)
        save()
        raise


if __name__ == "__main__":
    sys.exit(main())
