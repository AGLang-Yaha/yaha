#!/usr/bin/env python3
"""Regression tests: section headers include the active stage name.

Expected format when UI knows the stage: `Agent (id) : Stage : Action`.
Stage-transition bars show `Agent (id) : Stage` (no duplicate).
"""

import io
import unittest
from unittest.mock import patch

from rich.console import Console

from yaha.ui.terminal import YAHAUI


def _ui():
    ui = YAHAUI(verbose=True)
    ui.console = Console(width=200, record=True, file=io.StringIO())
    return ui


class TuiStageHeaderTests(unittest.TestCase):
    def test_prompt_user_includes_current_stage(self):
        ui = _ui()
        ui.set_stage_name("Preamble")
        with patch("builtins.input", return_value="ok"):
            ui.prompt_user("CodeAgent", "abc123", "Question?")
        out = ui.console.export_text()
        self.assertIn("CodeAgent (abc123) : Preamble : Question", out)

    def test_header_without_stage_preserves_old_format(self):
        ui = _ui()
        with patch("builtins.input", return_value="ok"):
            ui.prompt_user("CodeAgent", "abc123", "Question?")
        out = ui.console.export_text()
        self.assertIn("CodeAgent (abc123) : Question", out)
        self.assertNotIn("Preamble", out)

    def test_stage_transition_header_is_not_duplicated(self):
        ui = _ui()
        ui.set_stage_name("Preamble")
        ui.display_stage_transition("CodeAgent", "abc123", "Preamble")
        out = ui.console.export_text()
        self.assertIn("CodeAgent (abc123) : Preamble", out)
        self.assertNotIn("Preamble : Preamble", out)

    def test_context_suffix_appears_after_stage_header(self):
        ui = _ui()
        ui.set_stage_name("Plan")
        ui.set_ctx_provider(lambda: "Ctx usage: 45.88% (234K/512K)")
        with patch("builtins.input", return_value="ok"):
            ui.prompt_user("CodeAgent", "abc123", "Question?")
        out = ui.console.export_text()
        self.assertIn("CodeAgent (abc123) : Plan : Question", out)
        self.assertIn("Ctx usage: 45.88% (234K/512K)", out)

    def test_tool_call_header_includes_stage(self):
        ui = _ui()
        ui.set_stage_name("Coding")
        ui.display_tool_call("CodeAgent", "abc123", "bash_tool", {}, )
        out = ui.console.export_text()
        self.assertIn("CodeAgent (abc123) : Coding : Tool Call (bash_tool)", out)


if __name__ == "__main__":
    unittest.main()
