"""Turn a plain-English rule into a structured understanding.

The model only points at words in the sentence. Code decides everything else: every span must
be in the sentence, numbers are parsed from the spans, and the verdict comes from fixed checks.

    uv run python -m app.core.understand "No refunds over $5,000."
    uv run python -m app.core.understand --live-check
"""
import argparse
import json
import re
import sys

from app.core import config, rule_types
from app.core.model import ModelError, OllamaModel, build_prompt
from app.core.numbers import amount_json, find_amounts, find_durations, window
from converter.convert import load_schema

INVALID, REVIEW, CLARIFY, OK = "invalid", "needs_review", "needs_clarification", "ok_so_far"
ORDER = [INVALID, REVIEW, CLARIFY, OK]


def locate(sentence, span):
    """Character offsets of span in sentence (case-insensitive, first match), or None."""
    span = (span or "").strip()
    if not span:
        return None
    m = re.search(re.escape(span), sentence, re.I)
    return {"text": sentence[m.start():m.end()], "start": m.start(), "end": m.end()} if m else None


def _inside(loc, spans):
    return any(s["start"] <= loc["start"] and loc["end"] <= s["end"] for s in spans)


def _overlaps(loc, spans):
    return any(loc["start"] < s["end"] and s["start"] < loc["end"] for s in spans)


def _read_value(kind, loc, model_value):
    """Value for one slot entry, read by code from its span. Returns (value, extra, error)."""
    if kind in ("amount", "duration"):
        found = find_amounts(loc["text"]) if kind == "amount" else find_durations(loc["text"])
        if not found:
            return None, None, f"no {'dollar amount' if kind == 'amount' else 'duration'} in the span"
        if len(found) > 1:
            return None, None, f"more than one {kind} in the span"
        n = found[0]
        number = {"text": n["text"], "start": loc["start"] + n["start"], "end": loc["start"] + n["end"]}
        if kind == "amount":
            value, extra = amount_json(n["value"]), {"value_at": number}
            model_says = find_amounts(model_value or "") or find_amounts("$" + (model_value or ""))
            differs = bool(model_says) and model_says[0]["value"] != n["value"]
        else:
            value, extra = window(n["seconds"]), {"value_at": number, "seconds": n["seconds"]}
            model_says = find_durations(model_value or "")
            differs = bool(model_says) and model_says[0]["seconds"] != n["seconds"]
        if differs:
            extra["note"] = f"model said {model_value!r}; code read {n['text']!r} from the span"
        return value, extra, None
    # text and list: the value must be words inside the span; we keep the sentence's own characters.
    inner = locate(loc["text"], model_value)
    if inner is None:
        return None, None, f"value {model_value!r} is not in its span"
    where = {"text": inner["text"], "start": loc["start"] + inner["start"], "end": loc["start"] + inner["end"]}
    return inner["text"], {"value_at": where}, None


