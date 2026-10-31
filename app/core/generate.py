"""A complete rule to a Dogwood policy: the summary, the forms, the policy files and Dogwood's own output."""

import json
import subprocess
from datetime import UTC, datetime

from app.core import config, rule_types
from app.core.numbers import BLOCK_OPERATOR, how_long
from app.core.store import BASE, DogwoodGate, MockWorld, build_policy, case_lines, dogwood_cmd, window_seconds
from converter.convert import convert

POLICIES_ROOT = config.REPO / "pipeline-runs" / "policies"
EXPLAIN_TIMEOUT_SECONDS = 60
FORM_FILE = "forms.json"
ENGINE_FORM_FILE = "updated_form.json"
RULE_FILE = "updated_rule.dw"


def values_of(u):
    """Every slot value that has been settled, plus the operator its comparison wording calls for."""
    values = {n: s["value"] for n, s in u["slots"].items() if s["source"] != "missing" and s["value"] not in (None, [])}
    direction = u["slots"].get("max_amount", {}).get("direction")
    if direction:
        values["block_op"] = BLOCK_OPERATOR[direction]
    return values


def _scope(u):
    """The one input-field condition the sentence adds, from a qualifier with a glossary value."""
    for q in u["qualifiers"]:
        if q["mapped"] and "value" in q:
            return {"field": q["field"], "op": "==", "value": q["value"]}
    return None


def build_forms(u):
    values = values_of(u)
    scope = _scope(u)
    forms = []
    for template in rule_types.BY_ID[u["rule_type"]]["forms"]:
        form = rule_types.fill(template, values)
        form["id"] = f"{u['rule_type']}_{form['kind']}"
        form["sentence"] = u["sentence"]
        if scope and "only_when" not in form:
            form["only_when"] = scope
        forms.append(form)
    return forms


def readiness_problems(u):
    """Why a rule that looks complete still can't be generated, in words for a person."""
    problems = []
    order_id = u["slots"].get("order_id", {}).get("value")
    orders = MockWorld().orders
    if order_id and order_id not in orders:
        problems.append(f"order {order_id} is not in the store, which has {', '.join(sorted(orders))}")
    for q in u["qualifiers"]:
        if q["mapped"] and "value" not in q:
            problems.append(f"{q['text']!r} sets {q['field']}, but no cited source gives its value")
    return problems


def _limit_words(form):
    amount = f"${form['max']:,}"
    if form.get("op", ">") == ">=":
        return f"A refund of {amount} or more is blocked."
    return f"A refund over {amount} is blocked."


def _list_words(items):
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + f" and {items[-1]}"


def _allowlist_words(form):
    where = f"to {form['only_when']['value']} " if "only_when" in form else ""
    return f"Only {_list_words(form['allowed'])} can be deployed {where}. Any other service is blocked.".replace(
        " .", "."
    )


def _approval_words(requires):
    within = how_long(window_seconds(requires["within"]))
    if requires.get("event") == "request":
        return f"A refund only needs an approval to have been requested within the last {within}, not granted."
    person = f"a {requires['approver_role']}" if "approver_role" in requires else "anyone"
    order = "the same order" if "order_id" in requires.get("same", []) else "any order"
    text = f"A refund needs an approval by {person} for {order}, given within the last {within}."
    if "at_most" in requires:
        return (
            text
            + " The refund, plus anything already refunded against that approval, can be at most the approved amount."
        )
    return text + " The approved amount isn't checked."


def _tests_words(form, requires):
    where = f"to {form['only_when']['value']}" if "only_when" in form else "anywhere"
    within = how_long(window_seconds(requires["within"]))
    what = "for the same service and version" if "version" in requires.get("same", []) else "for any version"
    if requires.get("event") == "request":
        return f"A version can be deployed {where} once tests were started {what} within the last {within}."
    return f"A version can be deployed {where} only if its tests passed {what} within the last {within}."


def _form_words(form):
    if form["kind"] == "limit":
        return _limit_words(form)
    if form["kind"] == "allowlist":
        return _allowlist_words(form)
    requires = form["requires"]
    return _approval_words(requires) if requires["action"] == "RequestApproval" else _tests_words(form, requires)


def forms_summary(forms):
    """A policy in plain words, built from its forms by templates. The model has no part in it."""
    words = " ".join(_form_words(f) for f in forms)
    scopes = {f["only_when"]["value"] for f in forms if f.get("only_when", {}).get("field") == "order_id"}
    return f"For order {scopes.pop()}: {words}" if len(scopes) == 1 else words


def engine_form(forms):
    """The form that gives the context engine its window; rules with no approval have none."""
    return next((f for f in forms if f["kind"] == "requires_prior"), None)


def _run_dogwood(args):
    done = subprocess.run(
        dogwood_cmd() + args, capture_output=True, text=True, timeout=EXPLAIN_TIMEOUT_SECONDS, check=False
    )
    return done.returncode == 0, (done.stdout + done.stderr).strip()


def explain(rule_path):
    """Dogwood's own literal reading of the generated rule."""
    return _run_dogwood(["explain", str(rule_path), "--policy-schema", str(config.SCHEMA_PATH)])[1]


def write_policy(forms, out):
    """Write the forms and their Dogwood policy into a new folder. The combined policy is what Dogwood enforces."""
    out.mkdir(parents=True, exist_ok=False)
    (out / FORM_FILE).write_text(json.dumps(forms, indent=2) + "\n", encoding="utf-8")
    form = engine_form(forms)
    if form:
        (out / ENGINE_FORM_FILE).write_text(json.dumps(form, indent=2) + "\n", encoding="utf-8")
    rule_path = out / RULE_FILE
    rule_path.write_text(convert([(f["id"], f) for f in forms], config.SCHEMA_PATH, baseline=True), encoding="utf-8")
    return build_policy(BASE, rule_path, out / "policy.dw")


def validate(policy_path, out):
    """The real dogwood validate, with its output kept next to the policy."""
    ok, output = _run_dogwood(["validate", str(policy_path), "--policy-schema", str(config.SCHEMA_PATH)])
    (out / "validate.txt").write_text(output + "\n", encoding="utf-8")
    return ok, output


def gate_for(forms, out, policy_path):
    return DogwoodGate(out, policy_path, config.SCHEMA_PATH, form=engine_form(forms))


def replay_case(gate, case):
    """A case through Dogwood under one policy: its verdict on the last event, and the rules that fired."""
    lines, scored = case_lines(gate, case)
    ran, verdicts, problem = gate.replay(lines)
    by_ts = {v["ts"]: v for v in verdicts}
    verdict = by_ts.get(scored[-1]["ts"]) if ran else None
    if verdict is None:
        raise RuntimeError(f"dogwood gave no verdict for the case: {problem}")
    return {"verdict": verdict["decision"], "rules": [gate.rule_name(i) for i in verdict["rules"]], "trace": lines}


def new_policy_dir():
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
    return POLICIES_ROOT / stamp
