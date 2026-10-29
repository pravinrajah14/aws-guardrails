# Integration log: `shreyas/demo-app`

Branch `shreyas/demo-app` starts from `origin/prav_work` at `973d340` (Pravin, 2026-10-29 12:34). That commit already contains Jack's converter, Camillia's Part 2, Shreyas's Part 3 solver question, and the `./policy` launcher. No directories were moved or renamed.

Sources:

- **prav_work** `973d340`: base of the branch. It also makes refunds match `output.approved_amount` in `converter/generated.dw`.
- **camillia/part4** `be2a6bb` (2026-10-28): Part 4 test lists, tasks, labeler and reports.
- **kylie/test-bed** `04ac584` (2026-10-29 11:59): newer test-bed files, uploaded at the repo root.

Conflict rule: Pravin's pipeline wiring, Kylie's newest agent behavior, and Camillia's tests.

## Files brought in or merged

All paths are under `kylie-test-bed-upload/` unless they start with `/`.

| File | Source | Commit | Action | Why |
|---|---|---|---|---|
| `tests/recent_approval_1h_actions.json` | camillia/part4 | be2a6bb | Kept as is | 17-case list for the 1h rule |
| `tests/recent_approval_24h_actions.json` | camillia/part4 | be2a6bb | Kept as is | 17-case list for the 24h rule |
| `tasks/camillia_tasks.json` | camillia/part4 | be2a6bb | Kept as is | Her agent tasks |
| `rules/refund_recent_approval_1h.dw`, `rules/refund_recent_approval_24h.dw` | camillia/part4 | be2a6bb | Kept as is | The hand-written rules her lists and scorecards were scored against. The 1h file is the same as `rules/refund_recent_approval.dw` |
| `fixes.json` | camillia/part4 | be2a6bb | Kept as is | Input for her labeler |
| `label_failures_reruns.py` | camillia/part4 (`label_failures.py`) | be2a6bb | **Renamed**, code unchanged | Different tool from Kylie's with the same name (see below). Renamed with Shreyas's approval |
| `results/failure_labels_camillia.md` | camillia/part4 (`results/failure_labels.md`) | be2a6bb | **Renamed** | Kylie's labeler writes `results/failure_labels.md` and would overwrite it |
| `results/agent_scorecard_20261028-*.{json,md}` (3 pairs), `results/rule_scorecard_20261028-*.{json,md}` (2 pairs), `results/failure_labels_20261028-082640.json` | camillia/part4 | be2a6bb | Kept as is | Her test and report files |
| `.provenance/` (465 files of run logs) | camillia/part4 | be2a6bb | **Dropped**, gitignored | Local run logs, as instructed. Her scorecards point to session names that only exist in these logs |
| `agent.py` | kylie/test-bed `/agent.py` + prav_work | 04ac584 + 973d340 | **3-way merge, no conflicts** | Kylie's behavior (prompt versions `PROMPTS`, `prompt`/`repair`/`nudge` switches). Pravin's shorter docstrings kept. No behavior is lost |
| `run_task.py` | kylie/test-bed `/run_task.py` + prav_work | 04ac584 + 973d340 | **3-way merge, 2 conflicts resolved as a union** | Pravin: `--output-dir`, `Recorder(root=...)`, sha256 of inputs in the scorecard (the pipeline checks it). Kylie: `--prompt`/`--no-repair`/`--no-nudge`, `expect_by_rule`, a hard exit when `--approval` conflicts with the task. Docstring from Pravin |
| `label_failures.py` | kylie/test-bed `/label_failures.py` | 04ac584 | Kept as is | Offline labeler that reads `.provenance/`. Matches her new agent switches and README |
| `README.md` | kylie/test-bed `/README.md` | 04ac584 | Replaced | Newer copy of this file: the same text plus her Architecture and Part 4 sections. Pravin had not changed it. The root `/README.md` is still Pravin's |
| `tasks/refund_tasks.json` | kylie/test-bed `/tasks/refund_tasks.json` | 04ac584 | Replaced | Same tasks, reformatted, plus `expect_by_rule` for `refund_large` (no $500 cap under the recent-approval rule) |
| `results/architecture.png` | kylie/test-bed `/results/architecture.png` | 04ac584 | Kept as is | Linked from her README |
| `results/failure_labels.md`, `results/failure_labels.json` | kylie/test-bed `/results/` | 04ac584 | Kept as is | Her labeler's report (rule 0, prompt 14, agent 1), linked from her README |
| `/results/agent_scorecard_20261026-*.md` (6 files) | kylie/test-bed | 04ac584 | **Dropped** | Not on the list to bring in. These are markdown only, with no json, and they point to sessions that are not in the repo. They remain on `origin/kylie/test-bed` |
| `/.gitignore` | new | | Added `.provenance/` and `.venv/` | `pipeline-runs/` was already ignored |
| `/pyproject.toml`, `/uv.lock`, `/.python-version` | new | | Added | uv project, Python 3.12, `z3-solver` from `question/requirements.txt` |

