"""Check what the demo needs, warm up the model, start the server and open the browser."""

import argparse
import http.client
import json
import os
import shutil
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

import uvicorn

from app.core import answers, config

HOST = "127.0.0.1"
PORT = int(os.environ.get("GUARDRAILS_PORT", "8000"))
PORT_TRIES = 100
TAGS_TIMEOUT_SECONDS = 5
OLLAMA_WAIT_SECONDS = 30
OLLAMA_RETRY_SECONDS = 1
NO_ANSWER_ERRORS = (OSError, http.client.HTTPException, ValueError, KeyError, TypeError)
WARM_UP_TIMEOUT_SECONDS = 180
BROWSER_DELAY_SECONDS = 1


def check_dogwood():
    if os.environ.get("DOGWOOD_CMD") or shutil.which("dogwood"):
        return
    cargo = Path.home() / ".cargo" / "bin" / "dogwood"
    if not cargo.is_file():
        sys.exit("Dogwood is missing. Install it with: cargo install dogwood-cli")
    os.environ["DOGWOOD_CMD"] = str(cargo)


def _installed_models(url, timeout):
    with urllib.request.urlopen(f"{url}/api/tags", timeout=timeout) as response:
        return {m["name"] for m in json.load(response)["models"]}


def _gave_up_message(url, error):
    if isinstance(getattr(error, "reason", error), ConnectionRefusedError):
        return f"Ollama is not running: nothing is listening at {url}. Start it with: ollama serve"
    return f"Ollama is running at {url} but not answering ({error}). Restart it with: brew services restart ollama"


def _wait_for_models(url):
    deadline = time.monotonic() + OLLAMA_WAIT_SECONDS
    error = None
    while (left := deadline - time.monotonic()) > 0:
        if error:
            print(f"Waiting for Ollama at {url} ({left:.0f}s left)...", flush=True)
        try:
            return _installed_models(url, min(TAGS_TIMEOUT_SECONDS, left))
        except NO_ANSWER_ERRORS as e:
            error = e
        time.sleep(min(OLLAMA_RETRY_SECONDS, left))
    sys.exit(_gave_up_message(url, error))


def check_ollama():
    models = _wait_for_models(config.OLLAMA_URL.rstrip("/"))
    if config.MODEL not in models:
        sys.exit(f"Model {config.MODEL} is not installed. Get it with: ollama pull {config.MODEL}")


def _is_free(port):
    with socket.socket() as probe:
        # A server on every interface doesn't stop the bind below, since SO_REUSEADDR allows the narrower address.
        if probe.connect_ex((HOST, port)) == 0:
            return False
    with socket.socket() as probe:
        # uvicorn sets this too, so a port still in TIME_WAIT counts as free.
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((HOST, port))
        except OSError:
            return False
    return True


def free_port(start):
    for port in range(start, start + PORT_TRIES):
        if _is_free(port):
            return port
    sys.exit(f"No free port from {start} to {start + PORT_TRIES - 1}. Pick another start: GUARDRAILS_PORT=9000 ./demo")


def warm_up():
    """Load the model with the same num_ctx every call sends, so the first sentence doesn't pay for the load."""
    print(f"Warming up {config.MODEL}...", flush=True)
    body = {
        "model": config.MODEL,
        "messages": [{"role": "user", "content": "ok"}],
        "stream": False,
        "keep_alive": config.KEEP_ALIVE,
        "options": {"num_ctx": config.NUM_CTX, "num_predict": 1, "temperature": config.TEMPERATURE},
    }
    request = urllib.request.Request(
        config.OLLAMA_URL.rstrip("/") + "/api/chat",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=WARM_UP_TIMEOUT_SECONDS) as response:
        response.read()


def clear_saved_answers():
    backup, cleared = answers.backup_and_clear()
    print(
        f"Backed up {cleared} saved answers to {backup} and cleared them." if backup else "No saved answers to clear."
    )


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fresh", action="store_true", help="back up the saved answers to data/, then clear them")
    ap.add_argument("--no-browser", action="store_true", help="don't open the browser")
    args = ap.parse_args(argv)
    check_dogwood()
    check_ollama()
    if args.fresh:
        clear_saved_answers()
    warm_up()
    # Chosen after the slow warm-up so the port is still free when uvicorn binds it.
    port = free_port(PORT)
    if port != PORT:
        print(f"Port {PORT} is taken, so using {port}.", flush=True)
    url = f"http://{HOST}:{port}"
    print(f"Open {url}", flush=True)
    if not args.no_browser:
        threading.Timer(BROWSER_DELAY_SECONDS, webbrowser.open, [url]).start()
    uvicorn.run("app.web.server:app", host=HOST, port=port, log_level="warning")


if __name__ == "__main__":
    main()
