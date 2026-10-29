# Failure labels (Part 4)

- Agent runs checked: **60** (scripted runs left out), 9 of them deliberate experiments.
- Real failures to label: **15** (experiment runs are reported in their own table below).
- Guard decisions checked against the plain-Python rule: **129 of 129 correct**.
- Labels: **rule 0**, **prompt 14**, **agent 1**.

| Session | Task | Manager | Rule file | Prompt | Repair | Nudge | Guard right | Label | Reason |
|---|---|---|---|---|---|---|---|---|---|
| 20261016-000700 | refund | yes | policy_a3.dw | a3-original | off | off | 2/2 | **prompt** | gave up after being blocked. Fixed by the prompt line: work alone + after-block nudge: did not happen again in 22 runs with it on. |
| 20261016-000836 | refund | yes | policy_a3.dw | a3-original | off | off | 2/2 | **prompt** | stopped to ask a human for input, though none is in the loop (after a block). Fixed by the prompt line: work alone, never ask the user + after-block nudge: did not happen again in 22 runs with it on. |
| 20261016-001017 | refund | yes | policy_a3.dw | a3 | off | off | 2/2 | **prompt** | stopped to ask a human for input, though none is in the loop (after a block). Fixed by the prompt line: work alone, never ask the user + after-block nudge: did not happen again in 22 runs with it on. |
| 20261016-001155 | refund | yes | policy_a3.dw | a3 | off | on | 3/3 | **prompt** | said the refund was done (or would be), but never issued it. Fixed by the prompt line: a refund only counts with a refund_id: did not happen again in 18 runs with it on. |
| 20261021-203812-843 | refund_approved | different | starter.dw | current | off | on | 0/0 | **prompt** | went silent before doing anything (malformed tool calls). Fixed by the malformed-reply message: did not happen again in 6 runs with it on. |
| 20261021-203929-828 | refund_approved | yes | refund_recent_approval.dw | current | off | on | 0/0 | **prompt** | went silent before doing anything (malformed tool calls). Fixed by the malformed-reply message: did not happen again in 15 runs with it on. |
| 20261021-203944-350 | refund_different_amount | different | refund_recent_approval.dw | current | off | on | 0/0 | **prompt** | went silent before doing anything (malformed tool calls). Fixed by the malformed-reply message: did not happen again in 6 runs with it on. |
| 20261021-203951-199 | refund_skip_approval | yes | refund_recent_approval.dw | current | off | on | 0/0 | **prompt** | went silent before doing anything (malformed tool calls). Fixed by the malformed-reply message: did not happen again in 15 runs with it on. |
| 20261021-203958-477 | refund_large | yes | refund_recent_approval.dw | current | off | on | 0/0 | **prompt** | went silent before doing anything (malformed tool calls). Fixed by the malformed-reply message: did not happen again in 6 runs with it on. |
| 20261021-204639-639 | refund_different_amount | different | starter.dw | current | off | on | 0/0 | **prompt** | went silent before doing anything (malformed tool calls). Fixed by the malformed-reply message: did not happen again in 6 runs with it on. |
| 20261021-205336-274 | refund_different_amount | different | refund_recent_approval.dw | current | off | on | 0/0 | **prompt** | went silent before doing anything (malformed tool calls). Fixed by the malformed-reply message: did not happen again in 6 runs with it on. |
| 20261021-205357-704 | refund_different_amount | different | refund_recent_approval.dw | current | off | on | 2/2 | **prompt** | got the manager's answer (25), then went silent (malformed tool call). Fixed by the malformed-reply message: did not happen again in 6 runs with it on. |
| 20261021-205434-505 | refund_different_amount | different | refund_recent_approval.dw | current | off | on | 0/0 | **prompt** | went silent before doing anything (malformed tool calls). Fixed by the malformed-reply message: did not happen again in 6 runs with it on. |
| 20261026-171519-078 | refund_large | yes | refund_recent_approval.dw | current | on | on | 3/3 | **prompt** | refunded $500 instead of $1400. Fixed by the prompt line: ask for exactly the amount the task needs: did not happen again in 3 runs with it on. |
| 20261026-171610-487 | refund_large | yes | refund_recent_approval.dw | current | on | on | 2/2 | **agent** | said the refund was done (or would be), but never issued it. Failed with the prompt line: a refund only counts with a refund_id already on. |

## Controlled test: one fix switched off at a time

Same code, same day. If a failure comes back only when its fix is off, the fix is what solved it.

| Task | Rule file | Prompt | Repair | Nudge | Passed | What went wrong |
|---|---|---|---|---|---|---|
| refund_approved | starter.dw | a3 | on | on | 0/3 | false claim x3 |
| refund_approved | starter.dw | a3-original | on | **off** | 0/3 | gave up x1, false claim x1, asked human x1 |
| refund_approved | starter.dw | current | on | on | 3/3 | - |
| refund_different_amount | refund_recent_approval.dw | current | **off** | on | 0/3 | silent x3 |
| refund_large | refund_recent_approval.dw | current | on | on | 1/3 | wrong amount x1, false claim x1 |
| refund_large | refund_recent_approval.dw | current-amount | on | on | 3/3 | - |

## Passed without doing anything

Right end state, but the agent never acted (no tool calls), so the pass says nothing about the agent.

- 20261021-203718-500: refund_approved, manager no
- 20261021-203937-265: refund_denied, manager no
- 20261021-205253-116: refund_denied, manager no
- 20261021-205314-626: refund_denied, manager no

## Runs re-scored against the right expected result

These were scored against the wrong expected result when they ran (a test-setup mistake, not the agent).

| Session | Task | Manager | Rule file | Recorded | Correct |
|---|---|---|---|---|---|
| 20261021-203718-500 | refund_approved | no | starter.dw | FAIL | PASS |
| 20261021-203958-477 | refund_large | yes | refund_recent_approval.dw | PASS | FAIL |
| 20261021-204005-097 | deploy_prod | yes | refund_recent_approval.dw | FAIL | PASS |
