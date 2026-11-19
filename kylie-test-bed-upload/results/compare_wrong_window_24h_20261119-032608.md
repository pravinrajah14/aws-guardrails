# Rule comparison: wrong_window_24h (20261119-032608)

- Correct rule: `rules/refund_recent_approval.dw`  |  Candidate rule: `rules/practice/wrong_window_24h.dw`
- Scenarios: `tests/refund_recent_approval_actions.json` (9 cases, 10 decisions)
- Candidate passes dogwood validate: yes
- Wrong allows: **2**  |  Wrong blocks: **0**  |  Errors: 0
- **Result: FAIL**

| Case | Time | Correct rule | Tool's rule | Result |
|---|---|---|---|---|
| approved_30m_ago | @1801 | ALLOW | ALLOW | same |
| approved_59m_ago | @3541 | ALLOW | ALLOW | same |
| approved_61m_ago | @3661 | DENY | ALLOW | **WRONG ALLOW** |
| approved_3h_ago | @10801 | DENY | ALLOW | **WRONG ALLOW** |
| never_approved | @1 | DENY | DENY | same |
| manager_said_no | @601 | DENY | DENY | same |
| approved_other_order | @601 | DENY | DENY | same |
| approved_smaller_amount | @601 | DENY | DENY | same |
| approved_smaller_amount | @661 | ALLOW | ALLOW | same |
| lookup_always_allowed | @1 | ALLOW | ALLOW | same |
