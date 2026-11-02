This takes a rule written in plain English, turns it into a Dogwood policy, checks it, and runs a local AI agent in a fake store with every action being checked against that policy.

## Setup

On macOS with Homebrew:

    brew install uv ollama rust
    cargo install dogwood-cli
    brew services start ollama
    ollama pull qwen2.5:7b
    uv sync

## Run

    ./demo

It checks that everything is there, warms up the model, and opens http://127.0.0.1:8000. Once a rule is ready, the Check and run tab lets you run the agent against it. Run `./demo --fresh` to run it with no saved answers.

## Known Problems
- Stuck on "Waiting for Ollama": run `brew services restart ollama`, then `./demo` again.
- "dogwood not found": open a new terminal, or run `source "$HOME/.cargo/env"`.
- Port 8000 is busy: the app uses the next free port and prints the address.

## Future Work
- More rule types
- Amounts with cents, not just whole dollars
- Saved answers for a whole team
- Running policies in AWS AgentCore instead of our local fake store.
- Setup steps for Windows.
