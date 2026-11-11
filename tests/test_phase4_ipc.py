"""Tests for Phase 4 IPC Unix socket daemon."""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from decision_agent.ipc import IPCServer


class TestIPCServer(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.sock_path = Path(self.tmp_dir.name) / "test.sock"
        self.mock_rt = MagicMock()
        self.mock_rt.cfg.driver = "browser"
        self.mock_rt.tel.stats.step = 3
        self.mock_rt.tel.stats.elapsed.return_value = 4.2
        self.mock_rt.tel.stats.last_latency_ms = 195.0
        self.mock_rt.tel.stats.state = "done"
        self.mock_rt.run_task = AsyncMock(return_value=True)

        self.server = IPCServer(self.mock_rt, socket_path=self.sock_path)
        await self.server.start()

    async def asyncTearDown(self):
        await self.server.stop()
        self.tmp_dir.cleanup()

    async def _send_req(self, payload: dict) -> dict:
        reader, writer = await asyncio.open_unix_connection(str(self.sock_path))
        writer.write((json.dumps(payload) + "\n").encode())
        await writer.drain()
        line = await reader.readline()
        writer.close()
        await writer.wait_closed()
        return json.loads(line.decode().strip())

    async def test_ipc_run_command(self):
        res = await self._send_req({"cmd": "run", "task": "Search Turing", "url": "https://wiki.org"})
        self.assertTrue(res["ok"])
        self.assertEqual(res["step"], 3)
        self.mock_rt.run_task.assert_called_once_with("Search Turing", {}, "https://wiki.org")

    async def test_ipc_hold_command(self):
        res = await self._send_req({"cmd": "hold", "on": True})
        self.assertTrue(res["ok"])
        self.assertTrue(res["held"])
        self.mock_rt.agent.hold.assert_called_with(True)

    async def test_ipc_hint_command(self):
        res = await self._send_req({"cmd": "hint", "hint": "select second item", "remember": False})
        self.assertTrue(res["ok"])
        self.assertEqual(res["hint"], "select second item")
        self.mock_rt.agent.add_note.assert_called_with("select second item", remember=False)

    async def test_ipc_status_command(self):
        res = await self._send_req({"cmd": "status"})
        self.assertTrue(res["ok"])
        self.assertEqual(res["state"], "done")

    async def test_ipc_driver_switch_calls_set_driver(self):
        self.mock_rt.set_driver = AsyncMock()
        res = await self._send_req({"cmd": "run", "task": "Desktop click", "driver": "desktop"})
        self.assertTrue(res["ok"])
        self.mock_rt.set_driver.assert_awaited_once_with("desktop")


class TestMCPAndAuditFixes(unittest.IsolatedAsyncioTestCase):
    async def test_mcp_tools_and_driver_switch(self):
        from unittest.mock import patch
        from decision_agent import mcp_server

        mock_rt = MagicMock()
        mock_rt.set_driver = AsyncMock()
        mock_rt.run_task = AsyncMock(return_value=True)
        mock_rt.tel.stats.step = 2
        mock_rt.tel.stats.elapsed.return_value = 1.5
        mock_rt.tel.stats.last_latency_ms = 120.0

        with patch("decision_agent.mcp_server.get_runtime", return_value=mock_rt):
            res_b = await mcp_server.browser_task("Open page", url="https://example.com")
            self.assertIn("SUCCESS", res_b)
            mock_rt.set_driver.assert_awaited_with("browser")

            res_d = await mcp_server.desktop_task("Click button")
            self.assertIn("SUCCESS", res_d)
            mock_rt.set_driver.assert_awaited_with("desktop")

            self.assertIn("True", mcp_server.hold_agent(True))
            mock_rt.agent.hold.assert_called_once_with(True)

            self.assertIn("hint1", mcp_server.send_agent_hint("hint1", remember=False))
            mock_rt.agent.add_note.assert_called_once_with("hint1", remember=False)

    async def test_runtime_set_driver_closes_previous(self):
        from decision_agent.core import Config
        from decision_agent.runtime import build

        cfg = Config(account_id="0123456789abcdef0123456789abcdef", api_token="tok", driver="browser")
        rt = build(cfg)
        old_drv = rt.driver
        old_drv.close = AsyncMock()
        await rt.set_driver("desktop")
        old_drv.close.assert_awaited_once()
        self.assertEqual(rt.cfg.driver, "desktop")
        self.assertIs(rt.agent.br, rt.driver)
        await rt.shutdown()

    async def test_cli_kv_validation_and_ask_single_option(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from decision_agent.cli import _ask, _kv

        with self.assertRaises(SystemExit):
            _kv(["missing_equals"])

        ns = SimpleNamespace(question="Pick?", options=["only_one"], state="some state")
        mock_clef = MagicMock()
        mock_clef.decide = AsyncMock(return_value={"answers": {"q": {"choice": "only_one", "confidence": 0.9}}})
        mock_clef.http.aclose = AsyncMock()
        with (
            patch("decision_agent.core.Config.require"),
            patch("decision_agent.core.ClefClient", return_value=mock_clef),
        ):
            rc = await _ask(ns)
            self.assertEqual(rc, 0)
            q_sent = mock_clef.decide.call_args[0][1]["q"]
            self.assertGreaterEqual(len(q_sent["criteria"]), 2)


if __name__ == "__main__":
    unittest.main()
