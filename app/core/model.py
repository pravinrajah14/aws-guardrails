"""The extraction prompt and the call to the local Ollama model."""
import json
import time
import urllib.error
import urllib.request

from app.core import config, rule_types

SYSTEM = """You read one plain-English rule that a manager wrote for an AI agent working in a shop, and describe it as JSON. You never fix, complete or guess anything the sentence does not say.

Supported rule types. These are the only ones:
{types}

If the sentence is not clearly one of these types, answer rule_type "unsupported" and action "none", and say why in unsupported_reason.

Tools the agent can call, with their input fields:
{tools}

How to fill the answer:
- action: the tool the rule guards.
- slots: one entry per value the sentence states for a slot of the chosen type. "span" is the shortest exact words copied from the sentence that state the value (for example "$75" or "3 hours", not the whole sentence). "value" is the value copied from the span. For a list slot, give one entry per item. Leave a slot out if the sentence does not state it; never fill it yourself.
- vague_terms: words that stand in for a slot value without giving an exact value, such as a time word with no number or a size word with no amount. Give the span and the slot it stands in for. Do not also put it in slots.
- qualifiers: every other word or phrase that narrows which tool calls the rule covers, such as an environment (e.g. "staging"), a customer group, a product, a region or a time of day. Give the span and the input field of the guarded tool it would set, or "none" if that tool has no such field. Words that only make the rule a rule ("only", "no", "never", "if", "subject to approval") are not qualifiers.

Worked examples (these sentences are only illustrations):
{examples}"""


# Illustrations for the model. Deliberately not the demo or test sentences.
EXAMPLES = [
    ("Staging deploys need tests that passed in the last 2 hours.",
     {"rule_type": "deploy_needs_tests", "action": "DeployService", "unsupported_reason": "",
      "slots": [{"slot": "window", "span": "2 hours", "value": "2 hours"}],
      "vague_terms": [], "qualifiers": [{"span": "Staging", "field": "env"}]}),
    ("Never refund unless a team lead approved it shortly before.",
     {"rule_type": "approval_required", "action": "IssueRefund", "unsupported_reason": "",
      "slots": [{"slot": "approver_role", "span": "a team lead", "value": "team lead"}],
      "vague_terms": [{"span": "shortly before", "slot": "window"}], "qualifiers": []}),
    ("No refunds above $250 for customers in Ohio.",
     {"rule_type": "amount_cap", "action": "IssueRefund", "unsupported_reason": "",
      "slots": [{"slot": "max_amount", "span": "$250", "value": "$250"}],
      "vague_terms": [], "qualifiers": [{"span": "for customers in Ohio", "field": "none"}]}),
    ("Never look up orders for VIP customers.",
     {"rule_type": "unsupported", "action": "none",
      "unsupported_reason": "This limits looking up orders, which none of the supported rule types cover.",
      "slots": [], "vague_terms": [], "qualifiers": []}),
]


def _type_lines():
    lines = []
    for t in rule_types.RULE_TYPES:
        slots = "; ".join(
            f"{s['name']} ({'required' if s['required'] else 'optional'}, {s['kind']}): {s['description']}"
            for s in t["slots"])
        examples = " / ".join(f'"{e}"' for e in t["examples"])
        lines.append(f"- {t['id']}: {t['description']} Guards {t['action']}. Slots: {slots}. Examples: {examples}")
    return "\n".join(lines)


def answer_format(actions):
    """JSON schema passed to Ollama's `format`. Code still checks every answer against the lists."""
    span = {"type": "string"}
    fields = sorted({f for a in actions.values() for f in a["input"]})
    return {
        "type": "object",
        "properties": {
            "rule_type": {"type": "string", "enum": rule_types.supported_ids() + ["unsupported"]},
            "action": {"type": "string", "enum": sorted(actions) + ["none"]},
            "unsupported_reason": {"type": "string"},
            "slots": {"type": "array", "items": {
                "type": "object",
                "properties": {"slot": {"type": "string", "enum": rule_types.SLOT_NAMES}, "span": span,
                               "value": {"type": "string"}},
                "required": ["slot", "span", "value"]}},
            "vague_terms": {"type": "array", "items": {
                "type": "object",
                "properties": {"span": span, "slot": {"type": "string", "enum": rule_types.SLOT_NAMES}},
                "required": ["span", "slot"]}},
            "qualifiers": {"type": "array", "items": {
                "type": "object",
                "properties": {"span": span, "field": {"type": "string", "enum": fields + ["none"]}},
                "required": ["span", "field"]}},
        },
        "required": ["rule_type", "action", "unsupported_reason", "slots", "vague_terms", "qualifiers"],
    }


def build_prompt(sentence, actions):
    tools = {a: sorted(v["input"]) for a, v in actions.items()}
    system = SYSTEM.format(types=_type_lines(),
                           tools="\n".join(f"- {a}: {', '.join(f)}" for a, f in tools.items()),
                           examples="\n".join(f"Sentence: {e}\nAnswer: {json.dumps(a)}" for e, a in EXAMPLES))
    return {
        "system": system,
        "user": f"Rule sentence: {sentence}",
        "format": answer_format(actions),
        # What the model was shown, for the UI.
        "context": {"rule_types": rule_types.supported_ids(), "tools": tools,
                    "schema": str(config.SCHEMA_PATH.relative_to(config.REPO))},
    }


class ModelError(RuntimeError):
    pass


class OllamaModel:
    def __init__(self, name=config.MODEL, url=config.OLLAMA_URL, temperature=config.TEMPERATURE, seed=config.SEED):
        self.name, self.temperature, self.seed = name, temperature, seed
        self.url = url.rstrip("/") + "/api/chat"

    def info(self):
        return {"name": self.name, "temperature": self.temperature, "seed": self.seed}

    def complete(self, prompt):
        body = {
            "model": self.name,
            "messages": [{"role": "system", "content": prompt["system"]},
                         {"role": "user", "content": prompt["user"]}],
            "format": prompt["format"],
            "stream": False,
            "options": {"temperature": self.temperature, "seed": self.seed},
        }
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        start = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                reply = json.loads(resp.read())
        except urllib.error.URLError as e:
            raise ModelError(f"could not reach Ollama at {self.url}; is it running? ({e})") from None
        self.last_seconds = round(time.monotonic() - start, 2)
        ns = 1e9
        self.last_timing = {"total": self.last_seconds, "load": round(reply.get("load_duration", 0) / ns, 2),
                            "prompt": round(reply.get("prompt_eval_duration", 0) / ns, 2),
                            "prompt_tokens": reply.get("prompt_eval_count"),
                            "answer": round(reply.get("eval_duration", 0) / ns, 2),
                            "answer_tokens": reply.get("eval_count")}
        text = reply.get("message", {}).get("content", "")
        try:
            answer = json.loads(text)
        except json.JSONDecodeError:
            raise ModelError(f"model did not return JSON: {text[:200]!r}") from None
        if not isinstance(answer, dict):
            raise ModelError(f"model returned {type(answer).__name__}, not an object")
        return answer
