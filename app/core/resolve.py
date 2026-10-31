"""Fill a rule's gaps from saved answers, the glossary or examples, or ask: uv run python -m app.core.resolve "<sentence>" """

import argparse
import json
import sys

from app.core import answers, config, examples, glossary, rule_types, signals
from app.core.model import ModelError, OllamaModel
from app.core.numbers import amount_json, find_amounts, find_durations, window
from app.core.understand import INVALID, LIMIT_SCOPE, REVIEW, finish, readable, understand
from converter.convert import load_schema

NOT_STATED = "(not stated)"
EACH_REFUND, A_TOTAL = "each refund", "a total"
STATED, FILLED, ASKED, FLAGGED = "stated", "filled", "asked", "review"


class AnswerError(ValueError):
    pass


def _term(u, field):
    if field == LIMIT_SCOPE:
        return u["limit_scope"]["text"].lower()
    vague = next((v for v in u["vague_terms"] if v["slot"] == field), None)
    return vague["text"].lower() if vague else NOT_STATED


def gaps(u):
    """Fields a value is still owed for: {field, term, required}. Optional slots are filled when a source exists, never asked."""
    found = []
    for name, slot in u["slots"].items():
        if slot["source"] == "missing":
            found.append({"field": name, "term": _term(u, name), "required": slot["required"]})
    if "limit_scope" in u:
        found.append({"field": LIMIT_SCOPE, "term": _term(u, LIMIT_SCOPE), "required": True})
    return found


def _parse_window(raw):
    found = find_durations(raw) or find_durations("".join(raw.split()))
    if len(found) != 1:
        raise AnswerError(f"{raw!r} is not one length of time; try 1h or 30 minutes")
    return window(found[0]["seconds"])


def _parse_amount(raw):
    found = find_amounts(raw) or find_amounts("$" + raw)
    if len(found) != 1:
        raise AnswerError(f"{raw!r} is not one dollar amount")
    value = amount_json(found[0]["value"])
    if isinstance(value, str):
        raise AnswerError(f"{raw!r} has cents; amounts are whole dollars")
    return value


def _parse_role(raw):
    known = glossary.known_values("approver_role")
    if raw.strip().lower() not in known:
        raise AnswerError(f"{raw!r} is not a role the store knows: {', '.join(known)}")
    return raw.strip().lower()


def _parse_order_id(raw):
    if not glossary.is_order_id(raw.strip()):
        raise AnswerError(f"{raw!r} is not an order id like A100")
    return raw.strip()


def _parse_scope(raw):
    text = raw.strip().lower()
    if text in (EACH_REFUND, "each"):
        return EACH_REFUND
    if text in (A_TOTAL, "total"):
        return A_TOTAL
    raise AnswerError(f"{raw!r}: answer '{EACH_REFUND}' or '{A_TOTAL}'")


def _parse_services(raw):
    return [text for text, _, _ in signals.split_list(raw)]


PARSERS = {
    "window": _parse_window,
    "max_amount": _parse_amount,
    "approver_role": _parse_role,
    "order_id": _parse_order_id,
    "services": _parse_services,
    LIMIT_SCOPE: _parse_scope,
}


def _stored(value):
    return json.dumps(value) if isinstance(value, list | int) else value


def _loaded(field, stored):
    if field == "services" or field == "max_amount":
        return json.loads(stored)
    return stored


def _record_answers(u, given, picks, sentence):
    """Save this request's answers. Order ids apply to this request only: they never go in the file."""
    open_gaps = {g["field"]: g for g in gaps(u)}
    transient = {}
    for field in {**given, **picks}:
        if field not in open_gaps:
            raise AnswerError(f"this sentence has no open question about {field!r}")
        if field not in PARSERS:
            raise AnswerError(f"cannot take an answer for {field!r}")
    for field, raw in given.items():
        value = PARSERS[field](raw)
        if field == "order_id":
            transient[field] = value
        else:
            answers.save(u["rule_type"], field, open_gaps[field]["term"], _stored(value), sentence)
    for field, raw in picks.items():
        stored = _stored(PARSERS[field](raw))
        answers.pick(u["rule_type"], field, open_gaps[field]["term"], stored)
    return transient


def _drop_issues(u, field):
    u["issues"] = [i for i in u["issues"] if i.get("field") != field]


def _apply_value(u, gap, value, how, evidence):
    field = gap["field"]
    _drop_issues(u, field)
    if field == LIMIT_SCOPE:
        u["limit_scope"].update(answer=value, how=how, evidence=evidence)
        if value == A_TOTAL:
            total = u["limit_scope"]["total"]
            u["issues"].append(
                {
                    "verdict": INVALID,
                    "reason": f"a limit on {total} can't be enforced yet; only a limit on each refund can",
                    "field": None,
                }
            )
        return
    slot = u["slots"][field]
    slot.update(source=how, value=value, evidence=evidence)
    if field == "window":
        slot["seconds"] = find_durations(value)[0]["seconds"]


