"""Cases a person confirmed, kept as tests and rerun whenever a policy for that sentence changes."""

import hashlib
import json
import re
from datetime import UTC, datetime

from app.core import config
from app.core.generate import replay_case

CASES_DIR = config.REPO / "tests" / "confirmed"
HASH_CHARS = 10
SLUG_CHARS = 40


def _key(sentence):
    return " ".join(sentence.lower().split())


def save(sentence, says, expect, events):
    """Saved by content, so confirming the same example twice keeps one file."""
    CASES_DIR.mkdir(parents=True, exist_ok=True)
    body = json.dumps([_key(sentence), says, expect, events], sort_keys=True)
    digest = hashlib.sha256(body.encode()).hexdigest()[:HASH_CHARS]
    slug = re.sub(r"[^a-z0-9]+", "-", _key(sentence))[:SLUG_CHARS].strip("-")
    path = CASES_DIR / f"{slug}-{digest}.json"
    case = {
        "sentence": sentence,
        "says": says,
        "expect": expect,
        "events": events,
        "confirmed_by": config.ANSWERED_BY,
        "confirmed_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    path.write_text(json.dumps(case, indent=2) + "\n", encoding="utf-8")
    return path


def for_sentence(sentence):
    if not CASES_DIR.is_dir():
        return []
    cases = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(CASES_DIR.glob("*.json"))]
    return [c for c in cases if _key(c["sentence"]) == _key(sentence)]


def rerun(sentence, gate):
    """Each confirmed case for this sentence through Dogwood under this policy."""
    results = []
    for case in for_sentence(sentence):
        got = replay_case(gate, {"id": "confirmed", "events": case["events"]})["verdict"]
        results.append(
            {
                "says": case["says"],
                "expect": case["expect"],
                "got": got,
                "passed": got == case["expect"],
                "events": case["events"],
            }
        )
    return results
