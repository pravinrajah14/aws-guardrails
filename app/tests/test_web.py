"""The web layer with a fake model: the streaming API and what the page is allowed to load."""

import json
import re
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core import config, generate
from app.tests.test_flow import FakeModel
from app.web import server

STATIC = Path(server.__file__).parent / "static"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ANSWERS_PATH", tmp_path / "answers.sqlite")
    monkeypatch.setattr(generate, "POLICIES_ROOT", tmp_path / "policies")
    monkeypatch.setattr(server, "model", FakeModel())
    return TestClient(server.app)


def events(response):
    assert response.status_code == 200
    return [json.loads(line) for line in response.text.splitlines()]


def test_policy_streams_every_step_and_ends_with_status(client):
    got = events(client.post("/api/policy", json={"sentence": "No refunds over $5,000."}))
    steps = [e for e in got if e["type"] == "step"]
    assert steps[-1]["step"] == "Status" and steps[-1]["status"] == "ready"
    policy = client.get(f"/api/policy/{steps[-1]['policy']}").json()
    assert "amount > 5000" in policy["dogwood"] and policy["summary"]


def test_answering_through_the_api_saves_it(client):
    sentence = "Only refund if a manager approved it recently."
    first = events(client.post("/api/policy", json={"sentence": sentence}))
    assert first[-1]["status"] == "needs clarification"
    client.post("/api/policy", json={"sentence": sentence, "answers": {"window": "1h"}})
    assert events(client.post("/api/policy", json={"sentence": sentence}))[-1]["status"] == "ready"


def test_reset_backs_up_the_answers_and_the_sentence_asks_again(client):
    sentence = "Only refund if a manager approved it recently."
    client.post("/api/policy", json={"sentence": sentence, "answers": {"window": "1h"}})
    assert events(client.post("/api/policy", json={"sentence": sentence}))[-1]["status"] == "ready"
    reset = client.post("/api/answers/reset").json()
    assert reset["cleared"] == 1 and re.fullmatch(r"answers-backup-\d{8}-\d{6}\.json", reset["backup"])
    [row] = json.loads((config.ANSWERS_PATH.parent / reset["backup"]).read_text(encoding="utf-8"))
    assert (row["term"], row["value"]) == ("recently", "1h")
    assert events(client.post("/api/policy", json={"sentence": sentence}))[-1]["status"] == "needs clarification"


def test_reset_with_nothing_saved_writes_no_backup(client):
    assert client.post("/api/answers/reset").json() == {"cleared": 0, "backup": None}
    assert not list(config.ANSWERS_PATH.parent.glob("answers-backup-*"))


def test_a_bad_answer_is_reported_and_the_rule_still_reads(client):
    got = events(
        client.post(
            "/api/policy",
            json={"sentence": "Only refund if a manager approved it recently.", "answers": {"window": "soon"}},
        )
    )
    assert any(e["type"] == "answer_error" for e in got) and got[-1]["status"] == "needs clarification"


def test_model_requests_wait_their_turn(client):
    server.model_lock.acquire()
    threading.Timer(0.2, server.model_lock.release).start()
    got = events(client.post("/api/policy", json={"sentence": "No refunds over $5,000."}))
    assert got[0] == {"type": "waiting"} and got[-1]["status"] == "ready"


def test_setup_lists_demo_sentences_and_steps(client):
    setup = client.get("/api/setup").json()
    assert len(setup["demo_sentences"]) == 5
    assert setup["steps"][0] == "Understand" and setup["steps"][-1] == "Status"
    assert "refund_different_amount" in {t["id"] for t in setup["tasks"]}


def test_unknown_policy_and_log_are_404(client):
    assert client.get("/api/policy/nope").status_code == 404
    assert client.get("/logs/nope/nope/timeline.txt").status_code == 404
    assert client.get("/logs/nope/nope/secrets.txt").status_code == 404
    assert client.post("/api/run", json={"policy_id": "nope", "task_id": "x"}).status_code == 404


def test_page_loads_nothing_from_the_internet():
    for path in STATIC.iterdir():
        assert not re.search(r"https?://|//cdn|@import", path.read_text(encoding="utf-8")), path
