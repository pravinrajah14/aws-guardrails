# Planted-error check for Dogwood policies (Assignment #3, checking tool)

Shows that policies with errors in meaning still pass Dogwood's own validator,
and that a short event history exposes each error.

## Contents

- `schema.cedarschema` - action schema for the refund and deploy tools
  (IssueRefund, RequestApproval, RunTests, Deploy).
- `build.py` - writes 3 intended policies and 13 planted-error variants to
  `policies/`, runs `dogwood validate` on each, then replays one hand-built
  trace per variant (`traces/`) through both the intended policy and the
  variant and compares the final verdict.
- `sanity.py` - happy-path traces showing the intended policies allow
  legitimate work (so the DENY results are not from a deny-everything policy).
- `recently/` - two readings of "Don't deploy to production if the tests
  failed recently" and two traces where they disagree.
- `z3_find_disagreement.py` - proof of concept for the offline checker: Z3
  searches for a short history where intended and buggy P1 disagree, and each
  history it finds is replayed through Dogwood to confirm.

## Run

```
cargo install dogwood-cli            # or build github.com/dogwood-policy/dogwood
export DOGWOOD=$(which dogwood)
pip install z3-solver
python3 build.py
python3 sanity.py
python3 z3_find_disagreement.py
dogwood replay recently/reading_A_window.dw --policy-schema schema.cedarschema --trace recently/trace_fail_then_pass.log
```

Built and run against dogwood-cli v1.1.0 (main branch, Oct 15 2026).
