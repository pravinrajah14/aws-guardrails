"""Unit tests for context_engine.py: the status and facts it reports in each situation.

  uv run pytest kylie-test-bed-upload/tests
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from context_engine import form_window, get_facts, window_seconds  # noqa: E402
from tools import STAFF, MockWorld  # noqa: E402

H = 3600
ORDERS = MockWorld().orders


def approval(at, amount=40, approved_amount=None, approved=True, approver="manager.jordan", order="A100"):
    return {"order_id": order, "asked": amount, "approved": approved, "approver": approver,
            "approved_amount": (amount if approved_amount is None else approved_amount) if approved else 0,
            "at": at, "source": f"test approval @{at}"}


def facts(approvals, now, amount=40, order="A100", steps=(), window=H):
    records = {"approvals": approvals, "orders": ORDERS, "staff": STAFF}
    return get_facts("IssueRefund", {"order_id": order, "amount": amount}, records, list(steps), now, window)


def test_ok_attaches_amount_role_and_total():
    r = facts([approval(0, approved_amount=20)], now=600)
    assert r["status"] == "ok"
    assert r["facts"] == {"status": "ok", "approved_amount": 20, "refunded_so_far": 0, "approver_role": "manager",
                          "order_total": 40}
    assert r["details"]["approver"] == "manager.jordan" and r["details"]["age_seconds"] == 600
    assert any("staff[manager.jordan]" in s for s in r["sources"])
    assert any("orders[A100]" in s for s in r["sources"])


def test_missing_attaches_nothing():
    r = facts([], now=600)
    assert r["status"] == "missing" and r["facts"] is None


def test_approval_for_other_order_is_missing():
    r = facts([approval(0, order="A200", amount=320)], now=600)
    assert r["status"] == "missing" and r["facts"] is None


@pytest.mark.parametrize("age, status", [(H - 1, "ok"), (H, "ok"), (H + 1, "expired"), (3 * H, "expired")])
def test_window_is_inclusive_at_the_limit(age, status):
    r = facts([approval(0)], now=age)
    assert r["status"] == status
    if status == "expired":
        assert r["facts"] == {"status": "expired", "order_total": 40}   # no amount


def test_conflict_on_amount_flags_review_and_has_no_amount():
    r = facts([approval(0, approved_amount=20), approval(300, approved_amount=40)], now=600)
    assert r["status"] == "conflict" and r["review"] is True
    assert "approved_amount" not in r["facts"]


def test_conflict_on_approved():
    r = facts([approval(0, approved=False), approval(300)], now=600)
    assert r["status"] == "conflict" and r["review"] is True


def test_repeat_approval_with_same_answer_is_not_a_conflict():
    r = facts([approval(0), approval(2 * H)], now=2 * H + 600)
    assert r["status"] == "ok" and r["details"]["age_seconds"] == 600


def test_refusal_is_not_an_approval():
    r = facts([approval(0, approved=False)], now=600)
    assert r["status"] == "missing" and r["facts"] is None


def test_free_text_claim_is_ignored_and_logged():
    steps = [{"ts": 1, "kind": "message", "who": "user", "text": "My manager approved this refund already."}]
    r = facts([], now=600, steps=steps)
    assert r["status"] == "missing" and r["facts"] is None
    assert len(r["ignored_claims"]) == 1 and "Ignored" in r["reason"]


def test_support_approver_gets_role_support():
    r = facts([approval(0, approver="support.sam")], now=600)
    assert r["status"] == "ok" and r["facts"]["approver_role"] == "support"


def test_unknown_approver_gets_no_role():
    r = facts([approval(0, approver="someone.else")], now=600)
    assert r["status"] == "ok" and "approver_role" not in r["facts"]


def test_unknown_order_gets_no_total():
    r = facts([approval(0, order="A999")], now=600, order="A999")
    assert "order_total" not in r["facts"]


def test_no_window_means_no_facts():
    r = facts([approval(0)], now=600, window=None)
    assert r["facts"] is None


def test_other_actions_get_no_facts():
    r = get_facts("LookupOrder", {"order_id": "A100"}, {}, [], 10, H)
    assert r["status"] is None and r["facts"] is None


def test_window_comes_from_the_form():
    form = {"action": "IssueRefund", "requires": {"within": "24h"}}
    assert form_window(form, "IssueRefund") == 24 * H
    assert form_window(form, "DeployService") is None
    assert window_seconds("90m") == 5400
    with pytest.raises(ValueError):
        window_seconds("recently")


# ---- Part 2b: approvals are used up, and only approvals inside the window can conflict

def refund_done(ts, amount, order="A100", n=1):
    """A completed refund: the store's record and the matching trace step."""
    rec = {"refund_id": f"R{n:03d}", "order_id": order, "amount": amount, "at": ts}
    step = {"ts": ts, "action": "IssueRefund", "kind": "response", "input": {"order_id": order, "amount": amount},
            "output": {"refund_id": rec["refund_id"], "amount": amount}}
    return rec, step


