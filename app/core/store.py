"""The store's code, which lives outside this package: the one place that puts it on the import path."""

import sys

from app.core import config

STORE_DIR = config.REPO / "kylie-test-bed-upload"
sys.path.insert(0, str(STORE_DIR))

from agent import OllamaModel as AgentModel
from agent import run_agent
from context_engine import window_seconds
from enforce import DogwoodGate, build_policy, dogwood_cmd, find_form
from recorder import Recorder
from run_task import check as check_store
from run_task import load_tasks
from score_actions import case_lines
from tools import STAFF, TOOL_SPECS, MockWorld

BASE = STORE_DIR / "policies" / "base.dw"
__all__ = [
    "BASE",
    "STAFF",
    "STORE_DIR",
    "TOOL_SPECS",
    "AgentModel",
    "DogwoodGate",
    "MockWorld",
    "Recorder",
    "build_policy",
    "case_lines",
    "check_store",
    "dogwood_cmd",
    "find_form",
    "load_tasks",
    "run_agent",
    "window_seconds",
]
