"""The quiz's results as a leaderboard: counts next to every percentage, the baseline first, changes per case.

uv run python -m ranking_eval.report results/ranking-eval/<time>     rewrite the report from saved records
"""

import json
import sys
from collections import defaultdict
from pathlib import Path

BASELINE = "jaccard+jaccard"
KINDS = ("masked", "paraphrase", "near_miss", "should_ask")
CORRECT, CORRECT_NONE, WRONG, ABSENT, NOT_ASKED = "correct", "correct (none)", "wrong", "absent", "not asked"
REFERENCE_NAMES = {"oracle", "random", "none"}
MARK = {CORRECT: "✓", CORRECT_NONE: "✓·", WRONG: "✗", ABSENT: "–", NOT_ASKED: "?"}


def one_line(record):
    g = record.get("grade", {})
    if "error" in g:
        return f"ERROR {g['error'][:60]}"
    flag = {True: "y", False: "n", None: "-"}
    sugg = " ".join(f"{f}:{MARK[o]}" for f, o in g.get("suggestions", {}).items())
    return (
        f"U{flag[g['understood']]} W{flag[g['works']]} S{flag[g['right_source']]} A{flag[g['asks_right']]}"
        f" {sugg}  {record.get('seconds', 0):.0f}s"
    )


def frac(n, d):
    return f"{n}/{d} ({100 * n / d:.0f}%)" if d else "–"


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return (sum(xs) / len(xs), len(xs)) if xs else (None, 0)


def summarize(records):
    """Totals for one ranking pair in one mode."""
    s = defaultdict(int)
    rec_ex, rec_ev = [], []
    for r in records:
        g = r["grade"]
        if "error" in g:
            s["errors"] += 1
            continue
        for check in ("understood", "works", "right_source", "asks_right"):
            if g[check] is not None:
                s[f"{check}_n"] += 1
                s[check] += bool(g[check])
        for outcome in g["suggestions"].values():
            s[f"sugg:{outcome}"] += 1
        needed, asked = set(g["needed"]), g["asked"]
        s["needed"] += len(needed)
        s["needed_asked"] += len(needed & set(asked))
        s["asked"] += len(asked)
        s["wrong_ready"] += g["wrong_ready"]
        s["wrong_allows"] += g.get("wrong_allows", 0)
        rec_ex.append(g["recall_example"])
        rec_ev.append(g["recall_evidence"])
        for f, found in g["evidence_found"].items():
            outcome = g["suggestions"].get(f)
            ok = outcome in (CORRECT, CORRECT_NONE)
            s[f"quad:{'found' if found else 'missed'}:{'right' if ok else 'not'}"] += 1
    s["recall_example"], s["recall_example_n"] = _mean(rec_ex)
    s["recall_evidence"], s["recall_evidence_n"] = _mean(rec_ev)
    s["cases"] = len(records)
    return s


def _suggestion_counts(s):
    correct = s["sugg:correct"] + s["sugg:correct (none)"]
    graded = correct + s["sugg:wrong"] + s["sugg:absent"] + s["sugg:not asked"]
    return correct, graded


def _recall(value, n):
    return f"{value:.2f} (n={n})" if value is not None else "–"


def _delta(n, base_n, better_low=False):
    if base_n is None or n == base_n:
        return ""
    d = n - base_n
    good = (d < 0) if better_low else (d > 0)
    return f" ({'+' if d > 0 else ''}{d}{'' if good else ' ⚠'})"


def regressions(base, other):
    """Cases this ranking understood worse than the baseline (the R4 problem), and ones it fixed."""
    broke, fixed = [], []
    for case_id, r in other.items():
        b = base.get(case_id)
        if not b or "error" in r["grade"] or "error" in b["grade"]:
            continue
        if b["grade"]["understood"] and not r["grade"]["understood"]:
            broke.append(case_id)
        elif not b["grade"]["understood"] and r["grade"]["understood"]:
            fixed.append(case_id)
    return broke, fixed


