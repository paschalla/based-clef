"""Real-world integration and resilience tests for decision-agent (Clef)."""
from __future__ import annotations

import asyncio
import http.server
import json
import os
import socketserver
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from decision_agent.agent import Agent, _compact, _gate, _questions, load_secrets, redact_secret, save_secrets
from decision_agent.browser import BrowserDriver
from decision_agent.core import ClefClient, Config, LLMClient, Telemetry


REALWORLD_PAGES: dict[str, str] = {
    "/": """<!doctype html>
<html><head><meta charset="utf-8"><title>LLM Gateway Directory</title>
<style>
  body { font: 15px sans-serif; margin: 24px; }
  .hidden-trap { display: none; }
  .tiny-pixel { display: inline-block; width: 1px; height: 1px; overflow: hidden; }
</style></head>
<body>
  <h1>Free Tier LLM Gateways</h1>
  <button class="hidden-trap" onclick="window.location='/trap'">Hidden Trap</button>
  <a class="tiny-pixel" href="/trap">1px Trap</a>
  <form id="search-form" onsubmit="event.preventDefault(); window.location='/oauth?q=' + encodeURIComponent(document.getElementById('q').value);">
    <label>Search Providers <input id="q" type="search" placeholder="Search gateway or model"></label>
    <button id="search-btn" type="submit">Search Directory</button>
  </form>
</body></html>""",
    "/oauth": """<!doctype html>
<html><head><meta charset="utf-8"><title>OAuth & Credential Check</title></head>
<body>
  <h2>Provider Developer Portal</h2>
  <p>Authenticate to provision a high-quota free API key.</p>
  <button id="oauth-btn" onclick="document.getElementById('oauth-status').innerText='OAuth Linked';">Continue with GitHub</button>
  <p id="oauth-status"></p>
  <form id="cred-form" onsubmit="event.preventDefault(); if(document.getElementById('email').value && document.getElementById('pwd').value){ window.location='/dashboard'; }">
    <label>Developer Email <input id="email" type="email" required placeholder="Developer Email"></label>
    <label>Account Password <input id="pwd" type="password" required placeholder="Account Password"></label>
    <button id="login-btn" type="submit">Sign In to Console</button>
  </form>
</body></html>""",
    "/dashboard": """<!doctype html>
<html><head><meta charset="utf-8"><title>API Key Console</title></head>
<body>
  <h2>Free Tier API Key Console</h2>
  <p id="status-msg">Active Quota: 7.4B tokens/month</p>
  <button id="gen-key" onclick="document.getElementById('status-msg').innerText='Key Generated: sk-free-live-ok';">Generate Free API Key</button>
  <button id="del-acct" onclick="document.getElementById('status-msg').innerText='DELETED';">Delete Production Account</button>
</body></html>""",
    "/blank": """<!doctype html><html><head><title>Loading...</title></head><body><p>Hydrating SPA...</p></body></html>""",
}


class _SilentHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        body = REALWORLD_PAGES.get(path, REALWORLD_PAGES["/"]).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002
        return


