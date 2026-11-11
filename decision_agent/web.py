"""Based Clef Studio — local async Web Command Center (HTTP + SSE, zero framework bloat).

Provides an intuitive visual workflow from natural user intent (or `->` Clef shorthand)
to editable Action Sequence Blueprints, live annotated viewport inspection, calibrated
System 1 probability gates (`P1[TGT]`, `P2[ACT]`, `P3[DONE]`, `P4[SENS]`), and HITL controls.
"""
from __future__ import annotations

import asyncio
from http import HTTPStatus
import json
from pathlib import Path
import time
from typing import Any
from urllib.parse import urlparse

from .agent import redact_secret
from .intent import Playbook
from .runtime import Runtime

DEMO_DIR = Path(__file__).parent / "demo"
ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"


class StudioServer:
    def __init__(self, rt: Runtime, host: str = "127.0.0.1", port: int = 8765) -> None:
        self.rt = rt
        self.host = host
        self.port = port
        self._server: asyncio.AbstractServer | None = None
        self._sse_queues: set[asyncio.Queue[dict[str, Any]]] = set()
        self._pending_ask: dict[str, Any] | None = None
        self._pending_fut: asyncio.Future[dict[str, Any]] | None = None
        self._saved_names: list[str] = []
        self._run_task_handle: asyncio.Task[bool] | None = None

        self.rt.tel.subscribe(self._on_telemetry)
        self.rt.agent.ask = self._ask_from_agent
        self.rt.agent.on_saved = self._on_saved

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def _on_telemetry(self, ev: dict[str, Any]) -> None:
        # Strip any bulky fields before pushing over SSE; frame bytes are fetched via /api/frame
        slim = {k: v for k, v in ev.items() if k != "frame_b64"}
        for q in list(self._sse_queues):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(slim)
            except asyncio.QueueFull:
                pass

    def _on_saved(self, names: list[str]) -> None:
        self._saved_names = list(names)
        self._on_telemetry({"t": round(time.time(), 3), "kind": "secrets_saved", "names": self._saved_names})

    async def _ask_from_agent(self, kind: str, prompt: str, secret: bool = False, name: str | None = None) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending_fut = fut
        self._pending_ask = {"kind": kind, "prompt": prompt, "secret": secret, "name": name}
        self._on_telemetry({"t": round(time.time(), 3), "kind": "hitl_pending", **self._pending_ask})
        try:
            return await asyncio.wait_for(fut, timeout=self.rt.cfg.ask_timeout)
        except asyncio.TimeoutError:
            return {"ok": False, "value": None, "save": False}
        finally:
            self._pending_ask = None
            self._pending_fut = None
            self._on_telemetry({"t": round(time.time(), 3), "kind": "hitl_cleared"})

    def _status_payload(self) -> dict[str, Any]:
        s = self.rt.tel.stats
        cfg = self.rt.cfg
        snap = self.rt.tel.last_snapshot or {}
        pb = self.rt.agent.playbook
        return {
            "ok": True,
            "stats": {
                "state": s.state,
                "model": s.model,
                "task": s.task,
                "step": s.step,
                "attempt": s.attempt,
                "elapsed": round(s.elapsed(), 1),
                "latency_ms": round(s.last_latency_ms, 1),
                "confidence": s.last_confidence,
                "p_target": s.p_target,
                "p_action": s.p_action,
                "p_done": s.p_done,
                "p_sensitive": s.p_sensitive,
                "stage_index": s.stage_index,
                "stage_total": s.stage_total,
                "stage_label": s.stage_label,
                "clef_calls": s.clef_calls,
                "llm_calls": s.llm_calls,
                "in_tokens": s.in_tokens,
                "out_tokens": s.out_tokens,
                "stream_tps": round(s.stream_tps, 1),
            },
            "cfg": {
                "driver": cfg.driver,
                "mode": cfg.mode,
                "offline": cfg.offline,
                "has_credentials": cfg.has_credentials(),
                "threshold": cfg.threshold,
                "step_delay": cfg.step_delay,
                "highlight": cfg.highlight,
                "vision": cfg.vision,
                "decision_model": cfg.decision_model,
            },
            "held": self.rt.agent.held,
            "running": bool(self._run_task_handle and not self._run_task_handle.done()),
            "playbook": pb.to_dict() if pb else None,
            "pending_ask": self._pending_ask,
            "saved_secrets": self._saved_names,
            "viewport": {
                "url": snap.get("url", ""),
                "title": snap.get("title", ""),
                "elements": snap.get("elements", []),
                "viewport": snap.get("viewport", [1100, 800]),
                "has_frame": bool(self.rt.tel.last_frame_b64),
            },
            "logs": self.rt.tel.tail(60),
        }

    def _normalize_demo_url(self, url: str | None) -> str | None:
        if not url:
            return None
        u = url.strip()
        if u.startswith("/demo/"):
            return f"{self.base_url}{u}"
        return u

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle_conn, host=self.host, port=self.port)
        socks = self._server.sockets
        if socks:
            self.port = socks[0].getsockname()[1]

    async def stop(self) -> None:
        self.rt.tel.unsubscribe(self._on_telemetry)
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def run_forever(self) -> None:
        await self.start()
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    async def _handle_conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            req_line = await asyncio.wait_for(reader.readline(), timeout=10.0)
            if not req_line:
                return
            parts = req_line.decode("latin-1", errors="replace").strip().split(" ")
            if len(parts) < 2:
                return
            method, raw_target = parts[0].upper(), parts[1]
            parsed = urlparse(raw_target)
            path = parsed.path

            headers: dict[str, str] = {}
            while True:
                hline = await asyncio.wait_for(reader.readline(), timeout=10.0)
                if not hline or hline in (b"\r\n", b"\n"):
                    break
                decoded = hline.decode("latin-1", errors="replace").strip()
                if ":" in decoded:
                    k, v = decoded.split(":", 1)
                    headers[k.strip().lower()] = v.strip()

            content_len = int(headers.get("content-length", "0") or "0")
            body_bytes = await asyncio.wait_for(reader.readexactly(content_len), timeout=15.0) if content_len > 0 else b""

            if method == "GET" and path == "/api/events":
                await self._serve_sse(writer)
                return

            status, content_type, resp_bytes = await self._dispatch(method, path, body_bytes)
            phrase = HTTPStatus(status).phrase
            head = (
                f"HTTP/1.1 {status} {phrase}\r\n"
                f"Content-Type: {content_type}\r\n"
                f"Content-Length: {len(resp_bytes)}\r\n"
                "Cache-Control: no-store\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii")
            writer.write(head + resp_bytes)
            await writer.drain()
        except Exception:
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _serve_sse(self, writer: asyncio.StreamWriter) -> None:
        head = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: text/event-stream; charset=utf-8\r\n"
            "Cache-Control: no-cache\r\n"
            "Connection: keep-alive\r\n\r\n"
        ).encode("ascii")
        writer.write(head)
        await writer.drain()

        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=128)
        self._sse_queues.add(q)
        try:
            init_msg = json.dumps({"kind": "hello", "status": self._status_payload()}, default=str)
            writer.write(f"data: {init_msg}\n\n".encode("utf-8"))
            await writer.drain()
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15.0)
                    payload = json.dumps(ev, default=str)
                    writer.write(f"data: {payload}\n\n".encode("utf-8"))
                except asyncio.TimeoutError:
                    writer.write(b": keepalive\n\n")
                await writer.drain()
        except Exception:
            pass
        finally:
            self._sse_queues.discard(q)

    async def _dispatch(self, method: str, path: str, body_bytes: bytes) -> tuple[int, str, bytes]:
        if method == "GET":
            if path in ("/", "/index.html"):
                return 200, "text/html; charset=utf-8", _STUDIO_HTML.encode("utf-8")
            if path == "/demo/login":
                return 200, "text/html; charset=utf-8", (DEMO_DIR / "login.html").read_bytes()
            if path == "/demo/portal":
                return 200, "text/html; charset=utf-8", (DEMO_DIR / "portal.html").read_bytes()
            if path.startswith("/assets/"):
                rel = path.removeprefix("/assets/")
                candidate = (ASSETS_DIR / rel).resolve()
                if ASSETS_DIR in candidate.parents and candidate.is_file():
                    ctype = "image/svg+xml" if candidate.suffix == ".svg" else "image/png"
                    return 200, ctype, candidate.read_bytes()
                return 404, "text/plain; charset=utf-8", b"Not found"
            if path == "/api/status":
                return 200, "application/json; charset=utf-8", json.dumps(self._status_payload(), default=str).encode("utf-8")
            if path == "/api/frame":
                data = {
                    "ok": True,
                    "frame_b64": self.rt.tel.last_frame_b64,
                    "snapshot": self.rt.tel.last_snapshot,
                }
                return 200, "application/json; charset=utf-8", json.dumps(data, default=str).encode("utf-8")
            return 404, "application/json; charset=utf-8", b'{"ok":false,"error":"Not found"}'

        if method != "POST":
            return 405, "application/json; charset=utf-8", b'{"ok":false,"error":"Method not allowed"}'

        try:
            req = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
        except json.JSONDecodeError:
            return 400, "application/json; charset=utf-8", b'{"ok":false,"error":"Invalid JSON"}'

        if path == "/api/plan":
            task = str(req.get("task") or "").strip()
            task = task.replace(" /demo/", f" {self.base_url}/demo/")
            url = self._normalize_demo_url(req.get("url"))
            data = req.get("data") or {}
            inspect_live = bool(req.get("inspect_live", False))
            pb = await self.rt.compile_blueprint(task, data=data, url=url, inspect_live=inspect_live)
            res = {"ok": True, "playbook": pb.to_dict(), "missing_keys": pb.missing_keys(data)}
            return 200, "application/json; charset=utf-8", json.dumps(res, default=str).encode("utf-8")

        if path == "/api/playbook":
            pb_dict = req.get("playbook") or {}
            pb = Playbook.from_dict(pb_dict)
            self.rt.agent.playbook = pb
            self.rt.agent._sync_playbook_stats()
            return 200, "application/json; charset=utf-8", json.dumps({"ok": True, "playbook": pb.to_dict()}).encode("utf-8")

        if path == "/api/run":
            if self._run_task_handle and not self._run_task_handle.done():
                return 409, "application/json; charset=utf-8", b'{"ok":false,"error":"A task is already running"}'
            task = str(req.get("task") or "").strip()
            task = task.replace(" /demo/", f" {self.base_url}/demo/")
            url = self._normalize_demo_url(req.get("url"))
            data = {str(k): str(v) for k, v in (req.get("data") or {}).items() if str(k).strip()}
            driver = str(req.get("driver") or self.rt.cfg.driver)
            mode = str(req.get("mode") or self.rt.cfg.mode)
            if "offline" in req:
                want_offline = bool(req["offline"])
                if not want_offline and not self.rt.cfg.has_credentials():
                    return 400, "application/json; charset=utf-8", (
                        b'{"ok":false,"error":"Cloudflare credentials not configured. Enable Sandbox Offline mode or run `bclef init`."}'
                    )
                self.rt.set_offline(want_offline)
            self.rt.cfg.mode = mode
            pb_raw = req.get("playbook")
            pb = Playbook.from_dict(pb_raw) if isinstance(pb_raw, dict) and pb_raw.get("stages") else None
            if pb and pb.start_url and pb.start_url.startswith("/demo/"):
                pb.start_url = f"{self.base_url}{pb.start_url}"

            async def _bg() -> bool:
                await self.rt.set_driver(driver)
                return await self.rt.run_task(task, data, url, playbook=pb)

            self._run_task_handle = asyncio.create_task(_bg())
            return 202, "application/json; charset=utf-8", b'{"ok":true,"started":true}'

        if path == "/api/hold":
            on = bool(req.get("on", not self.rt.agent.held))
            self.rt.agent.hold(on)
            self._on_telemetry({"t": round(time.time(), 3), "kind": "hold", "held": on})
            return 200, "application/json; charset=utf-8", json.dumps({"ok": True, "held": on}).encode("utf-8")

        if path == "/api/stop":
            self.rt.agent.stop()
            if self._pending_fut and not self._pending_fut.done():
                self._pending_fut.set_result({"ok": False, "value": None, "save": False})
            return 200, "application/json; charset=utf-8", b'{"ok":true,"stopped":true}'

        if path == "/api/hint":
            hint = str(req.get("hint") or "").strip()
            remember = bool(req.get("remember", True))
            if hint:
                self.rt.agent.add_note(hint, remember=remember)
            return 200, "application/json; charset=utf-8", json.dumps({"ok": True, "hint": hint}).encode("utf-8")

        if path == "/api/answer":
            if self._pending_fut and not self._pending_fut.done():
                ans = {
                    "ok": bool(req.get("ok", False)),
                    "value": req.get("value"),
                    "save": bool(req.get("save", True)),
                }
                self._pending_fut.set_result(ans)
                return 200, "application/json; charset=utf-8", b'{"ok":true}'
            return 400, "application/json; charset=utf-8", b'{"ok":false,"error":"No pending prompt"}'

        if path == "/api/redact":
            for n in list(self._saved_names):
                redact_secret(n)
            self._saved_names.clear()
            return 200, "application/json; charset=utf-8", b'{"ok":true,"redacted":true}'

        if path == "/api/config":
            if "step_delay" in req:
                self.rt.cfg.step_delay = max(0.0, min(5.0, float(req["step_delay"])))
            if "threshold" in req:
                self.rt.cfg.threshold = max(0.1, min(0.99, float(req["threshold"])))
            if "highlight" in req:
                hl = bool(req["highlight"])
                self.rt.cfg.highlight = hl
                if hasattr(self.rt.driver, "highlight"):
                    self.rt.driver.highlight = hl
            if "vision" in req:
                self.rt.cfg.vision = bool(req["vision"])
            if "mode" in req and req["mode"] in ("autopilot", "preview", "step"):
                self.rt.cfg.mode = req["mode"]
            if "offline" in req:
                want_off = bool(req["offline"])
                if want_off or self.rt.cfg.has_credentials():
                    self.rt.set_offline(want_off)
            return 200, "application/json; charset=utf-8", json.dumps(self._status_payload(), default=str).encode("utf-8")

        return 404, "application/json; charset=utf-8", b'{"ok":false,"error":"Unknown endpoint"}'


