"""Unit tests for the pretend manager in tools.py.

  uv run pytest kylie-test-bed-upload/tests
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import MockWorld  # noqa: E402


def test_manager_denies_second_40_approval_on_40_order():
    world = MockWorld(approval="yes")
    first = world.request_approval("A100", 40, "damaged on arrival")
    assert first["approved"] is True and first["approved_amount"] == 40
    world.issue_refund("A100", 40)
    second = world.request_approval("A100", 40, "damaged on arrival")
    assert second["approved"] is False and second["approved_amount"] == 0
    assert "40" in second["reason"] and "already refunded" in second["reason"]


def test_manager_approves_what_is_left_on_the_order():
    world = MockWorld(approval="yes")
    world.request_approval("A100", 25, "damaged on arrival")
    world.issue_refund("A100", 25)
    assert world.request_approval("A100", 15, "rest of it")["approved"] is True
    assert world.request_approval("A100", 16, "one too many")["approved"] is False


def test_manager_counts_only_this_orders_refunds():
    world = MockWorld(approval="yes")
    world.request_approval("A200", 320, "broken")
    world.issue_refund("A200", 320)
    assert world.request_approval("A100", 40, "damaged on arrival")["approved"] is True
