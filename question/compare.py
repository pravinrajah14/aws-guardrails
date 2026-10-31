"""Find the shortest history where two policies decide differently, encoding each from its forms, then prove it with Dogwood."""

from z3 import And, Bool, BoolVal, Implies, Int, IntVal, Not, Optimize, Or, is_true, sat

from app.core.numbers import HOUR, MINUTE, how_long
from app.core.store import STAFF, TOOL_SPECS, MockWorld, window_seconds

MAX_EVENTS = 2
GRAINS = (HOUR, MINUTE, 1)
MAX_AMOUNT = 100000
MAX_SECONDS = 7 * 24 * HOUR
OUTSIDE_STORE = "Z900"
OTHER_SERVICE = "other-service"
VERSIONS = ["1.5.0", "1.4.0"]
ENVS = ["prod", "staging"]
TOOL_FOR = {spec["action"]: name for name, spec in TOOL_SPECS.items()}
REFUSED_AMOUNT = 0
SHOWN_ORDER = "A100"
SHOWN_APPROVED, SHOWN_REFUND = 25, 40


class Choice:
    """A string field that can take one of a few values. Z3 picks an index, which is far faster than string theory."""

    def __init__(self, name, values):
        self.values = list(dict.fromkeys(values))
        self.var = Int(name)

    def domain(self):
        return And(self.var >= 0, self.var < len(self.values))

    def equals(self, value):
        return self.var == self.values.index(value) if value in self.values else BoolVal(False)

    def same_as(self, other):
        pairs = [And(self.equals(v), other.equals(v)) for v in self.values if v in other.values]
        return Or(*pairs) if pairs else BoolVal(False)

    def read(self, model):
        return self.values[model.eval(self.var, model_completion=True).as_long()]


def _literal_values(forms, field):
    found = [f["only_when"]["value"] for f in forms if f.get("only_when", {}).get("field") == field]
    if field == "service":
        for f in forms:
            found += f.get("allowed", [])
    return found


class RefundHistory:
    action, prior_action = "IssueRefund", "RequestApproval"

    def __init__(self, forms, count):
        orders = list(MockWorld().orders) + [OUTSIDE_STORE] + _literal_values(forms, "order_id")
        self.request = {"order_id": Choice("req_order", orders), "amount": Int("req_amount")}
        self.events = [
            {
                "t": Int(f"t{i}"),
                "order_id": Choice(f"e{i}_order", orders),
                "asked": Int(f"e{i}_asked"),
                "approved": Bool(f"e{i}_ok"),
                "amount": Int(f"e{i}_amount"),
                "approver": Choice(f"e{i}_approver", list(STAFF)),
            }
            for i in range(count)
        ]
        self.now = Int("now")

    def constraints(self):
        cs = [self.request["order_id"].domain(), self.request["amount"] >= 1, self.request["amount"] <= MAX_AMOUNT]
        for order, details in MockWorld().orders.items():
            # The store-wide rule: a refund can't pass the order's total.
            cs.append(Implies(self.request["order_id"].equals(order), self.request["amount"] <= details["total"]))
        for e in self.events:
            cs += [
                e["order_id"].domain(),
                e["approver"].domain(),
                e["asked"] >= 1,
                e["asked"] <= MAX_AMOUNT,
                Implies(e["approved"], And(e["amount"] >= 1, e["amount"] <= e["asked"])),
                Implies(Not(e["approved"]), e["amount"] == REFUSED_AMOUNT),
            ]
        # Disagreeing approvals on one order are a conflict, which the store-wide rule always blocks; both policies agree there.
        for i, a in enumerate(self.events):
            for b in self.events[i + 1 :]:
                same = a["order_id"].same_as(b["order_id"])
                cs.append(Implies(same, And(a["approved"] == b["approved"], a["amount"] == b["amount"])))
        return cs

    def request_field(self, field):
        return self.request[field]

    def prior_matches(self, requires, gap_ok):
        """Dogwood's temporal clause: an earlier event in the window with these fields."""
        found = []
        for e in self.events:
            cs = [gap_ok(e)]
            if "order_id" in requires.get("same", []):
                cs.append(e["order_id"].same_as(self.request["order_id"]))
            if requires.get("event") != "request" and requires.get("output", {}).get("approved"):
                cs.append(e["approved"])
            found.append(And(*cs))
        return Or(*found) if found else BoolVal(False)

    def facts_hold(self, requires, in_window):
        """The checks on the engine's facts, which always come from the request's own order."""
        if "at_most" not in requires and "approver_role" not in requires:
            return BoolVal(True)
        found = []
        for e in self.events:
            cs = [e["order_id"].same_as(self.request["order_id"]), e["approved"], in_window(e)]
            if "at_most" in requires:
                cs.append(self.request["amount"] <= e["amount"])
            if "approver_role" in requires:
                names = [n for n, person in STAFF.items() if person["role"] == requires["approver_role"]]
                cs.append(Or(*[e["approver"].equals(n) for n in names]))
            found.append(And(*cs))
        return Or(*found) if found else BoolVal(False)

    def soft(self, opt):
        """Prefer an example that reads like the store's own cases."""
        opt.add_soft(self.request["order_id"].equals(SHOWN_ORDER), weight=4)
        opt.add_soft(self.request["amount"] == SHOWN_REFUND, weight=2)
        for e in self.events:
            opt.add_soft(e["order_id"].same_as(self.request["order_id"]), weight=4)
            opt.add_soft(e["asked"] == self.request["amount"], weight=2)
            opt.add_soft(e["approved"], weight=2)
            opt.add_soft(e["amount"] == SHOWN_APPROVED, weight=1)

    def events_for_case(self, model):
        events = []
        for e in self.events:
            t = model.eval(e["t"], model_completion=True).as_long()
            approved = is_true(model.eval(e["approved"], model_completion=True))
            inp = {
                "order_id": e["order_id"].read(model),
                "amount": model.eval(e["asked"], model_completion=True).as_long(),
                "reason": "counterexample",
            }
            amount = model.eval(e["amount"], model_completion=True).as_long()
            out = {"approved": approved, "approver": e["approver"].read(model), "approved_amount": amount}
            events.append({"at": t - 1, "tool": "request_approval", "input": inp})
            events.append({"at": t, "tool": "request_approval", "kind": "response", "input": inp, "output": out})
        return events

    def request_for_case(self, model):
        return {
            "order_id": self.request["order_id"].read(model),
            "amount": model.eval(self.request["amount"], model_completion=True).as_long(),
        }


