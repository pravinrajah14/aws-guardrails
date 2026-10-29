# Baseline, no agent (Part 1)

This is the regression bar. Every decision that is right here must stay right in later parts.

- Date: 2026-10-29
- Commit measured: `46c118b` on `shreyas/demo-app`
- Dogwood: `dogwood 1.1.0` (`~/.cargo/bin/dogwood`)
- Python 3.12.13 (uv 0.13.6, z3-solver 5.3.3.0)
- Scorer: `kylie-test-bed-upload/score_actions.py`, base permissions `policies/base.dw` (sha256 `ec55044e02d1...`)

## Rule files used

Both come from the pipeline (`./policy create`), so they are the converter's output, not hand-written files.

| Rule | Made by | File | sha256 |
|---|---|---|---|
| 1 hour | `./policy create --answer block` | `pipeline-runs/20261029-211404-604312/selected/updated_rule.dw` | `5e2663051c84...` |
| 24 hours | `./policy create --answer allow` | `pipeline-runs/20261029-211403-371778/selected/updated_rule.dw` | `4f83df50487a...` |

Both match on `output.approved_amount: context.input.amount` (Pravin's change). `pipeline-runs/` is not committed, so the 1h rule text is copied here:

```
@id("refund_needs_manager_approval")
permit (principal, action == Shop::Action::"IssueRefund", resource)
when temporal {
    formerly within 1h Shop::Action::"RequestApproval"::response{
        input.order_id: context.input.order_id,
        output.approved: true,
        output.approved_amount: context.input.amount
    }
};
```

The 24h rule is the same with `within 24h`.

## Results

| Action list | Cases | Rule | Right | Wrong allows (unsafe) | Wrong blocks (annoying) |
|---|---|---|---|---|---|
| `tests/recent_approval_1h_actions.json` (Camillia) | 17 | 1 hour | **17 / 17** | 0 | 0 |
| `tests/recent_approval_24h_actions.json` (Camillia) | 17 | 24 hours | **17 / 17** | 0 | 0 |
| `tests/refund_recent_approval_actions.json` | 18 (20 scored decisions) | 1 hour | **20 / 20** | 0 | 0 |
| **Total** | | | **54 / 54** | **0** | **0** |

### Mismatches

None.

## Checks on the baseline itself

- The hand-written rules give the same results: `rules/refund_recent_approval_1h.dw` 17/17, `rules/refund_recent_approval_24h.dw` 17/17, `rules/refund_recent_approval.dw` 20/20.
- The scorer does catch errors. Scoring the 1h list against the 24h rule gives 13/17, with 4 wrong allows (c02, c03, c06, c17). So 17/17 is a real result, not an empty pass.
- The pipeline's own scripted check (`selected/rule_scorecard.json`): the 1h run gets 21/21. The 24h run gets 18/21. The pipeline rescores the 24h choice against the 1h list, so `approved_61m_ago`, `approved_3h_ago` and `approved_23h_ago` come out DENY expected, ALLOW got. Its `summary.txt` says these cases depend on how "recently" is read. That is how the pipeline is designed, not a rule error, so it is not part of the bar above.

## How to reproduce

```
./policy create --answer allow        # 24h rule
./policy create --answer block        # 1h rule (run last; ./policy test uses the latest)
cd kylie-test-bed-upload
uv run python score_actions.py tests/recent_approval_1h_actions.json  --rules <1h updated_rule.dw>  --output-dir <dir>
uv run python score_actions.py tests/recent_approval_24h_actions.json --rules <24h updated_rule.dw> --output-dir <dir>
uv run python score_actions.py tests/refund_recent_approval_actions.json --rules <1h updated_rule.dw> --output-dir <dir>
```
