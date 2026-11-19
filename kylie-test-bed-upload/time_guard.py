"""
time_guard.py: how long does the guard take to check one agent action?

For every request in the scripted test cases, this does exactly what the guard
does during a live run: add the request to the history and run one
`dogwood replay`. No AI model is involved. It repeats that many times and
prints the median, which is the number for the [__ ms] box on slide 7.

Run (same setup as score_actions.py):
    python time_guard.py tests/refund_recent_approval_actions.json --rules rules/refund_recent_approval.dw
Options:
    --rounds 20   how many times to repeat every check (default 20)
"""

import argparse
import json
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path

from enforce import DogwoodGate, build_policy
from score_actions import SCHEMA, case_lines


def main():
    p = argparse.ArgumentParser()
    p.add_argument("actions", help="action list file (JSON)")
    p.add_argument("--rules", required=True, help="rule file under test (.dw)")
    p.add_argument("--base", default="policies/base.dw")
    p.add_argument("--rounds", type=int, default=20)
    args = p.parse_args()

    cases = json.loads(Path(args.actions).read_text(encoding="utf-8"))["cases"]
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    work = Path(".provenance") / f"timing-{stamp}"
    work.mkdir(parents=True, exist_ok=True)
    policy = build_policy(args.base, args.rules, work / "policy.dw")
    gate = DogwoodGate(work, policy, SCHEMA)

    ok, msg = gate.validate()
    if not ok:
        raise SystemExit(f"Rule file did not validate:\n{msg}")

    # One check = the history before a request, plus that request.
    checks = []
    for case in cases:
        lines, _ = case_lines(gate, case)
        for i, line in enumerate(lines):
            if "::request(" in line:
                checks.append(lines[: i + 1])

    gate.replay(checks[0])  # warm-up run, not counted

    times_ms = []
    for _ in range(args.rounds):
        for history in checks:
            start = time.perf_counter()
            gate.replay(history)
            times_ms.append((time.perf_counter() - start) * 1000)

    times_ms.sort()
    median = statistics.median(times_ms)
    p90 = times_ms[int(0.9 * (len(times_ms) - 1))]
    report = [
        f"# Guard timing ({stamp})",
        "",
        f"- Checks timed: {len(times_ms)} ({len(checks)} requests x {args.rounds} rounds)",
        f"- **Median: {median:.0f} ms per action**",
        f"- 90th percentile: {p90:.0f} ms",
        f"- Fastest / slowest: {times_ms[0]:.0f} / {times_ms[-1]:.0f} ms",
        "",
        "Each check is one `dogwood replay` of the history plus the new request, the same call the guard makes before every tool call. No AI model.",
    ]
    out = Path("results")
    out.mkdir(exist_ok=True)
    (out / f"guard_timing_{stamp}.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    print("\n".join(report))
    print(f"\nSaved results/guard_timing_{stamp}.md")


if __name__ == "__main__":
    main()
