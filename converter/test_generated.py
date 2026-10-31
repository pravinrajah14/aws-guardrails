"""generated.dw is checked in, so it must stay what convert.py writes from the forms."""

import json
from pathlib import Path

from converter.convert import convert

HERE = Path(__file__).parent
SCHEMA = HERE.parent / "kylie-test-bed-upload" / "policies" / "schema.cedarschema"
FORM_ORDER = ["manager_approval", "prod_allowlist", "prod_needs_tests", "refund_cap"]


def test_convert_reproduces_generated_policy():
    forms = [(name, json.loads((HERE / "forms" / f"{name}.json").read_text())) for name in FORM_ORDER]
    assert convert(forms, SCHEMA, baseline=True) == (HERE / "generated.dw").read_text()