def leaderboard(by_run, runs_meta, mode, hidden):
    names = sorted(
        {name for (name, m) in by_run if m == mode},
        key=lambda n: (n != BASELINE, n.split("+")[1] in REFERENCE_NAMES, n),
    )
    base_records = {r["case"]: r for r in by_run.get((BASELINE, mode), []) if r["hidden"] == hidden}
    base = summarize(list(base_records.values())) if base_records else None
    lines = [
        (
            "| Ranking (example + evidence) | Wrong suggestions | Correct suggestions | Absent | Understood | Works | Right source "
            "| Needed questions asked | Extra questions | Recall@3 example | Recall@3 evidence | Wrong in Ready | Reading broke vs baseline | Time (model calls, cached) |"
        ),
        "|" + "---|" * 14,
    ]
    summaries = {}
    for name in names:
        recs = [r for r in by_run[(name, mode)] if r["hidden"] == hidden]
        if not recs:
            continue
        s = summarize(recs)
        summaries[name] = s
        correct, graded = _suggestion_counts(s)
        base_wrong = base["sugg:wrong"] if base and name != BASELINE else None
        base_correct = _suggestion_counts(base)[0] if base and name != BASELINE else None
        broke, _ = regressions(base_records, {r["case"]: r for r in recs}) if name != BASELINE else ([], [])
        meta = runs_meta.get((name, mode), {})
        label = f"**{name}** (baseline)" if name == BASELINE else name
        if name.split("+")[1] in REFERENCE_NAMES:
            label += " *(reference)*"
        if s["errors"]:
            label += f" ⚠ {s['errors']} cases crashed"
        extra = s["asked"] - s["needed_asked"]
        lines.append(
            f"| {label} | {frac(s['sugg:wrong'], graded)}{_delta(s['sugg:wrong'], base_wrong, better_low=True)} "
            f"| {frac(correct, graded)}{_delta(correct, base_correct)} | {s['sugg:absent']} "
            f"| {frac(s['understood'], s['understood_n'])} | {frac(s['works'], s['works_n'])} "
            f"| {frac(s['right_source'], s['right_source_n'])} | {frac(s['needed_asked'], s['needed'])} | {extra} "
            f"| {_recall(s['recall_example'], s['recall_example_n'])} | {_recall(s['recall_evidence'], s['recall_evidence_n'])} "
            f"| {s['wrong_ready']} | {len(broke)}{' ⚠ ' + ', '.join(broke) if broke else ''} "
            f"| {meta.get('seconds', '–')}s ({meta.get('model_calls', '–')}, {meta.get('model_cached', '–')}) |"
        )
    return lines, summaries


def by_kind(by_run, mode, hidden, names):
    lines = ["| Ranking | " + " | ".join(KINDS) + " |", "|" + "---|" * (len(KINDS) + 1)]
    for name in names:
        cells = []
        for kind in KINDS:
            recs = [r for r in by_run[(name, mode)] if r["kind"] == kind and r["hidden"] == hidden]
            s = summarize(recs)
            correct, _graded = _suggestion_counts(s)
            cells.append(
                f"✓{correct} ✗{s['sugg:wrong']} –{s['sugg:absent']} · U {s['understood']}/{s['understood_n']}"
                if recs
                else "–"
            )
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    return lines


def quadrants(summaries):
    lines = [
        (
            "| Ranking | Found, suggestion right | Found, suggestion not right (model or later step) "
            "| Missed, right anyway (luck or another source) | Missed and not right (ranking failure) |"
        ),
        "|---|---|---|---|---|",
    ]
    for name, s in summaries.items():
        lines.append(
            f"| {name} | {s['quad:found:right']} | {s['quad:found:not']} | {s['quad:missed:right']} | {s['quad:missed:not']} |"
        )
    return lines


def matrix(by_run, mode, hidden, names, cases):
    """Every case against every ranking: one mark per needed question, then the checks that failed."""
    lines = ["| Case | Kind | " + " | ".join(names) + " |", "|" + "---|" * (len(names) + 2)]
    for case in cases:
        if case["hidden"] != hidden:
            continue
        cells = []
        for name in names:
            r = next((x for x in by_run.get((name, mode), []) if x["case"] == case["id"]), None)
            if r is None:
                cells.append("")
                continue
            g = r["grade"]
            if "error" in g:
                cells.append("error")
                continue
            marks = " ".join(MARK[o] for o in g["suggestions"].values())
            failed = [c[0].upper() for c in ("understood", "works", "right_source", "asks_right") if g[c] is False]
            cells.append((marks + (" ¬" + "".join(failed) if failed else "")).strip() or "·")
        if any(cells):
            lines.append(f"| {case['id']} | {case['kind']} | " + " | ".join(cells) + " |")
    return lines