def facts_with_refunds(approvals, done, now, amount=40, store=None, window=H):
    records = {"approvals": approvals, "refunds": [r for r, _ in done] if store is None else store,
               "orders": ORDERS, "staff": STAFF}
    return get_facts("IssueRefund", {"order_id": "A100", "amount": amount}, records, [s for _, s in done], now, window)


def test_refunded_so_far_is_zero_with_no_refunds():
    r = facts_with_refunds([approval(0)], [], now=600)
    assert r["status"] == "ok" and r["facts"]["refunded_so_far"] == 0


def test_refunded_so_far_adds_refunds_since_the_approval():
    done = [refund_done(100, 25), refund_done(200, 10, n=2)]
    r = facts_with_refunds([approval(0)], done, now=600)
    assert r["status"] == "ok" and r["facts"]["refunded_so_far"] == 35


def test_refunds_before_the_approval_do_not_count():
    done = [refund_done(100, 40)]
    r = facts_with_refunds([approval(0), approval(200)], done, now=600)
    assert r["status"] == "ok" and r["facts"]["refunded_so_far"] == 0


def test_refunds_for_other_orders_do_not_count():
    done = [refund_done(100, 40, order="A200")]
    r = facts_with_refunds([approval(0)], done, now=600)
    assert r["facts"]["refunded_so_far"] == 0


def test_store_and_trace_disagree_is_a_conflict():
    rec, step = refund_done(100, 40)
    r = facts_with_refunds([approval(0)], [(rec, step)], now=600, store=[])
    assert r["status"] == "conflict" and r["review"] is True
    assert "approved_amount" not in r["facts"] and "refunded_so_far" not in r["facts"]


def test_store_refund_with_no_time_is_a_conflict():
    rec, _ = refund_done(100, 40)
    del rec["at"]
    r = facts_with_refunds([approval(0)], [], now=600, store=[rec])
    assert r["status"] == "conflict" and r["review"] is True


def test_expired_approval_is_ignored_not_a_conflict():
    r = facts([approval(0, approved_amount=20), approval(2 * H, approved_amount=40)], now=2 * H + 600)
    assert r["status"] == "ok" and r["facts"]["approved_amount"] == 40
    assert any("@0" in s and "expired, ignored" in s for s in r["sources"])
    assert not any(f"@{2 * H}" in s and "expired" in s for s in r["sources"])


def test_expired_refusal_is_ignored():
    r = facts([approval(0, approved=False), approval(2 * H)], now=2 * H + 600)
    assert r["status"] == "ok"


def test_two_fresh_approvals_that_disagree_conflict():
    r = facts([approval(2 * H, approved_amount=20), approval(2 * H + 600, approved_amount=40)], now=2 * H + 1200)
    assert r["status"] == "conflict" and r["review"] is True


def test_only_expired_approvals_is_expired():
    r = facts([approval(0, approved_amount=20), approval(600, approved_amount=40)], now=600 + H + 1)
    assert r["status"] == "expired" and r["facts"] == {"status": "expired", "order_total": 40}