def check(sentence, answer, actions):
    """Apply the deterministic checks to the model's answer. Never adds a value the sentence lacks."""
    u = {"sentence": sentence, "rule_type": None, "action": None, "slots": {}, "vague_terms": [],
         "qualifiers": [], "unassigned_numbers": [], "rejected": [], "issues": []}

    def issue(verdict, reason):
        u["issues"].append({"verdict": verdict, "reason": reason})

    def finish():
        top = min((ORDER.index(i["verdict"]) for i in u["issues"]), default=ORDER.index(OK))
        u["verdict"] = ORDER[top]
        u["verdict_reason"] = "; ".join(i["reason"] for i in u["issues"] if i["verdict"] == ORDER[top]) \
            or "every required value is stated and can be represented"
        return u

    rt, action = answer.get("rule_type"), answer.get("action")
    if rt in (None, "", "unsupported"):
        why = (answer.get("unsupported_reason") or "").strip().rstrip(".")
        issue(INVALID, f"not a supported rule type{': ' + why if why else ''}. {rule_types.refusal_text()}")
        return finish()
    if rt not in rule_types.BY_ID:
        issue(INVALID, f"the model answered rule type {rt!r}, which is not on our list. {rule_types.refusal_text()}")
        return finish()
    t = rule_types.BY_ID[rt]
    u["rule_type"], u["description"] = rt, t["description"]
    if action not in actions:
        issue(INVALID, f"action {action!r} is not in the schema")
        return finish()
    if action != t["action"]:
        issue(INVALID, f"{rt} guards {t['action']}, but the model said the rule is about {action}")
        return finish()
    u["action"] = action
    inputs = actions[action]["input"]
    defs = rule_types.slots_of(rt)
    for name, d in defs.items():
        u["slots"][name] = {"source": "missing", "kind": d["kind"], "required": d["required"],
                            "field": d["field"], "value": [] if d["kind"] == "list" else None, "spans": []}

    def reject(what, span, reason):
        u["rejected"].append({"what": what, "span": span, "reason": reason})

    # Slots.
    stray, numberless = [], []
    for e in answer.get("slots") or []:
        name, span, model_value = e.get("slot"), e.get("span"), e.get("value")
        loc = locate(sentence, span)
        if loc is None:
            reject(f"slot {name}", span, "rejected: not in the sentence")
            continue
        if name not in defs:
            reject(f"slot {name}", span, f"rejected: {rt} has no slot {name!r}")
            stray.append(loc)
            continue
        slot = u["slots"][name]
        value, extra, err = _read_value(slot["kind"], loc, model_value)
        if err:
            if slot["kind"] in ("amount", "duration") and err.startswith("no "):
                # The model pointed at these words as the value, but they hold no number: a vague term.
                numberless.append({"span": span, "slot": name})
                err += "; kept as a vague term"
            reject(f"slot {name}", span, f"rejected: {err}")
            continue
        if slot["kind"] == "list":
            if value.lower() not in (v.lower() for v in slot["value"]):
                slot["value"].append(value)
                slot["spans"].append(loc)
                slot.setdefault("values_at", []).append(extra["value_at"])
            slot["source"] = "stated"
            continue
        if slot["source"] == "stated":
            if slot["value"] != value:
                issue(REVIEW, f"the sentence gives two values for {name}: {slot['value']!r} and {value!r}")
            continue
        slot.update({"source": "stated", "value": value, "spans": [loc]}, **extra)

    # Where the stated values themselves sit. A model span can be much wider than its value
    # (even the whole sentence), so qualifiers and numbers are checked against these instead.
    stated_spans = [x for slot in u["slots"].values()
                    for x in ([slot.get("value_at")] + slot.get("values_at", [])) if x]

    # Vague terms: kept with their span; a later part resolves them.
    for e in (answer.get("vague_terms") or []) + numberless:
        loc = locate(sentence, e.get("span"))
        if loc is None:
            reject("vague term", e.get("span"), "rejected: not in the sentence")
            continue
        if _overlaps(loc, u["vague_terms"] + stated_spans) or u["slots"].get(e.get("slot"), {}).get("source") == "stated":
            continue
        slot = e.get("slot") if e.get("slot") in defs else None
        u["vague_terms"].append({**loc, "slot": slot})
        issue(CLARIFY, f"'{loc['text']}' is vague" + (f" (it stands in for {slot})" if slot else ""))

    # Qualifiers: words that narrow the rule. Never dropped silently.
    mapped = []
    for e in answer.get("qualifiers") or []:
        loc = locate(sentence, e.get("span"))
        if loc is None:
            reject("qualifier", e.get("span"), "rejected: not in the sentence")
            continue
        if _overlaps(loc, stated_spans + u["vague_terms"]):
            continue  # already captured as a slot or a vague term
        field = e.get("field")
        ok = field in inputs
        u["qualifiers"].append({**loc, "field": field if ok else None, "mapped": ok})
        if ok:
            mapped.append(loc["text"])
        else:
            issue(REVIEW, f"'{loc['text']}' limits the rule, but {action} has no field it maps to")
    for loc in stray:
        if not _overlaps(loc, stated_spans + u["vague_terms"] + u["qualifiers"]):
            u["qualifiers"].append({**loc, "field": None, "mapped": False})
            issue(REVIEW, f"'{loc['text']}' was given as a slot {rt} does not have, and maps to nothing")
    has_scope = any("only_when" in f for f in t["forms"])
    if len(mapped) + has_scope > 1:
        issue(REVIEW, f"more conditions than one rule form can hold: {', '.join(mapped)}")

    # Every amount and duration in the sentence must be used by a slot.
    for n in find_amounts(sentence):
        if not _inside(n, stated_spans):
            u["unassigned_numbers"].append({"kind": "amount", "text": n["text"], "start": n["start"],
                                            "end": n["end"], "value": amount_json(n["value"])})
    for n in find_durations(sentence):
        if not _inside(n, stated_spans):
            u["unassigned_numbers"].append({"kind": "duration", "text": n["text"], "start": n["start"],
                                            "end": n["end"], "value": window(n["seconds"])})
    for n in u["unassigned_numbers"]:
        issue(REVIEW, f"the sentence states {n['text']!r} but no slot uses it")

    # Values must be representable as stated; never rounded.
    for name, slot in u["slots"].items():
        if slot["source"] == "stated" and slot["kind"] == "amount" and isinstance(slot["value"], str):
            issue(REVIEW, f"{name} is ${slot['value']}, but amounts are whole dollars; it will not be rounded")
        if slot["source"] == "missing" and slot["required"]:
            vague = any(v["slot"] == name for v in u["vague_terms"])
            if not vague:
                issue(CLARIFY, f"{name} is required but the sentence does not state it")
    return finish()


