"""Turn a plain-English rule into a structured understanding: the model points at words, code decides."""

import argparse
import json
import re
import sys

from app.core import config, glossary, rule_types, signals
from app.core.model import ModelError, OllamaModel, build_prompt
from app.core.numbers import (
    DIRECTION_TEXT,
    amount_json,
    contradicts_direction,
    find_amounts,
    find_bare_numbers,
    find_direction,
    find_durations,
    window,
)
from converter.convert import load_schema

INVALID, REVIEW, CLARIFY, OK = "invalid", "needs_review", "needs_clarification", "ok_so_far"
ORDER = [INVALID, REVIEW, CLARIFY, OK]
FILLED = ("stated", "glossary")
LIMIT_SCOPE = "limit_scope"
SCOPE_TYPES = (rule_types.AMOUNT_CAP, rule_types.CAPPED_WITH_APPROVAL)


def locate(sentence, span):
    """Character offsets of span in sentence (case-insensitive, first match), or None."""
    span = (span or "").strip()
    if not span:
        return None
    m = re.search(re.escape(span), sentence, re.IGNORECASE)
    return {"text": sentence[m.start() : m.end()], "start": m.start(), "end": m.end()} if m else None


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


def _issue(u, verdict, reason, field=None):
    u["issues"].append({"verdict": verdict, "reason": reason, "field": field})


def _reject(u, what, span, reason):
    u["rejected"].append({"what": what, "span": span, "reason": reason})


def finish(u):
    top = min((ORDER.index(i["verdict"]) for i in u["issues"]), default=ORDER.index(OK))
    u["verdict"] = ORDER[top]
    u["verdict_reason"] = (
        "; ".join(i["reason"] for i in u["issues"] if i["verdict"] == ORDER[top])
        or "every required value is stated and can be represented"
    )
    return u


def _list_items(loc):
    """One located entry per item when a list span holds several, or None when it holds one."""
    items = signals.split_list(loc["text"])
    if len(items) < 2:
        return None
    return [{"text": text, "start": loc["start"] + start, "end": loc["start"] + end} for text, start, end in items]


def _store_value(u, slot, name, loc, value, extra):
    if slot["kind"] == "list":
        if value.lower() not in (v.lower() for v in slot["value"]):
            slot["value"].append(value)
            slot["spans"].append(loc)
            slot.setdefault("values_at", []).append(extra["value_at"])
        slot["source"] = "stated"
    elif slot["source"] != "stated":
        slot.update({"source": "stated", "value": value, "spans": [loc]}, **extra)
    elif slot["value"] != value:
        _issue(u, REVIEW, f"the sentence gives two values for {name}: {slot['value']!r} and {value!r}")


def _read_slots(u, sentence, answer, defs):
    """Fill slots from the model's spans. Returns spans for slots this type lacks, and numberless value spans."""
    stray, numberless = [], []
    for e in answer.get("slots") or []:
        name, span, model_value = e.get("slot"), e.get("span"), e.get("value")
        loc = locate(sentence, span)
        if loc is None:
            _reject(u, f"slot {name}", span, "rejected: not in the sentence")
            continue
        if name not in defs:
            _reject(u, f"slot {name}", span, f"rejected: {u['rule_type']} has no slot {name!r}")
            stray.append(loc)
            continue
        slot = u["slots"][name]
        items = _list_items(loc) if slot["kind"] == "list" else None
        if items:
            for item in items:
                _store_value(u, slot, name, item, item["text"], {"value_at": item})
            continue
        value, extra, err = _read_value(slot["kind"], loc, model_value)
        if not err and name == "order_id" and not glossary.is_order_id(value):
            err = f"{value!r} is not an order id; the word 'order' alone names no order"
        if err and slot["kind"] in ("amount", "duration") and err.startswith("no "):
            # The model pointed at these words as the value, but they hold no number: a vague term.
            numberless.append({"span": span, "slot": name})
            err += "; kept as a vague term"
        if err:
            _reject(u, f"slot {name}", span, f"rejected: {err}")
            continue
        _store_value(u, slot, name, loc, value, extra)
    return stray, numberless


def _apply_glossary_to_slots(u, hits):
    for hit in hits:
        slot = u["slots"].get(hit["field"])
        if slot is None:
            continue
        where = {k: hit[k] for k in ("text", "start", "end")}
        if slot["source"] == "missing":
            slot.update(
                source="glossary", value=hit["value"], spans=[where], value_at=where, glossary_file=hit["source"]
            )
        elif _overlaps(hit, [slot["value_at"]]):
            slot["value"] = hit["value"]
        elif slot["value"] != hit["value"]:
            _issue(
                u, REVIEW, f"the sentence gives two values for {hit['field']}: {slot['value']!r} and {hit['value']!r}"
            )


