"""Compare a policy with its reference rule: find a difference, prove it in Dogwood, fix it from the reference, recheck."""

import json

from app.core import config, flow, generate, known_bugs, saved_cases
from question.compare import describe, find_difference

REFERENCES_DIR = config.REPO / "data" / "references"


def _key(sentence):
    return " ".join(sentence.lower().split())


def find_reference(sentence):
    for path in sorted(REFERENCES_DIR.glob("*.json")):
        reference = json.loads(path.read_text(encoding="utf-8"))
        if _key(reference["sentence"]) == _key(sentence):
            return {**reference, "name": path.stem}
    return None


def _prepare(forms, sentence, out):
    """Write forms as a policy and validate it. Returns a gate ready to replay."""
    policy = generate.write_policy([{**f, "sentence": sentence} for f in forms], out)
    ok, output = generate.validate(policy, out)
    if not ok:
        raise RuntimeError(f"policy did not validate: {output}")
    return generate.gate_for(forms, out, policy), output


def compare_forms(forms, reference_forms, sentence, out):
    """The shortest difference, replayed under both policies. predicted must match what Dogwood says."""
    case = find_difference(forms, reference_forms)
    if case is None:
        return None
    ours, _ = _prepare(forms, sentence, out / "policy")
    theirs, _ = _prepare(reference_forms, sentence, out / "reference")
    verdicts = [generate.replay_case(ours, case), generate.replay_case(theirs, case)]
    actual = [v["verdict"] for v in verdicts]
    if actual != case["predicted"]:
        raise RuntimeError(f"the search predicted {case['predicted']} but Dogwood said {actual}; {describe(case)}")
    return {"case": case, "story": describe(case), "policy": verdicts[0], "reference": verdicts[1]}


def forms_under_test(policy_id, bug):
    """The policy's forms, with a known bug planted when one is asked for."""
    out, record = flow.load_policy(policy_id)
    forms = json.loads((out / generate.FORM_FILE).read_text(encoding="utf-8"))
    if bug is None:
        return forms, record
    broken = known_bugs.plant(forms, bug)
    if broken is None:
        raise ValueError(f"{bug} doesn't apply to this rule")
    return broken, record


def check(policy_id, bug=None):
    forms, record = forms_under_test(policy_id, bug)
    reference = find_reference(record["sentence"])
    base = {"sentence": record["sentence"], "bug": bug, "summary": generate.forms_summary(forms)}
    if reference is None:
        return {
            **base,
            "has_reference": False,
            "message": "There is no reference for this rule. Confirm the edge examples instead.",
        }
    if not reference["forms"]:
        return {
            **base,
            "has_reference": True,
            "reference": reference["name"],
            "same": False,
            "message": f"The reference says this rule should be {reference['status']}: {reference['source']}.",
        }
    work = generate.new_policy_dir()
    work.mkdir(parents=True)
    found = compare_forms(forms, reference["forms"], record["sentence"], work)
    (work / "note.txt").write_text(f"reference check of {policy_id}, planted bug: {bug}\n", encoding="utf-8")
    result = {
        **base,
        "has_reference": True,
        "reference": reference["name"],
        "reference_source": reference["source"],
        "reference_summary": generate.forms_summary(reference["forms"]),
    }
    if found is None:
        return {**result, "same": True, "message": "No history tells this policy apart from the reference."}
    return {**result, "same": False, "difference": found, "dogwood": _rule_text(forms)}


def _rule_text(forms):
    return generate.convert([(f["id"], f) for f in forms], config.SCHEMA_PATH, baseline=True)


def fix(policy_id, bug=None):
    """Replace the policy with the reference, revalidate and recheck, and show before and after."""
    forms, record = forms_under_test(policy_id, bug)
    sentence = record["sentence"]
    reference = find_reference(sentence)
    if reference is None or not reference["forms"]:
        raise ValueError("there is no reference form to fix this rule from")
    work = generate.new_policy_dir()
    work.mkdir(parents=True)
    found = compare_forms(forms, reference["forms"], sentence, work)
    if found is None:
        raise ValueError("this policy already agrees with its reference")
    out = generate.new_policy_dir()
    fixed = [{**f, "sentence": sentence} for f in reference["forms"]]
    policy = generate.write_policy(fixed, out)
    validation = generate.validate(policy, out)
    gate = generate.gate_for(fixed, out, policy)
    after = generate.replay_case(gate, found["case"])
    recheck = find_difference(fixed, reference["forms"])
    saved = saved_cases.rerun(sentence, gate)
    summary = generate.forms_summary(fixed)
    status = {"status": "ready", "reasons": ["fixed from the reference and rechecked"]}
    if not validation[0] or recheck is not None or not all(c["passed"] for c in saved):
        status = {"status": "needs review", "reasons": ["the fix did not pass its recheck"]}
    record_out = {
        "sentence": sentence,
        "summary": summary,
        "status": status,
        "examples": record["examples"],
        "saved_cases": saved,
    }
    (out / "record.json").write_text(json.dumps(record_out, indent=2) + "\n", encoding="utf-8")
    return {
        "policy_id": out.name,
        "story": found["story"],
        "reference": reference["name"],
        "validated": validation[0],
        "validate_output": validation[1],
        "recheck_same": recheck is None,
        "status": status,
        "saved_cases": saved,
        "before": {
            "verdict": found["policy"]["verdict"],
            "summary": generate.forms_summary(forms),
            "dogwood": _rule_text(forms),
        },
        "after": {
            "verdict": after["verdict"],
            "summary": summary,
            "dogwood": (out / generate.RULE_FILE).read_text(encoding="utf-8"),
        },
        "reference_verdict": found["reference"]["verdict"],
    }
