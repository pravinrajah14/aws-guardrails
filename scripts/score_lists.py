"""Run every scripted action list through Dogwood and fail if a decision or engine status is wrong."""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / "kylie-test-bed-upload"
sys.path.insert(0, str(ROOT))

from converter.convert import convert

SCHEMA = STORE / "policies" / "schema.cedarschema"
FORMS = ROOT / "question" / "forms"
HAND_WRITTEN_1H = STORE / "rules" / "refund_recent_approval_1h.dw"
# (action list, form behind the generated rule); conflict cases also run under the hand-written rule.
LISTS = [
    ("recent_approval_1h_actions.json", "recently_1h.json"),
    ("recent_approval_24h_actions.json", "recently_24h.json"),
    ("refund_recent_approval_actions.json", "recently_1h.json"),
    ("context_engine_actions.json", "recently_1h.json"),
    ("conflict_actions.json", "recently_1h.json"),
    ("order_total_actions.json", "recently_1h.json"),
]


def score(actions, rules, form_path, out):
    done = subprocess.run(
        [
            sys.executable,
            "score_actions.py",
            f"tests/{actions}",
            "--rules",
            str(rules),
            "--form",
            str(form_path),
            "--output-dir",
            str(out),
        ],
        cwd=STORE,
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode:
        sys.exit(f"{actions}: scorer failed\n{done.stdout}{done.stderr}")
    (card,) = out.glob("rule_scorecard_*.json")
    return json.loads(card.read_text(encoding="utf-8"))


def report(label, card):
    current = card["current"]
    good = current["right"] == current["total"] and card["status_wrong"] == 0
    print(f"{'ok  ' if good else 'FAIL'} {label}: {current['right']}/{current['total']}")
    return good


def main():
    good = True
    with tempfile.TemporaryDirectory() as temp:
        temp = Path(temp)
        for actions, form_name in LISTS:
            form_path = FORMS / form_name
            form = json.loads(form_path.read_text(encoding="utf-8"))
            rule = temp / f"{form_path.stem}.dw"
            rule.write_text(convert([(form_name, form)], SCHEMA), encoding="utf-8")
            good &= report(f"{actions} under {form_name}", score(actions, rule, form_path, temp / f"{actions}.out"))
        conflict = score("conflict_actions.json", HAND_WRITTEN_1H, FORMS / "recently_1h.json", temp / "hand.out")
        good &= report("conflict_actions.json under the hand-written 1h rule", conflict)
    sys.exit(0 if good else 1)


if __name__ == "__main__":
    main()
