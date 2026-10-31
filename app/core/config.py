"""Settings for the core. Environment variables override the defaults."""

import getpass
import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

OLLAMA_URL = os.environ.get("GUARDRAILS_OLLAMA_URL", "http://127.0.0.1:11434")
# Extraction and the agent share this one model so Ollama only ever loads one.
MODEL = os.environ.get("GUARDRAILS_MODEL", "qwen2.5:7b")
TEMPERATURE = 0
SEED = 0
# The largest measured prompt (1375 tokens extraction, 1340 agent) plus 25%, rounded up to a power of two.
# Memory grows with this, and Ollama reloads the model when it changes, so every call must send the same value.
NUM_CTX = int(os.environ.get("GUARDRAILS_NUM_CTX", "2048"))
KEEP_ALIVE = "30m"
CONTEXT_WARN_FRACTION = 0.9
LOAD_WARN_SECONDS = 1
CALL_TIMEOUT_SECONDS = 60
MAX_TIMEOUTS = 2
# The action schema the converter checks forms against (converter/convert.py --schema default).
SCHEMA_PATH = Path(
    os.environ.get("GUARDRAILS_SCHEMA", REPO / "kylie-test-bed-upload" / "policies" / "schema.cedarschema")
)
ANSWERS_PATH = Path(os.environ.get("GUARDRAILS_ANSWERS", REPO / "data" / "answers.sqlite"))
ANSWERED_BY = os.environ.get("GUARDRAILS_ANSWERED_BY", getpass.getuser())


def call_warnings(prompt_tokens, load_seconds, first_call):
    """What to tell the operator about one model call: a prompt close to num_ctx, or a surprise reload."""
    warnings = []
    if prompt_tokens and prompt_tokens >= CONTEXT_WARN_FRACTION * NUM_CTX:
        warnings.append(f"prompt of {prompt_tokens} tokens is within 10% of num_ctx {NUM_CTX}")
    if not first_call and load_seconds >= LOAD_WARN_SECONDS:
        warnings.append(f"model load took {load_seconds:.2f}s after the first call; it was reloaded")
    return warnings
