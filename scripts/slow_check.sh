#!/usr/bin/env bash
# Real model: the default live check, then one run of every agent task. Results go under pipeline-runs/.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
out="$root/pipeline-runs/slow-check-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$out"
cd "$root"

uv run python -m app.core.understand --live-check
uv run python -c "
import json, sys
from app.core import report
data = json.loads(report.RUNS_PATH.read_text())
bad = [r['sentence'] for r in data['runs'] if 'error' in r or not report.matches(r, r['expect'])]
print(f\"live check: {len(data['runs']) - len(bad)}/{len(data['runs'])} as expected\")
sys.exit('not as expected: ' + '; '.join(bad) if bad else 0)
"

rule_dir="$out/rule"
uv run python question/find_question.py --out "$out/question" >/dev/null
uv run python question/answer.py --answer block --question "$out/question/question.json" --out "$rule_dir" >/dev/null

cd kylie-test-bed-upload
uv run python run_task.py --all --tasks tasks/refund_tasks.json --rules rules/starter.dw --output-dir "$out/starter"
uv run python run_task.py --all --tasks tasks/recent_approval_tasks.json --rules "$rule_dir/updated_rule.dw" --output-dir "$out/recent"
uv run python run_task.py --all --tasks tasks/camillia_tasks.json --rules "$rule_dir/updated_rule.dw" --output-dir "$out/camillia"

uv run python -c "
import glob, json, sys
rows = [r for f in glob.glob('$out/*/agent_scorecard_*.json') for r in json.load(open(f))['runs']]
bad = [r['task'] for r in rows if not r['passed']]
print(f'agent tasks: {len(rows) - len(bad)}/{len(rows)} passed')
sys.exit('failed: ' + ', '.join(bad) if bad else 0)
"
