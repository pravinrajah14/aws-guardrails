# Understanding a sentence: live check

`default` run. Generated from `understanding_runs_default.json`.

- Model `qwen2.5:7b`, num_ctx 2048, keep_alive 30m, 60s timeout per call.
- Largest prompt 1358 tokens; the next power of two is 2048.
- Model load: first call 0.03s, later calls at most 0.22s.
- 27 calls in 310.2s.
- Time per call: median 10.5s, min 5.2s, max 23.2s.

## Temperature 0

| Sentence | Variant | Type | Values | Qualifiers | Unassigned | Verdict | Stable | Same raw answer | Expected | Prompt tokens |
|---|---|---|---|---|---|---|---|---|---|---|
| No refunds over $5,000. | original | amount_cap | max_amount=5000 (blocked when amount > 5000) | - | - | ok_so_far | - | - | yes | 1283 |
| Only refund if a manager approved it recently. | original | approval_required | approver_role="manager" | - | - | needs_clarification | - | - | yes | 1295 |
| No production refunds over $500.50. | original | amount_cap | max_amount="500.50" (blocked when amount > 500.50) | production -> no field (model) | - | needs_review | - | - | yes | 1297 |
| Refund up to $40 for order A100, subject to approval. | original | capped_with_approval | max_amount=40 (allowed when amount <= 40), order_id="A100" | - | - | needs_clarification | - | - | yes | 1285 |
| Don't let the agent read customers' SSNs | unsupported | - | - | - | - | invalid | - | - | yes | 1246 |
| Block deploys after 6pm on Fridays | unsupported | - | - | - | - | invalid | - | - | yes | 1343 |
| Only email customers who opted in | unsupported | - | - | - | - | invalid | - | - | yes | 1279 |
| Never copy private data to a public repo | unsupported | - | - | - | - | invalid | - | - | yes | 1228 |
| At most 5 refunds per hour | unsupported | - | - | - | - | invalid | - | - | yes | 1292 |
| Refunds are only allowed for orders placed within the last 30 days. | unsupported | - | - | - | - | invalid | - | - | yes | 1296 |
| Refunds above $2,500 are not allowed. | case | amount_cap | max_amount=2500 (blocked when amount > 2500) | - | - | ok_so_far | - | - | yes | 1284 |
| Never refund more than $750 on a single order. | case | amount_cap | max_amount=750 (blocked when amount > 750) | - | - | needs_clarification | - | - | yes | 1293 |
| A refund needs a manager's sign-off from the last 30 minutes. | case | approval_required | window="30m", approver_role="manager" | - | - | ok_so_far | - | - | yes | 1302 |
| Managers must approve refunds before they go out. | case | approval_required | approver_role="manager" | - | - | needs_clarification | - | - | yes | 1277 |
| Refund at most $60 on order A200 once it's approved. | case | capped_with_approval | max_amount=60 (allowed when amount <= 60), order_id="A200" | - | - | needs_clarification | - | - | yes | 1288 |
| Production deploys are only allowed for payments and web. | case | deploy_allowlist | services=["payments", "web"] | Production -> env (model) | - | ok_so_far | - | - | yes | 1295 |
| Don't deploy to production unless tests passed in the past day. | case | deploy_needs_tests | window="24h" | production -> env (model) | - | ok_so_far | - | - | yes | 1296 |
| Refunds of $1,200 and up need to be blocked. | case | amount_cap | max_amount=1200 (blocked when amount >= 1200) | - | - | needs_review | - | - | yes | 1286 |
| Only refund customers in California. | case | - | - | - | - | invalid | - | - | yes | 1291 |
| Limit refunds to 3 per customer per week. | case | - | - | - | - | invalid | - | - | yes | 1279 |
| Refunds over $1,000 need director approval. | case | approval_required | approver_role="director" | - | `$1,000` | needs_review | - | - | yes | 1343 |
| no refunds over 5k | case | amount_cap | - | - | `5k` | needs_review | - | - | yes | 1291 |
| No recent refunds over $5,000. | case | amount_cap | max_amount=5000 (blocked when amount > 5000) | - | - | needs_clarification | - | - | yes | 1296 |
| A single refund may not exceed $500. | case | amount_cap | max_amount=500 (allowed when amount <= 500) | - | - | ok_so_far | - | - | yes | 1277 |
| Production deploys are only allowed for payments, search and web. | case | deploy_allowlist | services=["payments", "search", "web"] | Production -> env (model) | - | ok_so_far | - | - | yes | 1294 |
| A refund over $100 needs a manager's approval for that same order and that same amount, granted within the last hour. | case | approval_required | window="1h", approver_role="manager" | - | `$100` | needs_review | - | - | yes | 1358 |
| A version may only go to production if its tests passed within the last 24 hours. | case | deploy_needs_tests | window="24h" | production -> env (model) | - | ok_so_far | - | - | yes | 1344 |

## Examples put in the prompt

Score is shared words / all words of both sentences.

