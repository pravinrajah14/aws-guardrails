"""Validate and replay Dogwood policies before tool execution."""
import json
import os
import re
import shlex
import subprocess
from pathlib import Path

from context_engine import form_window, get_facts

NAMESPACE = "Shop"
PRINCIPAL = 'Shop::OAuthUser::"support-agent"'
RESOURCE = 'Shop::Gateway::"testbed"'

VERDICT_LINE = re.compile(r"^@(\d+) \(time point (\d+)\): (ALLOW|DENY)(?:\s+\[rules: ([^\]]*)\])?")


def dogwood_cmd():
    raw = os.environ.get("DOGWOOD_CMD", "dogwood")
    return shlex.split(raw, posix=(os.name != "nt"))


def cedar_value(v):
    """Python value -> Dogwood trace literal."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        return json.dumps(v)  # double-quoted with escapes
    if isinstance(v, dict):
        return cedar_record(v)
    raise TypeError(f"cannot put {type(v).__name__} in a trace")


def cedar_record(d):
    inner = ", ".join(f"{k}: {cedar_value(d[k])}" for k in sorted(d))
    return "{ " + inner + " }"


def policy_ids(policy_path):
    """Rule names in file order, so '[rules: 4]' can be shown as a name."""
    text = Path(policy_path).read_text(encoding="utf-8")
    return re.findall(r'@id\("([^"]+)"\)', text)


def permit_rules_by_action(policy_path):
    """Which permit rules apply to each action, so a block can say what was missing."""
    text = Path(policy_path).read_text(encoding="utf-8")
    found = {}
    for rule_id, action in re.findall(
        r'@id\("([^"]+)"\)\s*permit\s*\(\s*principal\s*,\s*action\s*==\s*\w+::Action::"(\w+)"', text
    ):
        found.setdefault(action, []).append(rule_id)
    return found


def build_policy(base_path, rule_path, out_path):
    """The store's base permissions + the rule file under test, written as one policy file."""
    base = Path(base_path).read_text(encoding="utf-8")
    rule = Path(rule_path).read_text(encoding="utf-8")
    combined = (
        f"// ===== base permissions: {Path(base_path).as_posix()}\n{base.rstrip()}\n\n"
        f"// ===== rule file under test: {Path(rule_path).as_posix()}\n{rule.rstrip()}\n"
    )
    Path(out_path).write_text(combined, encoding="utf-8")
    return Path(out_path)


def find_form(rule_path, form_path=None):
    """The rule form behind a rule file: the given path, or updated_form.json next to the rule
    (where the pipeline writes it). Returns (form or None, path or None)."""
    path = Path(form_path) if form_path else Path(rule_path).parent / "updated_form.json"
    if not path.is_file():
        if form_path:
            raise FileNotFoundError(f"rule form not found: {form_path}")
        return None, None
    return json.loads(path.read_text(encoding="utf-8")), path


def block_message(action, input_args, facts_status):
    """Plain words for the agent when Dogwood blocks, based on what the engine found."""
    if action != "IssueRefund":
        return ""
    if facts_status in ("missing", "expired"):
        return f"No valid approval on file for order {input_args['order_id']}; request approval first."
    if facts_status == "conflict":
        return "Conflicting approvals on file; a person must review. Do not retry."
    return ""