def changes(by_run, mode, hidden, names):
    """Per case, what changed from the baseline: fixed or broke, check by check."""
    base = {r["case"]: r for r in by_run.get((BASELINE, mode), []) if r["hidden"] == hidden}
    lines = []
    for name in names:
        if name == BASELINE:
            continue
        rows = []
        for r in by_run[(name, mode)]:
            b = base.get(r["case"])
            if r["hidden"] != hidden or not b or "error" in r["grade"] or "error" in b["grade"]:
                continue
            diffs = []
            for check in ("understood", "works", "right_source", "asks_right"):
                was, now = b["grade"][check], r["grade"][check]
                if was is not None and now is not None and was != now:
                    diffs.append(f"{check} {'fixed' if now else '**broke**'}")
            for f in set(b["grade"]["suggestions"]) | set(r["grade"]["suggestions"]):
                was, now = b["grade"]["suggestions"].get(f), r["grade"]["suggestions"].get(f)
                if was != now:
                    good = now in (CORRECT, CORRECT_NONE)
                    diffs.append(
                        f"{f} suggestion {was} → {now}"
                        + ("" if good or now is None else " **broke**" if was in (CORRECT, CORRECT_NONE) else "")
                    )
            if diffs:
                rows.append(f"  - {r['case']}: " + "; ".join(diffs))
        lines.append(f"- **{name}**: " + ("no change" if not rows else f"{len(rows)} cases changed"))
        lines += rows
    return lines


def write(out, cases):
    out = Path(out)
    records = [
        json.loads(line) for line in (out / "records.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
    by_run = defaultdict(list)
    for r in records:
        by_run[(r["ranking"], r["mode"])].append(r)
    runs_meta = {(m["ranking"], m["mode"]): m for m in meta["runs"]}
    md = [
        f"# Ranking evaluation ({meta['stamp']})",
        "",
        (
            f"- Model `{meta['model']['name']}` (temperature {meta['model']['temperature']}, seed {meta['model']['seed']}); "
            f"embeddings `{meta['embedding_model']}`; answer keys `{meta['cases_file']}` (draft, not yet checked by a second person)."
        ),
        f"- {len({r['case'] for r in records})} cases, modes: {', '.join(meta['modes'])}. Command: `{' '.join(meta['argv'])}`",
        "- Every cell is a count with its percentage. A gap of 1 or 2 cases counts as no difference.",
        (
            "- Suggestions are graded per needed question: correct, correct (none) when the key says no suggestion is right, "
            "wrong, absent, or not asked. A suggestion on a question nobody needed counts as wrong."
        ),
        (
            "- ⚠ marks a change from the baseline in the wrong direction. *Reading broke* lists cases the baseline understood "
            "and this ranking did not; any entry there blocks the change."
        ),
        "- Time is wall-clock for the run; model calls are real calls, cached ones were answered from earlier runs.",
        "",
    ]
    summaries_by_mode = {}
    for hidden in (False, True):
        if not any(r["hidden"] == hidden for r in records):
            continue
        if hidden:
            md += ["", "---", "", "# Hidden third (final comparison only)", ""]
        for mode in meta["modes"]:
            md += [f"## {mode.capitalize()} mode", ""]
            lines, summaries = leaderboard(by_run, runs_meta, mode, hidden)
            names = list(summaries)
            summaries_by_mode[(mode, hidden)] = summaries
            md += lines + [
                "",
                f"### {mode.capitalize()}: by case kind (suggestions ✓ correct ✗ wrong – absent · U understood)",
                "",
            ]
            md += by_kind(by_run, mode, hidden, names)
            md += [
                "",
                f"### {mode.capitalize()}: was the right past rule found? (evidence, needed questions with a right answer)",
                "",
            ]
            md += quadrants(summaries)
            md += ["", f"### {mode.capitalize()}: changes from the baseline, case by case", ""]
            md += changes(by_run, mode, hidden, names)
            md += [
                "",
                (
                    f"### {mode.capitalize()}: every case (✓ correct, ✓· correct no suggestion, ✗ wrong, – absent, ? not asked; "
                    "¬ then the checks that failed: U understood, W works, R right source, A asks)"
                ),
                "",
            ]
            md += matrix(by_run, mode, hidden, names, cases)
            md.append("")
    (out / "leaderboard.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    flat = {
        f"{name}|{mode}|{'hidden' if hidden else 'open'}": dict(s)
        for (mode, hidden), summaries in summaries_by_mode.items()
        for name, s in summaries.items()
    }
    (out / "leaderboard.json").write_text(
        json.dumps({"meta": meta, "summaries": flat}, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    from ranking_eval import keys

    write(Path(sys.argv[1]), keys.load())
    print(f"Rewrote {Path(sys.argv[1]) / 'leaderboard.md'}")
