"""Unix domain socket IPC server for Based Clef (`based-clef`).

Allows background agents, CLI pipes, and tmux scripts to compile blueprints and direct
the running automation layer over a low-latency local socket.

Usage:
  bclef ipc [--socket /path/to/socket]
  echo '{"cmd":"run","task":"Search wikipedia.org for Alan Turing"}' | nc -U ~/.local/state/based-clef/agent.sock
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from .core import LOG_DIR
from .intent import compile_intent_fast
from .runtime import Runtime

DEFAULT_SOCK = LOG_DIR / "agent.sock"


class IPCServer:
    def __init__(self, rt: Runtime, socket_path: Path = DEFAULT_SOCK) -> None:
        self.rt = rt
        self.socket_path = socket_path
        self._server: asyncio.AbstractServer | None = None
        self._lock = asyncio.Lock()

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await reader.readline()
            if not line:
                return
            req = json.loads(line.decode().strip())
            cmd = req.get("cmd", "run")
            response: dict[str, Any] = {"ok": True}

            if cmd == "run":
                task = req.get("task", "")
                url = req.get("url")
                data = req.get("data", {})
                driver = req.get("driver")
                async with self._lock:
                    if driver:
                        await self.rt.set_driver(driver)
                    ok = await self.rt.run_task(task, data, url)
                    s = self.rt.tel.stats
                    response = {
                        "ok": ok,
                        "step": s.step,
                        "elapsed": round(s.elapsed(), 2),
                        "latency_ms": round(s.last_latency_ms, 1),
                        "state": s.state,
                    }
            elif cmd == "plan":
                task = req.get("task", "")
                url = req.get("url")
                data = req.get("data", {})
                pb = compile_intent_fast(task, url=url, data=data)
                response = {"ok": True, "playbook": pb.to_dict()}
            elif cmd == "hold":
                on = bool(req.get("on", True))
                self.rt.agent.hold(on)
                response = {"ok": True, "held": on}
            elif cmd == "hint":
                hint = req.get("hint", "")
                remember = bool(req.get("remember", True))
                self.rt.agent.add_note(hint, remember=remember)
                response = {"ok": True, "hint": hint, "remember": remember}
            elif cmd == "status":
                s = self.rt.tel.stats
                response = {
                    "ok": True,
                    "state": s.state,
                    "task": s.task,
                    "step": s.step,
                    "stage_index": s.stage_index,
                    "stage_total": s.stage_total,
                    "stage_label": s.stage_label,
                    "attempt": s.attempt,
                    "model": s.model,
                    "confidence": s.last_confidence,
                    "tokens_in": s.in_tokens,
                    "tokens_out": s.out_tokens,
                }
            elif cmd == "stop":
                self.rt.agent.stop()
                response = {"ok": True, "stopped": True}
            else:
                response = {"ok": False, "error": f"Unknown command: {cmd}"}

            writer.write((json.dumps(response, default=str) + "\n").encode())
            await writer.drain()
        except Exception as e:
            err = {"ok": False, "error": str(e)}
            writer.write((json.dumps(err) + "\n").encode())
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def start(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            self.socket_path.unlink()
        self._server = await asyncio.start_unix_server(self.handle_client, path=str(self.socket_path))
        os.chmod(self.socket_path, 0o600)

    async def run_forever(self) -> None:
        await self.start()
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        if self.socket_path.exists():
            self.socket_path.unlink()
