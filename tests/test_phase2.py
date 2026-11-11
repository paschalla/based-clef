"""Unit tests for Phase 2: Vision & Real-World Browser Support."""
import asyncio
import json
import os
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from decision_agent.browser import BrowserDriver
from decision_agent.cli import main
from decision_agent.core import ClefClient, Config, Telemetry
from decision_agent.agent import Agent, _compact


class TestPhase2Config(unittest.TestCase):
    def test_config_defaults(self):
        cfg = Config()
        self.assertTrue(cfg.vision)
        self.assertEqual(cfg.max_images, 1)
        self.assertEqual(cfg.image_quality, 60)
        self.assertFalse(cfg.highlight)
        self.assertEqual(cfg.highlight_delay, 0.3)

    @patch.dict(os.environ, {
        "DECISION_VISION": "false",
        "DECISION_MAX_IMAGES": "3",
        "DECISION_IMAGE_QUALITY": "80",
        "DECISION_HIGHLIGHT": "true",
        "DECISION_HIGHLIGHT_DELAY": "0.5",
    })
    def test_config_from_env(self):
        cfg = Config.from_env()
        self.assertFalse(cfg.vision)
        self.assertEqual(cfg.max_images, 3)
        self.assertEqual(cfg.image_quality, 80)
        self.assertTrue(cfg.highlight)
        self.assertEqual(cfg.highlight_delay, 0.5)


class TestPhase2ClefClient(unittest.IsolatedAsyncioTestCase):
    async def test_decide_image_formatting_and_capping(self):
        cfg = Config(account_id="acc", api_token="tok", max_images=2)
        tel = Telemetry()
        mock_http = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"answers": {"q": {"confidence": 0.9}}}
        mock_http.post = AsyncMock(return_value=mock_resp)

        clef = ClefClient(cfg, tel, mock_http)

        # 3 raw base64 images; max_images is 2, so should cap at 2 and prefix with data:image/jpeg;base64,
        images = ["raw1", "data:image/png;base64,raw2", "raw3"]
        await clef.decide({"page": 1}, {"q": {}}, images_b64=images)

        call_args = mock_http.post.call_args
        body = call_args.kwargs["json"]
        self.assertIn("images", body)
        self.assertEqual(len(body["images"]), 2)
        self.assertEqual(body["images"][0], "data:image/jpeg;base64,raw1")
        self.assertEqual(body["images"][1], "data:image/png;base64,raw2")

    async def test_decide_max_images_cap_four(self):
        cfg = Config(account_id="acc", api_token="tok", max_images=10)
        tel = Telemetry()
        mock_http = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"answers": {}}
        mock_http.post = AsyncMock(return_value=mock_resp)

        clef = ClefClient(cfg, tel, mock_http)
        images = [f"img{i}" for i in range(6)]
        await clef.decide({"page": 1}, {}, images_b64=images)

        call_args = mock_http.post.call_args
        body = call_args.kwargs["json"]
        self.assertEqual(len(body["images"]), 4)


class TestPhase2BrowserDriver(unittest.IsolatedAsyncioTestCase):
    async def test_click_fallback_to_force(self):
        br = BrowserDriver(headless=True)
        br.page = MagicMock()
        loc = MagicMock()
        # Normal click raises, force click succeeds
        loc.click = AsyncMock(side_effect=[Exception("Element is obscured"), None])
        br.page.locator.return_value = loc

        await br.click(5)
        self.assertEqual(loc.click.call_count, 2)
        loc.click.assert_any_call(timeout=5000)
        loc.click.assert_any_call(timeout=5000, force=True)

    async def test_snapshot_retry_on_navigation_error(self):
        br = BrowserDriver(headless=True)
        br.page = MagicMock()
        br.page.evaluate = AsyncMock(side_effect=[
            Exception("Execution context was destroyed, most likely because of a navigation."),
            {"elements": [], "text": "", "url": "http://ok", "title": "OK", "scroll": [0, 0]}
        ])
        br.settle = AsyncMock()

        res = await br.snapshot()
        self.assertEqual(res["url"], "http://ok")
        self.assertEqual(br.page.evaluate.call_count, 2)
        br.settle.assert_awaited_once()

    async def test_screenshot_b64_clean(self):
        br = BrowserDriver(headless=True)
        br.page = MagicMock()
        br.page.screenshot = AsyncMock(return_value=b"fake-jpeg-bytes")

        b64 = await br.screenshot_b64(quality=75)
        br.page.screenshot.assert_awaited_once_with(type="jpeg", quality=75)
        self.assertIsInstance(b64, str)
        self.assertNotIn("\n", b64)

    async def test_highlight_skips_when_headless(self):
        br = BrowserDriver(headless=True, highlight=True, highlight_delay=0.3)
        br.page = MagicMock()
        loc = MagicMock()
        loc.evaluate = AsyncMock()
        br.page.locator.return_value = loc

        await br.highlight_element(1)
        loc.evaluate.assert_not_called()