def _stated_spans(u):
    """Where the values themselves sit. A model span can be much wider than its value, even the whole sentence."""
    return [x for slot in u["slots"].values() for x in ([slot.get("value_at")] + slot.get("values_at", [])) if x]


def _code_vague_terms(sentence, defs):
    """Vague time words count even when the model does not point at them."""
    if "window" not in defs:
        return []
    return [{"span": m.group(0), "slot": "window"} for m in signals.VAGUE_WINDOW.finditer(sentence)]


def _read_vague(u, sentence, entries, defs, taken):
    for e in entries:
        loc = locate(sentence, e.get("span"))
        if loc is None:
            _reject(u, "vague term", e.get("span"), "rejected: not in the sentence")
            continue
        filled = u["slots"].get(e.get("slot"), {}).get("source") in FILLED
        if _overlaps(loc, u["vague_terms"] + taken) or filled:
            continue
        slot = e.get("slot") if e.get("slot") in defs else None
        u["vague_terms"].append({**loc, "slot": slot})
        _issue(u, CLARIFY, f"'{loc['text']}' is vague" + (f" (it stands in for {slot})" if slot else ""), slot)


def _add_qualifier(u, entry, inputs, source):
    mapped = entry["field"] in inputs
    qualifier = {k: entry[k] for k in ("text", "start", "end")}
    qualifier.update(field=entry["field"] if mapped else None, mapped=mapped, source=source)
    if "source" in entry:
        qualifier["glossary_file"] = entry["source"]
        if mapped:
            qualifier["value"] = entry["value"]
    u["qualifiers"].append(qualifier)
    if not mapped:
        _issue(u, REVIEW, f"'{entry['text']}' limits the rule, but {u['action']} has no field it maps to")


def _read_qualifiers(u, sentence, answer, inputs, hits, stated):
    """Qualifiers are never dropped silently, and a glossary term's field beats the model's."""
    for e in answer.get("qualifiers") or []:
        loc = locate(sentence, e.get("span"))
        if loc is None:
            _reject(u, "qualifier", e.get("span"), "rejected: not in the sentence")
            continue
        hit = next((h for h in hits if _overlaps(h, [loc])), None)
        # Without a real order id in its words, a qualifier about "order" names no order.
        field = None if e.get("field") == "order_id" else e.get("field")
        if hit is None and not _overlaps(loc, stated + u["vague_terms"]):
            _add_qualifier(u, {**loc, "field": field}, inputs, "model")
        elif hit is not None and hit["field"] not in u["slots"] and not _overlaps(hit, u["qualifiers"]):
            _add_qualifier(u, hit, inputs, "model")
    for hit in hits:
        if hit["field"] not in u["slots"] and not _overlaps(hit, u["qualifiers"]):
            _add_qualifier(u, hit, inputs, "glossary")


def _check_stray_spans(u, stray, stated):
    for loc in stray:
        if not _overlaps(loc, stated + u["vague_terms"] + u["qualifiers"]):
            u["qualifiers"].append({**loc, "field": None, "mapped": False, "source": "model"})
            _issue(
                u, REVIEW, f"'{loc['text']}' was given as a slot {u['rule_type']} does not have, and maps to nothing"
            )


def _check_limit_scope(u, sentence):
    """Does the amount limit each refund, or a total? Code asks; the model never decides this."""
    slot = u["slots"].get("max_amount")
    found = signals.find_total_wording(sentence, u["rule_type"]) if u["rule_type"] in SCOPE_TYPES else None
    if not found or slot is None or slot["source"] != "stated":
        return None
    question = f"Does {slot['value_at']['text']} limit each refund, or {found['total']}?"
    u["limit_scope"] = {**found, "question": question, "options": ["each refund", "a total"]}
    _issue(u, CLARIFY, question, LIMIT_SCOPE)
    return {k: found[k] for k in ("start", "end")}


def _check_roles(u, sentence):
    if u["action"] != "IssueRefund":
        return
    known = glossary.known_values("approver_role")
    role = u["slots"].get("approver_role", {}).get("value")
    if role and role not in known:
        _issue(u, REVIEW, f"{role!r} is an unknown role; the store knows {', '.join(known)}")
        return
    for word in signals.unknown_roles(sentence):
        _issue(u, REVIEW, f"{word['text']!r} is an unknown role; the store knows {', '.join(known)}")


