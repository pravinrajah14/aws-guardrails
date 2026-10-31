import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

# find_question puts the store on sys.path, so it must be imported first.
from find_question import ACTIONS, BASE, OUT_ROOT, SCHEMA, STORE, convert  # isort: skip
from enforce import DogwoodGate, build_policy  # isort: skip
from score_actions import case_lines  # isort: skip


def verdicts_for(form, cases, workdir, tag):
    rule = workdir / f"rule_{tag}.dw"
    rule.write_text(convert([(tag, form)], str(SCHEMA)), encoding="utf-8")
    gate = DogwoodGate(workdir, build_policy(BASE, rule, workdir / f"policy_{tag}.dw"), SCHEMA, form=form)
    found = {}
    for case in cases:
        lines, scored = case_lines(gate, case)
        ran, verdicts, problem = gate.replay(lines)
        if not ran:
            sys.exit(f"{case['id']}: dogwood replay failed\n{problem}")
        by_ts = {v["ts"]: v["decision"] for v in verdicts}
        found[case["id"]] = [by_ts.get(s["ts"], "ERROR") for s in scored]
    return found


def pick_versions(q, answer):
    """The question's version this answer keeps and the one it drops, each with its form."""
    forms_dir = Path(__file__).resolve().parent / "forms"
    want = "ALLOW" if answer == "allow" else "DENY"
    versions = q["versions"]
    keep = [tag for tag, v in versions.items() if v["verdict"] == want]
    if len(keep) != 1:
        sys.exit("the answer does not pick out exactly one version")
    kept = versions[keep[0]]
    dropped = next(v for tag, v in versions.items() if tag != keep[0])
    form = json.loads((forms_dir / kept["form"]).read_text(encoding="utf-8"))
    other = json.loads((forms_dir / dropped["form"]).read_text(encoding="utf-8"))
    return kept, form, dropped, other, want


def latest_question():
    """Timestamped folders sort by time, so the last one is the newest."""
    found = sorted(OUT_ROOT.glob("*/question.json"))
    if not found:
        sys.exit("no question yet: run find_question.py first, or pass --question")
    return found[-1]


def fresh_output_dir(args, question_path, answer):
    out = Path(args.out).resolve() if args.out else Path(question_path).parent / f"answer_{answer}"
    if args.out and out.exists():
        sys.exit(f"output already exists: {out}; choose a new directory")
    if out.exists():
        shutil.rmtree(out)
    (out / "dogwood").mkdir(parents=True)
    return out


def add_answered_case(actions, q, answer, want, form):
    new_case = json.loads(json.dumps(q["case"]))
    new_case["why"] = f"Solver question, answered '{answer}': {q['question']}"
    new_case["events"][-1]["expect"] = want
    actions["cases"].append(new_case)
    actions["notes"] = (
        actions.get("notes", "") + f" Plus solver_question_1, answered '{answer}' "
        f"(recently = {form['requires']['within']})."
    ).strip()


def window_dependent_cases(form, other, cases, work):
    """Cases where the two versions disagree: (case id, expected verdicts, kept version's verdicts)."""
    mine = verdicts_for(form, cases, work, "kept")
    theirs = verdicts_for(other, cases, work, "dropped")
    depends = []
    for case in cases:
        expects = [e["expect"].upper() for e in case["events"] if "expect" in e]
        if mine[case["id"]] != theirs[case["id"]]:
            depends.append((case["id"], expects, mine[case["id"]]))
    return depends


def rescore(actions_path, rule_path, out, separate_output):
    """Run the scorer, move its scorecard to out/rule_scorecard.*, and return the scorecard."""
    command = [sys.executable, "score_actions.py", str(actions_path), "--rules", str(rule_path)]
    if separate_output:
        command += ["--output-dir", str(out / "scripted")]
    run = subprocess.run(command, cwd=STORE, capture_output=True, text=True, check=False)
    if run.returncode:
        sys.exit(run.stdout + run.stderr)
    saved = next(line for line in run.stdout.splitlines() if line.startswith("Saved "))
    stem = Path(saved.split()[1]).stem
    traces = saved.split("(traces in ", 1)[1].rstrip(")")
    card_dir = out / "scripted" if separate_output else STORE / "results"
    for ext in ("md", "json"):
        shutil.move(str(card_dir / f"{stem}.{ext}"), out / f"rule_scorecard.{ext}")
    if not separate_output:
        shutil.rmtree(STORE / traces, ignore_errors=True)
    return json.loads((out / "rule_scorecard.json").read_text(encoding="utf-8"))


def summary_text(q, answer, kept, dropped, form, card, depends):
    lines = [
        f"Question: {q['question']}",
        f"Answer: {answer} -> keep {kept['form']} (recently = {form['requires']['within']}), drop {dropped['form']}",
        f"Rescored with Part 2 action lists plus the answered question: {card['right']} of {card['total']} correct",
    ]
    for d in card["decisions"]:
        if not d["correct"]:
            lines.append(f"  wrong: {d['case']} {d['call']} expected {d['expect']}, got {d['got']}")
    if depends:
        lines.append("Cases whose right answer depends on how 'recently' is read:")
        for cid, expects, got in depends:
            lines.append(f"  {cid}: expected {'/'.join(expects)}, updated rule says {'/'.join(got)}")
    return "\n".join(lines) + "\n"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--answer", choices=["allow", "block"])
    p.add_argument("--question", help="question.json (default: the newest under pipeline-runs/question)")
    p.add_argument("--actions", default=str(ACTIONS))
    p.add_argument("--out", help="New directory for this answer and its scorecard")
    args = p.parse_args()

    question_path = args.question or latest_question()
    q = json.loads(Path(question_path).read_text(encoding="utf-8"))
    answer = args.answer
    if answer is None:
        print(q["question"])
        while answer not in ("allow", "block"):
            answer = input("allow or block? ").strip().lower()
    kept, form, dropped, other, want = pick_versions(q, answer)
    out = fresh_output_dir(args, question_path, answer)

    (out / "updated_form.json").write_text(json.dumps(form, indent=2) + "\n", encoding="utf-8")
    rule_path = out / "updated_rule.dw"
    rule_path.write_text(convert([("updated", form)], str(SCHEMA)), encoding="utf-8")

    actions = json.loads(Path(args.actions).read_text(encoding="utf-8"))
    add_answered_case(actions, q, answer, want, form)
    actions_path = out / "actions_with_answer.json"
    actions_path.write_text(json.dumps(actions, indent=2) + "\n", encoding="utf-8")

    cases = [c for c in actions["cases"] if any("expect" in e for e in c["events"])]
    depends = window_dependent_cases(form, other, cases, out / "dogwood")
    card = rescore(actions_path, rule_path, out, bool(args.out))
    summary = summary_text(q, answer, kept, dropped, form, card, depends)
    (out / "summary.txt").write_text(summary, encoding="utf-8")
    print(summary)


if __name__ == "__main__":
    main()
