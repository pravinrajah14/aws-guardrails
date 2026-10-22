"""A Provenance-style recorder for agent runs.

Like Berkeley's Provenance extension for VS Code, it:
- records everything inside one "assignment" (here, one agent session) and nothing else,
- keeps the log on your own computer in a .provenance/ folder, with no network calls,
- writes plain text you can open and read, and
- makes the record hard to edit quietly: every event stores a hash of the event before it,
  so changing or deleting a line breaks the chain (review.py checks this).

Files per session, in .provenance/<session_id>/:
  events.jsonl  one JSON event per line, hash-chained (the record of truth)
  timeline.txt  the same events as readable lines, written live
  trace.log     the history in Dogwood's format (written by enforce.py)
"""
import hashlib
import json
import platform
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

GENESIS = "0" * 64


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def event_hash(record):
    body = {k: v for k, v in record.items() if k != "hash"}
    return sha256_text(json.dumps(body, sort_keys=True, ensure_ascii=False))


class Recorder:
    def __init__(self, root=".provenance", session_id=None):
        self.start = time.monotonic()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")[:-3]  # ms, so back-to-back runs differ
        self.session_id = session_id or f"session-{stamp}"
        self.dir = Path(root) / self.session_id
        self.dir.mkdir(parents=True, exist_ok=False)
        self.events_path = self.dir / "events.jsonl"
        self.timeline_path = self.dir / "timeline.txt"
        self.seq = 0
        self.prev = GENESIS

    def elapsed(self):
        return time.monotonic() - self.start

    def log(self, kind, summary, **data):
        """Append one event. `summary` is the readable one-liner; `data` holds the details."""
        self.seq += 1
        record = {
            "seq": self.seq,
            "time": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "elapsed_s": round(self.elapsed(), 3),
            "kind": kind,
            "summary": summary,
            "data": data,
            "prev_hash": self.prev,
        }
        record["hash"] = event_hash(record)
        self.prev = record["hash"]
        with self.events_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        with self.timeline_path.open("a", encoding="utf-8") as f:
            f.write(f"[{record['elapsed_s']:8.2f}s] #{self.seq:03d} {kind:<14} {summary}\n")
        print(f"  #{self.seq:03d} {kind:<14} {summary}")
        return record

    def start_session(self, task_name, task_text, model, policy, schema):
        return self.log(
            "session_start", f"task '{task_name}' with model {model}",
            task=task_name, task_text=task_text, model=model,
            policy_file=str(policy), policy_sha256=file_hash(policy),
            schema_file=str(schema), schema_sha256=file_hash(schema),
            python=sys.version.split()[0], platform=platform.platform(),
        )
