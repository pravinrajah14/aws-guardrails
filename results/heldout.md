# Held-out sentences

One run each at temperature 0. Generated from `heldout_runs.json` by `uv run python -m app.core.report --heldout`.
Nothing was changed because of these results.

- Model `qwen2.5:7b`, num_ctx 2048, keep_alive 2m, 60s timeout per call.
- Largest prompt 1343 tokens; the next power of two is 2048.
- 13 calls in 140.1s.
- Time per call: median 11.2s, min 6.5s, max 15.1s.

| # | Sentence | Expected | Got | Verdict | Match |
|---|---|---|---|---|---|
| 1 | Refunds above $2,500 are not allowed. | amount_cap, blocked when > 2500 | amount_cap; max_amount=2500 (blocked when amount > 2500) | ok_so_far | yes |
| 2 | Never refund more than $750 on a single order. | amount_cap, blocked when > 750 | amount_cap; max_amount=750 (blocked when amount > 750); qualifiers: on a single order -> order_id (model) | ok_so_far | yes |
| 3 | A refund needs a manager's sign-off from the last 30 minutes. | approval_required, manager, 30 minutes | approval_required; window="30m", approver_role="manager" | ok_so_far | yes |
| 4 | Managers must approve refunds before they go out. | approval_required, manager, window missing | approval_required; approver_role="manager"; vague: before they go out | needs_clarification | yes |
| 5 | Refund at most $60 on order A200 once it's approved. | capped_with_approval, allowed when <= 60, A200 | capped_with_approval; max_amount=60 (allowed when amount <= 60), order_id="A200" | needs_clarification | yes |
| 6 | Production deploys are only allowed for payments and web. | deploy_allowlist, payments and web, production | deploy_allowlist; -; qualifiers: Production -> env (model) | needs_clarification | NO |
| 7 | Don't deploy to production unless tests passed in the past day. | deploy_needs_tests, 1 day, production | deploy_needs_tests; -; qualifiers: production -> env (model); vague: in the past day | needs_clarification | NO |
| 8 | Refunds of $1,200 and up need to be blocked. | amount_cap, blocked when >= 1200, or needs review; never strict | refused; - | invalid | NO |
| 9 | Only refund customers in California. | refused | refused; - | invalid | yes |
| 10 | Limit refunds to 3 per customer per week. | refused | refused; - | invalid | yes |
| 11 | Refunds over $1,000 need director approval. | report what it does (a threshold, plus approval, plus a role the store doesn't have) | approval_required; approver_role="director"; unassigned: `$1,000` | needs_review | report |
| 12 | no refunds over 5k | amount_cap at 5000, or needs review; never another number | amount_cap; -; vague: 5k; unassigned: `5k` | needs_review | yes |
| 13 | No recent refunds over $5,000. | report what it does (this is the brief's own example) | amount_cap; max_amount=5000 (blocked when amount > 5000); vague: recent | needs_clarification | report |
