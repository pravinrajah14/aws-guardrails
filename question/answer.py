import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from find_question import ACTIONS, BASE, OUT, SCHEMA, STORE, convert
from enforce import DogwoodGate, build_policy
from score_actions import case_lines


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


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--answer", choices=["allow", "block"])
    p.add_argument("--question", default=str(OUT / "question.json"))
    p.add_argument("--actions", default=str(ACTIONS))
    p.add_argument("--out", help="New directory for this answer and its scorecard")
    args = p.parse_args()

    q = json.loads(Path(args.question).read_text(encoding="utf-8"))
    answer = args.answer
    if answer is None:
        print(q["question"])
        while answer not in ("allow", "block"):
            answer = input("allow or block? ").strip().lower()
    want = "ALLOW" if answer == "allow" else "DENY"

    forms_dir = Path(__file__).resolve().parent / "forms"
    versions = q["versions"]
    keep = [tag for tag, v in versions.items() if v["verdict"] == want]
    if len(keep) != 1:
        sys.exit("the answer does not pick out exactly one version")
    kept = versions[keep[0]]
    dropped = next(v for tag, v in versions.items() if tag != keep[0])
    form = json.loads((forms_dir / kept["form"]).read_text(encoding="utf-8"))
    other = json.loads((forms_dir / dropped["form"]).read_text(encoding="utf-8"))

    out = Path(args.out).resolve() if args.out else OUT / f"answer_{answer}"
    if args.out and out.exists():
        sys.exit(f"output already exists: {out}; choose a new directory")
    if out.exists():
        shutil.rmtree(out)
    work = out / "dogwood"
    work.mkdir(parents=True)

    (out / "updated_form.json").write_text(json.dumps(form, indent=2) + "\n", encoding="utf-8")
    rule_path = out / "updated_rule.dw"
    rule_path.write_text(convert([("updated", form)], str(SCHEMA)), encoding="utf-8")

    actions = json.loads(Path(args.actions).read_text(encoding="utf-8"))
    new_case = json.loads(json.dumps(q["case"]))
    new_case["why"] = f"Solver question, answered '{answer}': {q['question']}"
    new_case["events"][-1]["expect"] = want
    actions["cases"].append(new_case)
    actions["notes"] = (actions.get("notes", "") + f" Plus solver_question_1, answered '{answer}' "
                        f"(recently = {form['requires']['within']}).").strip()
    actions_path = out / "actions_with_answer.json"
    actions_path.write_text(json.dumps(actions, indent=2) + "\n", encoding="utf-8")

    cases = [c for c in actions["cases"] if any("expect" in e for e in c["events"])]
    mine = verdicts_for(form, cases, work, "kept")
    theirs = verdicts_for(other, cases, work, "dropped")
    depends = []
    for case in cases:
        expects = [e["expect"].upper() for e in case["events"] if "expect" in e]
        if mine[case["id"]] != theirs[case["id"]]:
            depends.append((case["id"], expects, mine[case["id"]]))

    command = [sys.executable, "score_actions.py", str(actions_path), "--rules", str(rule_path)]
    if args.out:
        command += ["--output-dir", str(out / "scripted")]
    run = subprocess.run(command,
                         cwd=STORE, capture_output=True, text=True)
    if run.returncode:
        sys.exit(run.stdout + run.stderr)
    saved = next(l for l in run.stdout.splitlines() if l.startswith("Saved "))
    stem = Path(saved.split()[1]).stem
    traces = saved.split("(traces in ", 1)[1].rstrip(")")
    card_dir = out / "scripted" if args.out else STORE / "results"
    for ext in ("md", "json"):
        shutil.move(str(card_dir / f"{stem}.{ext}"), out / f"rule_scorecard.{ext}")
    if not args.out:
        shutil.rmtree(STORE / traces, ignore_errors=True)

    card = json.loads((out / "rule_scorecard.json").read_text(encoding="utf-8"))
    wrong = [d for d in card["decisions"] if not d["correct"]]
    lines = [
        f"Question: {q['question']}",
        f"Answer: {answer} -> keep {kept['form']} (recently = {form['requires']['within']}), "
        f"drop {dropped['form']}",
        f"Rescored with Part 2 action lists plus the answered question: {card['right']} of {card['total']} correct",
    ]
    for d in wrong:
        lines.append(f"  wrong: {d['case']} {d['call']} expected {d['expect']}, got {d['got']}")
    if depends:
        lines.append("Cases whose right answer depends on how 'recently' is read:")
        for cid, expects, got in depends:
            lines.append(f"  {cid}: expected {'/'.join(expects)}, updated rule says {'/'.join(got)}")
    summary = "\n".join(lines) + "\n"
    (out / "summary.txt").write_text(summary, encoding="utf-8")
    print(summary)


if __name__ == "__main__":
    main()
