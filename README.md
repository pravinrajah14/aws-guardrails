# AWS Guardrails

This takes a rule written in plain English, turns it into a Dogwood policy, checks it, and runs a local AI agent in a fake store with every action going through that policy. It runs on your laptop with no cloud accounts.

## Setup

On macOS, with Homebrew:

    brew install uv ollama rust
    cargo install dogwood-cli
    brew services start ollama
    ollama pull qwen2.5:7b
    uv sync

After this you don't need the internet.

## Run

    ./demo

It checks that everything is there, warms up the model, and opens http://127.0.0.1:8000. If 8000 is taken it uses the next free port and prints the URL. Pick a demo sentence or write your own rule. Once a rule is ready, the Check and run tab lets you run the agent against it.

Stop it with Ctrl-C.

To start with no saved answers, run `./demo --fresh`, or use "Reset saved answers" on the page. Both write the answers to `data/answers-backup-<timestamp>.json` first.

## Problems

`./demo` waits up to 30 seconds for Ollama and prints "Waiting for Ollama" while it does.

"Ollama is not running": start it with `brew services start ollama`, or run `ollama serve` in another terminal.

"Ollama is running but not answering": restart it with `brew services restart ollama`.

"Model qwen2.5:7b is not installed": run `ollama pull qwen2.5:7b`. This one needs the internet.

"Dogwood is missing": run `cargo install dogwood-cli`, and make sure `~/.cargo/bin` is on your PATH.
