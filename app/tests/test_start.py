"""The demo's startup checks against real local sockets."""

import http.server
import json
import socket
import threading

import pytest

from app.core import config
from app.web import start

MODEL = "fake:1b"
TAGS = json.dumps({"models": [{"name": MODEL}]}).encode()


@pytest.fixture(autouse=True)
def short_waits(monkeypatch):
    monkeypatch.setattr(start, "OLLAMA_WAIT_SECONDS", 0.6)
    monkeypatch.setattr(start, "OLLAMA_RETRY_SECONDS", 0.05)
    monkeypatch.setattr(start, "TAGS_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(config, "MODEL", MODEL)


@pytest.fixture
def listener():
    """Accepts connections and never answers them, like a hung server."""
    with socket.socket() as sock:
        sock.bind((start.HOST, 0))
        sock.listen()
        yield sock.getsockname()[1]


def _unused_port():
    with socket.socket() as sock:
        sock.bind((start.HOST, 0))
        return sock.getsockname()[1]


def _serve(monkeypatch, replies):
    """An Ollama stand-in giving each (status, body) in turn, repeating the last."""
    remaining = list(replies)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            status, body = remaining.pop(0) if len(remaining) > 1 else remaining[0]
            self.send_response(status)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer((start.HOST, 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(config, "OLLAMA_URL", f"http://{start.HOST}:{server.server_port}")
    return server


def test_retries_until_ollama_answers_and_says_it_is_waiting(monkeypatch, capsys):
    server = _serve(monkeypatch, [(500, b"loading"), (200, b"not json"), (200, TAGS)])
    start.check_ollama()
    server.shutdown()
    assert capsys.readouterr().out.count("Waiting for Ollama") == 2


def test_nothing_listening_gives_up_saying_not_running(monkeypatch, capsys):
    monkeypatch.setattr(config, "OLLAMA_URL", f"http://{start.HOST}:{_unused_port()}")
    with pytest.raises(SystemExit, match="Ollama is not running"):
        start.check_ollama()
    assert "Waiting for Ollama" in capsys.readouterr().out


def test_a_hung_server_gives_up_saying_not_answering(monkeypatch, listener):
    monkeypatch.setattr(config, "OLLAMA_URL", f"http://{start.HOST}:{listener}")
    with pytest.raises(SystemExit, match="running at .* but not answering"):
        start.check_ollama()


def test_errors_until_the_deadline_give_up_saying_not_answering(monkeypatch):
    server = _serve(monkeypatch, [(500, b"broken")])
    with pytest.raises(SystemExit, match="running at .* but not answering"):
        start.check_ollama()
    server.shutdown()


def test_a_missing_model_stops_at_once_without_waiting(monkeypatch, capsys):
    server = _serve(monkeypatch, [(200, json.dumps({"models": []}).encode())])
    with pytest.raises(SystemExit, match="is not installed"):
        start.check_ollama()
    server.shutdown()
    assert "Waiting" not in capsys.readouterr().out


def test_a_taken_port_is_skipped_and_left_running(listener):
    assert start.free_port(listener) > listener
    with socket.create_connection((start.HOST, listener)):
        pass


def test_a_free_port_is_used_as_is():
    port = _unused_port()
    assert start.free_port(port) == port


def test_a_server_on_every_interface_counts_as_taken():
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", 0))
        sock.listen()
        assert not start._is_free(sock.getsockname()[1])


def test_main_serves_and_opens_the_port_it_picked(monkeypatch, listener, capsys):
    served, opened = [], []
    monkeypatch.setattr(start, "PORT", listener)
    monkeypatch.setattr(start, "check_dogwood", lambda: None)
    monkeypatch.setattr(start, "check_ollama", lambda: None)
    monkeypatch.setattr(start, "warm_up", lambda: None)
    monkeypatch.setattr(start.uvicorn, "run", lambda *a, **k: served.append(k["port"]))
    monkeypatch.setattr(start.webbrowser, "open", opened.append)
    monkeypatch.setattr(start, "BROWSER_DELAY_SECONDS", 0)
    timers = []
    real_timer = threading.Timer
    monkeypatch.setattr(start.threading, "Timer", lambda *a: timers.append(real_timer(*a)) or timers[-1])
    start.main([])
    timers[0].join()
    [port] = served
    url = f"http://{start.HOST}:{port}"
    assert port != listener and opened == [url]
    out = capsys.readouterr().out
    assert f"Open {url}" in out and f"Port {listener} is taken" in out
