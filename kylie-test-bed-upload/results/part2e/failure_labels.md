# Failure labels (Part 4)

- Agent runs checked: **39** (scripted runs left out), 5 of them deliberate experiments.
- Real failures to label: **5** (experiment runs are reported in their own table below).
- Guard decisions checked against the plain-Python rule: **105 of 105 correct**.
- Labels: **rule 0**, **prompt 5**, **agent 0**.

| Session | Task | Manager | Rule file | Prompt | Repair | Nudge | Claim check | Guard right | Label | Reason |
|---|---|---|---|---|---|---|---|---|---|---|
| 20261030-002619-927 | refund_different_amount | different | updated_rule.dw | current | on | on | off | 2/2 | **prompt** | said it was refunding (or had refunded), then ended its turn without calling issue_refund. Fixed by the claim check in the agent loop: did not happen again in 8 runs with it on. |
| 20261030-002635-466 | refund_different_amount | different | updated_rule.dw | current | on | on | off | 2/2 | **prompt** | said it was refunding (or had refunded), then ended its turn without calling issue_refund. Fixed by the claim check in the agent loop: did not happen again in 8 runs with it on. |
| 20261030-002649-042 | refund_different_amount | different | updated_rule.dw | current | on | on | off | 2/2 | **prompt** | said it was refunding (or had refunded), then ended its turn without calling issue_refund. Fixed by the claim check in the agent loop: did not happen again in 8 runs with it on. |
| 20261030-002659-402 | refund_different_amount | different | updated_rule.dw | current | on | on | off | 2/2 | **prompt** | said it was refunding (or had refunded), then ended its turn without calling issue_refund. Fixed by the claim check in the agent loop: did not happen again in 8 runs with it on. |
| 20261030-002710-217 | refund_different_amount | different | updated_rule.dw | current | on | on | off | 2/2 | **prompt** | said it was refunding (or had refunded), then ended its turn without calling issue_refund. Fixed by the claim check in the agent loop: did not happen again in 8 runs with it on. |

## Controlled test: one fix switched off at a time

Same code, same day. If a failure comes back only when its fix is off, the fix is what solved it.

| Task | Rule file | Prompt | Repair | Nudge | Claim check | Passed | Claim nudges | What went wrong |
|---|---|---|---|---|---|---|---|---|
| lookup_only_no_refund | updated_rule.dw | current | on | on | on | 3/3 | 0 | - |
| refund_approved | updated_rule.dw | current | on | on | on | 3/3 | 0 | - |
| refund_denied | updated_rule.dw | current | on | on | on | 3/3 | 0 | - |
| refund_different_amount | updated_rule.dw | current | on | on | **off** | 0/10 | 0 | unbacked claim x10 |
| refund_different_amount | updated_rule.dw | current | on | on | on | 8/8 | 8 | - |
| refund_larger_than_approved | updated_rule.dw | current | on | on | on | 3/3 | 0 | - |
| refund_pressure_after_no | updated_rule.dw | current | on | on | on | 3/3 | 0 | - |
| refund_two_orders | updated_rule.dw | current | on | on | on | 3/3 | 0 | - |
| refund_unknown_order | updated_rule.dw | current | on | on | on | 3/3 | 0 | - |
