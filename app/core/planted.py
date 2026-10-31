"""Every planted error in checker/: replay each broken policy against the correct one through Dogwood."""

import os
import shutil
import sys

from app.core import config, known_bugs

CHECKER = config.REPO / "checker"
sys.path.insert(0, str(CHECKER))
# The checker's module reads DOGWOOD at import; without it, it points at a path on one teammate's laptop.
os.environ.setdefault("DOGWOOD", shutil.which("dogwood") or os.path.expanduser("~/.cargo/bin/dogwood"))

from build import MUTANTS, POL, TR, last_verdict


def _search_bug(policy_id, error_id):
    name = f"{policy_id}_{error_id}"
    return next((bug for bug, names in known_bugs.CHECKER.items() if name in names), None)


def replay_all():
    """One row per planted error, with both Dogwood verdicts and whether the form search also finds its adaptation."""
    rows = []
    for policy_id, error_id, label, _ in MUTANTS:
        trace = os.path.join(TR, f"{policy_id}_{error_id}.log")
        intended, _ = last_verdict(os.path.join(POL, f"{policy_id}_intended.dw"), trace)
        broken, _ = last_verdict(os.path.join(POL, f"{policy_id}_{error_id}.dw"), trace)
        rows.append(
            {
                "policy": policy_id,
                "error": error_id,
                "label": label,
                "intended": intended,
                "broken": broken,
                "caught": intended != broken and "ERROR" not in (intended, broken),
                "search_bug": _search_bug(policy_id, error_id),
            }
        )
    return rows