def _check_scope(u, forms):
    mapped = [q["text"] for q in u["qualifiers"] if q["mapped"]]
    has_scope = any("only_when" in f for f in forms)
    if len(mapped) + has_scope > 1:
        _issue(u, REVIEW, f"more conditions than one rule form can hold: {', '.join(mapped)}")


def _check_numbers(u, sentence, stated):
    """Every number in the sentence must be used by a slot."""
    found = [{**n, "kind": "amount", "value": amount_json(n["value"])} for n in find_amounts(sentence)]
    found += [{**n, "kind": "duration", "value": window(n["seconds"])} for n in find_durations(sentence)]
    found += [{**n, "kind": "count", "value": n["text"]} for n in find_bare_numbers(sentence)]
    threshold_has_no_slot = "max_amount" not in u["slots"]
    for n in sorted(found, key=lambda n: n["start"]):
        if not _inside(n, stated):
            u["unassigned_numbers"].append({k: n[k] for k in ("kind", "text", "start", "end", "value")})
            if n["kind"] == "amount" and threshold_has_no_slot:
                _issue(u, REVIEW, f"{n['text']!r} is a threshold, and {u['rule_type']} has no slot for one")
            else:
                _issue(u, REVIEW, f"the sentence states {n['text']!r} but no slot uses it")


def _check_direction(u, sentence, name, slot):
    at = slot["value_at"]
    found = find_direction(sentence, at["start"], at["end"])
    slot["direction"] = found["kind"] if found else None
    if found:
        slot["direction_text"] = DIRECTION_TEXT[found["kind"]].format(slot["value"])
        slot["direction_words"] = {k: found[k] for k in ("text", "start", "end")}
        if contradicts_direction(sentence, found):
            _issue(
                u,
                REVIEW,
                f"'{found['text']}' {at['text']} reads as {slot['direction_text']}, but the sentence says otherwise",
            )
    else:
        _issue(u, REVIEW, f"the words around {at['text']!r} do not say which way {name} compares")


def _check_values(u, sentence):
    """Values must be representable as stated and never rounded; required slots must be filled."""
    for name, slot in u["slots"].items():
        if slot["source"] == "stated" and slot["kind"] == "amount":
            _check_direction(u, sentence, name, slot)
            if isinstance(slot["value"], str):
                _issue(u, REVIEW, f"{name} is ${slot['value']}, but amounts are whole dollars; it will not be rounded")
        vague = any(v["slot"] == name for v in u["vague_terms"])
        if slot["source"] == "missing" and slot["required"] and not vague:
            _issue(u, CLARIFY, f"{name} is required but the sentence does not state it", name)


def _start(sentence):
    return {
        "sentence": sentence,
        "rule_type": None,
        "action": None,
        "slots": {},
        "vague_terms": [],
        "qualifiers": [],
        "unassigned_numbers": [],
        "rejected": [],
        "issues": [],
    }


def _refusal(answer, actions):
    """Why the answer's type or action can't be used, or None."""
    rt, action = answer.get("rule_type"), answer.get("action")
    if rt in (None, "", rule_types.UNSUPPORTED):
        why = (answer.get("unsupported_reason") or "").strip().rstrip(".")
        return f"not a supported rule type{': ' + why if why else ''}. {rule_types.refusal_text()}"
    if rt not in rule_types.BY_ID:
        return f"the model answered rule type {rt!r}, which is not on our list. {rule_types.refusal_text()}"
    if action not in actions:
        return f"action {action!r} is not in the schema"
    if action != rule_types.BY_ID[rt]["action"]:
        return f"{rt} guards {rule_types.BY_ID[rt]['action']}, but the model said the rule is about {action}"
    return None


def _note_readings(u, model_reading, code_reading, reason):
    u["readings"] = {"model": model_reading, "code": code_reading}
    _issue(u, REVIEW, f"{reason}: the model read {model_reading}; code read {code_reading}")


