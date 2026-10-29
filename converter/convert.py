#!/usr/bin/env python3
"""Compile structured rule forms into Dogwood policies."""
import argparse
import json
import re
import sys
from pathlib import Path

KINDS = {"limit", "allowlist", "requires_prior"}
OPS = {">", ">=", "<", "<=", "==", "!="}
WINDOW = re.compile(r"^[0-9]+(s|m|h)$")
ID = re.compile(r"^[a-z][a-z0-9_]*$")
TOP_KEYS = {"id", "sentence", "action", "kind", "only_when", "field", "max", "allowed", "requires"}
REQ_KEYS = {"action", "within", "same", "output", "output_same"}


class FormError(ValueError):
    pass


# ------------------------------------------------------------------ schema

def load_schema(path):
    """Light parse of the .cedarschema: namespace, and per action its input/output fields."""
    text = Path(path).read_text(encoding="utf-8")
    ns = re.search(r"namespace\s+(\w+)", text)
    if not ns:
        raise FormError(f"no namespace found in {path}")
    types = {}
    for name, body in re.findall(r"type\s+(\w+)\s*=\s*\{(.*?)\}\s*;", text, re.S):
        types[name] = dict(
            (m.group(1), m.group(2).strip())
            for m in re.finditer(r"(\w+)\??\s*:\s*([\w:]+)", body)
        )
    actions = {}
    for name, body in re.findall(r'action\s+"(\w+)"\s+appliesTo\s*\{(.*?)\n\s*\}\s*;', text, re.S):
        inp = re.search(r"input\s*:\s*(\w+)", body)
        out = re.search(r"output\??\s*:\s*(\w+)", body)
        actions[name] = {
            "input": types.get(inp.group(1), {}) if inp else {},
            "output": types.get(out.group(1), {}) if out else {},
        }
    return ns.group(1), actions


# ------------------------------------------------------------------ literals