def understand(sentence, model=None, schema_path=None):
    schema_path = schema_path or config.SCHEMA_PATH
    _, actions = load_schema(schema_path)
    problems = rule_types.schema_problems(actions)
    if problems:
        raise RuntimeError("rule types do not match the schema: " + "; ".join(problems))
    model = model or OllamaModel()
    prompt = build_prompt(sentence, actions)
    answer = model.complete(prompt)
    u = check(sentence, answer, actions)
    u["prompt_context"] = prompt["context"]
    u["model"] = {**model.info(), "seconds": getattr(model, "last_seconds", None),
                  "timing": getattr(model, "last_timing", None), "answer": answer}
    return u


def readable(u):
    lines = [f"Sentence: {u['sentence']}",
             f"Type:     {u['rule_type'] or '-'}" + (f" (guards {u['action']})" if u["action"] else "")]
    for name, s in u["slots"].items():
        if s["source"] == "stated":
            spans = ", ".join(f"\"{x['text']}\" [{x['start']}:{x['end']}]" for x in s["spans"])
            lines.append(f"  {name} = {json.dumps(s['value'])}  stated: {spans}" + (f"  ({s['note']})" if s.get("note") else ""))
        else:
            lines.append(f"  {name}: missing ({'required' if s['required'] else 'optional'})")
    for v in u["vague_terms"]:
        lines.append(f"  vague: \"{v['text']}\" [{v['start']}:{v['end']}]" + (f" for {v['slot']}" if v["slot"] else ""))
    for q in u["qualifiers"]:
        lines.append(f"  qualifier: \"{q['text']}\" -> " + (q["field"] if q["mapped"] else "no field"))
    for n in u["unassigned_numbers"]:
        lines.append(f"  unassigned {n['kind']}: \"{n['text']}\"")
    for r in u["rejected"]:
        lines.append(f"  {r['what']} {r['span']!r}: {r['reason']}")
    lines.append(f"Verdict:  {u['verdict']} -- {u['verdict_reason']}")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sentence", nargs="?")
    ap.add_argument("--model", default=config.MODEL)
    ap.add_argument("--live-check", action="store_true", help="run the demo and unsupported sentences against the real model")
    ap.add_argument("--out", help="with --live-check: write the markdown report here")
    a = ap.parse_args(argv)
    try:
        if a.live_check:
            from app.core.live_check import run
            run(a.model, a.out)
            return
        if not a.sentence:
            ap.error("give a sentence or --live-check")
        u = understand(a.sentence, OllamaModel(a.model))
    except ModelError as e:
        sys.exit(f"error: {e}")
    print(json.dumps(u, indent=2))
    print()
    print(readable(u))


if __name__ == "__main__":
    main()
