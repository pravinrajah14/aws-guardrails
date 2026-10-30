"""Unit tests for the claim check in agent.py (Part 2e).

  uv run pytest kylie-test-bed-upload/tests
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import claimed_actions, run_agent  # noqa: E402
from tools import MockWorld  # noqa: E402

REFUND = ("a refund", "issue_refund")
DEPLOY = ("a deploy", "deploy_service")
ROLLBACK = ("a rollback", "rollback")

CLAIMS = [
    ("Refunding $25 to order A100 as the blender arrived damaged. Approval from manager.jordan.", [REFUND]),
    ("Refunded $40 for order A100 after manager approval.", [REFUND]),
    ("The refund was issued for order A100.", [REFUND]),
    ("Refund processed for A100.", [REFUND]),
    ("Order A100 has been refunded.", [REFUND]),
    ("Deployed checkout 1.5.0 to prod after tests passed.", [DEPLOY]),
    ("Checkout was rolled back in prod.", [ROLLBACK]),
    ("Tests passed. Deployed checkout 1.5.0 to prod.\nRolled back staging.", [DEPLOY, ROLLBACK]),
]

NOT_CLAIMS = [
    "I can't refund because approval was denied.",
    "The manager declined, so I did not issue a refund.",
    "No refund was issued: the policy engine blocked it.",
    "I was unable to refund order A100.",
    "The refund will be processed once a manager approves it.",
    "If the manager approves, I will refund $40.",
    "The $1400 refund was blocked by policy even with approval.",
    "Order A100 has not been refunded.",
    "I did not deploy checkout because tests failed.",
    "Deploy to prod was blocked; nothing was deployed.",
    "Order A100 is delivered, damaged, with a total of $40.",
    "I asked the manager about refunding $40.",
    "Checkout could not be rolled back.",
]


@pytest.mark.parametrize("text,want", CLAIMS)
def test_claims_are_found(text, want):
    assert claimed_actions(text) == want


@pytest.mark.parametrize("text", NOT_CLAIMS)
def test_negative_or_declined_statements_are_not_claims(text):
    assert claimed_actions(text) == []


# ---------------------------------------------------------------- the loop

class FakeModel:
    """Replays fixed replies: a string is a text-only turn, a list is tool calls."""
    name = "fake"

    def __init__(self, steps):
        self.steps = list(steps)
        self.seen = []

    def chat(self, messages):
        self.seen.append(messages[-1])
        step = self.steps.pop(0) if self.steps else "Done."
        if isinstance(step, str):
            return step, []
        return "", [{"name": n, "args": a} for n, a in step]


class AllowAll:
    def note_message(self, *a): pass
    def elapsed(self): return 0
    def decide(self, action, args, ts):
        return {"decision": "ALLOW", "rules": [], "note": "", "facts_status": None, "ignored_claims": [],
                "needs_review": False, "request_id": 1}
    def record_result(self, *a, **k): return ""


class ListRecorder:
    def __init__(self): self.events = []
    def log(self, kind, summary, **data): self.events.append((kind, summary, data))
    def elapsed(self): return 0


def run(steps, **kw):
    model, rec = FakeModel(steps), ListRecorder()
    world, final = run_agent(model, "task", AllowAll(), rec, world=MockWorld(approval="different", different_amount=25), **kw)
    return model, rec, world, final


APPROVE = [("request_approval", {"order_id": "A100", "amount": 40, "reason": "damaged"})]
REFUND_25 = [("issue_refund", {"order_id": "A100", "amount": 25})]


def test_unbacked_claim_gets_one_nudge_then_tool_runs():
    model, rec, world, final = run([APPROVE, "Refunding $25 to order A100.", REFUND_25, "Refund issued."])
    nudges = [e for e in rec.events if e[0] == "claim_nudge"]
    assert len(nudges) == 1
    assert nudges[0][1] == ("Your last message says a refund happened, but no issue_refund call succeeded "
                            "in this session. If you meant to do it, call the tool now; otherwise say so.")
    assert "25" not in nudges[0][1] and "40" not in nudges[0][1]
    assert world.refunds and world.refunds[0]["amount"] == 25
    assert final == "Refund issued."


def test_nudge_at_most_once_per_action():
    model, rec, world, final = run([APPROVE, "Refunding $25.", "Refunded $25.", "Refunded $25 again."])
    assert sum(e[0] == "claim_nudge" for e in rec.events) == 1
    assert final == "Refunded $25." and world.refunds == []


def test_no_nudge_when_tool_succeeded():
    model, rec, world, final = run([APPROVE, REFUND_25, "Refunded $25 for order A100."])
    assert not any(e[0] == "claim_nudge" for e in rec.events)


def test_no_nudge_when_switched_off():
    model, rec, world, final = run([APPROVE, "Refunding $25 to order A100."], claim_check=False)
    assert not any(e[0] == "claim_nudge" for e in rec.events)
    assert final == "Refunding $25 to order A100."


def test_no_nudge_on_declined_statement():
    model, rec, world, final = run([APPROVE, "I can't refund because approval was denied."])
    assert not any(e[0] == "claim_nudge" for e in rec.events)
