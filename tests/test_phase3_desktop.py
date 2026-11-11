"""Tests for Phase 3 DesktopDriver integration and CLI driver flag."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from decision_agent.core import Config
from decision_agent.desktop import DesktopDriver
from decision_agent.runtime import build


class TestDesktopDriver(unittest.IsolatedAsyncioTestCase):
    def test_config_driver_default_and_env(self):
        cfg = Config()
        self.assertEqual(cfg.driver, "browser")
        with patch.dict("os.environ", {"DECISION_DRIVER": "desktop"}):
            loaded = Config.from_env()
            self.assertEqual(loaded.driver, "desktop")

    @patch("decision_agent.desktop.DesktopDriver._grab_image")
    async def test_desktop_screenshot_b64(self, mock_grab):
        from PIL import Image
        mock_img = Image.new("RGB", (800, 600), color=(255, 0, 128))
        mock_grab.return_value = mock_img

        drv = DesktopDriver()
        b64 = await drv.screenshot_b64(quality=50)
        self.assertTrue(isinstance(b64, str))
        self.assertTrue(len(b64) > 100)

    @patch("decision_agent.desktop.DesktopDriver._collect_atspi_elements")
    @patch("decision_agent.desktop.DesktopDriver._grab_image")
    @patch("decision_agent.desktop._get_active_window_info")
    async def test_desktop_snapshot_atspi(self, mock_win, mock_grab, mock_atspi):
        from PIL import Image
        mock_win.return_value = {"id": "12345", "title": "Test App"}
        mock_grab.return_value = Image.new("RGB", (800, 600))
        mock_atspi.return_value = [
            {"role": "push button", "name": "Submit", "type": "push button", "bbox": (10, 20, 100, 30), "center": (60, 35)},
            {"role": "entry", "name": "Username", "type": "entry", "bbox": (10, 60, 200, 30), "center": (110, 75)},
        ]

        drv = DesktopDriver()
        snap = await drv.snapshot()
        self.assertEqual(len(snap["elements"]), 2)
        self.assertEqual(snap["elements"][0]["id"], 1)
        self.assertEqual(snap["elements"][0]["name"], "Submit")
        self.assertEqual(snap["elements"][1]["name"], "Username")
        self.assertIn("Test App", snap["text"])

    @patch("subprocess.run")
    async def test_desktop_actions(self, mock_sub):
        drv = DesktopDriver(highlight=False)
        drv._elements_by_id[1] = {"center": (200, 300)}

        await drv.click(1)
        mock_sub.assert_called_with(["xdotool", "mousemove", "200", "300", "click", "1"], check=False)

        await drv.press("Return")
        mock_sub.assert_called_with(["xdotool", "key", "--clearmodifiers", "Return"], check=False)

    @patch("subprocess.run")
    async def test_self_targeting_guard_blocks_clicks_outside_isolated_window(self, mock_sub):
        drv = DesktopDriver(highlight=False)
        drv.window_rect = (100, 100, 800, 600)  # Isolated window rect: x: 100..900, y: 100..700
        drv._elements_by_id[1] = {"center": (300, 400)}  # Inside target window
        drv._elements_by_id[2] = {"center": (50, 50)}    # Outside target window (e.g. terminal / HUD)

        # 1. Click inside should proceed
        await drv.click(1)
        mock_sub.assert_called_with(["xdotool", "mousemove", "300", "400", "click", "1"], check=False)

        # Reset mock
        mock_sub.reset_mock()

        # 2. Click outside should be strictly blocked by the isolation guard
        await drv.click(2)
        mock_sub.assert_not_called()

        # 3. Fill outside should also be strictly blocked
        await drv.fill(2, "secret_password")
        mock_sub.assert_not_called()

    @patch("decision_agent.desktop.DesktopDriver._grab_image")
    async def test_screenshot_b64_crops_to_isolated_window(self, mock_grab):
        from PIL import Image
        full_img = Image.new("RGB", (1920, 1200), color=(10, 20, 30))
        mock_grab.return_value = full_img

        drv = DesktopDriver(highlight=False)
        drv.window_rect = (200, 150, 800, 600)

        b64 = await drv.screenshot_b64(quality=70)
        self.assertTrue(isinstance(b64, str))
        self.assertTrue(len(b64) > 50)

    def test_runtime_build_driver_selection(self):
        cfg_desk = Config(account_id="0123456789abcdef0123456789abcdef", api_token="tok", driver="desktop")
        rt = build(cfg_desk)
        self.assertIsInstance(rt.driver, DesktopDriver)

        cfg_brow = Config(account_id="0123456789abcdef0123456789abcdef", api_token="tok", driver="browser")
        rt_b = build(cfg_brow)
        from decision_agent.browser import BrowserDriver
        self.assertIsInstance(rt_b.driver, BrowserDriver)


if __name__ == "__main__":
    unittest.main()