class DogwoodGate:
    def __init__(self, session_dir, policy, schema="policies/schema.cedarschema", form=None, store=None):
        """form: the rule form (gives the context engine its window). store: the MockWorld whose
        records the engine reads; without one, callers pass records to context_for themselves."""
        self.session_dir = Path(session_dir)
        self.policy = Path(policy).as_posix()
        self.schema = Path(schema).as_posix()
        self.rule_names = policy_ids(policy)
        self.permits = permit_rules_by_action(policy)
        self.trace_path = self.session_dir / "trace.log"
        self.pending_path = self.session_dir / "pending.log"
        self.trace_lines = []
        self.clock = 0       # Dogwood timestamps, in seconds since the session started
        self.request_seq = 0
        self.form = form
        self.store = store
        self.steps = []      # earlier steps, for the context engine

    # ---- helpers -------------------------------------------------------

    def _run(self, args):
        cmd = dogwood_cmd() + args
        try:
            done = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except FileNotFoundError:
            raise RuntimeError(
                "Could not find the dogwood CLI. Build it (see README) or set DOGWOOD_CMD."
            )
        return done.returncode, done.stdout, done.stderr

    def _tick(self, elapsed_seconds):
        # Dogwood needs strictly increasing timestamps.
        self.clock = max(self.clock + 1, int(elapsed_seconds))
        return self.clock

    def event_line(self, ts, action, kind, input_args, output=None, request_id="", with_context=False, facts=None):
        fields = [f"input: {cedar_record(input_args)}"]
        if output is not None:
            fields.append(f"output: {cedar_record(output)}")
        fields += [f"callerPrincipal: {PRINCIPAL}", f"callerResource: {RESOURCE}", f"requestId: {json.dumps(request_id)}"]
        parts = [f"@{ts}", f"scope(principal: {PRINCIPAL}, resource: {RESOURCE})"]
        if with_context:
            extra = f", facts: {cedar_record(facts)}" if facts else ""
            parts.append(f"request_context(input: {cedar_record(input_args)}{extra})")
        parts.append(f'{NAMESPACE}::Action::"{action}"::{kind}(' + ", ".join(fields) + ")")
        return " ".join(parts)

    # ---- public API ----------------------------------------------------

    def validate(self):
        """Check the policy against the schema once, before the agent starts."""
        code, out, err = self._run(["validate", self.policy, "--policy-schema", self.schema])
        return code == 0, (out + err).strip()

    def replay(self, lines):
        """Run `dogwood replay` on a list of trace lines."""
        self.pending_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        code, out, err = self._run([
            "replay", self.policy, "--policy-schema", self.schema,
            "--trace", self.pending_path.as_posix(),
        ])
        verdicts = []
        for m in (VERDICT_LINE.match(l.strip()) for l in out.splitlines()):
            if m:
                rules = [int(x) for x in m.group(4).split(",")] if m.group(4) else []
                verdicts.append({"ts": int(m.group(1)), "decision": m.group(3), "rules": rules})
        return code == 0, verdicts, (err or out).strip()

    def note_message(self, who, text, elapsed_seconds=0):
        """Free text (e.g. the user's task) the engine may see but never trusts as evidence."""
        self.steps.append({"ts": max(self.clock, int(elapsed_seconds)), "kind": "message", "who": who, "text": text})

    def context_for(self, action, input_args, ts, records=None, steps=None):
        """Ask the context engine for this request's facts, from the store (or the given records)."""
        if records is None:
            s = self.store
            records = {"approvals": getattr(s, "approvals", []), "orders": getattr(s, "orders", {}),
                       "staff": getattr(s, "staff", {})}
        return get_facts(action, input_args, records, self.steps if steps is None else steps,
                         ts, form_window(self.form, action))

    def rule_name(self, index):
        return self.rule_names[index] if index < len(self.rule_names) else f"rule {index}"

    def decide(self, action, input_args, elapsed_seconds):
        """Ask Dogwood about one tool call. Returns a dict with the verdict and the trace line."""
        self.request_seq += 1
        request_id = f"r{self.request_seq}"
        ts = self._tick(elapsed_seconds)
        ctx = self.context_for(action, input_args, ts)
        line = self.event_line(ts, action, "request", input_args, request_id=request_id, with_context=True,
                               facts=ctx["facts"])

        ok, verdicts, problem = self.replay(self.trace_lines + [line])
        if not ok or not verdicts or verdicts[-1]["ts"] != ts:
            # Fail closed: if Dogwood errors or gives no verdict for this request, block it.
            decision, rules = "DENY", []
            note = "dogwood error, failing closed: " + problem[:400]
        else:
            decision, rules, note = verdicts[-1]["decision"], verdicts[-1]["rules"], ""

        # The request is part of history whatever the verdict.
        self.trace_lines.append(line)
        self.steps.append({"ts": ts, "action": action, "kind": "request", "input": input_args})
        self.trace_path.write_text("\n".join(self.trace_lines) + "\n", encoding="utf-8")

        names = [self.rule_name(i) for i in rules]
        if decision == "DENY" and not names and not note:
            needed = self.permits.get(action, [])
            note = block_message(action, input_args, ctx["status"]) or "no permit rule matched (deny by default)"
            if needed and not ctx["status"]:
                note += "; this action is only allowed by: " + ", ".join(needed)
            elif ctx["status"] == "ok":
                note += f"; facts on file: {ctx['reason']}"
        return {
            "decision": decision,
            "rules": names,
            "note": note,
            "request_id": request_id,
            "dogwood_ts": ts,
            "trace_line": line,
            "facts": ctx["facts"],
            "facts_status": ctx["status"],
            "facts_reason": ctx["reason"],
            "facts_sources": ctx["sources"],
            "facts_details": ctx["details"],
            "needs_review": ctx["review"],
            "ignored_claims": ctx["ignored_claims"],
        }

    def record_result(self, action, input_args, request_id, elapsed_seconds, output=None, failed=False):
        """Add the tool's outcome to history: a response if it worked, an error if not."""
        ts = self._tick(elapsed_seconds)
        if failed:
            line = self.event_line(ts, action, "error", input_args, request_id=request_id)
        else:
            line = self.event_line(ts, action, "response", input_args, output=output, request_id=request_id)
            if action == "RequestApproval" and self.store is not None and getattr(self.store, "approvals", None):
                # Stamp the store's approval record with the same clock Dogwood sees.
                self.store.approvals[-1]["at"] = ts
                self.store.approvals[-1]["source"] = f"store approvals[{len(self.store.approvals) - 1}] ({request_id})"
        self.trace_lines.append(line)
        self.steps.append({"ts": ts, "action": action, "kind": "error" if failed else "response",
                           "input": input_args, "output": output})
        self.trace_path.write_text("\n".join(self.trace_lines) + "\n", encoding="utf-8")
        return line
