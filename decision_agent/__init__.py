"""Based Clef (`based-clef` / `decision_agent`) — System 1 Computer-Use Engine."""
from __future__ import annotations

from .agent import Agent, load_secrets, redact_secret, save_secrets
from .browser import BrowserDriver, find_browser_executable
from .core import APP_NAME, ClefClient, Config, HeuristicClefClient, HeuristicLLMClient, LLMClient, Stats, Telemetry
from .intent import Playbook, Stage, compile_intent_fast, extract_url_from_intent
from .runtime import Runtime, build, stats_line
from .web import StudioServer

__version__ = "0.2.0"

__all__ = [
    "__version__",
    "APP_NAME",
    "Agent",
    "BrowserDriver",
    "ClefClient",
    "Config",
    "HeuristicClefClient",
    "HeuristicLLMClient",
    "LLMClient",
    "Playbook",
    "Runtime",
    "Stage",
    "Stats",
    "StudioServer",
    "Telemetry",
    "build",
    "compile_intent_fast",
    "extract_url_from_intent",
    "find_browser_executable",
    "load_secrets",
    "redact_secret",
    "save_secrets",
    "stats_line",
]
