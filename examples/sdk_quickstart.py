#!/usr/bin/env python3
"""Based Clef Python SDK Example — Intent Compilation, One-Shot Decisions, and Browser Execution.

Run offline with zero API keys:
    ./.venv/bin/python3 examples/sdk_quickstart.py --offline

Or run live against Cloudflare Workers AI (`@cf/cloudflare/clef-flash`):
    ./.venv/bin/python3 examples/sdk_quickstart.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from based_clef import Config, HeuristicClefClient, ClefClient, Telemetry, build, compile_intent_fast


async def main() -> None:
    parser = argparse.ArgumentParser(description="Based Clef SDK Quickstart")
    parser.add_argument("--offline", action="store_true", help="Run with local heuristic System 1 engine")
    args = parser.parse_args()

    cfg = Config.from_env()
    if args.offline or not cfg.has_credentials():
        cfg.offline = True
    cfg.headless = True
    cfg.step_delay = 0.1

    # 1. Compile natural language / pipeline shorthand into an inspectable Playbook
    pb = compile_intent_fast(
        "search 'clef-flash' -> click 'Filter Catalog' -> click 'Generate API Key'",
        url=(Path(__file__).resolve().parent.parent / "decision_agent" / "demo" / "portal.html").as_uri(),
    )
    print("=== 1. Compiled Action Sequence Blueprint ===")
    print(json.dumps(pb.to_dict(), indent=2))

    # 2. One-shot structured decision via ClefClient
    tel = Telemetry()
    clef = HeuristicClefClient(cfg, tel) if cfg.offline else ClefClient(cfg, tel)
    decision = await clef.decide(
        state={"deployment": "production", "latency_budget_ms": 250},
        questions={
            "tier": {
                "type": "choice",
                "instructions": "Select the optimal model tier for a 250ms interactive loop.",
                "criteria": {
                    "clef-flash": "@cf/cloudflare/clef-flash (~200ms single forward pass)",
                    "frontier-vlm": "Autoregressive 70B VLM (~3500ms streaming)",
                },
            }
        },
    )
    await clef.http.aclose()
    print("\n=== 2. One-Shot Calibrated Decision ===")
    print(json.dumps(decision["answers"]["tier"], indent=2))

    # 3. Execute the compiled Playbook in headless Chrome/Chromium
    rt = build(cfg)
    ok = await rt.run_task(pb.goal, data={}, url=pb.start_url, playbook=pb)
    await rt.shutdown()
    print(f"\n=== 3. Execution Result: {'SUCCESS' if ok else 'FAILED'} in {rt.tel.stats.step} steps ===")


if __name__ == "__main__":
    asyncio.run(main())