def _saved_evidence(row):
    return f"from saved answer ({row['answered_by']}, {row['answered_at']})"


def _fill_from_saved(u, gap, given):
    rows = answers.find(u["rule_type"], gap["field"], gap["term"])
    if not rows:
        return False
    values = list(dict.fromkeys(row["value"] for row in rows))
    if len(values) > 1:
        _drop_issues(u, gap["field"])
        u["conflicts"].append({"field": gap["field"], "term": gap["term"], "rows": rows})
        shown = " or ".join(repr(v) for v in values)
        u["issues"].append(
            {
                "verdict": REVIEW,
                "reason": f"saved answers for {gap['term']!r} disagree: {shown}; a person must pick one",
                "field": None,
            }
        )
        return True
    how = ASKED if gap["field"] in given else "saved_answer"
    _apply_value(u, gap, _loaded(gap["field"], rows[-1]["value"]), how, _saved_evidence(rows[-1]))
    return True


def _fill_from_glossary(u, gap):
    entry = glossary.gap_entry(gap["field"], gap["term"])
    if entry is None:
        return False
    _apply_value(u, gap, entry["value"], "glossary", f"from glossary ({entry['source']})")
    return True


def _question_text(u, gap):
    if gap["field"] == LIMIT_SCOPE:
        return u["limit_scope"]["question"]
    description = rule_types.slots_of(u["rule_type"])[gap["field"]]["description"]
    if gap["term"] == NOT_STATED:
        return f"The sentence does not say {gap['field']}. What is it? ({description})"
    return f"What does {gap['term']!r} mean for {gap['field']}? ({description})"


def _question(u, gap, evidence):
    values = list(dict.fromkeys(json.dumps(e["value"]) for e in evidence))
    proposal = json.loads(values[0]) if len(values) == 1 else None
    question = {
        "field": gap["field"],
        "term": gap["term"],
        "question": _question_text(u, gap),
        "proposal": proposal,
        "examples_disagree": len(values) > 1,
        "evidence": evidence,
    }
    if gap["field"] == LIMIT_SCOPE:
        question["options"] = u["limit_scope"]["options"]
    return question


def _fill_gaps(u, given, transient):
    questions = []
    for gap in gaps(u):
        field = gap["field"]
        if field in transient:
            _apply_value(u, gap, transient[field], ASKED, "from your answer; not saved, since it is an order id")
        elif not (_fill_from_saved(u, gap, given) or _fill_from_glossary(u, gap)) and gap["required"]:
            evidence = [] if field == LIMIT_SCOPE else examples.stating(u["sentence"], u["rule_type"], field)
            questions.append(_question(u, gap, evidence))
    return questions


def _vague_without_a_slot(u):
    return [
        {
            "field": None,
            "term": v["text"].lower(),
            "question": f"{v['text']!r} is vague and no field takes it; please reword the rule.",
            "proposal": None,
            "examples_disagree": False,
            "evidence": [],
        }
        for v in u["vague_terms"]
        if v["slot"] is None
    ]


def _fields_used(rule_type):
    used = {}

    def add(action, fields):
        used.setdefault(action, set()).update(fields)

    for form in rule_types.BY_ID[rule_type]["forms"]:
        fields = [form[k] for k in ("field",) if k in form]
        if "only_when" in form:
            fields.append(form["only_when"]["field"])
        requires = form.get("requires")
        if requires:
            fields += requires.get("same", [])
            fields += requires.get("at_most", {})
            add(requires["action"], requires.get("same", []))
            add(requires["action"], [f"output.{k}" for k in requires.get("output", {})])
        add(form["action"], fields)
    return used


def build_context(u):
    """What this sentence was read against, each item with where it came from."""
    schema = str(config.SCHEMA_PATH.relative_to(config.REPO))
    _, actions = load_schema(config.SCHEMA_PATH)
    tools = []
    if u["rule_type"]:
        tools = [
            {"tool": action, "fields_used": sorted(fields), "source": schema}
            for action, fields in _fields_used(u["rule_type"]).items()
            if action in actions
        ]
    return {
        "schema": tools,
        "glossary": [
            {"text": h["text"], "field": h["field"], "value": h["value"], "source": h["source"]}
            for h in glossary.find_terms(u["sentence"])
        ],
        "examples": examples.with_weights(examples.similar_others(u["sentence"])),
        "saved_answers": answers.for_rule_type(u["rule_type"]) if u["rule_type"] else [],
    }


def _span(item, source, field, note=""):
    return {
        "text": item["text"],
        "start": item["start"],
        "end": item["end"],
        "source": source,
        "field": field,
        "note": note,
    }


def _slot_source(name, slot, at):
    if slot["source"] == "stated" or at["text"].casefold() == str(slot["value"]).casefold():
        return STATED
    return ASKED if slot["source"] == ASKED else FILLED


