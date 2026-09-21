import unittest
from yaha.ui.terminal import YAHAUI, console

class TestTUILongLines(unittest.TestCase):
    def setUp(self):
        self.ui = YAHAUI(verbose=True)

    def test_clip_line_cjk_display_width(self):
        # CJK chars consume 2 terminal cells; clipping must respect that
        line = "ab" + "汉" * 100  # width = 2 + 200 = 202 cells
        clipped = self.ui._clip_line(line, 10)
        self.assertTrue(clipped.endswith(" chars]"))
        # Visible part before the marker must fit within the limit
        visible = clipped.split("…")[0]
        self.assertLessEqual(self.ui._line_width(visible), 10)
        # Short lines pass through untouched
        self.assertEqual(self.ui._clip_line("short", 80), "short")

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
