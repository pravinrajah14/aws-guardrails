"""Review a recorded session, the way course staff would read a Provenance log.

  python review.py .provenance/session-20261015-213000

Checks the hash chain (was anything edited or deleted?), then prints the timeline
and a short summary: how many actions were allowed, blocked, and failed.
"""
import json
import sys
from pathlib import Path

from recorder import GENESIS, event_hash


def main():
    if len(sys.argv) != 2:
        sessions = sorted(Path(".provenance").glob("session-*"))
        if not sessions:
            sys.exit("usage: python review.py <session folder>")
        folder = sessions[-1]
        print(f"(no folder given, using the latest: {folder})")
    else:
        folder = Path(sys.argv[1])

    lines = (folder / "events.jsonl").read_text(encoding="utf-8").splitlines()
    events = [json.loads(l) for l in lines if l.strip()]

    prev, intact = GENESIS, True
    for i, e in enumerate(events, start=1):
        problems = []
        if e["seq"] != i:
            problems.append(f"expected seq {i}, found {e['seq']}")
        if e["prev_hash"] != prev:
            problems.append("prev_hash does not match the event before it")
        if event_hash(e) != e["hash"]:
            problems.append("event was changed after it was written")
        if problems:
            intact = False
            print(f"CHAIN BROKEN at event {i}: " + "; ".join(problems))
        prev = e["hash"]

    print(f"\nSession {folder.name}: {len(events)} events, chain {'INTACT' if intact else 'BROKEN'}\n")
    for e in events:
        print(f"[{e['elapsed_s']:8.2f}s] #{e['seq']:03d} {e['kind']:<14} {e['summary']}")

    decisions = [e for e in events if e["kind"] == "policy_decision"]
    allowed = sum(1 for e in decisions if e["data"]["decision"] == "ALLOW")
    errors = sum(1 for e in events if e["kind"] == "tool_error")
    check = next((e for e in events if e["kind"] == "task_check"), None)
    print("\nSummary")
    print(f"  tool calls checked : {len(decisions)}")
    print(f"  allowed            : {allowed}")
    print(f"  blocked            : {len(decisions) - allowed}")
    print(f"  tool errors        : {errors}")
    print(f"  task check         : {check['summary'] if check else 'not reached'}")


if __name__ == "__main__":
    main()