_STUDIO_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Based Clef Studio — System 1 Computer-Use Command Center</title>
  <meta name="description" content="Interactive intent-to-action blueprint compiler, calibrated probability gate scope, and live viewport controller for Based Clef.">
  <style>
    :root {
      --bg-base: #090c12;
      --bg-surface: #0f141d;
      --bg-elevated: #151c28;
      --bg-input: #0b0f17;
      --border: #202a3c;
      --border-strong: #2e3c54;
      --text-primary: #f1f5f9;
      --text-secondary: #94a3b8;
      --text-muted: #64748b;
      --p1-magenta: #ff33cc;
      --p2-coral: #ff6b2b;
      --cyan: #38bdf8;
      --emerald: #34d399;
      --amber: #f59e0b;
      --rose: #f87171;
      --mono: ui-monospace, "JetBrains Mono", "SFMono-Regular", Menlo, Consolas, monospace;
      --sans: -apple-system, BlinkMacSystemFont, "Inter", "Segoe UI", Roboto, sans-serif;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: var(--sans);
      background: var(--bg-base);
      color: var(--text-primary);
      font-size: 13.5px;
      line-height: 1.45;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
    }
    /* Top Telemetry Rail */
    header.topbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 10px 18px;
      background: var(--bg-surface);
      border-bottom: 1px solid var(--border);
      flex-wrap: wrap;
    }
    .brand-lockup {
      display: flex;
      align-items: center;
      gap: 11px;
    }
    .brand-mark {
      width: 30px;
      height: 30px;
      flex-shrink: 0;
    }
    .brand-titles h1 {
      font-size: 15px;
      font-weight: 700;
      letter-spacing: -0.02em;
      color: var(--text-primary);
    }
    .brand-titles .spec-rail {
      font-family: var(--mono);
      font-size: 10.5px;
      color: var(--text-muted);
      letter-spacing: 0.05em;
      text-transform: uppercase;
    }
    .telemetry-strip {
      display: flex;
      align-items: center;
      gap: 14px;
      flex-wrap: wrap;
      font-family: var(--mono);
      font-size: 12px;
    }
    .state-badge {
      padding: 3px 9px;
      border-radius: 4px;
      font-weight: 700;
      font-size: 11px;
      text-transform: uppercase;
      background: var(--bg-elevated);
      border: 1px solid var(--border-strong);
      color: var(--cyan);
    }
    .state-badge[data-state="done"] { color: var(--emerald); border-color: rgba(52,211,153,0.4); }
    .state-badge[data-state="error"] { color: var(--rose); border-color: rgba(248,113,113,0.4); }
    .state-badge[data-state="waiting"], .state-badge[data-state="hold"] { color: var(--amber); border-color: rgba(245,158,11,0.45); }
    .metric { color: var(--text-secondary); }
    .metric strong { color: var(--text-primary); font-weight: 600; }

    /* HITL Alert Banner */
    #hitl-banner {
      display: none;
      background: #231a0b;
      border-bottom: 1px solid #854d0e;
      padding: 12px 18px;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      flex-wrap: wrap;
    }
    #hitl-banner.active { display: flex; }
    .hitl-prompt {
      font-weight: 600;
      color: #fde68a;
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .hitl-controls {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }

    /* Secrets Redact Banner */
    #secrets-banner {
      display: none;
      background: #082218;
      border-bottom: 1px solid #065f46;
      padding: 8px 18px;
      align-items: center;
      justify-content: space-between;
      font-size: 12.5px;
      color: #a7f3d0;
    }
    #secrets-banner.active { display: flex; }

    /* Main Workspace Grid */
    main.workspace {
      display: grid;
      grid-template-columns: 410px 1fr;
      flex: 1;
      min-height: 0;
    }
    @media (max-width: 980px) {
      main.workspace { grid-template-columns: 1fr; }
    }
    .left-column {
      border-right: 1px solid var(--border);
      background: var(--bg-surface);
      display: flex;
      flex-direction: column;
      min-height: 0;
    }
    .right-column {
      display: flex;
      flex-direction: column;
      min-height: 0;
      background: var(--bg-base);
    }

    .section-block {
      padding: 14px 16px;
      border-bottom: 1px solid var(--border);
    }
    .section-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 10px;
    }
    .section-title {
      font-family: var(--mono);
      font-size: 11px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      color: var(--text-secondary);
    }

    /* Presets */
    .preset-bar {
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
      margin-bottom: 10px;
    }
    .chip {
      font-size: 11.5px;
      padding: 4px 8px;
      background: var(--bg-elevated);
      border: 1px solid var(--border);
      border-radius: 4px;
      color: var(--text-secondary);
      cursor: pointer;
      transition: border-color 0.15s, color 0.15s;
    }
    .chip:hover {
      border-color: var(--cyan);
      color: var(--text-primary);
    }

    /* Inputs & Buttons */
    textarea, input[type="text"], input[type="password"], input[type="url"], select {
      width: 100%;
      background: var(--bg-input);
      border: 1px solid var(--border-strong);
      border-radius: 5px;
      color: var(--text-primary);
      padding: 8px 10px;
      font-family: inherit;
      font-size: 13px;
      transition: border-color 0.15s;
    }
    textarea:focus, input:focus, select:focus {
      outline: none;
      border-color: var(--cyan);
    }
    textarea#intent-input {
      resize: vertical;
      min-height: 66px;
      font-family: var(--sans);
    }
    .field-row {
      display: flex;
      gap: 8px;
      margin-top: 8px;
      align-items: center;
    }
    .btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      padding: 7px 12px;
      border-radius: 5px;
      border: 1px solid var(--border-strong);
      background: var(--bg-elevated);
      color: var(--text-primary);
      font-size: 12.5px;
      font-weight: 600;
      cursor: pointer;
      white-space: nowrap;
      transition: background 0.15s, border-color 0.15s;
    }
    .btn:hover { background: #1e293b; }
    .btn-engage {
      background: #0284c7;
      border-color: #0369a1;
      color: #fff;
    }
    .btn-engage:hover { background: #0369a1; }
    .btn-compile {
      background: #172133;
      border-color: #2d4263;
      color: var(--cyan);
    }
    .btn-hold[aria-pressed="true"] {
      background: #78350f;
      border-color: var(--amber);
      color: #fef3c7;
    }
    .btn-stop {
      color: var(--rose);
      border-color: rgba(248,113,113,0.35);
    }
    .btn-sm {
      padding: 4px 8px;
      font-size: 11.5px;
    }

    /* Segmented Mode Selector */
    .mode-group {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      background: var(--bg-input);
      border: 1px solid var(--border);
      border-radius: 5px;
      padding: 2px;
      margin-top: 8px;
    }
    .mode-btn {
      background: transparent;
      border: none;
      color: var(--text-secondary);
      padding: 5px 6px;
      font-size: 11.5px;
      font-weight: 600;
      border-radius: 4px;
      cursor: pointer;
    }
    .mode-btn.active {
      background: var(--bg-elevated);
      color: var(--text-primary);
      box-shadow: 0 1px 2px rgba(0,0,0,0.4);
    }

    /* Data Drawer */
    .kv-list { display: flex; flex-direction: column; gap: 6px; margin-top: 8px; }
    .kv-row { display: grid; grid-template-columns: 110px 1fr auto; gap: 6px; align-items: center; }

    /* Action Sequence / Blueprint List */
    .blueprint-scroll {
      flex: 1;
      overflow-y: auto;
      padding: 12px 16px;
    }
    .stage-list {
      display: flex;
      flex-direction: column;
      gap: 7px;
    }
    .stage-item {
      display: grid;
      grid-template-columns: 26px 54px 1fr auto;
      gap: 8px;
      align-items: center;
      padding: 8px 10px;
      background: var(--bg-input);
      border: 1px solid var(--border);
      border-radius: 6px;
    }
    .stage-item[data-status="active"] {
      border-color: var(--cyan);
      background: #0f1b2d;
    }
    .stage-item[data-status="completed"] {
      border-color: rgba(52,211,153,0.4);
      opacity: 0.9;
    }
    .stage-num {
      font-family: var(--mono);
      font-size: 11px;
      color: var(--text-muted);
    }
    .stage-item[data-status="completed"] .stage-num { color: var(--emerald); font-weight: 700; }
    .stage-item[data-status="active"] .stage-num { color: var(--cyan); font-weight: 700; }
    .stage-kind {
      font-family: var(--mono);
      font-size: 10px;
      text-transform: uppercase;
      padding: 2px 5px;
      border-radius: 3px;
      text-align: center;
      background: var(--bg-elevated);
      color: var(--text-secondary);
      border: 1px solid var(--border);
    }
    .stage-intent-input {
      background: transparent !important;
      border: 1px solid transparent !important;
      padding: 2px 4px !important;
      font-size: 12.5px !important;
    }
    .stage-intent-input:focus {
      background: var(--bg-surface) !important;
      border-color: var(--border-strong) !important;
    }
    .sens-tag {
      font-family: var(--mono);
      font-size: 10px;
      color: var(--amber);
      padding: 1px 4px;
      border: 1px solid rgba(245,158,11,0.4);
      border-radius: 3px;
    }

    /* Right Column: Probability Gates + Viewport + Stream */
    .gates-bar {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 10px;
      padding: 12px 18px;
      background: var(--bg-surface);
      border-bottom: 1px solid var(--border);
    }
    @media (max-width: 720px) {
      .gates-bar { grid-template-columns: repeat(2, 1fr); }
    }
    .gate-cell {
      background: var(--bg-input);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 8px 10px;
    }
    .gate-head {
      display: flex;
      justify-content: space-between;
      font-family: var(--mono);
      font-size: 11px;
      margin-bottom: 6px;
    }
    .gate-track {
      height: 5px;
      background: #1a2232;
      border-radius: 3px;
      overflow: hidden;
    }
    .gate-fill {
      height: 100%;
      width: 0%;
      transition: width 0.2s ease;
    }

    /* Viewport Canvas */
    .viewport-wrap {
      flex: 1;
      display: flex;
      flex-direction: column;
      padding: 14px 18px;
      min-height: 300px;
    }
    .viewport-bar {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 10px;
      padding: 7px 12px;
      background: var(--bg-surface);
      border: 1px solid var(--border);
      border-bottom: none;
      border-radius: 7px 7px 0 0;
      font-family: var(--mono);
      font-size: 11.5px;
      color: var(--text-secondary);
    }
    .viewport-stage {
      position: relative;
      flex: 1;
      background: #06080d;
      border: 1px solid var(--border);
      border-radius: 0 0 7px 7px;
      overflow: hidden;
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 280px;
    }
    .viewport-inner {
      position: relative;
      max-width: 100%;
      max-height: 52vh;
      aspect-ratio: 1100 / 800;
      width: 100%;
    }
    #viewport-img {
      width: 100%;
      height: 100%;
      object-fit: contain;
      display: none;
    }
    #viewport-svg {
      position: absolute;
      inset: 0;
      width: 100%;
      height: 100%;
      pointer-events: all;
    }
    .el-box {
      fill: rgba(56, 189, 248, 0.04);
      stroke: rgba(56, 189, 248, 0.42);
      stroke-width: 1.5;
      cursor: pointer;
      transition: fill 0.12s, stroke 0.12s;
    }
    .el-box:hover {
      fill: rgba(56, 189, 248, 0.18);
      stroke: var(--cyan);
      stroke-width: 2.5;
    }
    .el-box.active-target {
      fill: rgba(255, 51, 204, 0.16);
      stroke: var(--p1-magenta);
      stroke-width: 3.5;
    }
    .viewport-empty {
      text-align: center;
      color: var(--text-muted);
      padding: 32px;
      max-width: 440px;
    }

    /* Steering & Log Footer */
    .bottom-deck {
      border-top: 1px solid var(--border);
      background: var(--bg-surface);
      padding: 10px 18px 12px;
    }
    .log-box {
      margin-top: 8px;
      height: 115px;
      overflow-y: auto;
      background: var(--bg-input);
      border: 1px solid var(--border);
      border-radius: 5px;
      padding: 7px 10px;
      font-family: var(--mono);
      font-size: 11.5px;
      color: var(--text-secondary);
    }
    .log-line { white-space: pre-wrap; word-break: break-word; padding: 1px 0; }
    .log-line[data-kind="step"] { color: var(--p1-magenta); }
    .log-line[data-kind="plan"], .log-line[data-kind="stage"] { color: var(--cyan); }
    .log-line[data-kind="error"] { color: var(--rose); }
    .log-line[data-kind="ask"], .log-line[data-kind="warn"] { color: var(--amber); }
    .toggle-pill {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      font-size: 12px;
      color: var(--text-secondary);
      cursor: pointer;
      user-select: none;
    }
  </style>
