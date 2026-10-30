from decimal import Decimal

import pytest

from app.core import config, rule_types
from app.core.numbers import amount_json, find_amounts, find_durations, window
from converter.convert import convert, load_schema, validate_form

SAMPLE = {"max_amount": 40, "order_id": "A100", "window": "1h", "approver_role": "manager",
          "services": ["payments", "web"]}


def test_supported_list():
    assert rule_types.supported_ids() == [
        "amount_cap", "approval_required", "capped_with_approval", "deploy_allowlist", "deploy_needs_tests"]


def test_rule_types_match_schema():
    _, actions = load_schema(config.SCHEMA_PATH)
    assert rule_types.schema_problems(actions) == []


@pytest.mark.parametrize("type_id", rule_types.supported_ids())
def test_templates_pass_the_converter(type_id, tmp_path):
    _, actions = load_schema(config.SCHEMA_PATH)
    t = rule_types.BY_ID[type_id]
    forms = []
    for i, template in enumerate(t["forms"]):
        form = rule_types.fill(template, SAMPLE)
        form["id"] = f"{type_id}_{i}"
        validate_form(form, actions)
        forms.append((form["id"], form))
    assert "@id" in convert(forms, config.SCHEMA_PATH)


def test_missing_optional_slot_drops_key():
    form = rule_types.fill(rule_types.BY_ID["approval_required"]["forms"][0], {"window": "1h"})
    assert "approver_role" not in form["requires"]


def test_refusal_names_supported_types():
    text = rule_types.refusal_text()
    for type_id in rule_types.supported_ids():
        assert type_id in text


@pytest.mark.parametrize("text,value", [
    ("$5,000", Decimal("5000")), ("$40", Decimal("40")), ("5,000 dollars", Decimal("5000")),
    ("$500.50", Decimal("500.50")), ("$ 12", Decimal("12")), ("$1,234,567.89", Decimal("1234567.89")),
])
def test_amounts(text, value):
    [a] = find_amounts(text)
    assert a["value"] == value and a["text"] == text


def test_amounts_keep_cents():
    assert amount_json(Decimal("500.50")) == "500.50"
    assert amount_json(Decimal("5000")) == 5000
    assert amount_json(Decimal("40.00")) == 40


@pytest.mark.parametrize("text", ["5 refunds", "$5k", "$5,00", "order A100", "6pm"])
def test_not_amounts(text):
    assert find_amounts(text) == []


def test_amount_offsets():
    s = "No refunds over $5,000."
    [a] = find_amounts(s)
    assert s[a["start"]:a["end"]] == "$5,000"


@pytest.mark.parametrize("text,seconds,win", [
    ("1 hour", 3600, "1h"), ("30 minutes", 1800, "30m"), ("24h", 86400, "24h"),
    ("2 days", 172800, "48h"), ("an hour", 3600, "1h"), ("1.5 hours", 5400, "90m"),
    ("45 secs", 45, "45s"), ("24-hour", 86400, "24h"),
])
def test_durations(text, seconds, win):
    [d] = find_durations(text)
    assert d["seconds"] == seconds and window(d["seconds"]) == win


@pytest.mark.parametrize("text", ["recently", "per hour", "6pm on Fridays", "a manager", "3 months"])
def test_not_durations(text):
    assert find_durations(text) == []
