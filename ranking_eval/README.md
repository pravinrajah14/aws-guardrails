# Ranking evaluation (the rule quiz)

Tests different ways of ranking the example library, as set out in the proposal *Structure-Based Ranking for Company Context*. Every case in `cases.json` goes through the real tool (`resolve`, then generate, validate and replay) under each ranking. Each one is graded on five checks, in two modes.

```
uv run python -m ranking_eval.quiz --smoke                         one case of each kind, every ranking (plumbing check)
uv run python -m ranking_eval.quiz --pass evidence                 pass 1: every ranking in the evidence score
uv run python -m ranking_eval.quiz --pass example --evidence bm25  pass 2: the example score, best evidence ranking fixed
uv run python -m ranking_eval.quiz --final                         include the hidden third (final comparison only)
uv run python -m ranking_eval.report results/ranking-eval/<time>   rewrite a report from its saved records
```

Results go to `results/ranking-eval/<time>/`:
- `leaderboard.md`: the report
- `leaderboard.json`: the same totals as data
- `records.jsonl`: every case under every ranking, with what the tool did and how it was graded
- `meta.json`: model, run times and the command

Needs Ollama with `qwen2.5:7b` and `nomic-embed-text`, and the Dogwood CLI, same as the app.

## Two searches, one hook

The tool searches the library twice. Since this branch, each search has its own score in `app/core/examples.py`:

| Search | Function | Used for |
|---|---|---|
| Example score | `most_similar` | the 3 past rules shown to the model |
| Evidence score | `stating` | the past rules that may suggest a missing value |

`examples.use_ranking(...)` swaps the ranking for a block of code. With nothing swapped in, the tool behaves exactly as before. `app/tests/test_ranking_hook.py` checks that against frozen copies of the old functions.

## Rankings (`rankings.py`)

| Ranking | Example score | Evidence score | Evidence pool |
|---|---|---|---|
| `jaccard` (baseline) | word overlap (today) | word overlap | same rule type (today) |
| `jaccard-action` | *(same as jaccard, so not run)* | word overlap | same action and prior action |
| `bm25` | Okapi BM25 over the library: rare shared words count more | BM25 | same action and prior action |
| `structural` | profile match: action, prior action, form kind, matching fields, values set | profile match | same action and prior action |
| `hybrid` | structure filter, then BM25 | BM25 | same action and prior action |
| `embeddings` | cosine similarity of `nomic-embed-text` vectors | same | same action and prior action |
| `behavioral` | *(can't: needs the model's reading)* | structural top 5, reranked by how alike the two policies decide the same histories | same action and prior action |

Three reference rows sit beside them. They aren't candidates:

| Reference | Why it's there |
|---|---|
| `oracle` | Exactly the past rules the key marks relevant: the best any ranking could do |
| `random` | A seeded shuffle: how much ordering matters at all |
| `none` | No examples and no suggestions: the floor |

**Evidence pool.** The pool is which past rules may suggest a value. Two rules are in the same pool when they control the same action and look back at the same earlier action: `IssueRefund` after `RequestApproval` (R4 and the manager-approval form), or `DeployService` after `RunTests` (R6). The pool never looks at a rule type. Only the baseline keeps today's same-rule-type pool. Comparing `jaccard` with `jaccard-action` shows what the pool change does on its own, and every row below `jaccard-action` differs from it only in its score. The pool ignores conditions like the environment, so a staging tests rule and a production one share a pool.

**Behavioral** sets the open slot to the same placeholder in both policies. It replays only histories that don't depend on how long ago something happened, so a candidate can't agree with the draft just because it was filled with the candidate's own value.

**Model calls are cached by prompt** (`.cache/model.jsonl`, temperature 0, seed 0). Rankings that change only the evidence score share one model call per case.

## Modes

| Mode | What's switched off |
|---|---|
| isolated | The case's `leave_out` policies are removed from the library, the glossary's gap fills are off, and there are no saved answers. Past rules are the only possible source of a value. |
| realistic | Nothing: everything on, as the tool is used. Saved answers come from `saved_answers.json` (empty, a fresh install), never from your own `data/answers.sqlite`. |

## Checks

1. **Understood:** the action, the form kinds and every stated value match the key. The rule type is recorded but not required. A case the key says to refuse passes when the tool refuses it.
2. **Works:** the policy validates, its edge examples agree, and it decides every history in `replay.py` the same way as the key's reference policy. The histories sit on the boundaries: 59 vs 61 minutes, $500 vs $501, the approver's role, staging vs production. Wrong allows and wrong blocks are counted apart, as in Kylie's `compare_rules.py`.
3. **Right source:** each value came from where the key expects: sentence, glossary, saved answer, ask, or unfilled.
4. **Right suggestion:** for each question the key needs, the suggestion was correct, wrong, absent, or not asked. A suggestion on a question nobody needed counts as wrong.
5. **Asks only when needed:** the questions asked are exactly the needed ones.

The report also gives:
- Recall@3 for both searches. It counts by sentence, since a converter form and its Dogwood twin are one sentence.
- A found/missed × right/not table, which separates ranking failures from model failures.
- Wrong values in policies that reached Ready.
- Every case where the model understood the rule under the baseline but not under another ranking. Any such case blocks the change.

## Files to review

| File | What it holds | Status |
|---|---|---|
| `cases.json` | 59 answer keys: 9 masked, 20 paraphrases, 15 near misses, 15 should-ask. 20 are hidden (seeded, a third of each kind). | **Draft:** `checked_by` is null until a second person checks each one. Cases marked `JUDGEMENT` in their notes need the team's call. |
| `profiles.json` | Structure profiles for the 18 library policies | Hand-written; check each against its Dogwood |
