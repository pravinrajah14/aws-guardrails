"""The live check's report, driven by a fake model (no Ollama needed)."""
from app.core import live_check


class FakeOllama:
    def __init__(self, name, temperature=0, seed=0):
        self.name, self.temperature, self.seed = name, temperature, seed
        self.last_seconds = 0.1
        self.last_timing = {"total": 0.1, "load": 0.0, "prompt": 0.1, "prompt_tokens": 10,
                            "answer": 0.1, "answer_tokens": 10}

    def info(self):
        return {"name": self.name, "temperature": self.temperature, "seed": self.seed}

    def complete(self, prompt):
        if "a100" in prompt["user"].lower():
            word = "a100" if "order a100" in prompt["user"] else "A100"
            return {"rule_type": "capped_with_approval", "action": "IssueRefund", "unsupported_reason": "",
                    "slots": [{"slot": "max_amount", "span": "$40", "value": "$40"},
                              {"slot": "order_id", "span": f"order {word}", "value": word}],
                    "vague_terms": [], "qualifiers": []}
        return {"rule_type": "unsupported", "action": "none", "unsupported_reason": "no", "slots": [],
                "vague_terms": [], "qualifiers": []}


def test_report_shows_values_as_typed(tmp_path, monkeypatch):
    monkeypatch.setattr(live_check, "OllamaModel", FakeOllama)
    out = tmp_path / "understanding.md"
    live_check.run("fake", out)
    lines = out.read_text().splitlines()
    typed_upper = [l for l in lines if l.startswith("| Refund up to $40 for order A100, subject to approval. |")]
    typed_lower = [l for l in lines if l.startswith("| refund up to $40 for order a100, subject to approval. |")]
    assert len(typed_upper) == 2 and all('order_id="A100"' in l for l in typed_upper)  # both tables
    assert len(typed_lower) == 1 and 'order_id="a100"' in typed_lower[0]
    assert (tmp_path / "understanding_runs.json").exists()