class TestPhase2Agent(unittest.IsolatedAsyncioTestCase):
    async def test_step_captures_screenshot_when_vision_enabled(self):
        cfg = Config(vision=True, image_quality=50)
        tel = Telemetry()
        clef = MagicMock()
        clef.decide = AsyncMock(return_value={"answers": {"target": {"confidence": 0.9, "choice": "0"},
                                                          "action": {"confidence": 0.9, "choice": "wait"},
                                                          "done": {"noul": 1.0}}})
        llm = MagicMock()
        br = MagicMock()
        br.snapshot = AsyncMock(return_value={"elements": [], "text": "", "url": "http://ex", "title": "T", "scroll": [0, 0]})
        br.screenshot_b64 = AsyncMock(return_value="sample_b64_frame")

        agent = Agent(cfg, tel, clef, llm, br)
        result = await agent._step("test task", 1)
        self.assertEqual(result, "done")
        br.screenshot_b64.assert_awaited_once_with(50)
        clef.decide.assert_awaited_once()
        self.assertEqual(clef.decide.call_args.kwargs["images_b64"], ["sample_b64_frame"])

    async def test_decide_reuses_frame_across_attempts(self):
        cfg = Config(threshold=0.8, max_attempts=2)
        tel = Telemetry()
        clef = MagicMock()
        # Attempt 1 low confidence, attempt 2 high confidence
        clef.decide = AsyncMock(side_effect=[
            {"answers": {"target": {"confidence": 0.5, "choice": "1"}, "action": {"confidence": 0.5, "choice": "click"}, "done": {"noul": 0.0}}},
            {"answers": {"target": {"confidence": 0.9, "choice": "1"}, "action": {"confidence": 0.9, "choice": "click"}, "done": {"noul": 0.0}}},
        ])
        llm = MagicMock()
        llm.complete = AsyncMock(return_value="hint")
        br = MagicMock()
        snap = {"elements": [{"id": 1, "role": "button", "name": "B", "value": "", "empty": False}], "text": "", "url": "http://ex", "title": "T", "scroll": [0, 0]}

        agent = Agent(cfg, tel, clef, llm, br)
        d, via = await agent._decide("task", snap, img_b64="frame123")
        self.assertEqual(via, "clef#2")
        self.assertEqual(clef.decide.call_count, 2)
        self.assertEqual(clef.decide.call_args_list[0].kwargs["images_b64"], ["frame123"])
        self.assertEqual(clef.decide.call_args_list[1].kwargs["images_b64"], ["frame123"])

    def test_log_example_strips_images(self, tmp_path=None):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            log_path = Path(td) / "training.jsonl"
            cfg = Config()
            tel = Telemetry()
            clef = MagicMock()
            llm = MagicMock()
            br = MagicMock()
            agent = Agent(cfg, tel, clef, llm, br)
            agent.train_log = log_path

            snap = {
                "elements": [],
                "text": "test",
                "url": "http://test",
                "title": "T",
                "scroll": [0, 0],
                "images": ["raw_base64_data"],
            }
            agent._log_example("task", snap, {"action": "wait"}, "clef", True)

            self.assertTrue(log_path.exists())
            line = json.loads(log_path.read_text().strip())
            self.assertNotIn("images", line["state"])
            self.assertNotIn("image", line["state"])


class TestPhase2JsonAndText(unittest.TestCase):
    def test_json_from_handles_markdown_and_malformed_done(self):
        from decision_agent.agent import _json_from
        text = '''Here is the step:
```json
{"done":, "action": "fill", "target": 1}
```'''
        res = _json_from(text)
        self.assertEqual(res, {"done": False, "action": "fill", "target": 1})

    def test_json_from_plain_json(self):
        from decision_agent.agent import _json_from
        text = '{"done": true, "action": "wait", "target": null}'
        res = _json_from(text)
        self.assertEqual(res, {"done": True, "action": "wait", "target": None})


class TestPhase2Cli(unittest.TestCase):
    @patch("decision_agent.runtime.build")
    @patch("decision_agent.cli.asyncio.run")
    def test_cli_flags_passed_to_config(self, mock_run, mock_build):
        mock_run.side_effect = lambda coro: (coro.close(), True)[1]
        mock_rt = MagicMock()
        mock_build.return_value = mock_rt
        with patch.object(Config, "require"):
            # Test --vision, --highlight, --highlight-delay
            main(["run", "some task", "--vision", "--highlight", "--highlight-delay", "0.45", "--headless"])
            cfg = mock_build.call_args[0][0]
            self.assertTrue(cfg.vision)
            self.assertTrue(cfg.highlight)
            self.assertEqual(cfg.highlight_delay, 0.45)
            self.assertTrue(cfg.headless)

            # Test --no-vision, --no-highlight
            main(["run", "some task", "--no-vision", "--no-highlight", "--headless"])
            cfg2 = mock_build.call_args[0][0]
            self.assertFalse(cfg2.vision)
            self.assertFalse(cfg2.highlight)


if __name__ == "__main__":
    unittest.main()
