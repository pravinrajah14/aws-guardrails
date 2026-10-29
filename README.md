# AWS Guardrails

Create a refund policy from the included rule forms, choose its approval window,
and test the saved policy with scripted checks and a local agent.

## Run it

Open a terminal in this repository and create a policy:

```sh
./policy create
```

The command displays both JSON rule forms and a situation where two meanings of
“recently” disagree. Answer
`block` to select a one-hour approval window, or `allow` to select 24 hours.
The command generates the selected policy, validates it with Dogwood and runs
the existing scripted rescore. It then prints the selected JSON form and generated
Dogwood policy so you can inspect them directly. No agent runs during this step.

Then test the saved policy:

```sh
./policy test
```

This runs the scripted checks again and sends the same policy to the guarded
agent. By default, it uses `qwen2.5:7b` and runs each of the eight agent tasks once.
It does not regenerate the policy or change expected answers.

Ollama must be running for testing. If needed, start it in another terminal:

```sh
ollama serve
```

Useful options:

| Command | Purpose |
|---|---|
| `./policy create --answer block` | Select the one-hour policy without prompting. |
| `./policy test --runs 3` | Run each agent task three times. |
| `./policy test --model qwen2.5:1.5b` | Use another installed model. |
| `./policy test --from-policy pipeline-runs/<creation-folder>` | Test an older creation. |

## First-time setup

You need Python 3.10+, Cargo, Dogwood and Ollama. If the dependencies are already
installed on this laptop, skip this section.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r question/requirements.txt
cargo install dogwood-cli
ollama pull qwen2.5:7b
```

Installation downloads dependencies and model weights. After setup, the demo runs
locally; no AWS account or cloud API key is used.

The launcher finds Python in `.venv`, the existing sibling `context-engine-lab`
environment, or `python3`. It looks for Dogwood on PATH or in `~/.cargo/bin`.
For another installation, set `GUARDRAILS_PYTHON` or `DOGWOOD_CMD` explicitly.

## Read the results

Each creation or test saves a new folder under `pipeline-runs/`. The terminal
prints its location. A test folder contains:

| File or folder | What it tells you |
|---|---|
| `SUMMARY.md` | Overall policy and agent scores. |
| `manifest.json` | Commands, input hashes and the creation used for testing. |
| `selected/updated_rule.dw` | The generated policy actually tested. |
| `selected/rule_scorecard.json` | Expected versus actual scripted decisions. |
| `agent/agent_scorecard_*.md` | Pass/fail for each agent task. |
| `agent/traces/<session>/timeline.txt` | Readable sequence of actions and decisions. |
| `agent/traces/<session>/events.jsonl` | Detailed, hash-chained session events. |

Input snapshots are also saved. Policy hashes confirm that the agent loaded the
selected generated rule. Testing refuses a policy edited after creation; create
it again instead. Existing output folders are never overwritten.

A nonzero test exit code can mean recorded test failures. Read the summary and
logs to distinguish failed tasks from a setup or execution error.

## Troubleshooting

| Message or situation | What to do |
|---|---|
| Python or Z3 is missing | Complete the first-time setup above. |
| Dogwood is missing | Install `dogwood-cli` or set `DOGWOOD_CMD` to its executable path. |
| Ollama is unavailable | Run `ollama serve` in another terminal. |
| The model is not installed | Run `ollama pull qwen2.5:7b`, or select an installed model with `--model`. |
| No saved policy exists | Run `./policy create` before testing. |
| The policy, schema or base permissions changed | Run `./policy create` again before testing. |
| An output folder already exists | Choose a new `--out` folder, or omit that option to use a timestamp. |
| Testing finishes with failures | Open `SUMMARY.md`, the scorecards and session timelines to inspect them. |

Choosing the 24-hour policy may disagree with scripted expectations written for
one hour. Those disagreements appear in the scorecard; expected answers are not
changed automatically.