def _slot_spans(u):
    out = []
    for name, slot in u["slots"].items():
        if slot["source"] in ("stated", "glossary"):
            for at in [slot["value_at"]] if slot.get("value_at") else slot.get("values_at", []):
                note = f"from glossary ({slot['glossary_file']})" if slot.get("glossary_file") else ""
                out.append(_span(at, _slot_source(name, slot, at), name, note))
    return out


def _term_spans(u, questions):
    open_fields = {q["field"] for q in questions}
    out = []
    for v in u["vague_terms"]:
        slot = u["slots"].get(v["slot"]) or {}
        if v["slot"] in open_fields or not slot or slot["source"] == ASKED:
            out.append(_span(v, ASKED, v["slot"]))
        else:
            out.append(_span(v, FILLED, v["slot"], slot.get("evidence", "")))
    scope = u.get("limit_scope")
    if scope:
        source = FILLED if scope.get("how") in ("saved_answer", "glossary") else ASKED
        out.append(_span(scope, source, LIMIT_SCOPE, scope.get("evidence", "")))
    return out


def highlights(u, questions):
    """Where each part of the sentence got its meaning, for the UI's marks. Ordered, not overlapping."""
    spans = _slot_spans(u) + _term_spans(u, questions)
    for q in u["qualifiers"]:
        if q["mapped"] and q.get("glossary_file"):
            spans.append(_span(q, FILLED, q["field"], f"{q['value']} from glossary ({q['glossary_file']})"))
        elif not q["mapped"]:
            spans.append(_span(q, FLAGGED, None, "no field to map this to"))
    spans += [_span(n, FLAGGED, None, "no slot uses this") for n in u["unassigned_numbers"]]
    ordered, end = [], 0
    for span in sorted(spans, key=lambda s: (s["start"], s["end"])):
        if span["start"] >= end:
            ordered.append(span)
            end = span["end"]
    return ordered


def resolve(sentence, model=None, given=None, picks=None):
    """given and picks map a field to the user's raw answer. Raises AnswerError on an answer that can't be used."""
    given, picks = given or {}, picks or {}
    u = understand(sentence, model)
    u["conflicts"] = []
    context = build_context(u)
    if u["verdict"] == INVALID:
        return {"understanding": u, "context": context, "questions": [], "highlights": []}
    transient = _record_answers(u, given, picks, sentence)
    questions = _fill_gaps(u, given, transient) + _vague_without_a_slot(u)
    context["saved_answers"] = answers.for_rule_type(u["rule_type"])
    finish(u)
    return {"understanding": u, "context": context, "questions": questions, "highlights": highlights(u, questions)}


def _pairs(items, ap):
    pairs = {}
    for item in items or []:
        if "=" not in item:
            ap.error(f"{item!r}: expected field=value")
        field, value = item.split("=", 1)
        pairs[field.strip()] = value.strip()
    return pairs


def _print_question(q):
    print(f"Question: {q['question']}")
    if q["proposal"] is not None:
        print(f"  proposed: {q['proposal']} (confirm with --answer {q['field']}={q['proposal']})")
    elif q["examples_disagree"]:
        print("  the examples disagree:")
    for e in q["evidence"]:
        note = f"  ({e['note']})" if e["note"] else ""
        print(f"  {e['value']}  weight {e['weight']}  {e['id']}: {e['sentence']}{note}")


def print_result(result):
    u = result["understanding"]
    print(readable(u))
    for name, slot in u["slots"].items():
        if slot.get("evidence"):
            print(f"  {name} = {json.dumps(slot['value'])}  {slot['evidence']}")
    scope = u.get("limit_scope")
    if scope and scope.get("answer"):
        print(f"  limit scope = {scope['answer']}  {scope['evidence']}")
    for q in result["questions"]:
        _print_question(q)
    context = result["context"]
    print("Context:")
    for tool in context["schema"]:
        print(f"  tool {tool['tool']}: {', '.join(tool['fields_used'])} ({tool['source']})")
    for hit in context["glossary"]:
        print(f"  glossary {hit['text']!r} -> {hit['field']}={hit['value']} ({hit['source']})")
    for e in context["examples"]:
        print(f"  example {e['id']} weight {e['weight']}: {e['sentence']} ({e['source']})")
    for row in context["saved_answers"]:
        print(f"  saved {row['field']} {row['term']!r} = {row['value']} ({row['answered_by']}, {row['answered_at']})")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("sentence")
    ap.add_argument("--answer", action="append", metavar="field=value", help="answer a question; saved for next time")
    ap.add_argument("--pick", action="append", metavar="field=value", help="settle conflicting saved answers")
    a = ap.parse_args(argv)
    try:
        result = resolve(a.sentence, OllamaModel(), _pairs(a.answer, ap), _pairs(a.pick, ap))
    except (ModelError, AnswerError) as e:
        sys.exit(f"error: {e}")
    print_result(result)


if __name__ == "__main__":
    main()
