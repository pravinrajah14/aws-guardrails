"""Validate and replay Dogwood policies before tool execution."""
import json
import os
import re
import shlex
import subprocess
from pathlib import Path

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


class DogwoodGate:
    def __init__(self, session_dir, policy, schema="policies/schema.cedarschema"):
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

    def event_line(self, ts, action, kind, input_args, output=None, request_id="", with_context=False):
        fields = [f"input: {cedar_record(input_args)}"]
        if output is not None:
            fields.append(f"output: {cedar_record(output)}")
        fields += [f"callerPrincipal: {PRINCIPAL}", f"callerResource: {RESOURCE}", f"requestId: {json.dumps(request_id)}"]
        parts = [f"@{ts}", f"scope(principal: {PRINCIPAL}, resource: {RESOURCE})"]
        if with_context:
            parts.append(f"request_context(input: {cedar_record(input_args)})")
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

    def rule_name(self, index):
        return self.rule_names[index] if index < len(self.rule_names) else f"rule {index}"

    def decide(self, action, input_args, elapsed_seconds):
        """Ask Dogwood about one tool call. Returns a dict with the verdict and the trace line."""
        self.request_seq += 1
        request_id = f"r{self.request_seq}"
        ts = self._tick(elapsed_seconds)
        line = self.event_line(ts, action, "request", input_args, request_id=request_id, with_context=True)

        ok, verdicts, problem = self.replay(self.trace_lines + [line])
        if not ok or not verdicts or verdicts[-1]["ts"] != ts:
            # Fail closed: if Dogwood errors or gives no verdict for this request, block it.
            decision, rules = "DENY", []
            note = "dogwood error, failing closed: " + problem[:400]
        else:
            decision, rules, note = verdicts[-1]["decision"], verdicts[-1]["rules"], ""

        # The request is part of history whatever the verdict.
        self.trace_lines.append(line)
        self.trace_path.write_text("\n".join(self.trace_lines) + "\n", encoding="utf-8")

        names = [self.rule_name(i) for i in rules]
        if decision == "DENY" and not names and not note:
            needed = self.permits.get(action, [])
            note = "no permit rule matched (deny by default)"
            if needed:
                note += "; this action is only allowed by: " + ", ".join(needed)
        return {
            "decision": decision,
            "rules": names,
            "note": note,
            "request_id": request_id,
            "dogwood_ts": ts,
            "trace_line": line,
        }

    def record_result(self, action, input_args, request_id, elapsed_seconds, output=None, failed=False):
        """Add the tool's outcome to history: a response if it worked, an error if not."""
        ts = self._tick(elapsed_seconds)
        if failed:
            line = self.event_line(ts, action, "error", input_args, request_id=request_id)
        else:
            line = self.event_line(ts, action, "response", input_args, output=output, request_id=request_id)
        self.trace_lines.append(line)
        self.trace_path.write_text("\n".join(self.trace_lines) + "\n", encoding="utf-8")
        return line
