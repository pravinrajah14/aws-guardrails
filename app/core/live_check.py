"""Live check: run the demo and unsupported sentences against the real model and report stability.

Each sentence (and each demo variant) runs 3 times at the app's settings (temperature 0, seed 0).
Each original also runs once at temperature 0.3 with seeds 1, 2 and 3. A run's signature is its
rule type, stated values and verdict. Text values are compared ignoring case, because the
lowercase variant types the order id as "a100".
"""
import json
import statistics
from datetime import datetime
from pathlib import Path

from app.core import config
from app.core.model import OllamaModel
from app.core.understand import understand

DEMO = [
    {"sentence": "No refunds over $5,000.",
     "expect": {"rule_type": "amount_cap", "values": {"max_amount": 5000}, "verdict": "ok_so_far"},
     "variants": {"no period": "No refunds over $5,000",
                  "lowercase": "no refunds over $5,000.",
                  "rephrase": "Refunds over $5,000 are not allowed."}},
    {"sentence": "Only refund if a manager approved it recently.",
     "expect": {"rule_type": "approval_required", "values": {"approver_role": "manager"},
                "verdict": "needs_clarification", "vague": "recently"},
     "variants": {"no period": "Only refund if a manager approved it recently",
                  "lowercase": "only refund if a manager approved it recently.",
                  "rephrase": "Don't refund unless a manager recently approved it."}},
    {"sentence": "No production refunds over $500.50.",
     "expect": {"rule_type": "amount_cap", "values": {"max_amount": "500.50"}, "verdict": "needs_review"},
     "variants": {"no period": "No production refunds over $500.50",
                  "lowercase": "no production refunds over $500.50.",
                  "rephrase": "Production refunds over $500.50 are not allowed."}},
    {"sentence": "Refund up to $40 for order A100, subject to approval.",
     # The task names no verdict for this one; the approval window is not stated.
     "expect": {"rule_type": "capped_with_approval", "values": {"max_amount": 40, "order_id": "A100"}},
     "variants": {"no period": "Refund up to $40 for order A100, subject to approval",
                  "lowercase": "refund up to $40 for order a100, subject to approval.",
                  "rephrase": "Refunds for order A100 are capped at $40 and need approval."}},
]
UNSUPPORTED = [
    "Don't let the agent read customers' SSNs",
    "Block deploys after 6pm on Fridays",
    "Only email customers who opted in",
    "Never copy private data to a public repo",
    "At most 5 refunds per hour",
]
RUNS = 3
SEEDS = [1, 2, 3]


def _fold(v):
    if isinstance(v, str):
        return v.casefold()
    if isinstance(v, list):
        return sorted(_fold(x) for x in v)
    return v


def signature(u):
    values = {k: _fold(s["value"]) for k, s in u["slots"].items() if s["source"] == "stated"}
    return {"rule_type": u["rule_type"], "values": values, "verdict": u["verdict"]}


def shown(u):
    """The signature with values as typed, for the report."""
    values = {k: s["value"] for k, s in u["slots"].items() if s["source"] == "stated"}
    return {"rule_type": u["rule_type"], "values": values, "verdict": u["verdict"]}


def matches(sig, u, expect):
    if expect.get("rule_type", sig["rule_type"]) != sig["rule_type"]:
        return False
    if "verdict" in expect and expect["verdict"] != sig["verdict"]:
        return False
    if "values" in expect and {k: _fold(v) for k, v in expect["values"].items()} != sig["values"]:
        return False
    if "vague" in expect and expect["vague"] not in [v["text"].casefold() for v in u["vague_terms"]]:
        return False
    return True


def _show(sig):
    vals = ", ".join(f"{k}={json.dumps(v)}" for k, v in sig["values"].items()) or "-"
    return sig["rule_type"] or "-", vals, sig["verdict"]


