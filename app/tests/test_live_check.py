"""The live check's runner and report, driven by fake models (no Ollama needed)."""

import json

from app.core import config, live_check, report
from app.core.model import ModelTimeout


class FakeOllama:
    def __init__(self, name, temperature=0, seed=0):
        self.name, self.temperature, self.seed = name, temperature, seed
        self.last_seconds = 0.1
        self.last_timing = {
            "total": 0.1,
            "load": 0.0,
            "prompt": 0.1,
            "prompt_tokens": 1100,
            "answer": 0.1,
            "answer_tokens": 10,
        }

    def info(self):
        return {"name": self.name, "temperature": self.temperature, "seed": self.seed}

    def complete(self, prompt):
        if "a100" in prompt["user"].lower():
            word = "a100" if "order a100" in prompt["user"] else "A100"
            return {
                "rule_type": "capped_with_approval",
                "action": "IssueRefund",
                "unsupported_reason": "",
                "slots": [
                    {"slot": "max_amount", "span": "$40", "value": "$40"},
                    {"slot": "order_id", "span": f"order {word}", "value": word},
                ],
                "vague_terms": [],
                "qualifiers": [],
            }
        return {
            "rule_type": "unsupported",
            "action": "none",
            "unsupported_reason": "no",
            "slots": [],
            "vague_terms": [],
            "qualifiers": [],
        }


class TimingOutOllama(FakeOllama):
    calls = 0

    def complete(self, prompt):
        type(self).calls += 1
        raise ModelTimeout("no answer")


def _quiet_ps(monkeypatch):
    monkeypatch.setattr(live_check, "ollama_ps", lambda: "NAME  ID")


def test_report_shows_values_as_typed(tmp_path, monkeypatch):
    monkeypatch.setattr(live_check, "OllamaModel", FakeOllama)
    _quiet_ps(monkeypatch)
    out = tmp_path / "understanding.md"
    live_check.run("fake", out, full=True)
    results = [line for line in out.read_text().splitlines() if "capped_with_approval" in line]
    upper = [line for line in results if line.startswith("| Refund up to $40 for order A100, subject to approval. |")]
    lower = [line for line in results if line.startswith("| refund up to $40 for order a100, subject to approval. |")]
    assert len(upper) == 2 and all('order_id="A100"' in line for line in upper)
    assert len(lower) == 1 and 'order_id="a100"' in lower[0]


def test_report_is_built_only_from_the_runs_file(tmp_path, monkeypatch):
    monkeypatch.setattr(live_check, "OllamaModel", FakeOllama)
    _quiet_ps(monkeypatch)
    out = tmp_path / "understanding.md"
    live_check.run("fake", out)
    first = out.read_text()
    out.unlink()
    report.write_understanding(tmp_path / "understanding_runs_default.json", out)
    assert out.read_text() == first
    assert "NAME  ID" in first


def test_default_run_is_each_sentence_once_at_temperature_0(tmp_path, monkeypatch):
    monkeypatch.setattr(live_check, "OllamaModel", FakeOllama)
    _quiet_ps(monkeypatch)
    live_check.run("fake", tmp_path / "understanding.md")
    data = json.loads((tmp_path / "understanding_runs_default.json").read_text())
    assert data["mode"] == "default"
    assert data["num_ctx"] == config.NUM_CTX
    expected = [d["sentence"] for d in live_check.DEMO] + live_check.UNSUPPORTED
    assert [r["sentence"] for r in data["runs"]][: len(expected)] == expected
    assert len({r["sentence"] for r in data["runs"]}) == len(data["runs"])
    assert {c["sentence"] for c in live_check.cases()} <= {r["sentence"] for r in data["runs"]}
    assert {r["temperature"] for r in data["runs"]} == {0}
    assert all(r["timing"]["prompt_tokens"] == 1100 for r in data["runs"])


def test_two_timeouts_stop_the_run_and_still_report(tmp_path, monkeypatch):
    monkeypatch.setattr(live_check, "OllamaModel", TimingOutOllama)
    _quiet_ps(monkeypatch)
    out = tmp_path / "understanding.md"
    stopped = live_check.run("fake", out)
    assert "2 timeouts" in stopped
    assert TimingOutOllama.calls == config.MAX_TIMEOUTS
    assert "Run stopped early" in out.read_text()


def test_full_run_is_the_repeated_and_seeded_set():
    calls = live_check.full_calls()
    assert len(calls) == (16 + len(live_check.UNSUPPORTED)) * live_check.FULL_REPEATS + (
        4 + len(live_check.UNSUPPORTED)
    ) * len(live_check.FULL_SEEDS)
