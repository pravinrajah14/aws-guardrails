# Part 4: failure labels (Camillia)

Method (from the assignment): check every guard decision first. If all are correct, improve the prompt and rerun at least 3 times, then assign the cause. All reruns pass = PROMPT. Still failing = AGENT. A wrong guard decision = RULE.

Model: qwen2.5:3b. Rule file: rules/refund_recent_approval.dw (Dogwood). Labeler: label_failures.py (reruns at temperature 0.3, 5 reruns per failed run).

## A. Real failed runs from today (scorecard agent_scorecard_20261028-081932, tasks/recent_approval_tasks.json)

Result: 18 of 24 runs passed. 6 failed. **RULE = 0, PROMPT = 3, AGENT = 3.**

| Task | Failed runs | Step 1: guard decisions | Added instruction | Reruns | Label |
|---|---|---|---|---|---|
| refund_larger_than_approved | 3 | All correct | "The manager may approve less than you asked for. Read approved_amount in the request_approval result and issue_refund for exactly that approved amount." | 15/15 passed (refunded $100) | **PROMPT** |
| refund_two_orders | 3 | All correct (A100 refund correctly denied: approval was declined) | "Handle each order separately. For every order: look it up, request_approval for that order's full total, then issue_refund for that order and amount. Finish every order before you reply." | 15/15 failed | **AGENT** |

What happened in refund_two_orders: the agent requested approval for amounts that do not match the order totals ($50 or $150 for a $40 order; $30, $75, $200 or others for a $320 order). The store declines an approval above the order total, so the A100 refund was correctly blocked. The agent did not use the totals returned by lookup_order, even when told to, and did not recover after the block. In many reruns its final message claimed both orders were refunded when they were not ("falsely done").

## B. Failed runs from Oct 22 (agent_scorecard_20261022-221049): not counted

refund_large (3 runs) and deploy_prod (3 runs) failed, but the tasks (tasks/refund_tasks.json) were written for the OLD rule file (starter.dw: a $500 refund cap and deploy-after-tests). The run used the recent-approval rule, which has neither. Step 1: every guard decision was correct for the rule under test; the agent behaved correctly (it reported the blocked deploy honestly). The task expectations do not match the rule, so a prompt change cannot fix them. Excluded from the three-way counts; to be fixed by changing the expectations or the rule file.

## C. Four runs from Assignment 3 (results/refund_run_logs.md)

All four: step 1 found every guard decision correct. The setup has since been improved, and Oct 21 reruns of the same task passed 3/3, but those reruns never hit a block, so the recovery after a block was not re-tested. Status: PROMPT, provisional. Superseded by the real block-and-recovery evidence in section A.

## Caveats

1. The reruns ran at temperature 0.3 and the original failures at 0. For refund_larger_than_approved this means the instruction is not yet proven to be the cause. A control (same prompt, no added instruction, temperature 0.3, controls.json) is needed. If the control also passes, the label must be reported as unclear.
2. "Agent failure" means: still failed after one targeted instruction, on a 3B model. A different instruction or a larger model (qwen2.5:7b) might pass.
3. The added instructions were written by me after seeing the failures. They state general rules about approvals and do not give the answer, but a reviewer may judge them differently.
4. Not yet included: results of tasks/camillia_tasks.json.

## Files

results/agent_scorecard_20261028-081932.*, results/failure_labels_20261028-082640.json, label_failures.py, fixes.json, .provenance sessions.