class DeployHistory:
    action, prior_action = "DeployService", "RunTests"

    def __init__(self, forms, count):
        services = [*_literal_values(forms, "service"), "checkout", OTHER_SERVICE]
        self.request = {
            "service": Choice("req_service", services),
            "version": Choice("req_version", VERSIONS),
            "env": Choice("req_env", ENVS),
        }
        self.events = [
            {
                "t": Int(f"t{i}"),
                "service": Choice(f"e{i}_service", services),
                "version": Choice(f"e{i}_version", VERSIONS),
                "passed": Bool(f"e{i}_passed"),
            }
            for i in range(count)
        ]
        self.now = Int("now")

    def constraints(self):
        cs = [c.domain() for c in self.request.values()]
        for e in self.events:
            cs += [e["service"].domain(), e["version"].domain()]
        return cs

    def request_field(self, field):
        return self.request[field]

    def prior_matches(self, requires, gap_ok):
        found = []
        for e in self.events:
            cs = [gap_ok(e)]
            for field in requires.get("same", []):
                cs.append(e[field].same_as(self.request[field]))
            if requires.get("event") != "request" and requires.get("output", {}).get("passed"):
                cs.append(e["passed"])
            found.append(And(*cs))
        return Or(*found) if found else BoolVal(False)

    def facts_hold(self, requires, in_window):
        return BoolVal(True)

    def soft(self, opt):
        for e in self.events:
            opt.add_soft(e["service"].same_as(self.request["service"]), weight=2)
            opt.add_soft(e["version"].same_as(self.request["version"]), weight=2)
            opt.add_soft(e["passed"], weight=1)

    def events_for_case(self, model):
        events = []
        for e in self.events:
            t = model.eval(e["t"], model_completion=True).as_long()
            inp = {"service": e["service"].read(model), "version": e["version"].read(model)}
            out = {"passed": is_true(model.eval(e["passed"], model_completion=True))}
            events.append({"at": t - 1, "tool": "run_tests", "input": inp})
            events.append({"at": t, "tool": "run_tests", "kind": "response", "input": inp, "output": out})
        return events

    def request_for_case(self, model):
        return {field: choice.read(model) for field, choice in self.request.items()}


HISTORIES = {"IssueRefund": RefundHistory, "DeployService": DeployHistory}
OPERATORS = {
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
}


def _condition(c, h):
    field = h.request_field(c["field"])
    if isinstance(field, Choice):
        same = field.equals(c["value"])
        return same if c["op"] == "==" else Not(same)
    if c["op"] == "==":
        return field == IntVal(c["value"])
    if c["op"] == "!=":
        return field != IntVal(c["value"])
    return OPERATORS[c["op"]](field, IntVal(c["value"]))


