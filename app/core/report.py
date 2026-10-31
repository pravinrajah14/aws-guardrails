"""Write the default live check report from its saved runs: uv run python -m app.core.report"""

import json
import statistics
from pathlib import Path

from app.core import config
from app.core.understand import FILLED

RUNS_PATH = config.REPO / "results" / "understanding_runs_default.json"
REPORT_PATH = config.REPO / "results" / "understanding.md"
UNDERSTANDING_NOTES = [
    "",
    "## Notes",
    "",
    "- Stable means every run of a row had the same type, values and verdict, ignoring case in text values.",
    "- Same raw answer means the model's JSON was byte-for-byte the same.",
    "- Values are shown as typed: `A100`, or `a100` in the lowercase variant.",
    "- The prompt was tuned on the demo sentences, so this table is not a held-out test.",
    (
        "- The library holds some of these sentences word for word (`form_manager_approval`, `R2`), so the model "
        "sees their answer in its prompt."
    ),
    (
        '- R4 means "refund equals the approved amount" and only applies over $100. So it is an example for '
        "approval_required, not the reference."
    ),
    "",
]


def shown(u):
    """What a run decided, with values as typed."""
    slots = {k: s for k, s in u["slots"].items() if s["source"] in FILLED}
    return {
        "rule_type": u["rule_type"],
        "values": {k: s["value"] for k, s in slots.items()},
        "directions": {k: s["direction"] for k, s in slots.items() if "direction" in s},
        "direction_text": {k: s["direction_text"] for k, s in slots.items() if "direction_text" in s},
        "qualifiers": [{"text": q["text"], "field": q["field"], "source": q["source"]} for q in u["qualifiers"]],
        "unassigned": [n["text"] for n in u["unassigned_numbers"]],
        "verdict": u["verdict"],
    }


def _fold(v):
    if isinstance(v, str):
        return v.casefold()
    if isinstance(v, list):
        return sorted(_fold(x) for x in v)
    return v


def signature(s):
    """Type, values and verdict. Text values compare ignoring case: the lowercase variant types a100."""
    return {
        "rule_type": s["rule_type"],
        "values": {k: _fold(v) for k, v in s["values"].items()},
        "directions": s["directions"],
        "verdict": s["verdict"],
    }


def matches(run, expect):
    s = signature(run["shown"])
    if "rule_type" in expect and expect["rule_type"] != s["rule_type"]:
        return False
    if any(reason not in run["verdict_reason"] for reason in expect.get("reasons", [])):
        return False
    if "unassigned" in expect and expect["unassigned"] != run["shown"]["unassigned"]:
        return False
    if "verdict" in expect and expect["verdict"] != s["verdict"]:
        return False
    if "values" in expect and {k: _fold(v) for k, v in expect["values"].items()} != s["values"]:
        return False
    if "directions" in expect and expect["directions"] != s["directions"]:
        return False
    for wanted in expect.get("qualifiers", []):
        if not any(
            q["text"].casefold() == wanted["text"] and q["field"] == wanted["field"] for q in run["shown"]["qualifiers"]
        ):
            return False
    return "vague" not in expect or expect["vague"] in [v.casefold() for v in run["vague"]]


def _values(s):
    def one(k, v):
        rule = s["direction_text"].get(k)
        return f"{k}={json.dumps(v)}" + (f" ({rule})" if rule else "")

    return ", ".join(one(k, v) for k, v in s["values"].items()) or "-"


def _qualifiers(s):
    return ", ".join(f"{q['text']} -> {q['field'] or 'no field'} ({q['source']})" for q in s["qualifiers"]) or "-"


def _unassigned(s):
    return ", ".join(f"`{n}`" for n in s["unassigned"]) or "-"


def summary(s):
    return f"{s['rule_type'] or '-'} / {_values(s)} / {s['verdict']}"


def _yes(b):
    return "yes" if b else "**NO**"


def _group(runs):
    groups = {}
    for r in runs:
        groups.setdefault((r["label"], r["sentence"]), []).append(r)
    return groups


def _row(label, sentence, runs):
    good = [r for r in runs if "error" not in r]
    if not good:
        return f"| {sentence} | {label} | - | - | - | - | **{runs[0]['error']}** | - | - | **NO** | - |"
    first = good[0]["shown"]
    repeated = len(runs) > 1
    stable = all(signature(r["shown"]) == signature(first) for r in good)
    same_answer = all(r["answer"] == good[0]["answer"] for r in good)
    expected = len(good) == len(runs) and all(matches(r, r["expect"]) for r in good)
    return (
        f"| {sentence} | {label} | {first['rule_type'] or '-'} | {_values(first)} | {_qualifiers(first)} "
        f"| {_unassigned(first)} | {first['verdict']} "
        f"| {_yes(stable) if repeated else '-'} | {_yes(same_answer) if repeated else '-'} | {_yes(expected)} "
        f"| {good[0]['timing']['prompt_tokens']} |"
    )


