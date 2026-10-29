import argparse
import json
import re
import sys
from pathlib import Path

from z3 import And, Bool, BoolVal, Int, IntVal, Length, Not, Optimize, Or, String, StringVal, sat

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / "kylie-test-bed-upload"
CONVERTER = ROOT / "converter"
sys.path.insert(0, str(STORE))
sys.path.insert(0, str(CONVERTER))

from convert import convert, load_schema, validate_form
from enforce import DogwoodGate, build_policy
from score_actions import case_lines
from tools import STAFF, TOOL_SPECS

SCHEMA = STORE / "policies" / "schema.cedarschema"
BASE = STORE / "policies" / "base.dw"
ACTIONS = STORE / "tests" / "refund_recent_approval_actions.json"
OUT = Path(__file__).resolve().parent / "out"
UNITS = {"s": 1, "m": 60, "h": 3600}
GRAINS = (3600, 60, 1)
MAX_EVENTS = 3

TOOL_FOR = {spec["action"]: name for name, spec in TOOL_SPECS.items()}


def window_seconds(w):
    return int(w[:-1]) * UNITS[w[-1]]


def schema_types(path):
    text = Path(path).read_text(encoding="utf-8")
    types = {}
    for name, body in re.findall(r"type\s+(\w+)\s*=\s*\{(.*?)\}\s*;", text, re.S):
        types[name] = {m.group(1): m.group(2) for m in re.finditer(r"(\w+)\??\s*:\s*(\w+)", body)}
    fields = {}
    for name, body in re.findall(r'action\s+"(\w+)"\s+appliesTo\s*\{(.*?)\n\s*\}\s*;', text, re.S):
        inp = re.search(r"input\s*:\s*(\w+)", body)
        out = re.search(r"output\??\s*:\s*(\w+)", body)
        fields[name] = {
            "input": types.get(inp.group(1), {}) if inp else {},
            "output": types.get(out.group(1), {}) if out else {},
        }
    return fields


def make_var(name, cedar_type):
    if cedar_type == "Long":
        return Int(name)
    if cedar_type == "Bool":
        return Bool(name)
    return String(name)


def literal(v):
    if isinstance(v, bool):
        return BoolVal(v)
    if isinstance(v, int):
        return IntVal(v)
    return StringVal(v)


def value_pool(path):
    pool = {}
    for case in json.loads(Path(path).read_text(encoding="utf-8"))["cases"]:
        for ev in case["events"]:
            action = TOOL_SPECS[ev["tool"]]["action"]
            for part in ("input", "output"):
                rec = ev.get(part)
                if rec and rec not in pool.setdefault((action, part), []):
                    pool[(action, part)].append(rec)
    return pool


def same_record(vars_, rec):
    return And(*[vars_[k] == literal(v) for k, v in rec.items() if k in vars_])


class History:
    def __init__(self, fields, prior_action, request_action, k):
        self.fields = fields
        self.prior_action = prior_action
        self.request_action = request_action
        self.events = []
        for i in range(k):
            self.events.append({
                "t": Int(f"t{i}"),
                "input": {f: make_var(f"e{i}_in_{f}", t) for f, t in fields[prior_action]["input"].items()},
                "output": {f: make_var(f"e{i}_out_{f}", t) for f, t in fields[prior_action]["output"].items()},
            })
        self.now = Int("now")
        self.request = {f: make_var(f"req_{f}", t) for f, t in fields[request_action]["input"].items()}

    def gap(self, ev):
        return self.now - ev["t"]

    def shape(self):
        cs = [self.events[0]["t"] == 0]
        for a, b in zip(self.events, self.events[1:]):
            cs.append(a["t"] < b["t"])
        cs.append(self.events[-1]["t"] < self.now)
        for scope in [self.request] + [ev[p] for ev in self.events for p in ("input", "output")]:
            for v in scope.values():
                if v.sort().name() == "Int":
                    cs.append(And(v >= 0, v <= 100000))
                elif v.sort().name() == "String":
                    cs.append(Length(v) >= 1)
        return cs


