"""The thin enforcement layer: every tool call goes through Dogwood before it runs.

How it works:
1. Keep a trace of everything that happened this session, in Dogwood's trace format.
2. When the agent wants to call a tool, write the trace plus that new request
   to a file and run `dogwood replay` on it.
3. The verdict on the last line is the decision for this call: ALLOW or DENY.
4. After a tool runs, add its result (a "response" or "error" event) to the trace,
   so later rules like "approval before refund" can see it.

Dogwood's reference interpreter is a CLI, so we call it as a subprocess.
Set DOGWOOD_CMD if it is not on your PATH (for example "wsl ~/.cargo/bin/dogwood").
"""
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


class DogwoodGate:
    def __init__(self, session_dir, policy="policies/policy.dw", schema="policies/schema.cedarschema"):
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

    def _event(self, ts, action, kind, input_args, output=None, request_id="", with_context=False):
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

    def decide(self, action, input_args, elapsed_seconds):
        """Ask Dogwood about one tool call. Returns a dict with the verdict and the trace line."""
        self.request_seq += 1
        request_id = f"r{self.request_seq}"
        ts = self._tick(elapsed_seconds)
        line = self._event(ts, action, "request", input_args, request_id=request_id, with_context=True)

        self.pending_path.write_text("\n".join(self.trace_lines + [line]) + "\n", encoding="utf-8")
        code, out, err = self._run([
            "replay", self.policy, "--policy-schema", self.schema,
            "--trace", self.pending_path.as_posix(),
        ])
        verdicts = [m for m in (VERDICT_LINE.match(l.strip()) for l in out.splitlines()) if m]
        if code != 0 or not verdicts or int(verdicts[-1].group(1)) != ts:
            # Fail closed: if Dogwood errors or gives no verdict for this request, block it.
            decision, rules = "DENY", []
            note = "dogwood error, failing closed: " + (err or out).strip()[:400]
        else:
            last = verdicts[-1]
            decision = last.group(3)
            rules = [int(x) for x in last.group(4).split(",")] if last.group(4) else []
            note = ""

        # The request is part of history whatever the verdict.
        self.trace_lines.append(line)
        self.trace_path.write_text("\n".join(self.trace_lines) + "\n", encoding="utf-8")

        names = [self.rule_names[i] if i < len(self.rule_names) else f"rule {i}" for i in rules]
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
            line = self._event(ts, action, "error", input_args, request_id=request_id)
        else:
            line = self._event(ts, action, "response", input_args, output=output, request_id=request_id)
        self.trace_lines.append(line)
        self.trace_path.write_text("\n".join(self.trace_lines) + "\n", encoding="utf-8")
        return line
