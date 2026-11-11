"""Phase 5 test suite — Intent-to-Playbook Compiler, Execution Modes, Offline Sandbox, Studio Server, and Rebrand SDK."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock
import urllib.request

import based_clef
from based_clef import (
    Agent,
    BrowserDriver,
    Config,
    HeuristicClefClient,
    HeuristicLLMClient,
    Playbook,
    Runtime,
    Stage,
    StudioServer,
    Telemetry,
    compile_intent_fast,
    extract_url_from_intent,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
LOGIN_DEMO_URL = f"file://{REPO_ROOT / 'decision_agent' / 'demo' / 'login.html'}"
PORTAL_DEMO_URL = f"file://{REPO_ROOT / 'decision_agent' / 'demo' / 'portal.html'}"


class TestRebrandAndIntentCompiler(unittest.TestCase):
    def test_based_clef_sdk_exports_and_env_precedence(self):
        self.assertTrue(hasattr(based_clef, "__version__"))
        self.assertIs(based_clef.Agent, Agent)
        self.assertIs(based_clef.Playbook, Playbook)

        old_env = dict(os.environ)
        try:
            os.environ["BASED_CLEF_ACCOUNT_ID"] = "acct_based_123"
            os.environ["BASED_CLEF_API_TOKEN"] = "tok_based_456"
            os.environ["BASED_CLEF_MODE"] = "preview"
            cfg = Config.from_env()
            self.assertEqual(cfg.account_id, "acct_based_123")
            self.assertEqual(cfg.api_token, "tok_based_456")
            self.assertEqual(cfg.mode, "preview")
            self.assertTrue(cfg.has_credentials())
        finally:
            os.environ.clear()
            os.environ.update(old_env)

    def test_extract_url_ignores_emails_and_parses_domains(self):
        self.assertIsNone(extract_url_from_intent("Log in with dev@example.com and password"))
        self.assertEqual(
            extract_url_from_intent("Open wikipedia.org and search for Ada Lovelace"),
            "https://wikipedia.org",
        )
        self.assertEqual(
            extract_url_from_intent("Navigate to https://console.groq.com/keys -> click Create"),
            "https://console.groq.com/keys",
        )

    def test_pipeline_shorthand_and_sensitive_stage_detection(self):
        pb = compile_intent_fast(
            "goto https://app.example.com/settings -> fill workspace name -> click Delete Workspace",
            data={"email": "dev@example.com"},
        )
        self.assertEqual(pb.start_url, "https://app.example.com/settings")
        self.assertEqual(len(pb.stages), 3)
        self.assertEqual(pb.stages[0].kind, "navigate")
        self.assertEqual(pb.stages[1].kind, "fill")
        self.assertEqual(pb.stages[2].kind, "click")
        self.assertTrue(pb.stages[2].sensitive)

    def test_login_intent_extracts_inline_email_and_missing_keys(self):
        pb = compile_intent_fast("Sign in as ada@analytical.org on portal.example.com")
        self.assertEqual(pb.start_url, "https://portal.example.com")
        self.assertEqual(pb.extracted_data.get("email"), "ada@analytical.org")
        self.assertIn("password", pb.required_keys)
        self.assertEqual(pb.missing_keys(pb.extracted_data), ["password"])

    def test_playbook_lifecycle_and_secret_safe_serialization(self):
        pb = Playbook(
            raw_intent="Log in and generate key",
            goal="Log in and generate key",
            start_url=LOGIN_DEMO_URL,
            required_keys=["email", "password"],
            extracted_data={"email": "dev@example.com", "password": "MUST_NEVER_PERSIST"},
            stages=[
                Stage(id=1, intent="Open login page", kind="navigate", url=LOGIN_DEMO_URL),
                Stage(id=2, intent="Fill email", kind="fill", target_hint="Email", value_key="email"),
                Stage(id=3, intent="Click Sign In", kind="click", target_hint="Sign In"),
            ],
        )
        nav = pb.mark_navigated(LOGIN_DEMO_URL)
        self.assertIsNotNone(nav)
        self.assertEqual(pb.stages[0].status, "completed")
        self.assertEqual(pb.stages[1].status, "active")

        adv = pb.record_effect("fill", "Email", step_num=1, confidence=0.94, via="clef")
        self.assertIsNotNone(adv)
        self.assertEqual(pb.stages[1].status, "completed")
        self.assertEqual(pb.stages[2].status, "active")
        self.assertIn("Stage 3/3", pb.prompt_context())

        with tempfile.TemporaryDirectory() as td:
            out_file = Path(td) / "test.playbook.json"
            pb.save(out_file)
            raw_text = out_file.read_text(encoding="utf-8")
            self.assertNotIn("MUST_NEVER_PERSIST", raw_text)
            loaded = Playbook.load(out_file)
            self.assertEqual(loaded.goal, "Log in and generate key")
            self.assertEqual(len(loaded.stages), 3)
            # Saved playbooks reset stage statuses to pending for clean replay
            self.assertTrue(all(s.status == "pending" for s in loaded.stages))


class TestExecutionModesAndOfflineBrowser(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.tmp = Path(self.td.name)
        self.log_path = self.tmp / "telemetry.jsonl"
        self.train_path = self.tmp / "training.jsonl"

    async def asyncTearDown(self):
        self.td.cleanup()

    async def test_preview_mode_cancel_stops_before_acting(self):
        cfg = Config(headless=True, offline=True, mode="preview")
        tel = Telemetry(log_path=self.log_path)
        clef = HeuristicClefClient(cfg, tel)
        llm = HeuristicLLMClient(cfg, tel)
        drv = MagicMock()
        drv.snapshot = AsyncMock(return_value={"url": LOGIN_DEMO_URL, "title": "Sign In", "elements": [], "text": ""})
        drv.goto = AsyncMock()

        agent = Agent(cfg, tel, clef, llm, drv)
        agent.train_log = self.train_path
        agent.ask = AsyncMock(return_value={"ok": False, "value": None, "save": False})

        ok = await agent.run("Log in with email and password", data={"email": "a@b.com", "password": "pw"}, url=LOGIN_DEMO_URL)
        self.assertFalse(ok)
        agent.ask.assert_awaited_once()
        self.assertEqual(tel.stats.clef_calls, 0)

    async def test_step_mode_requests_approval_before_action(self):
        cfg = Config(headless=True, offline=True, mode="step", max_steps=3, step_delay=0.0)
        tel = Telemetry(log_path=self.log_path)
        clef = HeuristicClefClient(cfg, tel)
        llm = HeuristicLLMClient(cfg, tel)
        br = BrowserDriver(headless=True, highlight=False)
        await br.start()
        try:
            agent = Agent(cfg, tel, clef, llm, br)
            agent.train_log = self.train_path
            # Deny on first step gate to confirm step co-pilot intercepts execution cleanly
            agent.ask = AsyncMock(return_value={"ok": False, "value": None, "save": False})
            ok = await agent.run(
                "Log in with the given email and password",
                data={"email": "dev@example.com", "password": "pw"},
                url=LOGIN_DEMO_URL,
            )
            self.assertFalse(ok)
            agent.ask.assert_awaited_once()
            args, _ = agent.ask.await_args
            self.assertEqual(args[0], "approve")
            self.assertIn("Step #1", args[1])
        finally:
            await br.close()

    async def test_offline_login_demo_and_playbook_export(self):
        cfg = Config(headless=True, offline=True, step_delay=0.0)
        tel = Telemetry(log_path=self.log_path)
        clef = HeuristicClefClient(cfg, tel)
        llm = HeuristicLLMClient(cfg, tel)
        br = BrowserDriver(headless=True, highlight=False)
        await br.start()
        try:
            export_path = self.tmp / "exported.playbook.json"
            agent = Agent(cfg, tel, clef, llm, br)
            agent.train_log = self.train_path
            ok = await agent.run(
                "Log in with the given email and password",
                data={"email": "dev@example.com", "password": "super-secret-password-99"},
                url=LOGIN_DEMO_URL,
                save_playbook_path=export_path,
            )
            self.assertTrue(ok)
            snap = await br.snapshot()
            self.assertIn("Welcome back", snap["text"])
            self.assertTrue(export_path.exists())
            saved_raw = export_path.read_text(encoding="utf-8")
            self.assertNotIn("super-secret-password-99", saved_raw)
            train_raw = self.train_path.read_text(encoding="utf-8")
            self.assertNotIn("super-secret-password-99", train_raw)
        finally:
            await br.close()

    async def test_offline_portal_demo_select_dropdown_and_sensitive_gate(self):
        cfg = Config(headless=True, offline=True, step_delay=0.0)
        tel = Telemetry(log_path=self.log_path)
        clef = HeuristicClefClient(cfg, tel)
        llm = HeuristicLLMClient(cfg, tel)
        br = BrowserDriver(headless=True, highlight=False)
        await br.start()
        try:
            agent = Agent(cfg, tel, clef, llm, br)
            agent.train_log = self.train_path
            ok = await agent.run(
                "Search 'clef-flash', select Production in Environment Tier, and click Generate API Key",
                data={"search": "clef-flash", "tier": "Production"},
                url=PORTAL_DEMO_URL,
            )
            self.assertTrue(ok)
            snap = await br.snapshot()
            self.assertIn("bc_live_demo_9f8a7c6b", snap["text"])

            # Now test irreversible "Delete Workspace" action triggers sensitive HITL gate and blocks when denied
            agent2 = Agent(cfg, tel, clef, llm, br)
            agent2.train_log = self.train_path
            agent2.ask = AsyncMock(return_value={"ok": False, "value": None, "save": False})
            ok_del = await agent2.run("Click Delete Workspace", url=PORTAL_DEMO_URL)
            self.assertFalse(ok_del)
            agent2.ask.assert_awaited_once()
            snap_after = await br.snapshot()
            self.assertNotIn("WORKSPACE DELETED", snap_after["text"])
        finally:
            await br.close()


class TestStudioWebServer(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.tmp = Path(self.td.name)
        cfg = Config(headless=True, offline=True, step_delay=0.0)
        self.rt = based_clef.build(cfg)
        self.rt.tel.log_path = self.tmp / "studio_tel.jsonl"
        self.rt.agent.train_log = self.tmp / "studio_train.jsonl"
        self.studio = StudioServer(self.rt, host="127.0.0.1", port=0)
        await self.studio.start()

    async def asyncTearDown(self):
        await self.studio.stop()
        await self.rt.shutdown()
        self.td.cleanup()

    def _req(self, path: str, method: str = "GET", payload: dict | None = None) -> tuple[int, dict | str]:
        url = f"{self.studio.base_url}{path}"
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
            ctype = resp.headers.get("Content-Type", "")
            if "application/json" in ctype:
                return resp.status, json.loads(raw)
            return resp.status, raw

    async def test_studio_endpoints_sse_and_end_to_end_run(self):
        # 1. HTML index & demo pages
        code, html = await asyncio.to_thread(self._req, "/")
        self.assertEqual(code, 200)
        self.assertIn("Based Clef Studio", html)

        # 2. Initial status
        code, st = await asyncio.to_thread(self._req, "/api/status")
        self.assertEqual(code, 200)
        self.assertTrue(st["ok"])
        self.assertTrue(st["cfg"]["offline"])

        # 3. SSE /api/events emits initial `hello` event
        reader, writer = await asyncio.open_connection("127.0.0.1", self.studio.port)
        try:
            writer.write(b"GET /api/events HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
            await writer.drain()
            sse_buf = b""
            while b"\n\n" not in sse_buf.split(b"\r\n\r\n", 1)[-1]:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=5.0)
                if not chunk:
                    break
                sse_buf += chunk
            body_part = sse_buf.split(b"\r\n\r\n", 1)[1].decode("utf-8")
            self.assertTrue(body_part.startswith("data: "))
            hello_ev = json.loads(body_part.splitlines()[0].removeprefix("data: "))
            self.assertEqual(hello_ev["kind"], "hello")
        finally:
            writer.close()
            await writer.wait_closed()

        # 4. Blueprint compilation via /api/plan
        code, plan_res = await asyncio.to_thread(
            self._req,
            "/api/plan",
            "POST",
            {"task": "Log in with the given email and password", "url": "/demo/login", "data": {"email": "dev@example.com"}},
        )
        self.assertEqual(code, 200)
        self.assertTrue(plan_res["ok"])
        self.assertGreaterEqual(len(plan_res["playbook"]["stages"]), 2)
        self.assertIn("password", plan_res["missing_keys"])

        # 5. Hold, Hint, and Config updates
        code, hold_res = await asyncio.to_thread(self._req, "/api/hold", "POST", {"on": True})
        self.assertEqual(code, 200)
        self.assertTrue(hold_res["held"])
        await asyncio.to_thread(self._req, "/api/hold", "POST", {"on": False})

        code, _ = await asyncio.to_thread(self._req, "/api/hint", "POST", {"hint": "Prefer primary button", "remember": True})
        self.assertEqual(code, 200)

        code, cfg_res = await asyncio.to_thread(self._req, "/api/config", "POST", {"threshold": 0.75, "mode": "autopilot"})
        self.assertEqual(code, 200)
        self.assertEqual(cfg_res["cfg"]["threshold"], 0.75)

        # 6. Full /api/run against /demo/login and verify live /api/frame capture
        code, run_res = await asyncio.to_thread(
            self._req,
            "/api/run",
            "POST",
            {
                "task": "Log in with the given email and password",
                "url": "/demo/login",
                "data": {"email": "dev@example.com", "password": "pw123"},
                "offline": True,
            },
        )
        self.assertEqual(code, 202)
        self.assertTrue(run_res["started"])

        assert self.studio._run_task_handle is not None
        ok = await asyncio.wait_for(self.studio._run_task_handle, timeout=20.0)
        self.assertTrue(ok)

        code, frame_res = await asyncio.to_thread(self._req, "/api/frame")
        self.assertEqual(code, 200)
        self.assertTrue(frame_res["ok"])
        self.assertIsNotNone(frame_res["frame_b64"])
        self.assertIn("Welcome back", frame_res["snapshot"]["text"])


class TestCLISubcommands(unittest.TestCase):
    def _run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "based_clef", *args],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=25,
        )

    def test_cli_plan_doctor_and_offline_ask(self):
        r_plan = self._run_cli("plan", "goto wikipedia.org -> search Ada Lovelace", "--json")
        self.assertEqual(r_plan.returncode, 0, r_plan.stderr)
        pb = json.loads(r_plan.stdout)
        self.assertEqual(pb["schema"], "based-clef/playbook@1")
        self.assertEqual(pb["start_url"], "https://wikipedia.org")

        r_doc = self._run_cli("doctor", "--json")
        self.assertEqual(r_doc.returncode, 0, r_doc.stderr)
        report = json.loads(r_doc.stdout)
        self.assertIn("python", report)
        self.assertTrue(report["browser_found"])

        r_ask = self._run_cli("ask", "Is this ready?", "--state", "Ready to deploy release v0.2.0.", "--offline")
        self.assertEqual(r_ask.returncode, 0, r_ask.stderr)
        ans = json.loads(r_ask.stdout)
        self.assertIn("confidence", ans)


if __name__ == "__main__":
    unittest.main()