def permits(form, h):
    r = form["requires"]
    w = window_seconds(r["within"])
    matches = []
    for ev in h.events:
        cs = [h.gap(ev) >= 0, h.gap(ev) <= w]
        cs += [ev["input"][f] == h.request[f] for f in r.get("same", [])]
        cs += [ev["output"][k] == literal(v) for k, v in r.get("output", {}).items()]
        cs += [ev["output"][previous] == h.request[current]
               for previous, current in r.get("output_same", {}).items()]
        # Facts checks: the context engine reads the fact from the approval's output field of the
        # same name, and the approver's role from the store's staff directory.
        for current, fact in r.get("at_most", {}).items():
            if fact not in ev["output"]:
                sys.exit(f"at_most: the solver cannot model fact {fact!r}")
            # spent_so_far (refunds already made against the approval) is 0 here: the solver's
            # histories hold only earlier approvals, never a refund.
            cs.append(h.request[current] <= ev["output"][fact])
        if "approver_role" in r:
            names = [n for n, s in STAFF.items() if s["role"] == r["approver_role"]]
            cs.append(Or(*[ev["output"]["approver"] == StringVal(n) for n in names]) if names else BoolVal(False))
        matches.append(And(*cs))
    gated = Or(*matches)
    if "only_when" in form:
        c = form["only_when"]
        lhs, rhs = h.request[c["field"]], literal(c["value"])
        cond = {">": lhs > rhs, ">=": lhs >= rhs, "<": lhs < rhs, "<=": lhs <= rhs,
                "==": lhs == rhs, "!=": lhs != rhs}[c["op"]]
        return Or(Not(cond), gated)
    return gated


def prefer_realistic(opt, h, pool):
    for ev in h.events:
        for part in ("input", "output"):
            seen = pool.get((h.prior_action, part), [])
            if seen:
                opt.add_soft(Or(*[same_record(ev[part], rec) for rec in seen]), weight=4)
        for v in ev["output"].values():
            for w in ev["input"].values():
                if v.sort() == w.sort():
                    opt.add_soft(v == w, weight=1)
    seen = pool.get((h.request_action, "input"), [])
    if seen:
        opt.add_soft(Or(*[same_record(h.request, rec) for rec in seen]), weight=4)


def solve(form_a, form_b, fields, pool):
    for k in range(1, MAX_EVENTS + 1):
        for grain in GRAINS:
            h = History(fields, form_a["requires"]["action"], form_a["action"], k)
            a, b = permits(form_a, h), permits(form_b, h)
            opt = Optimize()
            opt.add(*h.shape())
            opt.add(a != b)
            for ev in h.events:
                opt.add(h.gap(ev) % grain == 0)
            prefer_realistic(opt, h, pool)
            opt.minimize(h.now)
            if opt.check() == sat:
                return h, opt.model(), a, b, grain
    return None


def disagreement_range(form_a, form_b, fields):
    h = History(fields, form_a["requires"]["action"], form_a["action"], 1)
    bounds = []
    for direction in ("min", "max"):
        opt = Optimize()
        opt.add(*h.shape())
        opt.add(permits(form_a, h) != permits(form_b, h))
        g = h.gap(h.events[0])
        (opt.minimize if direction == "min" else opt.maximize)(g)
        if opt.check() != sat:
            return None
        bounds.append(opt.model().eval(g).as_long())
    return bounds


def py(model, v):
    x = model.eval(v, model_completion=True)
    s = x.sort().name()
    if s == "Int":
        return x.as_long()
    if s == "Bool":
        return bool(x)
    return x.as_string()


def at_label(seconds):
    for unit, size in (("h", 3600), ("m", 60)):
        if seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def how_long(seconds):
    parts = []
    for name, size in (("hour", 3600), ("minute", 60), ("second", 1)):
        n, seconds = divmod(seconds, size)
        if n:
            parts.append(f"{n} {name}{'s' if n != 1 else ''}")
    if not parts:
        return "just now"
    return (", ".join(parts[:-1]) + " and " + parts[-1]) if len(parts) > 1 else parts[0]


def describe_prior(action, inp, out):
    if action == "RequestApproval":
        if not out.get("approved"):
            return f"A manager turned down a ${inp['amount']} refund for order {inp['order_id']}"
        if out.get("approved_amount", inp["amount"]) != inp["amount"]:
            return (f"A manager approved ${out['approved_amount']} of a ${inp['amount']} refund request "
                    f"for order {inp['order_id']}")
        return f"A manager approved a ${inp['amount']} refund for order {inp['order_id']}"
    if action == "RunTests":
        return f"Tests for {inp['service']} {inp['version']} {'passed' if out.get('passed') else 'failed'}"
    shown = ", ".join(f"{k}={v}" for k, v in {**inp, **out}.items())
    return f"{action} returned ({shown})"


def describe_request(action, inp):
    if action == "IssueRefund":
        return f"The agent now wants to refund ${inp['amount']} on order {inp['order_id']}."
    if action == "DeployService":
        return f"The agent now wants to deploy {inp['service']} {inp['version']} to {inp['env']}."
    shown = ", ".join(f"{k}={v}" for k, v in inp.items())
    return f"The agent now wants to call {action} ({shown})."


