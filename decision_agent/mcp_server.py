"""MCP Server exposing Based Clef (`based-clef`) as standard Model Context Protocol tools.

Allows any conversational AI (Claude, Cursor, Continue, Gemini, Antigravity)
to compile user intent into UI blueprints and drive browser or desktop automation.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

try:
    from mcp.server.mcpserver import MCPServer
except ImportError:
    from mcp.server.fastmcp import FastMCP as MCPServer

from .core import Config
from .intent import compile_intent_fast
from .runtime import build

mcp = MCPServer("based-clef")
_runtime = None
_lock = asyncio.Lock()


def get_runtime() -> Any:
    global _runtime
    if _runtime is None:
        cfg = Config.from_env()
        cfg.headless = True
        _runtime = build(cfg)
    return _runtime


@mcp.tool()
def compile_intent(task: str, url: str | None = None) -> str:
    """Compile a natural language goal or `->` pipeline shorthand into an Action Sequence Playbook JSON.

    Args:
        task: Natural language intent or `->` pipeline steps.
        url: Optional starting URL.
    """
    pb = compile_intent_fast(task, url=url)
    return json.dumps(pb.to_dict(), indent=2)


@mcp.tool()
async def browser_task(task: str, url: str | None = None, data: dict[str, str] | None = None) -> str:
    """Execute a web navigation and interaction task in the browser.

    Args:
        task: Natural language description or `->` shorthand (e.g. 'Search wikipedia.org for Alan Turing and open article').
        url: Optional starting URL (auto-extracted from task if omitted).
        data: Optional key-value inputs to fill in forms (e.g. {'email': 'user@domain.com'}).
    """
    async with _lock:
        rt = get_runtime()
        await rt.set_driver("browser")
        ok = await rt.run_task(task, data or {}, url)
        stats = rt.tel.stats
        res_str = "SUCCESS" if ok else "FAILED"
        return f"Task {res_str} in {stats.step} steps ({stats.elapsed():.1f}s). Latency={stats.last_latency_ms:.0f}ms."


@mcp.tool()
async def desktop_task(task: str, data: dict[str, str] | None = None) -> str:
    """Execute a native Linux desktop application automation task (X11 / AT-SPI).

    Args:
        task: Natural language description of what to do on the desktop.
        data: Optional key-value inputs for text entry.
    """
    async with _lock:
        rt = get_runtime()
        await rt.set_driver("desktop")
        ok = await rt.run_task(task, data or {}, None)
        stats = rt.tel.stats
        res_str = "SUCCESS" if ok else "FAILED"
        return f"Desktop Task {res_str} in {stats.step} steps ({stats.elapsed():.1f}s)."


@mcp.tool()
def hold_agent(on: bool) -> str:
    """Toggle silent hold on the running agent. True pauses in-flight actions; False resumes."""
    rt = get_runtime()
    rt.agent.hold(on)
    return f"Agent hold state set to: {on}"


@mcp.tool()
def send_agent_hint(hint: str, remember: bool = True) -> str:
    """Send real-time guidance or user context to Based Clef.

    Args:
        hint: Guidance text (e.g. 'Choose the second account in the list').
        remember: Whether to retain this hint for the rest of the task.
    """
    rt = get_runtime()
    rt.agent.add_note(hint, remember=remember)
    return f"Hint received: '{hint}' (remember={remember})"


@mcp.tool()
def agent_status() -> str:
    """Return live telemetry, stage progress, and probability gate metrics as JSON."""
    rt = get_runtime()
    s = rt.tel.stats
    return json.dumps({
        "state": s.state,
        "task": s.task,
        "step": s.step,
        "stage": f"{s.stage_index}/{s.stage_total}",
        "stage_label": s.stage_label,
        "confidence": s.last_confidence,
        "latency_ms": round(s.last_latency_ms, 1),
    })


def run_server() -> None:
    """Run the MCP server over stdio transport."""
    mcp.run()


if __name__ == "__main__":
    run_server()
