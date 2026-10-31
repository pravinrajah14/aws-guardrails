#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

uv run ruff check .
uv run ruff format --check .
uv run pytest -q
uv run python converter/test_behavior.py
uv run python scripts/score_lists.py