def run(model_name=config.MODEL, out=None):
    out = Path(out or config.REPO / "results" / "understanding.md")
    cases = []
    for d in DEMO:
        cases.append(("original", d["sentence"], d["expect"]))
        cases += [(label, s, d["expect"]) for label, s in d["variants"].items()]
    cases += [("unsupported", s, {"verdict": "invalid", "rule_type": None}) for s in UNSUPPORTED]

    raw, rows, seconds = [], [], []

    def once(sentence, temperature, seed):
        m = OllamaModel(model_name, temperature=temperature, seed=seed)
        u = understand(sentence, m)
        seconds.append(u["model"]["timing"])
        raw.append({"sentence": sentence, "temperature": temperature, "seed": seed,
                    "answer": u["model"]["answer"], "signature": signature(u), "verdict_reason": u["verdict_reason"]})
        print(f"  t={temperature} seed={seed} {u['model']['seconds']:>5}s  {_show(signature(u))}  {sentence}", flush=True)
        return u

    for label, sentence, expect in cases:
        us = [once(sentence, config.TEMPERATURE, config.SEED) for _ in range(RUNS)]
        sigs = [signature(u) for u in us]
        rows.append({"label": label, "sentence": sentence, "runs": f"{RUNS} @ t=0",
                     "sig": sigs[0], "shown": shown(us[0]), "stable": all(s == sigs[0] for s in sigs),
                     "same_answer": all(u["model"]["answer"] == us[0]["model"]["answer"] for u in us),
                     "expected": all(matches(s, u, expect) for s, u in zip(sigs, us)),
                     "all_sigs": sigs})

    seeded = []
    for label, sentence, expect in cases:
        if label not in ("original", "unsupported"):
            continue
        base = next(r for r in rows if r["sentence"] == sentence)
        us = [once(sentence, 0.3, seed) for seed in SEEDS]
        sigs = [signature(u) for u in us]
        seeded.append({"sentence": sentence, "sigs": sigs, "shown": [shown(u) for u in us],
                       "same_as_t0": sum(s == base["sig"] for s in sigs),
                       "expected": sum(matches(s, u, expect) for s, u in zip(sigs, us))})

    write_report(out, model_name, rows, seeded, seconds)
    out.with_name(out.stem + "_runs.json").write_text(json.dumps(raw, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {out}")


def _yes(b):
    return "yes" if b else "**NO**"


def write_report(out, model_name, rows, seeded, timing):
    totals = [t["total"] for t in timing]
    warm = [t for t in timing if t["load"] < 1]
    lines = [
        "# Part 3: understanding a sentence, live check",
        "",
        f"Generated by `uv run python -m app.core.understand --live-check` on {datetime.now():%Y-%m-%d %H:%M}.",
        f"Model `{model_name}` through Ollama, JSON schema in `format`. The app runs at temperature 0, seed 0.",
        "",
        "A run's signature is: rule type, stated values, verdict. Text values are compared ignoring case.",
        "\"Stable\" means all runs of that row had the same signature. \"Same raw answer\" means the model's JSON",
        "was byte-for-byte the same too. \"Expected\" checks every run against the task's expected result",
        "(the variants are held to their original's expectation).",
        "",
        "## Temperature 0, 3 runs per row",
        "",
        "| Sentence | Variant | Type | Stated values | Verdict | Stable | Same raw answer | Expected |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        t, v, verdict = _show(r["shown"])
        lines.append(f"| {r['sentence']} | {r['label']} | {t} | {v} | {verdict} | {_yes(r['stable'])} "
                     f"| {_yes(r['same_answer'])} | {_yes(r['expected'])} |")
    unstable = [r for r in rows if not r["stable"]]
    for r in unstable:
        lines.append("")
        lines.append(f"Unstable: `{r['sentence']}`: " + "; ".join(" / ".join(_show(s)) for s in r["all_sigs"]))
    lines += [
        "",
        "## Temperature 0.3, seeds 1, 2, 3 (one run each, originals only)",
        "",
        "| Sentence | Same signature as temperature 0 | Expected | Signatures seen |",
        "|---|---|---|---|",
    ]
    for s in seeded:
        seen = []
        for sig in s["shown"]:
            text = " / ".join(_show(sig))
            if text not in seen:
                seen.append(text)
        lines.append(f"| {s['sentence']} | {s['same_as_t0']}/3 | {s['expected']}/3 | {'<br>'.join(seen)} |")
    lines += [
        "",
        "## Time per model call",
        "",
        f"{len(totals)} calls. Median {statistics.median(totals):.1f}s, min {min(totals):.1f}s, max {max(totals):.1f}s.",
        f"With the model already loaded: prompt reading median "
        f"{statistics.median(t['prompt'] for t in warm):.2f}s ({warm[0]['prompt_tokens']} tokens), "
        f"answer writing median {statistics.median(t['answer'] for t in warm):.1f}s "
        f"({statistics.median(t['answer_tokens'] for t in warm):.0f} tokens).",
        "",
        "Every run's raw model answer is in `understanding_runs.json` next to this file.",
        "",
        "## Notes",
        "",
        "- Values shown are from the first run, as typed. The lowercase variant of the A100 sentence states `a100`;",
        "  code keeps what the sentence says and does not fix the case.",
        "- Prompt changes made before this run, while trying the demo sentences by hand (no expected answer changed):",
        "  1. The model gave the whole sentence as the amount span, which hid \"production\" inside it. Code now",
        "     checks qualifiers and unassigned numbers against where the value itself sits, not the model's span;",
        "     the prompt asks for the shortest span.",
        "  2. It called \"Only refund if\" a qualifier and \"a manager\" vague. The prompt now says which words are",
        "     not qualifiers and has four worked examples, none of them a demo or test sentence.",
        "  3. It puts \"recently\" in the window slot instead of vague_terms (once with an invented value \"2 hours\").",
        "     Code rejects any amount or duration span with no number in it and keeps those words as a vague term.",
        "     No vague word is written in code.",
        "",
    ]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