def _main_table(runs):
    lines = [
        (
            "| Sentence | Variant | Type | Values | Qualifiers | Unassigned | Verdict | Stable | Same raw answer | Expected "
            "| Prompt tokens |"
        ),
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    return lines + [_row(label, sentence, rs) for (label, sentence), rs in _group(runs).items()]


def _seeded_table(seeded, base):
    lines = [
        "",
        f"## Temperature {seeded[0]['temperature']}, one run per seed (originals only)",
        "",
        "| Sentence | Same as temperature 0 | Expected | Seen |",
        "|---|---|---|---|",
    ]
    for (_, sentence), rs in _group(seeded).items():
        good = [r for r in rs if "error" not in r]
        first = next((r["shown"] for r in base if r["sentence"] == sentence and "error" not in r), None)
        same = sum(first is not None and signature(r["shown"]) == signature(first) for r in good)
        hits = sum(matches(r, r["expect"]) for r in good)
        seen = list(dict.fromkeys(summary(r["shown"]) for r in good))
        lines.append(f"| {sentence} | {same}/{len(rs)} | {hits}/{len(rs)} | {'<br>'.join(seen)} |")
    return lines


def _examples_table(runs):
    lines = [
        "",
        "## Examples put in the prompt",
        "",
        "Score is shared words / all words of both sentences.",
        "",
        "| Sentence | Examples (id, score, shared words) |",
        "|---|---|",
    ]
    for (_, sentence), rs in _group(runs).items():
        used = rs[0].get("examples_used", [])
        cell = "<br>".join(f"{e['id']} {e['score']} ({', '.join(e['shared_words'])})" for e in used)
        lines.append(f"| {sentence} | {cell or '-'} |")
    return lines


def _load_line(timed):
    if not timed:
        return []
    later = [t["load"] for t in timed[1:]] or [0]
    return [f"- Model load: first call {timed[0]['load']}s, later calls at most {max(later)}s."]


def _settings(data):
    timed = [r["timing"] for r in data["runs"] if "error" not in r]
    largest = max((t["prompt_tokens"] or 0 for t in timed), default=0)
    fits = 2 ** len(bin(largest - 1)[2:]) if largest > 1 else 1
    lines = [
        (
            f"- Model `{data['model']}`, num_ctx {data['num_ctx']}, keep_alive {data['keep_alive']}, "
            f"{data['call_timeout_seconds']}s timeout per call."
        ),
        f"- Largest prompt {largest} tokens; the next power of two is {fits}.",
        *_load_line(timed),
        f"- {len(data['runs'])} calls in {data['seconds']}s.",
    ]
    if timed:
        totals = [t["total"] for t in timed]
        lines.append(
            f"- Time per call: median {statistics.median(totals):.1f}s, min {min(totals):.1f}s, max {max(totals):.1f}s."
        )
    if data["stopped"]:
        lines.append(f"- **Run stopped early: {data['stopped']}**")
    return lines


def _ollama_section(data):
    return [
        "",
        "## Ollama",
        "",
        "At the start of the run:",
        "",
        "```",
        data["ollama_ps_start"],
        "```",
        "",
        "At the end of the run:",
        "",
        "```",
        data["ollama_ps_end"],
        "```",
    ]


def understanding_markdown(data):
    base = [r for r in data["runs"] if r["temperature"] == config.TEMPERATURE]
    seeded = [r for r in data["runs"] if r["temperature"] != config.TEMPERATURE]
    lines = [
        "# Understanding a sentence: live check",
        "",
        f"`{data['mode']}` run. Generated from `understanding_runs_{data['mode']}.json`.",
        "",
        *_settings(data),
        "",
        "## Temperature 0",
        "",
        *_main_table(base),
    ]
    if seeded:
        lines += _seeded_table(seeded, base)
    lines += _examples_table(base)
    lines += _ollama_section(data)
    lines += UNDERSTANDING_NOTES
    return "\n".join(lines)


def write_understanding(runs_path=RUNS_PATH, out=REPORT_PATH):
    data = json.loads(Path(runs_path).read_text(encoding="utf-8"))
    Path(out).write_text(understanding_markdown(data), encoding="utf-8")


if __name__ == "__main__":
    write_understanding()