def build_case(h, model):
    now = py(model, h.now)
    events, prior = [], []
    for ev in h.events:
        t = py(model, ev["t"])
        inp = {f: py(model, v) for f, v in ev["input"].items()}
        out = {f: py(model, v) for f, v in ev["output"].items()}
        prior.append((now - t, inp, out))
        events.append({"at": at_label(t), "tool": TOOL_FOR[h.prior_action], "kind": "response",
                       "input": inp, "output": out})
    req = {f: py(model, v) for f, v in h.request.items()}
    events.append({"at": at_label(now), "tool": TOOL_FOR[h.request_action], "input": req})
    return {"id": "solver_question_1", "events": events}, prior, req


def question_text(h, prior, req):
    lines = [f"{describe_prior(h.prior_action, inp, out)} {how_long(ago)} ago."
             for ago, inp, out in prior]
    return " ".join(lines + [describe_request(h.request_action, req), "Allow or block?"])


def dogwood_verdict(form, case, workdir, tag):
    rule = workdir / f"rule_{tag}.dw"
    rule.write_text(convert([(tag, form)], str(SCHEMA)), encoding="utf-8")
    policy = build_policy(BASE, rule, workdir / f"policy_{tag}.dw")
    gate = DogwoodGate(workdir, policy, SCHEMA, form=form)
    ok, msg = gate.validate()
    if not ok:
        sys.exit(f"{tag}: policy did not validate\n{msg}")
    scored_case = json.loads(json.dumps(case))
    scored_case["events"][-1]["expect"] = "?"
    lines, scored = case_lines(gate, scored_case)
    ran, verdicts, problem = gate.replay(lines)
    by_ts = {v["ts"]: v["decision"] for v in verdicts}
    if not ran or scored[-1]["ts"] not in by_ts:
        sys.exit(f"{tag}: dogwood replay failed\n{problem}")
    (workdir / f"trace_{tag}.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return by_ts[scored[-1]["ts"]]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("form_a", nargs="?", default=str(Path(__file__).parent / "forms" / "recently_1h.json"))
    p.add_argument("form_b", nargs="?", default=str(Path(__file__).parent / "forms" / "recently_24h.json"))
    p.add_argument("--actions", default=str(ACTIONS))
    p.add_argument("--out", default=str(OUT))
    args = p.parse_args()

    forms = {}
    _, conv_actions = load_schema(SCHEMA)
    for tag, path in (("a", args.form_a), ("b", args.form_b)):
        form = json.loads(Path(path).read_text(encoding="utf-8"))
        validate_form(form, conv_actions)
        if form["kind"] != "requires_prior":
            sys.exit(f"{path}: only requires_prior forms are supported")
        forms[tag] = (Path(path), form)
    (pa, fa), (pb, fb) = forms["a"], forms["b"]
    if (fa["action"], fa["requires"]["action"]) != (fb["action"], fb["requires"]["action"]):
        sys.exit("the two forms must guard the same action with the same earlier action")

    fields = schema_types(SCHEMA)
    found = solve(fa, fb, fields, value_pool(args.actions))
    if found is None:
        sys.exit(f"no disagreement found with up to {MAX_EVENTS} earlier events: the two forms agree")
    h, model, ea, eb, grain = found
    predicted = {"a": "ALLOW" if bool(model.eval(ea)) else "DENY",
                 "b": "ALLOW" if bool(model.eval(eb)) else "DENY"}

    case, prior, req = build_case(h, model)
    out = Path(args.out)
    work = out / "dogwood"
    work.mkdir(parents=True, exist_ok=True)
    actual = {tag: dogwood_verdict(forms[tag][1], case, work, tag) for tag in ("a", "b")}
    if actual != predicted:
        sys.exit(f"solver predicted {predicted} but dogwood returned {actual}; encoding does not match the rule")

    text = question_text(h, prior, req)
    span = disagreement_range(fa, fb, fields) if len(h.events) == 1 else None
    result = {
        "question": text,
        "rule": fa.get("sentence", ""),
        "versions": {
            "a": {"form": pa.name, "within": fa["requires"]["within"], "verdict": actual["a"]},
            "b": {"form": pb.name, "within": fb["requires"]["within"], "verdict": actual["b"]},
        },
        "solver": {"name": "z3", "earlier_events": len(h.events), "time_grain_seconds": grain,
                   "disagree_when_seconds_ago": span},
        "case": case,
    }
    (out / "question.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    (out / "question.txt").write_text(text + "\n", encoding="utf-8")

    print(text)
    print()
    for tag in ("a", "b"):
        v = result["versions"][tag]
        print(f"  {v['form']} (within {v['within']}): {v['verdict']}  [confirmed by dogwood replay]")
    if span:
        print(f"  The two versions disagree whenever the earlier event is more than "
              f"{how_long(span[0] - 1)} and at most {how_long(span[1])} old.")
    print(f"\nSaved {out / 'question.json'}")


if __name__ == "__main__":
    main()