## Known issues left as they are

- **`label_failures_reruns.py` rerun mode is broken with the new agent.** It sets `agent.SYSTEM_PROMPT`, but `run_agent` now reads `PROMPTS[prompt]`, which is built when the module is imported, so the added fix line is ignored. Its `Namespace` also has no `prompt`/`no_repair`/`no_nudge`, so `run_task.run_one` raises `AttributeError`. `--check-only` still works. Its usage line still says `python label_failures.py`.
- **`run_task.py --approval X`** now exits when X differs from the task's approval mode (Kylie's change). It used to print a note and continue. `run_pipeline.py` never passes `--approval`, so the pipeline is not affected.
- **Committed `question/out/` is stale.** `question/out/answer_*/updated_rule.dw` still matches on `input.amount` (written before Pravin's converter change), and `answer_allow/summary.txt` shows 16/21. Fresh pipeline runs write to `pipeline-runs/` and match on `output.approved_amount`.

## Part 2: context engine (changes to teammates' files)

**Why the refund rule moved from exact match to "at most".** Pravin's rule matched `output.approved_amount: context.input.amount` inside the temporal clause, so a refund had to equal the approved amount exactly. Part 2 makes the approved amount a ceiling: a refund may be anything up to what the manager approved (approved 25, refund 20 is allowed). Dogwood's temporal pattern can only test equality, so the amount check moved out of it. The context engine (`kylie-test-bed-upload/context_engine.py`) reads the newest approval from the store and attaches `facts.approved_amount`. The rule then checks `context.input.amount <= context.facts.approved_amount` in a normal `when` clause. The `formerly within` clause stays (now only `order_id` and `approved: true`), so Dogwood still decides recency on its own. The rule also needs `facts.status == "ok"` and `facts.approver_role == "manager"` (from the rule sentence "a manager approved it"). With no facts, deny-by-default blocks.

Accepted consequence (Shreyas's decision, 2026-10-29): Camillia's `c15` (approved $200, refund $90, expected DENY) in both `tests/recent_approval_*_actions.json` is now a known wrong allow against her old expectation. Her files are unchanged.

| File | Owner | Change |
|---|---|---|
| `converter/convert.py` | Jack/Pravin | New `requires` fields `at_most` and `approver_role`, which emit the facts `when` clause. `load_schema` also reads each action's `facts` type. `output_same` still works for other rules |
| `converter/forms/manager_approval.json`, `question/forms/recently_1h.json`, `question/forms/recently_24h.json` | Jack/Shreyas | `output_same` replaced by `at_most: {amount: approved_amount}` and `approver_role: "manager"` |
| `converter/generated.dw` | generated | Regenerated; `dogwood validate` passes |
| `converter/test_behavior.py` | Pravin | Refund requests now carry the facts the engine would attach. "approval is exact, not a ceiling: block 90" became "approval is a ceiling: allow 90 of 100 approved" (same decision as c15). Added cases: support approver, unknown role, facts with no approval in the trace |
| `question/find_question.py`, `question/answer.py` | Shreyas | The solver models `at_most` and `approver_role`. Gates get the form so the engine knows the window |
| `kylie-test-bed-upload/policies/schema.cedarschema` | Kylie | `RefundFacts` type; `IssueRefund` context gets `facts?` |
| `kylie-test-bed-upload/tools.py` | Kylie | `STAFF` directory (`manager.jordan` manager, `support.sam` support); `MockWorld.staff` |
| `kylie-test-bed-upload/enforce.py` | Kylie/Pravin | `decide()` calls the engine and puts facts into `request_context`. It returns facts, status, sources and the review flag, and gives plain block messages. `record_result` stamps the store's approval records with Dogwood's clock. New `find_form()` |
| `kylie-test-bed-upload/agent.py` | Kylie | Records `facts`, `claim_ignored` and `needs_review` events. Gives the user's task to the engine as untrusted free text |
| `kylie-test-bed-upload/run_task.py`, `score_actions.py` | Kylie/Pravin | `--form` (default: `updated_form.json` next to the rule). The scorer builds store records from each case, scores `expect_status`, and counts wrong allows and wrong blocks |
| `kylie-test-bed-upload/label_failures.py` | Kylie | Oracle understands the facts rule. Tasks it does not model use the expected result recorded in the run |
| `run_pipeline.py` | Pravin | Hashes `context_engine.py` with the other code |

Hand-written rules in `kylie-test-bed-upload/rules/` have no form beside them, so the engine attaches nothing and their decisions are unchanged (rescored: 17/17, 17/17, 20/20).
