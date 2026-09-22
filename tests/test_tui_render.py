# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

import unittest
from yaha.ui.terminal import YAHAUI, console

class TestTUILongLines(unittest.TestCase):
    def setUp(self):
        self.ui = YAHAUI(verbose=True)

    def test_clip_line_cjk_display_width(self):
        # CJK chars consume 2 terminal cells; clipping must respect that
        line = "ab" + "汉" * 100  # width = 2 + 200 = 202 cells
        # Use a limit wide enough to also hold the truncation marker (a 10-cell
        # budget cannot hold `…[+N chars]`; see the tiny-limit test below).
        clipped = self.ui._clip_line(line, 40)
        self.assertTrue(clipped.endswith(" chars]"))
        # The WHOLE returned line - marker included - must fit the limit.
        self.assertLessEqual(self.ui._line_width(clipped), 40)
        # Visible part before the marker must fit within the limit too
        visible = clipped.split("…")[0]
        self.assertLessEqual(self.ui._line_width(visible), 40)
        # Short lines pass through untouched
        self.assertEqual(self.ui._clip_line("short", 80), "short")

    # --- regression: the truncation marker is budgeted INSIDE the limit ----
    # Previously _clip_line clipped the content to `limit` and THEN appended
    # "…[+N chars]", so the returned line was up to `limit + marker_width`
    # cells wide; a repaint could therefore emit a line wider than the display
    # (measured: _clip_line("x"*5000, 76) -> 90 cells; _fit_text -> 196 cells).

    def test_clip_line_full_width_never_exceeds_limit(self):
        line = "x" * 5000
        for limit in (76, 40, 30, 20, 12, 10):
            clipped = self.ui._clip_line(line, limit)
            self.assertLessEqual(self.ui._line_width(clipped), limit)

    def test_clip_line_cjk_full_width_never_exceeds_limit(self):
        line = "ab" + "汉" * 100  # 202 cells
        for limit in (60, 40, 30, 20, 12, 10):
            clipped = self.ui._clip_line(line, limit)
            self.assertLessEqual(self.ui._line_width(clipped), limit)

    def test_clip_line_marks_truncation_when_it_fits(self):
        clipped = self.ui._clip_line("x" * 5000, 40)
        self.assertTrue(clipped.endswith(" chars]"))
        self.assertLessEqual(self.ui._line_width(clipped), 40)

    def test_clip_line_tiny_limit_degrades_without_marker(self):
        # No room for the marker: degrade to a marker-free truncation, but
        # still never exceed the budget.
        for limit in (1, 2, 3, 5):
            clipped = self.ui._clip_line("y" * 500, limit)
            self.assertLessEqual(self.ui._line_width(clipped), limit)

    def test_clip_line_passthrough_and_degenerate(self):
        self.assertEqual(self.ui._clip_line("short", 80), "short")
        self.assertEqual(self.ui._clip_line("", 80), "")
        self.assertEqual(self.ui._clip_line("abc", 0), "")

    def test_fit_text_emitted_lines_fit_console(self):
        width = self.ui._console_width()
        for payload in ("x" * 5000, "汉" * 5000, ("w" * 300 + "\n") * 3):
            out = self.ui._fit_text(payload)
            for ln in out.splitlines():
                self.assertLessEqual(self.ui._line_width(ln), width)

    def test_fit_text_narrow_console_has_no_floor_overflow(self):
        # The old `max(width - 4, 40)` floor ignored terminals narrower than
        # 40 cells and let emitted lines exceed the display width.
        real = self.ui._console_width
        self.ui._console_width = lambda: 30
        try:
            out = self.ui._fit_text("x" * 5000)
            for ln in out.splitlines():
                self.assertLessEqual(self.ui._line_width(ln), 30)
            # Line-capped payload: the "more lines truncated" marker must also
            # stay within budget.
            out2 = self.ui._fit_text(("w" * 300 + "\n") * 50, max_lines=10)
            for ln in out2.splitlines():
                self.assertLessEqual(self.ui._line_width(ln), 30)
        finally:
            self.ui._console_width = real

    def test_display_tool_call_concise_line_fits(self):
        ui = YAHAUI(verbose=False)
        with console.capture() as cap:
            ui.display_tool_call(
                "BaseAgent", "testid01", "bash_tool", {"cmdline": "z" * 5000}
            )
        out = cap.get()
        width = ui._console_width()
        for ln in out.splitlines():
            self.assertLessEqual(ui._line_width(ln), width)

    def test_fit_text_line_cap(self):
        text = "\n".join(f"line {i}" for i in range(10))
        out = self.ui._fit_text(text, max_lines=3)
        out_lines = out.splitlines()
        self.assertEqual(len(out_lines), 4)  # 3 kept + truncation marker
        self.assertIn("[7 more lines truncated]", out_lines[-1])

    def test_fit_text_head_and_tail(self):
        text = "\n".join(f"line {i}" for i in range(50))
        out = self.ui._fit_text(text, max_lines=20, tail_lines=5)
        out_lines = out.splitlines()
        self.assertEqual(len(out_lines), 20)  # 14 head + marker + 5 tail
        self.assertEqual(out_lines[0], "line 0")
        self.assertEqual(out_lines[-1], "line 49")  # stack-trace final line kept
        self.assertIn("more lines truncated", out_lines[14])

    def test_fit_text_clips_long_lines(self):
        text = "x" * 5000
        out = self.ui._fit_text(text)
        self.assertIn(" chars]", out)
        self.assertLess(len(out), 5000)

    def test_display_tool_result_handles_huge_output(self):
        huge = "row\n" * 500 + "z" * 10000
        with console.capture() as cap:
            self.ui.display_tool_result("BaseAgent", "testid01", "bash_tool", huge)
        out = cap.get()
        self.assertIn("more lines truncated", out)
        self.assertLess(len(out), len(huge))

class TestCtxUsageBar(unittest.TestCase):
    def setUp(self):
        self.ui = YAHAUI(verbose=True)

    def _rule_title(self):
        with console.capture() as cap:
            self.ui.display_progress("BaseAgent", "testid01", "hello")
        return cap.get()

    def test_ctx_display_text_format(self):
        # Emulate the Agent-side provider without constructing a full Agent
        class FakeAgentish:
            context_size = 128000
            _last_prompt_tokens = 57600
            _usage_is_predicted = False
            history = []
            def _context_usage_ratio(self):
                return self._last_prompt_tokens / self.context_size
            def _usage_is_estimated(self):
                return False

        from yaha.core.agent import Agent
        text = Agent._ctx_display_text(FakeAgentish())
        self.assertEqual(text, "Ctx usage: 45% (57K/128K)")

    def test_section_rule_suffix_lifecycle(self):
        # No provider -> no suffix
        out = self._rule_title()
        self.assertNotIn("Ctx usage", out)

        # Provider registered -> suffix appears
        self.ui.set_ctx_provider(lambda: "Ctx usage: 45% (57K/128K)")
        out = self._rule_title()
        self.assertIn("Ctx usage: 45% (57K/128K)", out)

        # Disabled again -> suffix gone
        self.ui.set_ctx_provider(None)
        out = self._rule_title()
        self.assertNotIn("Ctx usage", out)

    def test_section_rule_swallows_provider_errors(self):
        def boom():
            raise RuntimeError("provider failed")
        self.ui.set_ctx_provider(boom)
        out = self._rule_title()
        self.assertNotIn("Ctx usage", out)  # silent fallback, no crash
        self.assertIn("Progress", out)

if __name__ == "__main__":
    unittest.main()
