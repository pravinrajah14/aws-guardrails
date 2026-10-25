# Rule scorecard, no agent (20261025-215617)

- Rule file: `/home/claude/aws-guardrails/question/out/answer_block/updated_rule.dw`  |  Action lists: `/home/claude/aws-guardrails/question/out/answer_block/actions_with_answer.json`
- **19 of 21 decisions correct.** Any wrong decision is the rule's fault.

| Case | Call | Expected | Got | Rule(s) | Correct |
|---|---|---|---|---|---|
| approved_just_now | `issue_refund({'order_id': 'A100', 'amount': 40})` | ALLOW | ALLOW | refund_needs_manager_approval | yes |
| approved_30m_ago | `issue_refund({'order_id': 'A100', 'amount': 40})` | ALLOW | ALLOW | refund_needs_manager_approval | yes |
| approved_59m_ago | `issue_refund({'order_id': 'A100', 'amount': 40})` | ALLOW | ALLOW | refund_needs_manager_approval | yes |
| approved_exactly_60m_ago | `issue_refund({'order_id': 'A100', 'amount': 40})` | ALLOW | ALLOW | refund_needs_manager_approval | yes |
| approved_61m_ago | `issue_refund({'order_id': 'A100', 'amount': 40})` | DENY | DENY | - | yes |
| approved_3h_ago | `issue_refund({'order_id': 'A100', 'amount': 40})` | DENY | DENY | - | yes |
| approved_23h_ago | `issue_refund({'order_id': 'A100', 'amount': 40})` | DENY | DENY | - | yes |
| stale_then_fresh_approval | `issue_refund({'order_id': 'A100', 'amount': 40})` | ALLOW | ALLOW | refund_needs_manager_approval | yes |
| never_approved | `issue_refund({'order_id': 'A100', 'amount': 40})` | DENY | DENY | - | yes |
| manager_said_no | `issue_refund({'order_id': 'A100', 'amount': 40})` | DENY | DENY | - | yes |
| approved_other_order | `issue_refund({'order_id': 'A100', 'amount': 40})` | DENY | DENY | - | yes |
| two_orders_approved_refund_each | `issue_refund({'order_id': 'A100', 'amount': 40})` | ALLOW | ALLOW | refund_needs_manager_approval | yes |
| two_orders_approved_refund_each | `issue_refund({'order_id': 'A200', 'amount': 320})` | ALLOW | ALLOW | refund_needs_manager_approval | yes |
| approved_smaller_amount | `issue_refund({'order_id': 'A100', 'amount': 40})` | DENY | ALLOW | refund_needs_manager_approval | **NO** |
| approved_smaller_amount | `issue_refund({'order_id': 'A100', 'amount': 20})` | ALLOW | DENY | - | **NO** |
| approved_larger_order_full_amount | `issue_refund({'order_id': 'A200', 'amount': 320})` | ALLOW | ALLOW | refund_needs_manager_approval | yes |
| approval_request_errored | `issue_refund({'order_id': 'A100', 'amount': 40})` | DENY | DENY | - | yes |
| lookup_always_allowed | `lookup_order({'order_id': 'A100'})` | ALLOW | ALLOW | allow_lookup | yes |
| asking_for_approval_always_allowed | `request_approval({'order_id': 'A100', 'amount': 40, 'reason': 'damaged'})` | ALLOW | ALLOW | allow_approval_requests | yes |
| deploy_blocked_by_default | `deploy_service({'service': 'checkout', 'version': '1.5.0', 'env': 'prod'})` | DENY | DENY | - | yes |
| solver_question_1 | `issue_refund({'order_id': 'A100', 'amount': 40})` | DENY | DENY | - | yes |
