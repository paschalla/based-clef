"""Unit tests for Phase 1 fixes in decision_agent."""
import unittest
from unittest.mock import MagicMock, patch

from decision_agent.cli import _ask_cli_sync
from decision_agent.gui import DecisionTk


class TestCliAsk(unittest.TestCase):
    @patch("builtins.input", return_value="y")
    def test_ask_approve_yes(self, mock_input):
        res = _ask_cli_sync("approve", "Allow this action?")
        self.assertEqual(res, {"ok": True, "value": None, "save": False})
        mock_input.assert_called_once_with("Allow this action? [y/N]: ")

    @patch("builtins.input", return_value="n")
    def test_ask_approve_no(self, mock_input):
        res = _ask_cli_sync("approve", "Allow this action?")
        self.assertEqual(res, {"ok": False, "value": None, "save": False})

    @patch("builtins.input", return_value="my-val")
    def test_ask_value_plain(self, mock_input):
        res = _ask_cli_sync("value", "Value needed for user", secret=False)
        self.assertEqual(res, {"ok": True, "value": "my-val", "save": True})
        mock_input.assert_called_once_with("Value needed for user: ")

    @patch("getpass.getpass", return_value="secret123")
    def test_ask_value_secret(self, mock_getpass):
        res = _ask_cli_sync("value", "Value needed for password", secret=True)
        self.assertEqual(res, {"ok": True, "value": "secret123", "save": True})
        mock_getpass.assert_called_once_with("Value needed for password: ")

    @patch("builtins.input", return_value="")
    def test_ask_value_empty(self, mock_input):
        res = _ask_cli_sync("value", "Value needed", secret=False)
        self.assertEqual(res, {"ok": False, "value": None, "save": False})

    @patch("builtins.input", return_value="click button 2")
    def test_ask_stuck_with_hint(self, mock_input):
        res = _ask_cli_sync("stuck", "Agent is stuck")
        self.assertEqual(res, {"ok": True, "value": "click button 2", "save": False})

    @patch("builtins.input", return_value="")
    def test_ask_stuck_empty(self, mock_input):
        res = _ask_cli_sync("stuck", "Agent is stuck")
        self.assertEqual(res, {"ok": False, "value": None, "save": False})

    @patch("builtins.input", side_effect=EOFError)
    def test_ask_eof(self, mock_input):
        res = _ask_cli_sync("approve", "Allow?")
        self.assertEqual(res, {"ok": False, "value": None, "save": False})


class TestGuiSavedNames(unittest.TestCase):
    @patch("decision_agent.gui._alert")
    def test_saved_names_alerts_once(self, mock_alert):
        from decision_agent.core import Telemetry

        # Instantiate DecisionTk without Tk window initialization
        app = DecisionTk.__new__(DecisionTk)
        app.saved_names = None
        app.saved_names_shown = None
        app.n_label = MagicMock()
        app.notice = MagicMock()
        app.stats = MagicMock()
        app.hint = MagicMock()
        app.text = MagicMock()
        app.text.index.return_value = "1.0"
        app.rt = MagicMock()
        app.rt.cfg.log_lines = 100
        app.rt.tel = Telemetry()
        app.pending = None
        app.shown = None
        app.queue = []
        app.root = MagicMock()

        # Initially no saved_names
        app.tick()
        mock_alert.assert_not_called()

        # New saved_names arrived
        app._on_saved(["password", "token"])
        app.tick()
        mock_alert.assert_called_once_with("secrets saved")
        app.notice.pack.assert_called_once()
        app.n_label.config.assert_called_with(text="saved secrets: password, token")

        # Second tick: should NOT alert again
        mock_alert.reset_mock()
        app.notice.pack.reset_mock()
        app.tick()
        mock_alert.assert_not_called()
        app.notice.pack.assert_not_called()

        # If another batch of saved secrets arrives, it should alert once more
        app._on_saved(["api_key"])
        app.tick()
        mock_alert.assert_called_once_with("secrets saved")


if __name__ == "__main__":
    unittest.main()