</head>
<body>
  <header class="topbar">
    <div class="brand-lockup">
      <svg class="brand-mark" viewBox="0 0 64 64" aria-hidden="true">
        <polygon points="32,3 58,18 58,46 32,61 6,46 6,18" fill="#101520" stroke="#28354a" stroke-width="2"/>
        <path d="M24,25 A12,12 0 1,1 42,35 C42,46 31,52 20,53" fill="none" stroke="url(#clefGrad)" stroke-width="4.5" stroke-linecap="round"/>
        <circle cx="24" cy="25" r="4.5" fill="none" stroke="#ff33cc" stroke-width="2"/>
        <circle cx="50" cy="23" r="4" fill="none" stroke="#ff33cc" stroke-width="2"/>
        <circle cx="50" cy="37" r="4" fill="none" stroke="#ff6b2b" stroke-width="2"/>
        <defs>
          <linearGradient id="clefGrad" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0%" stop-color="#ff33cc"/>
            <stop offset="100%" stop-color="#ff6b2b"/>
          </linearGradient>
        </defs>
      </svg>
      <div class="brand-titles">
        <h1>based-clef <span style="font-weight:400;color:var(--text-muted);">// studio</span></h1>
        <div class="spec-rail">SYS.01 // DIRECTIVE SUB DRIVER // ~200MS SYSTEM 1 LOOP</div>
      </div>
    </div>

    <div class="telemetry-strip">
      <span id="badge-state" class="state-badge" data-state="idle">IDLE</span>
      <span class="metric">MODEL <strong id="stat-model">-</strong></span>
      <span class="metric">STEP <strong id="stat-step">0</strong></span>
      <span class="metric">STAGE <strong id="stat-stage">0/0</strong></span>
      <span class="metric">LATENCY <strong id="stat-lat">0 ms</strong></span>
      <span class="metric">CALLS <strong id="stat-calls">0/0</strong></span>
      <label class="toggle-pill" title="Run with local heuristic System 1 engine (no API keys required)">
        <input id="chk-offline" type="checkbox" onchange="updateConfig({offline: this.checked})">
        <span>Sandbox Offline</span>
      </label>
    </div>
  </header>

  <!-- Human-in-the-Loop Prompt Banner -->
  <section id="hitl-banner" aria-live="assertive">
    <div class="hitl-prompt">
      <span>[HITL INTERCEPT]</span>
      <span id="hitl-text">Approval required</span>
    </div>
    <div class="hitl-controls">
      <input id="hitl-input" type="text" placeholder="Optional value or guidance..." style="width:220px;display:none;">
      <label id="hitl-save-wrap" class="toggle-pill" style="display:none;color:#fde68a;">
        <input id="hitl-save" type="checkbox" checked>
        <span>Save to secrets.env (0600)</span>
      </label>
      <button id="btn-hitl-approve" class="btn btn-engage btn-sm" onclick="answerHitl(true)">Approve</button>
      <button id="btn-hitl-deny" class="btn btn-stop btn-sm" onclick="answerHitl(false)">Deny</button>
    </div>
  </section>

  <!-- Saved Secrets Redact Notice -->
  <section id="secrets-banner">
    <span id="secrets-text">Saved secrets to ~/.config/based-clef/secrets.env (0600)</span>
    <div style="display:flex;gap:8px;">
      <button id="btn-redact-secrets" class="btn btn-stop btn-sm" onclick="redactSecrets()">Redact Now</button>
      <button id="btn-dismiss-secrets" class="btn btn-sm" onclick="document.getElementById('secrets-banner').classList.remove('active')">Dismiss</button>
    </div>
  </section>

  <main class="workspace">
    <!-- Left Column: Intent Composer + Editable Action Sequence -->
    <aside class="left-column">
      <div class="section-block">
        <div class="section-header">
          <span class="section-title">01 // Intent Composer</span>
          <span id="cred-indicator" style="font-family:var(--mono);font-size:10.5px;color:var(--text-muted);"></span>
        </div>

        <div class="preset-bar">
          <button id="preset-login" class="chip" type="button" onclick="loadPreset('login')">Demo: Login</button>
          <button id="preset-portal" class="chip" type="button" onclick="loadPreset('portal')">Demo: API Key Pipeline</button>
          <button id="preset-safety" class="chip" type="button" onclick="loadPreset('safety')">Demo: Sensitive Gate</button>
          <button id="preset-wiki" class="chip" type="button" onclick="loadPreset('wiki')">Web: Wikipedia</button>
        </div>

        <textarea id="intent-input" placeholder="Describe your goal naturally OR use `->` pipeline shorthand:&#10;e.g. Open /demo/portal -> search 'clef-flash' -> click Generate API Key"></textarea>

        <div class="field-row">
          <input id="url-input" type="text" placeholder="Starting URL (optional — auto-extracted from intent)">
          <select id="driver-select" style="width:110px;" aria-label="Driver">
            <option value="browser">Browser</option>
            <option value="desktop">Desktop</option>
          </select>
        </div>

        <!-- Execution Mode -->
        <div class="mode-group" role="tablist" aria-label="Execution Mode">
          <button id="mode-autopilot" class="mode-btn active" type="button" onclick="setMode('autopilot')">Direct Drive</button>
          <button id="mode-preview" class="mode-btn" type="button" onclick="setMode('preview')">Preview Gate</button>
          <button id="mode-step" class="mode-btn" type="button" onclick="setMode('step')">Step Co-Pilot</button>
        </div>

        <!-- RAM-Isolated Form Data -->
        <details id="data-drawer" style="margin-top:10px;">
          <summary style="cursor:pointer;font-size:12px;color:var(--text-secondary);display:flex;justify-content:space-between;">
            <span>Form Data &amp; Secrets (RAM-Isolated)</span>
            <span id="data-count" style="font-family:var(--mono);color:var(--emerald);">0 keys</span>
          </summary>
          <div id="kv-container" class="kv-list"></div>
          <button id="btn-add-kv" class="btn btn-sm" type="button" style="margin-top:6px;" onclick="addKvRow('', '', false)">+ Add Field</button>
        </details>

        <!-- Primary Controls -->
        <div class="field-row" style="margin-top:12px;">
          <button id="btn-compile" class="btn btn-compile" type="button" onclick="compileBlueprint(false)" title="Compile intent into editable stages without acting">Compile Blueprint</button>
          <button id="btn-engage" class="btn btn-engage" type="button" style="flex:1;" onclick="engageRun()">Engage</button>
          <button id="btn-hold" class="btn btn-hold" type="button" aria-pressed="false" onclick="toggleHold()" title="Silent Hold (Alt+H)">Hold</button>
          <button id="btn-stop" class="btn btn-stop" type="button" onclick="stopRun()">Stop</button>
        </div>
      </div>

      <!-- Editable Action Sequence / Playbook -->
      <div class="blueprint-scroll">
        <div class="section-header">
          <span class="section-title">02 // Action Sequence Blueprint</span>
          <div style="display:flex;gap:5px;">
            <button id="btn-add-stage" class="btn btn-sm" type="button" onclick="addStage()">+ Step</button>
            <button id="btn-export-playbook" class="btn btn-sm" type="button" onclick="exportPlaybook()">Export</button>
            <label class="btn btn-sm" style="cursor:pointer;">
              Import
              <input id="file-import-playbook" type="file" accept=".json" style="display:none;" onchange="importPlaybook(event)">
            </label>
          </div>
        </div>
        <div id="missing-keys-alert" style="display:none;margin-bottom:8px;padding:6px 8px;border-radius:4px;background:#231a0b;border:1px solid #854d0e;color:#fde68a;font-size:11.5px;"></div>
        <div id="stage-list" class="stage-list">
          <div style="color:var(--text-muted);font-size:12.5px;padding:12px 4px;">
            Type an intent above and click <strong>Compile Blueprint</strong> to preview &amp; customize steps, or click <strong>Engage</strong> to compile and run immediately.
          </div>
        </div>
      </div>
    </aside>

    <!-- Right Column: Probability Gates + Annotated Live Viewport + Telemetry -->
    <section class="right-column">
      <div class="gates-bar">
        <div class="gate-cell">
          <div class="gate-head"><span style="color:var(--p1-magenta);">P1 [TARGET]</span><strong id="val-p1">-</strong></div>
          <div class="gate-track"><div id="bar-p1" class="gate-fill" style="background:var(--p1-magenta);"></div></div>
        </div>
        <div class="gate-cell">
          <div class="gate-head"><span style="color:var(--p2-coral);">P2 [ACTION]</span><strong id="val-p2">-</strong></div>
          <div class="gate-track"><div id="bar-p2" class="gate-fill" style="background:var(--p2-coral);"></div></div>
        </div>
        <div class="gate-cell">
          <div class="gate-head"><span style="color:var(--emerald);">P3 [DONE]</span><strong id="val-p3">-</strong></div>
          <div class="gate-track"><div id="bar-p3" class="gate-fill" style="background:var(--emerald);"></div></div>
        </div>
        <div class="gate-cell">
          <div class="gate-head"><span style="color:var(--amber);">P4 [SENSITIVE]</span><strong id="val-p4">-</strong></div>
          <div class="gate-track"><div id="bar-p4" class="gate-fill" style="background:var(--amber);"></div></div>
        </div>
      </div>

      <div class="viewport-wrap">
        <div class="viewport-bar">
          <span id="vp-url" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">viewport://idle</span>
          <span id="vp-hover-info" style="color:var(--cyan);">Click any highlighted box to steer or add stage</span>
        </div>
        <div class="viewport-stage">
          <div id="vp-empty" class="viewport-empty">
            <strong style="color:var(--text-primary);display:block;margin-bottom:6px;">Live Viewport Inspector Standby</strong>
            Select a Demo Preset on the left and press <strong>Engage</strong> to watch real-time DOM bounding boxes, <code>#ff33cc</code> target locks, and System 1 probability gates.
          </div>
          <div id="vp-inner" class="viewport-inner" style="display:none;">
            <img id="viewport-img" alt="Live agent viewport">
            <svg id="viewport-svg" viewBox="0 0 1100 800" preserveAspectRatio="xMidYMid meet"></svg>
          </div>
        </div>
      </div>

      <div class="bottom-deck">
        <div class="field-row" style="margin-top:0;">
          <input id="hint-input" type="text" placeholder="Steer mid-flight: e.g. 'Prefer the Staging tier' or 'Click #3'" onkeydown="if(event.key==='Enter')sendHint()">
          <label class="toggle-pill">
            <input id="chk-remember-hint" type="checkbox" checked>
            <span>Remember</span>
          </label>
          <button id="btn-send-hint" class="btn btn-sm" type="button" onclick="sendHint()">Send Guidance</button>
          <label class="toggle-pill" style="margin-left:8px;" title="Inter-step observability pause">
            <span>Pace</span>
            <input id="rng-delay" type="range" min="0" max="2.5" step="0.1" value="0.5" style="width:75px;" onchange="updateConfig({step_delay: parseFloat(this.value)})">
          </label>
        </div>
        <div id="log-box" class="log-box" aria-label="Telemetry event stream"></div>
      </div>
    </section>
  </main>

  <script>
    let currentMode = 'autopilot';
    let currentPlaybook = null;
    let activeTargetId = null;
    let playbookDirty = false;

    function esc(s) {
      const d = document.createElement('div');
      d.textContent = s == null ? '' : String(s);
      return d.innerHTML;
    }

    function setMode(mode) {
      currentMode = mode;
      for (const m of ['autopilot', 'preview', 'step']) {
        document.getElementById('mode-' + m).classList.toggle('active', m === mode);
      }
      updateConfig({mode});
    }

    function addKvRow(key = '', val = '', secret = false) {
      const wrap = document.getElementById('kv-container');
      const row = document.createElement('div');
      row.className = 'kv-row';
      const isSec = secret || /pass|secret|token|key/i.test(key);
      row.innerHTML = `
        <input type="text" class="kv-k" placeholder="key" value="${esc(key)}" oninput="refreshKvCount()">
        <input type="${isSec ? 'password' : 'text'}" class="kv-v" placeholder="value (RAM only)" value="${esc(val)}">
        <button class="btn btn-sm" type="button" onclick="this.parentElement.remove();refreshKvCount()">×</button>
      `;
      wrap.appendChild(row);
      refreshKvCount();
    }

    function refreshKvCount() {
      const keys = Object.keys(collectData());
      document.getElementById('data-count').textContent = keys.length + (keys.length === 1 ? ' key' : ' keys');
    }

    function collectData() {
      const out = {};
      document.querySelectorAll('#kv-container .kv-row').forEach(r => {
        const k = r.querySelector('.kv-k').value.trim();
        const v = r.querySelector('.kv-v').value;
        if (k && v !== '') out[k] = v;
      });
      return out;
    }

    function loadPreset(name) {
      const kw = document.getElementById('kv-container');
      kw.innerHTML = '';
      playbookDirty = false;
      currentPlaybook = null;
      if (name === 'login') {
        document.getElementById('intent-input').value = 'Log in with the given email and password';
        document.getElementById('url-input').value = '/demo/login';
        addKvRow('email', 'operator@example.com', false);
        addKvRow('password', 'hunter2', true);
        document.getElementById('data-drawer').open = true;
      } else if (name === 'portal') {
        document.getElementById('intent-input').value = "Open /demo/portal -> search 'clef-flash' -> click 'Filter Catalog' -> click 'Generate API Key'";
        document.getElementById('url-input').value = '/demo/portal';
      } else if (name === 'safety') {
        document.getElementById('intent-input').value = "Open /demo/portal -> click 'Delete Workspace'";
        document.getElementById('url-input').value = '/demo/portal';
      } else if (name === 'wiki') {
        document.getElementById('intent-input').value = "Search wikipedia.org for 'Alan Turing' and open the article";
        document.getElementById('url-input').value = 'https://www.wikipedia.org';
      }
      refreshKvCount();
      compileBlueprint(false);
    }

    async function compileBlueprint(inspectLive = false) {
      const task = document.getElementById('intent-input').value.trim();
      const url = document.getElementById('url-input').value.trim() || null;
      if (!task) return;
      const r = await fetch('/api/plan', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({task, url, data: collectData(), inspect_live: inspectLive})
      });
      const j = await r.json();
      if (j.ok && j.playbook) {
        currentPlaybook = j.playbook;
        playbookDirty = false;
        if (j.playbook.start_url && !document.getElementById('url-input').value.trim()) {
          document.getElementById('url-input').value = j.playbook.start_url;
        }
        renderPlaybook(j.playbook, j.missing_keys || []);
      }
    }

    function renderPlaybook(pb, missingKeys = []) {
      const list = document.getElementById('stage-list');
      const missEl = document.getElementById('missing-keys-alert');
      if (missingKeys && missingKeys.length) {
        missEl.style.display = 'block';
        missEl.textContent = 'Missing inputs (will prompt if needed): ' + missingKeys.join(', ');
      } else {
        missEl.style.display = 'none';
      }
      if (!pb || !pb.stages || !pb.stages.length) {
        list.innerHTML = '<div style="color:var(--text-muted);font-size:12.5px;">No stages compiled yet.</div>';
        return;
      }
      list.innerHTML = '';
      pb.stages.forEach((st, idx) => {
        const row = document.createElement('div');
        row.className = 'stage-item';
        row.dataset.status = st.status || 'pending';
        const marker = st.status === 'completed' ? '✓' : (st.status === 'active' ? '▶' : String(idx + 1).padStart(2, '0'));
        row.innerHTML = `
          <span class="stage-num">${marker}</span>
          <span class="stage-kind">${esc(st.kind || 'auto')}</span>
          <input type="text" class="stage-intent-input" value="${esc(st.intent)}" onchange="editStage(${idx}, this.value)">
          <div style="display:flex;gap:4px;align-items:center;">
            ${st.sensitive ? '<span class="sens-tag" title="Triggers Human-in-the-Loop approval">GATE</span>' : ''}
            <button class="btn btn-sm" type="button" title="Remove stage" onclick="removeStage(${idx})">×</button>
          </div>
        `;
        list.appendChild(row);
      });
    }

    function editStage(idx, val) {
      if (!currentPlaybook) return;
      currentPlaybook.stages[idx].intent = val;
      playbookDirty = true;
    }

    function removeStage(idx) {
      if (!currentPlaybook) return;
      currentPlaybook.stages.splice(idx, 1);
      currentPlaybook.stages.forEach((s, i) => s.id = i + 1);
      playbookDirty = true;
      renderPlaybook(currentPlaybook);
    }

    function addStage(text = 'Verify result on screen', kind = 'auto') {
      if (!currentPlaybook) {
        currentPlaybook = {
          schema: 'based-clef/playbook@1',
          goal: document.getElementById('intent-input').value.trim() || 'Custom Playbook',
          start_url: document.getElementById('url-input').value.trim() || null,
          driver: document.getElementById('driver-select').value,
          stages: []
        };
      }
      currentPlaybook.stages.push({
        id: currentPlaybook.stages.length + 1,
        intent: text,
        kind: kind,
        status: 'pending',
        sensitive: /delete|pay|purchase|remove|checkout/i.test(text)
      });
      playbookDirty = true;
      renderPlaybook(currentPlaybook);
    }

    function exportPlaybook() {
      if (!currentPlaybook) return;
      const blob = new Blob([JSON.stringify(currentPlaybook, null, 2)], {type: 'application/json'});
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'based-clef.playbook.json';
      a.click();
    }

    function importPlaybook(ev) {
      const f = ev.target.files[0];
      if (!f) return;
      const reader = new FileReader();
      reader.onload = () => {
        try {
          const pb = JSON.parse(reader.result);
          currentPlaybook = pb;
          playbookDirty = true;
          if (pb.goal) document.getElementById('intent-input').value = pb.goal;
          if (pb.start_url) document.getElementById('url-input').value = pb.start_url;
          renderPlaybook(pb);
        } catch (e) {
          appendLog('error', 'Invalid playbook JSON file');
        }
      };
      reader.readAsText(f);
    }

    async function engageRun() {
      const task = document.getElementById('intent-input').value.trim();
      const url = document.getElementById('url-input').value.trim() || null;
      if (!task && !currentPlaybook) return;
      const body = {
        task: task || (currentPlaybook && currentPlaybook.goal) || '',
        url,
        data: collectData(),
        driver: document.getElementById('driver-select').value,
        mode: currentMode,
        offline: document.getElementById('chk-offline').checked,
        playbook: playbookDirty ? currentPlaybook : null
      };
      const r = await fetch('/api/run', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body)
      });
      const j = await r.json();
      if (!j.ok) appendLog('error', j.error || 'Run failed to start');
    }

    async function toggleHold() {
      const r = await fetch('/api/hold', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
      const j = await r.json();
      if (j.ok) setHeldUI(j.held);
    }

    function setHeldUI(held) {
      const btn = document.getElementById('btn-hold');
      btn.setAttribute('aria-pressed', held ? 'true' : 'false');
      btn.textContent = held ? 'Resume' : 'Hold';
    }

    async function stopRun() {
      await fetch('/api/stop', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
    }

    async function sendHint(customText) {
      const inp = document.getElementById('hint-input');
      const hint = (customText || inp.value).trim();
      if (!hint) return;
      const remember = document.getElementById('chk-remember-hint').checked;
      await fetch('/api/hint', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({hint, remember})
      });
      if (!customText) inp.value = '';
    }

    async function updateConfig(patch) {
      const r = await fetch('/api/config', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(patch)
      });
      const j = await r.json();
      if (j.ok) applyStatus(j);
    }

    function showHitl(ask) {
      const banner = document.getElementById('hitl-banner');
      if (!ask) {
        banner.classList.remove('active');
        return;
      }
      banner.classList.add('active');
      document.getElementById('hitl-text').textContent = ask.prompt || 'Input requested';
      const inp = document.getElementById('hitl-input');
      const saveWrap = document.getElementById('hitl-save-wrap');
      const btnOk = document.getElementById('btn-hitl-approve');
      const btnNo = document.getElementById('btn-hitl-deny');

      if (ask.kind === 'value' || ask.kind === 'stuck' || ask.kind === 'preview') {
        inp.style.display = 'inline-block';
        inp.type = ask.secret ? 'password' : 'text';
        inp.value = '';
        inp.placeholder = ask.kind === 'value' ? 'Enter value (RAM isolated)...' : 'Optional note or refinement...';
        inp.focus();
      } else {
        inp.style.display = 'none';
      }
      saveWrap.style.display = ask.kind === 'value' ? 'inline-flex' : 'none';
      btnOk.textContent = ask.kind === 'value' ? 'Submit' : (ask.kind === 'preview' ? 'Execute Blueprint' : 'Approve');
      btnNo.textContent = ask.kind === 'stuck' ? 'Stop' : 'Deny';
    }

    async function answerHitl(ok) {
      const val = document.getElementById('hitl-input').value || null;
      const save = document.getElementById('hitl-save').checked;
      await fetch('/api/answer', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ok, value: val, save})
      });
      showHitl(null);
    }

    async function redactSecrets() {
      await fetch('/api/redact', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
      document.getElementById('secrets-banner').classList.remove('active');
      appendLog('secrets', 'Redacted saved secrets from disk');
    }

    function setGate(idx, val) {
      const lbl = document.getElementById('val-p' + idx);
      const bar = document.getElementById('bar-p' + idx);
      if (val == null) {
        lbl.textContent = '-';
        bar.style.width = '0%';
      } else {
        const pct = Math.max(0, Math.min(100, Math.round(val * 100)));
        lbl.textContent = val.toFixed(2);
        bar.style.width = pct + '%';
      }
    }

    async function refreshFrame() {
      const r = await fetch('/api/frame');
      const j = await r.json();
      if (!j.ok) return;
      const snap = j.snapshot || {};
      const img = document.getElementById('viewport-img');
      const inner = document.getElementById('vp-inner');
      const empty = document.getElementById('vp-empty');
      if (j.frame_b64) {
        img.src = 'data:image/jpeg;base64,' + j.frame_b64;
        img.style.display = 'block';
        inner.style.display = 'block';
        empty.style.display = 'none';
      } else if (snap.elements && snap.elements.length) {
        img.style.display = 'none';
        inner.style.display = 'block';
        empty.style.display = 'none';
      }
      if (snap.url) {
        document.getElementById('vp-url').textContent = (snap.title ? snap.title + ' — ' : '') + snap.url;
      }
      renderViewportBoxes(snap);
    }

    function renderViewportBoxes(snap) {
      const svg = document.getElementById('viewport-svg');
      const vp = snap.viewport || [1100, 800];
      svg.setAttribute('viewBox', `0 0 ${vp[0]} ${vp[1]}`);
      svg.innerHTML = '';
      (snap.elements || []).forEach(el => {
        if (!el.bbox || el.bbox.length < 4) return;
        const [x, y, w, h] = el.bbox;
        if (w <= 0 || h <= 0) return;
        const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
        rect.setAttribute('x', x);
        rect.setAttribute('y', y);
        rect.setAttribute('width', w);
        rect.setAttribute('height', h);
        rect.setAttribute('rx', '3');
        rect.setAttribute('class', 'el-box' + (el.id === activeTargetId ? ' active-target' : ''));
        rect.addEventListener('mouseenter', () => {
          document.getElementById('vp-hover-info').textContent = `#${el.id} ${el.role} "${el.name || ''}"`;
        });
        rect.addEventListener('click', () => {
          const directive = `Act on #${el.id} "${el.name || el.role}"`;
          if (document.getElementById('badge-state').dataset.state !== 'idle' &&
              document.getElementById('badge-state').dataset.state !== 'done') {
            sendHint(directive);
          } else {
            const kind = (el.tag === 'input' || el.tag === 'textarea' || el.tag === 'select') ? 'fill' : 'click';
            addStage(`${kind === 'fill' ? 'Fill' : 'Click'} "${el.name || '#' + el.id}"`, kind);
          }
        });
        svg.appendChild(rect);
      });
    }

    function appendLog(kind, text) {
      const box = document.getElementById('log-box');
      const div = document.createElement('div');
      div.className = 'log-line';
      div.dataset.kind = kind || '';
      const ts = new Date().toTimeString().slice(0, 8);
      div.textContent = `${ts} ${(kind || 'info').padEnd(8)} ${text}`;
      box.appendChild(div);
      while (box.children.length > 100) box.removeChild(box.firstChild);
      box.scrollTop = box.scrollHeight;
    }

    function applyStatus(st) {
      const s = st.stats || {};
      const c = st.cfg || {};
      const badge = document.getElementById('badge-state');
      badge.textContent = (s.state || 'idle').toUpperCase();
      badge.dataset.state = s.state || 'idle';
      document.getElementById('stat-model').textContent = (s.model || '-').split('/').pop();
      document.getElementById('stat-step').textContent = s.step || 0;
      document.getElementById('stat-stage').textContent = `${s.stage_index || 0}/${s.stage_total || 0}`;
      document.getElementById('stat-lat').textContent = `${Math.round(s.latency_ms || 0)} ms`;
      document.getElementById('stat-calls').textContent = `${s.clef_calls || 0}/${s.llm_calls || 0}`;
      document.getElementById('chk-offline').checked = !!c.offline;
      document.getElementById('cred-indicator').textContent = c.has_credentials
        ? 'WORKERS AI READY'
        : (c.offline ? 'SANDBOX ENGINE' : 'NO API TOKEN');
      setHeldUI(!!st.held);
      setGate(1, s.p_target);
      setGate(2, s.p_action);
      setGate(3, s.p_done);
      setGate(4, s.p_sensitive);
      if (st.playbook && !playbookDirty) {
        currentPlaybook = st.playbook;
        renderPlaybook(st.playbook);
      }
      showHitl(st.pending_ask);
    }

    function connectSSE() {
      const es = new EventSource('/api/events');
      es.onmessage = async (msg) => {
        const ev = JSON.parse(msg.data);
        if (ev.kind === 'hello' && ev.status) {
          applyStatus(ev.status);
          (ev.status.logs || []).forEach(l => {
            const box = document.getElementById('log-box');
            const d = document.createElement('div');
            d.className = 'log-line';
            d.textContent = l;
            box.appendChild(d);
          });
          if (ev.status.viewport && (ev.status.viewport.has_frame || (ev.status.viewport.elements || []).length)) {
            refreshFrame();
          }
          return;
        }
        if (ev.kind === 'viewport') {
          refreshFrame();
          return;
        }
        if (ev.kind === 'hitl_pending') {
          showHitl(ev);
          return;
        }
        if (ev.kind === 'hitl_cleared') {
          showHitl(null);
          return;
        }
        if (ev.kind === 'secrets_saved') {
          document.getElementById('secrets-text').textContent = 'Saved secrets (0600): ' + (ev.names || []).join(', ');
          document.getElementById('secrets-banner').classList.add('active');
          return;
        }
        if (ev.kind === 'plan' && ev.playbook) {
          currentPlaybook = ev.playbook;
          playbookDirty = false;
          renderPlaybook(ev.playbook, ev.missing_keys || []);
        }
        if (ev.kind === 'stage' && ev.stages && currentPlaybook) {
          currentPlaybook.stages = ev.stages;
          renderPlaybook(currentPlaybook);
        }
        if (ev.kind === 'step') {
          activeTargetId = ev.el_id || null;
          refreshFrame();
        }
        if (ev.msg) appendLog(ev.kind, ev.msg);
        const r = await fetch('/api/status');
        const st = await r.json();
        if (st.ok) applyStatus(st);
      };
      es.onerror = () => {
        es.close();
        setTimeout(connectSSE, 2000);
      };
    }

    document.addEventListener('keydown', (e) => {
      if (e.altKey && e.key.toLowerCase() === 'h') {
        e.preventDefault();
        toggleHold();
      }
    });

    connectSSE();
  </script>
</body>
</html>
"""