def _choose_reading(u, sentence, answer, actions):
    """The answer to go on with, or None when both sides refuse. Code's signals outrank a model refusal."""
    codes = signals.type_signals(sentence)
    refusal = _refusal(answer, actions)
    if refusal is None:
        if codes and answer["rule_type"] not in codes:
            _note_readings(u, answer["rule_type"], " or ".join(codes), "the two readings disagree")
        return answer
    if not codes:
        rule_type = answer.get("rule_type")
        if rule_type in rule_types.BY_ID:
            u["rule_type"], u["description"] = rule_type, rule_types.BY_ID[rule_type]["description"]
        _issue(u, INVALID, refusal)
        return None
    _note_readings(u, refusal, codes[0], "code recognizes this sentence but the model did not")
    return signals.code_answer(sentence, codes[0])


def check(sentence, answer, actions):
    """Apply the deterministic checks to the model's answer. Never adds a value the sentence lacks."""
    u = _start(sentence)
    answer = _choose_reading(u, sentence, answer, actions)
    if answer is None:
        return finish(u)
    u["rule_type"], u["description"] = answer["rule_type"], rule_types.BY_ID[answer["rule_type"]]["description"]
    u["action"] = answer["action"]
    inputs = actions[u["action"]]["input"]
    rule_type = rule_types.BY_ID[u["rule_type"]]
    defs = rule_types.slots_of(u["rule_type"])
    for name, d in defs.items():
        u["slots"][name] = {
            "source": "missing",
            "kind": d["kind"],
            "required": d["required"],
            "field": d["field"],
            "value": [] if d["kind"] == "list" else None,
            "spans": [],
        }
    hits = glossary.find_terms(sentence)
    stray, numberless = _read_slots(u, sentence, answer, defs)
    _apply_glossary_to_slots(u, hits)
    stated = _stated_spans(u)
    scope_span = _check_limit_scope(u, sentence)
    taken = stated + ([scope_span] if scope_span else [])
    vague = (answer.get("vague_terms") or []) + numberless + _code_vague_terms(sentence, defs)
    _read_vague(u, sentence, vague, defs, taken + hits)
    _read_qualifiers(u, sentence, answer, inputs, hits, taken)
    _check_stray_spans(u, stray, taken)
    _check_scope(u, rule_type["forms"])
    _check_roles(u, sentence)
    _check_numbers(u, sentence, stated)
    _check_values(u, sentence)
    return finish(u)


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
    u["examples_used"] = prompt["examples"]
    u["model"] = {
        **model.info(),
        "seconds": getattr(model, "last_seconds", None),
        "timing": getattr(model, "last_timing", None),
        "answer": answer,
    }
    return u


def readable(u):
    lines = [
        f"Sentence: {u['sentence']}",
        f"Type:     {u['rule_type'] or '-'}" + (f" (guards {u['action']})" if u["action"] else ""),
    ]
    for name, s in u["slots"].items():
        if s["source"] in FILLED:
            spans = ", ".join(f'"{x["text"]}" [{x["start"]}:{x["end"]}]' for x in s["spans"])
            lines.append(
                f"  {name} = {json.dumps(s['value'])}  stated: {spans}"
                + (f"  ({s['note']})" if s.get("note") else "")
                + (f"  [{s['direction_text']}]" if s.get("direction_text") else "")
            )
        else:
            lines.append(f"  {name}: missing ({'required' if s['required'] else 'optional'})")
    for v in u["vague_terms"]:
        lines.append(f'  vague: "{v["text"]}" [{v["start"]}:{v["end"]}]' + (f" for {v['slot']}" if v["slot"] else ""))
    for q in u["qualifiers"]:
        lines.append(f'  qualifier: "{q["text"]}" -> ' + (q["field"] if q["mapped"] else "no field"))
    for n in u["unassigned_numbers"]:
        lines.append(f'  unassigned {n["kind"]}: "{n["text"]}"')
    for r in u["rejected"]:
        lines.append(f"  {r['what']} {r['span']!r}: {r['reason']}")
    if "readings" in u:
        lines.append(f"  model read: {u['readings']['model']}")
        lines.append(f"  code read:  {u['readings']['code']}")
    lines.append(f"Verdict:  {u['verdict']} -- {u['verdict_reason']}")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sentence", nargs="?")
    ap.add_argument("--model", default=config.MODEL)
    ap.add_argument("--live-check", action="store_true", help="run the demo and unsupported sentences once each")
    ap.add_argument("--full", action="store_true", help="with --live-check: run the variants, repeats and seeded runs")
    ap.add_argument("--out", help="with --live-check: write the markdown report here")
    a = ap.parse_args(argv)
    try:
        if a.live_check:
            from app.core.live_check import run

            stopped = run(a.model, a.out, a.full)
            if stopped:
                sys.exit(f"error: {stopped}")
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