class _LocalWebServer:
    def __init__(self) -> None:
        socketserver.TCPServer.allow_reuse_address = True
        self.httpd = socketserver.TCPServer(("127.0.0.1", 0), _SilentHandler)
        self.port = self.httpd.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self) -> "_LocalWebServer":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class TestClefSchemaAndTargetResilience(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.log_path = Path(self.tmp.name) / "events.jsonl"
        self.train_path = Path(self.tmp.name) / "training.jsonl"
        self.cfg = Config(account_id="acc", api_token="tok", vision=False)
        self.tel = Telemetry(log_path=self.log_path)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_questions_guarantees_minimum_two_choices_on_zero_elements(self):
        """Cloudflare Clef returns HTTP 422 if choice criteria has <2 items."""
        empty_snap = {"elements": [], "text": "Loading...", "url": "about:blank", "title": "", "scroll": [0, 0]}
        q = _questions("Wait for page load", empty_snap)
        criteria = q["target"]["criteria"]
        self.assertGreaterEqual(len(criteria), 2)
        self.assertIn("0", criteria)
        self.assertIn("-1", criteria)

    async def test_act_handles_null_zero_negative_and_url_targets_safely(self):
        """Verify _act never raises ValueError/StopIteration on non-numeric or sentinel targets."""
        clef = MagicMock()
        llm = MagicMock()
        br = MagicMock()
        br.goto = AsyncMock()
        br.click = AsyncMock()
        br.fill = AsyncMock()
        br.scroll = AsyncMock()

        agent = Agent(self.cfg, self.tel, clef, llm, br)
        agent.train_log = self.train_path
        snap = {
            "elements": [{"id": 1, "tag": "button", "role": "button", "type": "button", "name": "Continue", "value": "", "empty": None}],
            "text": "Ready",
            "url": "http://127.0.0.1/",
            "title": "Test",
            "scroll": [0, 0],
        }

        # 1. Escalation LLM returns target="None" or None -> should not call int("None")
        with patch("asyncio.sleep", new=AsyncMock()):
            ok_none = await agent._act({"action": "click", "target": "None"}, "task", snap)
            self.assertFalse(ok_none)
            br.click.assert_not_awaited()

            # 2. Clef picks "0" ("none of these") or "-1" ("wait / page loading")
            ok_zero = await agent._act({"action": "click", "target": "0"}, "task", snap)
            self.assertTrue(ok_zero)
            br.click.assert_not_awaited()

        # 3. Escalation LLM emits a URL target or page.goto action
        ok_goto = await agent._act({"action": "page.goto", "target": "https://example.org/keys"}, "task", snap)
        self.assertTrue(ok_goto)
        br.goto.assert_awaited_once_with("https://example.org/keys")

        # 4. Escalation LLM emits element label instead of numeric ID
        ok_label = await agent._act({"action": "click", "target": "Continue"}, "task", snap)
        self.assertTrue(ok_label)
        br.click.assert_awaited_once_with(1)

        # 5. Missing fill target returns False instead of StopIteration
        ok_fill_missing = await agent._act({"action": "fill", "target": "999"}, "task", snap)
        self.assertFalse(ok_fill_missing)

    async def test_oauth_and_credential_voice_prompt_fires(self):
        """Verify audible voice prompt fires when hitting an OAuth button or missing secret."""
        cfg = Config(account_id="acc", api_token="tok", vision=False, voice_prompt=True)
        tel = Telemetry(log_path=self.log_path)
        clef = MagicMock()
        clef.decide = AsyncMock(
            return_value={
                "answers": {
                    "target": {"confidence": 0.92, "choice": "1"},
                    "action": {"confidence": 0.95, "choice": "click"},
                    "done": {"noul": 0.05},
                    "sensitive": {"noul": 0.10},
                }
            }
        )
        llm = MagicMock()
        br = MagicMock()
        snap_before = {
            "elements": [{"id": 1, "tag": "button", "role": "button", "type": "button", "name": "Continue with GitHub", "value": "", "empty": None}],
            "text": "Sign in",
            "url": "http://127.0.0.1/oauth",
            "title": "OAuth",
            "scroll": [0, 0],
        }
        snap_after = {
            "elements": [{"id": 1, "tag": "button", "role": "button", "type": "button", "name": "Continue with GitHub", "value": "linked", "empty": None}],
            "text": "OAuth Linked",
            "url": "http://127.0.0.1/oauth",
            "title": "OAuth",
            "scroll": [0, 0],
        }
        br.snapshot = AsyncMock(side_effect=[snap_before, snap_after])
        br.click = AsyncMock()
        br.settle = AsyncMock()

        agent = Agent(cfg, tel, clef, llm, br)
        agent.train_log = self.train_path
        agent.ask = AsyncMock(return_value={"ok": True, "value": None, "save": False})

        with patch("decision_agent.agent._voice_prompt") as mock_voice:
            res = await agent._step("Authenticate with GitHub OAuth", 1)
            self.assertEqual(res, "ok")
            mock_voice.assert_called_once()
            self.assertIn("OAuth or credential check", mock_voice.call_args[0][0])
            agent.ask.assert_awaited_once()
            br.click.assert_awaited_once_with(1)


class TestRealWorldBrowserWorkflow(unittest.IsolatedAsyncioTestCase):
    """End-to-end headless Chrome tests against a multi-page local web server."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = _LocalWebServer().start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.log_path = Path(self.tmp.name) / "events.jsonl"
        self.train_path = Path(self.tmp.name) / "training.jsonl"
        self.secrets_path = Path(self.tmp.name) / "secrets.env"
        self.br = BrowserDriver(headless=True)
        await self.br.start()

    async def asyncTearDown(self) -> None:
        await self.br.close()
        self.tmp.cleanup()

    async def test_dom_pruning_ignores_hidden_traps_and_masks_passwords(self):
        """Verify _SNAPSHOT_JS filters hidden/1px traps and masks password field values."""
        await self.br.goto(f"{self.server.base_url}/")
        snap = await self.br.snapshot()
        names = [e["name"] for e in snap["elements"]]
        self.assertNotIn("Hidden Trap", names)
        self.assertNotIn("1px Trap", names)
        self.assertIn("Search Providers", names)
        self.assertIn("Search Directory", names)

        await self.br.goto(f"{self.server.base_url}/oauth")
        snap_oauth = await self.br.snapshot()
        pwd_el = next(e for e in snap_oauth["elements"] if e["type"] == "password")
        await self.br.fill(pwd_el["id"], "super-secret-password-999")
        snap_filled = await self.br.snapshot()
        pwd_after = next(e for e in snap_filled["elements"] if e["type"] == "password")
        self.assertEqual(pwd_after["value"], "***")
        self.assertFalse(pwd_after["empty"])

    async def test_end_to_end_multi_page_auth_and_key_generation_with_secret_isolation(self):
        """Drive headless Chrome across /, /oauth, and /dashboard with ladder escalation, secret prompt, and sensitive action blocking."""
        cfg = Config(
            account_id="acc",
            api_token="tok",
            threshold=0.75,
            max_attempts=2,
            max_steps=12,
            headless=True,
            vision=True,
            voice_prompt=True,
        )
        tel = Telemetry(log_path=self.log_path)

        # Script realistic Clef decisions based on current URL and DOM state
        async def fake_clef_decide(state: dict, questions: dict, images_b64=None) -> dict:
            # Verify raw password never leaks into Clef state
            serialized = json.dumps(state)
            self.assertNotIn("hunter2-live-secret", serialized)
            self.assertGreaterEqual(len(questions["target"]["criteria"]), 2)
            self.assertTrue(images_b64 and len(images_b64) == 1)

            url = state["url"]
            els = state["elements"]
            if url.endswith("/"):
                search_inp = next(e for e in els if e.get("type") == "search")
                search_btn = next(e for e in els if e.get("type") == "submit")
                if search_inp.get("empty"):
                    # Click on a textbox: Agent._act automatically interprets as fill!
                    return {
                        "answers": {
                            "target": {"confidence": 0.91, "choice": str(search_inp["id"])},
                            "action": {"confidence": 0.88, "choice": "click"},
                            "done": {"noul": 0.01},
                            "sensitive": {"noul": 0.01},
                        }
                    }
                return {
                    "answers": {
                        "target": {"confidence": 0.94, "choice": str(search_btn["id"])},
                        "action": {"confidence": 0.95, "choice": "click"},
                        "done": {"noul": 0.05},
                        "sensitive": {"noul": 0.01},
                    }
                }
            if "/oauth" in url:
                email_el = next(e for e in els if e.get("type") == "email")
                pwd_el = next(e for e in els if e.get("type") == "password")
                submit_el = next(e for e in els if e.get("type") == "submit")
                if email_el.get("empty"):
                    return {
                        "answers": {
                            "target": {"confidence": 0.89, "choice": str(email_el["id"])},
                            "action": {"confidence": 0.90, "choice": "fill"},
                            "done": {"noul": 0.02},
                            "sensitive": {"noul": 0.02},
                        }
                    }
                if pwd_el.get("empty"):
                    # Simulate low confidence on attempt 1 -> triggers LLM rewriter hint -> high confidence on attempt 2
                    if "hint" not in state:
                        return {
                            "answers": {
                                "target": {"confidence": 0.52, "choice": str(pwd_el["id"])},
                                "action": {"confidence": 0.55, "choice": "fill"},
                                "done": {"noul": 0.02},
                                "sensitive": {"noul": 0.02},
                            }
                        }
                    return {
                        "answers": {
                            "target": {"confidence": 0.93, "choice": str(pwd_el["id"])},
                            "action": {"confidence": 0.94, "choice": "fill"},
                            "done": {"noul": 0.02},
                            "sensitive": {"noul": 0.02},
                        }
                    }
                return {
                    "answers": {
                        "target": {"confidence": 0.92, "choice": str(submit_el["id"])},
                        "action": {"confidence": 0.95, "choice": "click"},
                        "done": {"noul": 0.10},
                        "sensitive": {"noul": 0.05},
                    }
                }
            if "/dashboard" in url:
                if "sk-free-live-ok" in state.get("page_text", ""):
                    return {
                        "answers": {
                            "target": {"confidence": 0.95, "choice": "0"},
                            "action": {"confidence": 0.95, "choice": "wait"},
                            "done": {"noul": 0.96},
                            "sensitive": {"noul": 0.0},
                        }
                    }
                gen_btn = next(e for e in els if "Generate" in e.get("name", ""))
                return {
                    "answers": {
                        "target": {"confidence": 0.91, "choice": str(gen_btn["id"])},
                        "action": {"confidence": 0.93, "choice": "click"},
                        "done": {"noul": 0.15},
                        "sensitive": {"noul": 0.04},
                    }
                }
            raise AssertionError(f"Unexpected URL: {url}")

        clef = MagicMock()
        clef.decide = AsyncMock(side_effect=fake_clef_decide)
        llm = MagicMock()
        llm.complete = AsyncMock(return_value="Fill the empty Account Password field next.")

        agent = Agent(cfg, tel, clef, llm, self.br)
        agent.train_log = self.train_path

        # Provide password interactively via ask callback when prompted
        async def ask_handler(kind: str, prompt: str, secret: bool = False, name: str | None = None) -> dict:
            if kind == "value" and secret:
                return {"ok": True, "value": "hunter2-live-secret", "save": True}
            return {"ok": False, "value": None, "save": False}

        agent.ask = ask_handler

        with (
            patch("decision_agent.agent.SECRETS_PATH", self.secrets_path),
            patch("decision_agent.agent.load_secrets", side_effect=lambda p=self.secrets_path: load_secrets(p)),
            patch("decision_agent.agent.save_secrets", side_effect=lambda new, p=self.secrets_path: save_secrets(new, p)),
            patch("decision_agent.agent._voice_prompt") as mock_voice,
        ):
            ok = await agent.run(
                "Search 'OmniRoute', sign in to console, and generate a free API key",
                data={"email": "dev@example.com"},
                url=f"{self.server.base_url}/",
            )
            self.assertTrue(ok)
            mock_voice.assert_called_once()

        # Verify final DOM state reached key generation
        final_snap = await self.br.snapshot()
        self.assertIn("sk-free-live-ok", final_snap["text"])

        # Verify secret was persisted with 0600 permissions ONLY after success and never leaked to logs
        self.assertTrue(self.secrets_path.exists())
        self.assertEqual(self.secrets_path.stat().st_mode & 0o777, 0o600)
        saved = load_secrets(self.secrets_path)
        self.assertEqual(saved.get("account_password"), "hunter2-live-secret")

        train_text = self.train_path.read_text()
        events_text = self.log_path.read_text()
        self.assertNotIn("hunter2-live-secret", train_text)
        self.assertNotIn("hunter2-live-secret", events_text)

    async def test_sensitive_action_blocked_and_notes_lifecycle(self):
        """Verify irreversible action ('Delete Production Account') is gated and blocked when denied."""
        cfg = Config(account_id="acc", api_token="tok", vision=False)
        tel = Telemetry(log_path=self.log_path)
        clef = MagicMock()
        clef.decide = AsyncMock(
            return_value={
                "answers": {
                    "target": {"confidence": 0.96, "choice": "2"},
                    "action": {"confidence": 0.96, "choice": "click"},
                    "done": {"noul": 0.01},
                    "sensitive": {"noul": 0.92},
                }
            }
        )
        llm = MagicMock()
        agent = Agent(cfg, tel, clef, llm, self.br)
        agent.train_log = self.train_path
        agent.add_note("One-shot guidance", remember=False)
        agent.add_note("Persistent rule", remember=True)
        agent.ask = AsyncMock(return_value={"ok": False, "value": None, "save": False})

        ok = await agent.run("Attempt destructive action", url=f"{self.server.base_url}/dashboard")
        self.assertFalse(ok)
        agent.ask.assert_awaited_once()
        # Verify one-shot note was dropped after decision while persistent note remained
        self.assertEqual(agent._notes_text(), "Persistent rule")
        # Verify page state was NOT mutated to DELETED
        snap = await self.br.snapshot()
        self.assertNotIn("DELETED", snap["text"])

    async def test_live_cloudflare_clef_on_real_dom_if_credentials_present(self):
        """Run a live Cloudflare Clef inference against the local web app if ~/.config/decision/env is configured."""
        cfg = Config.from_env()
        if not (cfg.account_id and cfg.api_token):
            self.skipTest("Cloudflare credentials not present in environment")
        tel = Telemetry(log_path=self.log_path)
        clef = ClefClient(cfg, tel)
        try:
            await self.br.goto(f"{self.server.base_url}/dashboard")
            snap = await self.br.snapshot()
            img_b64 = await self.br.screenshot_b64(quality=60)
            res = await clef.decide(
                _compact(snap),
                _questions("Click 'Generate Free API Key'", snap),
                images_b64=[img_b64],
            )
            self.assertIn("answers", res)
            conf, done, decision = _gate(res)
            self.assertIsInstance(conf, float)
            self.assertIn(decision["action"], ("click", "fill", "press", "scroll", "wait"))
            self.assertGreater(tel.stats.clef_calls, 0)
            self.assertGreater(tel.stats.last_latency_ms, 0.0)
        finally:
            await clef.http.aclose()


if __name__ == "__main__":
    unittest.main()