def lit(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        return json.dumps(v)
    raise FormError(f"unsupported value {v!r}")


def check_value_type(v, cedar_type, where):
    ok = {"String": str, "Long": int, "Bool": bool}.get(cedar_type)
    if ok is None:
        return
    if not isinstance(v, ok) or (ok is int and isinstance(v, bool)):
        raise FormError(f"{where}: {v!r} is not a {cedar_type}")


# ------------------------------------------------------------------ validation

def need(cond, msg):
    if not cond:
        raise FormError(msg)


def validate_form(f, actions):
    need(isinstance(f, dict), "form must be a JSON object")
    extra = set(f) - TOP_KEYS
    need(not extra, f"unknown field(s): {sorted(extra)}")
    for k in ("id", "kind", "action"):
        need(k in f, f"missing '{k}'")
    need(ID.match(f["id"]), f"id {f['id']!r} must be lowercase letters, digits, underscores")
    need(f["kind"] in KINDS, f"kind must be one of {sorted(KINDS)}")
    act = f["action"]
    need(act in actions, f"unknown action {act!r}; schema has {sorted(actions)}")
    inp = actions[act]["input"]

    if "only_when" in f:
        c = f["only_when"]
        need(set(c) == {"field", "op", "value"}, "only_when needs exactly field, op, value")
        need(c["field"] in inp, f"only_when: {act} has no input field {c['field']!r}")
        need(c["op"] in OPS, f"only_when: op must be one of {sorted(OPS)}")
        check_value_type(c["value"], inp[c["field"]], "only_when")

    kind = f["kind"]
    if kind == "limit":
        need("field" in f and "max" in f, "limit needs 'field' and 'max'")
        need(f["field"] in inp, f"{act} has no input field {f['field']!r}")
        need(inp[f["field"]] == "Long", f"limit field {f['field']!r} must be a Long")
        need(isinstance(f["max"], int) and not isinstance(f["max"], bool), "'max' must be an integer")
    elif kind == "allowlist":
        need("field" in f and f.get("allowed"), "allowlist needs 'field' and a non-empty 'allowed'")
        need(f["field"] in inp, f"{act} has no input field {f['field']!r}")
        for v in f["allowed"]:
            check_value_type(v, inp[f["field"]], "allowed")
    else:
        need("requires" in f, "requires_prior needs 'requires'")
        r = f["requires"]
        extra = set(r) - REQ_KEYS
        need(not extra, f"requires: unknown field(s) {sorted(extra)}")
        need("action" in r, "requires needs 'action'")
        need(r["action"] in actions, f"requires: unknown action {r['action']!r}")
        need("within" in r and WINDOW.match(str(r["within"])),
             "requires needs 'within' like '1h' -- 'recently' must be a number")
        prior = actions[r["action"]]
        for fld in r.get("same", []):
            need(fld in inp, f"same: {act} has no input field {fld!r}")
            need(fld in prior["input"], f"same: {r['action']} has no input field {fld!r}")
            need(inp[fld] == prior["input"][fld], f"same: {fld!r} has different types in {act} and {r['action']}")
        for fld, val in r.get("output", {}).items():
            need(fld in prior["output"], f"output: {r['action']} has no output field {fld!r}")
            check_value_type(val, prior["output"][fld], f"output.{fld}")
        for previous, current in r.get("output_same", {}).items():
            need(previous in prior["output"], f"output_same: unknown prior output {previous!r}")
            need(current in inp, f"output_same: unknown current input {current!r}")
            need(prior["output"][previous] == inp[current], "output_same: field types differ")
            need(previous not in r.get("output", {}), "output_same: output field also has a literal constraint")
        need(r.get("same") or r.get("output") or r.get("output_same"), "requires needs a matching condition")


# ------------------------------------------------------------------ emit

def condition(c):
    return f"context.input.{c['field']} {c['op']} {lit(c['value'])}"


def snake(name):
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def convert_form(f, ns):
    act = f"{ns}::Action::{json.dumps(f['action'])}"
    scope = f"(principal, action == {act}, resource)"
    out = []
    if f.get("sentence"):
        out.append("// " + f["sentence"].replace("\n", " "))

    if f["kind"] in ("limit", "allowlist"):
        guards = [condition(f["only_when"])] if "only_when" in f else []
        if f["kind"] == "limit":
            guards.append(f"context.input.{f['field']} > {f['max']}")
        else:
            allowed = ", ".join(lit(v) for v in f["allowed"])
            guards.append(f"!([{allowed}].contains(context.input.{f['field']}))")
        out.append(f'@id("{f["id"]}")\nforbid {scope}\nwhen {{ ' + " && ".join(guards) + " };")
        return "\n".join(out)

    # requires_prior -> permit. If it only applies sometimes (only_when), the other
    # cases need their own permit, or deny-by-default would block them too.
    r = f["requires"]
    fields = [f"input.{x}: context.input.{x}" for x in r.get("same", [])]
    fields += [f"output.{k}: {lit(v)}" for k, v in r.get("output", {}).items()]
    fields += [f"output.{previous}: context.input.{current}"
               for previous, current in r.get("output_same", {}).items()]
    body = ",\n".join("        " + x for x in fields)
    when = ""
    if "only_when" in f:
        c = condition(f["only_when"])
        out.append(f'@id("{f["id"]}_not_applicable")\npermit {scope}\nwhen {{ !({c}) }};\n')
        when = f"\nwhen {{ {c} }}"
    out.append(
        f'@id("{f["id"]}")\npermit {scope}{when}\n'
        f"when temporal {{\n"
        f"    formerly within {r['within']} {ns}::Action::{json.dumps(r['action'])}::response{{\n"
        f"{body}\n"
        f"    }}\n"
        f"}};"
    )
    return "\n".join(out)


def convert(forms, schema_path, baseline=False):
    ns, actions = load_schema(schema_path)
    blocks, ids, gated = [], set(), {}
    for src, f in forms:
        try:
            validate_form(f, actions)
            need(f["id"] not in ids, f"duplicate id {f['id']!r}")
            if f["kind"] == "requires_prior":
                need(f["action"] not in gated,
                     f"{f['action']} already has a requires_prior rule ({gated.get(f['action'])}); "
                     "two permits would OR together -- merge them into one form")
                gated[f["action"]] = f["id"]
        except FormError as e:
            raise FormError(f"{src}: {e}") from None
        ids.add(f["id"])
        blocks.append(convert_form(f, ns))
    head = ["// GENERATED by converter/convert.py from rule forms. Do not edit by hand."]
    if baseline:
        head.append("// Baseline: actions no requires_prior rule gates are always allowed.")
        for a in actions:
            if a not in gated:
                head.append(f'@id("allow_{snake(a)}")\npermit (principal, action == {ns}::Action::"{a}", resource);')
    return "\n".join(head) + "\n\n" + "\n\n".join(blocks) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("forms", nargs="+")
    ap.add_argument("--schema", default=str(Path(__file__).parent.parent / "kylie-test-bed-upload" / "policies" / "schema.cedarschema"))
    ap.add_argument("--baseline", action="store_true")
    ap.add_argument("-o", "--out")
    a = ap.parse_args()
    try:
        forms = [(p, json.loads(Path(p).read_text(encoding="utf-8"))) for p in a.forms]
        text = convert(forms, a.schema, a.baseline)
    except (FormError, json.JSONDecodeError, OSError) as e:
        sys.exit(f"error: {e}")
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
