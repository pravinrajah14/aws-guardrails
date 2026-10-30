"""Settings for the core. Environment variables override the defaults."""
import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

OLLAMA_URL = os.environ.get("GUARDRAILS_OLLAMA_URL", "http://127.0.0.1:11434")
MODEL = os.environ.get("GUARDRAILS_MODEL", "qwen2.5:7b")
TEMPERATURE = 0
SEED = 0
# The action schema the converter checks forms against (converter/convert.py --schema default).
SCHEMA_PATH = Path(os.environ.get("GUARDRAILS_SCHEMA",
                                  REPO / "kylie-test-bed-upload" / "policies" / "schema.cedarschema"))