def _applies(form, h):
    return _condition(form["only_when"], h) if "only_when" in form else BoolVal(True)


def _forbidden(form, h):
    field = h.request_field(form["field"])
    if form["kind"] == "limit":
        return And(_applies(form, h), OPERATORS[form.get("op", ">")](field, IntVal(form["max"])))
    return And(_applies(form, h), Not(Or(*[field.equals(v) for v in form["allowed"]])))


def _permitted(form, h):
    r = form["requires"]
    seconds = window_seconds(r["within"])
    # A request sits one second before its response in the histories this builds.
    lead = 1 if r.get("event") == "request" else 0

    def in_window(e):
        return h.now - e["t"] + lead <= seconds

    gated = And(h.prior_matches(r, in_window), h.facts_hold(r, lambda e: h.now - e["t"] <= seconds))
    return Or(Not(_applies(form, h)), gated)


def allows(forms, h):
    """The policy's decision as a formula. Actions with no requires_prior rule are allowed by the baseline."""
    gates = [f for f in forms if f["kind"] == "requires_prior"]
    blocks = [_forbidden(f, h) for f in forms if f["kind"] != "requires_prior"]
    permitted = Or(*[_permitted(f, h) for f in gates]) if gates else BoolVal(True)
    return And(permitted, Not(Or(*blocks)) if blocks else BoolVal(True))


def _shape(h, grain):
    cs = h.constraints() + [h.now <= MAX_SECONDS]
    previous = None
    for e in h.events:
        cs += [e["t"] >= 2 if previous is None else e["t"] >= previous["t"] + 2, h.now > e["t"]]
        cs.append((h.now - e["t"]) % grain == 0)
        previous = e
    cs.append(h.now >= 2)
    return cs


def _action_of(forms_a, forms_b):
    actions = {f["action"] for f in forms_a + forms_b}
    if len(actions) != 1:
        raise ValueError(f"the two policies guard different actions: {sorted(actions)}")
    return actions.pop()


def find_difference(forms_a, forms_b):
    """The shortest history where the two decide differently: fewest events, then earliest. Returns a case or None."""
    action = _action_of(forms_a, forms_b)
    max_events = MAX_EVENTS if any(f["kind"] == "requires_prior" for f in forms_a + forms_b) else 0
    for count in range(max_events + 1):
        for grain in GRAINS if count else (1,):
            h = HISTORIES[action](forms_a + forms_b, count)
            a, b = allows(forms_a, h), allows(forms_b, h)
            opt = Optimize()
            opt.add(*_shape(h, grain))
            opt.add(a != b)
            h.soft(opt)
            opt.minimize(h.now)
            if opt.check() == sat:
                return _case(h, opt.model(), a, b)
    return None


def _case(h, model, a, b):
    now = model.eval(h.now, model_completion=True).as_long()
    request = h.request_for_case(model)
    events = [*h.events_for_case(model), {"at": now, "tool": TOOL_FOR[h.action], "input": request, "expect": "?"}]
    return {
        "id": "counterexample",
        "events": events,
        "predicted": ["ALLOW" if is_true(model.eval(f, model_completion=True)) else "DENY" for f in (a, b)],
    }


def describe(case):
    """The case in plain words."""
    now = case["events"][-1]["at"]
    parts = []
    for e in case["events"][:-1]:
        if e.get("kind") != "response":
            continue
        ago = how_long(now - e["at"])
        if e["tool"] == "request_approval":
            parts.append(_approval_words(e, ago))
        else:
            word = "passed" if e["output"]["passed"] else "failed"
            parts.append(f"Tests for {e['input']['service']} {e['input']['version']} {word} {ago} ago.")
    last = case["events"][-1]["input"]
    if "amount" in last:
        parts.append(f"The agent now wants to refund ${last['amount']:,} on order {last['order_id']}.")
    else:
        parts.append(f"The agent now wants to deploy {last['service']} {last['version']} to {last['env']}.")
    return " ".join(parts)


def _approval_words(event, ago):
    inp, out = event["input"], event["output"]
    who = f"{out['approver']} ({STAFF[out['approver']]['role']})"
    if not out["approved"]:
        return f"{who} refused a ${inp['amount']:,} request for order {inp['order_id']} {ago} ago."
    return f"{who} was asked for ${inp['amount']:,} on order {inp['order_id']} and approved ${out['approved_amount']:,}, {ago} ago."