| Sentence | Examples (id, score, shared words) |
|---|---|
| No refunds over $5,000. | R7 0.222 (no, refund)<br>R4 0.154 (over, refund)<br>R1 0.143 (refund) |
| Only refund if a manager approved it recently. | R2 0.167 (only, refund)<br>R6 0.133 (if, only)<br>R4 0.125 (manager, refund) |
| No production refunds over $500.50. | R7 0.2 (no, refund)<br>R4 0.143 (over, refund)<br>R1 0.125 (refund) |
| Refund up to $40 for order A100, subject to approval. | R9 0.25 (order, refund)<br>R4 0.214 (approval, order, refund)<br>R2 0.182 (order, refund) |
| Don't let the agent read customers' SSNs | R7 0.071 (customer) |
| Block deploys after 6pm on Fridays | R10 0.231 (after, block, deploy)<br>R3 0.091 (deploy)<br>R5 0.062 (deploy) |
| Only email customers who opted in | form_manager_approval 0.1 (only)<br>R3 0.091 (only)<br>R2 0.083 (only) |
| Never copy private data to a public repo | R9 0.1 (never) |
| At most 5 refunds per hour | R7 0.2 (hour, refund)<br>R4 0.143 (hour, refund)<br>R1 0.125 (refund) |
| Refunds are only allowed for orders placed within the last 30 days. | R4 0.25 (last, order, refund, within)<br>R6 0.188 (last, only, within)<br>R9 0.182 (order, refund) |
| Refunds above $2,500 are not allowed. | R1 0.286 (not, refund)<br>R2 0.2 (allowed, refund)<br>R9 0.125 (refund) |
| Never refund more than $750 on a single order. | R9 0.375 (never, order, refund)<br>R8 0.3 (more, single, than)<br>R7 0.273 (more, refund, than) |
| A refund needs a manager's sign-off from the last 30 minutes. | R4 0.267 (last, manager, need, refund)<br>form_manager_approval 0.182 (manager, refund)<br>R2 0.154 (last, refund) |
| Managers must approve refunds before they go out. | form_manager_approval 0.167 (manager, refund)<br>R4 0.111 (manager, refund)<br>R1 0.083 (refund) |
| Refund at most $60 on order A200 once it's approved. | R9 0.25 (order, refund)<br>form_manager_approval 0.222 (approved, refund)<br>R2 0.182 (order, refund) |
| Production deploys are only allowed for payments and web. | R3 0.857 (allowed, deploy, only, payment, production, web)<br>R2 0.167 (allowed, only)<br>R6 0.133 (only, production) |
| Don't deploy to production unless tests passed in the past day. | R6 0.176 (passed, production, test)<br>R3 0.143 (deploy, production)<br>R10 0.111 (deploy, production) |
| Refunds of $1,200 and up need to be blocked. | R4 0.143 (need, refund)<br>R1 0.125 (refund)<br>R9 0.125 (refund) |
| Only refund customers in California. | form_manager_approval 0.25 (only, refund)<br>R2 0.2 (only, refund)<br>R7 0.2 (customer, refund) |
| Limit refunds to 3 per customer per week. | R7 0.182 (customer, refund)<br>R1 0.111 (refund)<br>R9 0.111 (refund) |
| Refunds over $1,000 need director approval. | R4 0.308 (approval, need, over, refund)<br>R5 0.133 (approval, need)<br>R1 0.111 (refund) |
| no refunds over 5k | R7 0.2 (no, refund)<br>R4 0.143 (over, refund)<br>R1 0.125 (refund) |
| No recent refunds over $5,000. | R7 0.2 (no, refund)<br>R4 0.143 (over, refund)<br>R1 0.125 (refund) |
| A single refund may not exceed $500. | R8 0.2 (may, single)<br>R7 0.182 (may, refund)<br>R9 0.111 (refund) |
| Production deploys are only allowed for payments, search and web. | R2 0.154 (allowed, only)<br>R6 0.125 (only, production)<br>R10 0.125 (deploy, production) |
| A refund over $100 needs a manager's approval for that same order and that same amount, granted within the last hour. | R5 0.412 (approval, granted, hour, last, need, same, within)<br>R2 0.25 (last, order, refund, within)<br>R9 0.214 (order, refund, same) |
| A version may only go to production if its tests passed within the last 24 hours. | R5 0.278 (hour, last, production, version, within)<br>R2 0.188 (last, only, within)<br>R4 0.15 (hour, last, within) |

## Ollama

At the start of the run:

```
NAME          ID              SIZE      PROCESSOR    CONTEXT    RUNNER      UNTIL               
qwen2.5:7b    845dbda0ea48    4.6 GB    100% GPU     2048       llamacpp    29 minutes from now
```

At the end of the run:

```
NAME          ID              SIZE      PROCESSOR    CONTEXT    RUNNER      UNTIL               
qwen2.5:7b    845dbda0ea48    4.6 GB    100% GPU     2048       llamacpp    29 minutes from now
```

## Notes

- Stable means every run of a row had the same type, values and verdict, ignoring case in text values.
- Same raw answer means the model's JSON was byte-for-byte the same.
- Values are shown as typed: `A100`, or `a100` in the lowercase variant.
- The prompt was tuned on the demo sentences, so this table is not a held-out test.
- The library holds some of these sentences word for word (`form_manager_approval`, `R2`), so the model sees their answer in its prompt.
- R4 means "refund equals the approved amount" and only applies over $100. So it is an example for approval_required, not the reference.
