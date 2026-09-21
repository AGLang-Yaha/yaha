#!/usr/bin/env python3
"""Tests for the shared tool-output safety guard.

`read_file` and `bash_tool` can each return an arbitrarily large payload, which
can eat the whole remaining context window in one call. `yaha/tools/output_guard.py`
caps ONE tool result at half of the window that is still free at call time and,
when it truncates, tells the model the bytes it actually received, the bytes it
lost, and the limit that was applied.

The tests cover four things:
  * the pure budget arithmetic (unit factor, 1/2 fraction, floor);
  * the pure guard (byte-exact passthrough, boundary, counts, UTF-8 safety);
  * each tool actually applying it (and staying legacy when no budget is given);
  * the agent dispatch injecting a call-time budget for BOTH tools.

Built like tests/test_error_log.py: a real Agent over a tmp YAHAEngine workspace
where the real wiring is under test, and direct calls for the pure paths.
"""
import io
import re
import shutil
import subprocess  # noqa: F401  (kept for parity with sibling suites)
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rich.console import Console

from yaha.core.agent import Agent
from yaha.core.engine import YAHAEngine
from yaha.tools import output_guard
from yaha.tools import file_tools, system_tools
from yaha.tools.output_guard import (
    MIN_OUTPUT_BUDGET_BYTES,
    TRUNCATION_MARKER_PREFIX,
    compute_output_budget,
    guard_output,
)


_AGENT_FIXTURE = '''
## OutputGuardNoTool
- **Inherited:** None
- **Code:** OGN
- **Toolset:**
> preamble
'''

_STAGE = {
    'name': 'Preamble',
    'allowed_tools': [],
    'exit_tools': [],
    'required_tools': [],
}

# The footer is the machine-readable half of the contract: it must name the
# bytes actually read, the original size, the bytes truncated and the limit.
_FOOTER_RE = re.compile(
    r'read (\d+) bytes of (\d+) bytes; truncated (\d+) bytes '
    r'\(limit = 50% of the remaining context window = (\d+) bytes\)'
)

_FOOTER_SEPARATOR = '\n\n'


def _footer_counts(text):
    """(shown, original, dropped, limit) parsed back out of the marker."""
    match = _FOOTER_RE.search(text)
    assert match, 'no truncation footer in: %r' % text[-400:]
    return tuple(int(g) for g in match.groups())


def _kept_payload(text):
    """The content part of a guarded result (footer and its separator removed)."""
    idx = text.index(TRUNCATION_MARKER_PREFIX)
    head = text[:idx]
    assert head.endswith(_FOOTER_SEPARATOR), repr(head[-8:])
    return head[: -len(_FOOTER_SEPARATOR)]


def _make_agent():
    """Build a no-tool Agent over a tmp workspace (no TTY, no API)."""
    ws = Path(tempfile.mkdtemp(prefix='yaha_outguard_'))
    (ws / '.yaha').mkdir(parents=True)
    for sub in ('memory', 'ext', 'logs'):
        (ws / '.yaha' / sub).mkdir()
    (ws / '.yaha' / 'tool-schema.md').write_text('[]')
    (ws / '.yaha' / 'AgentClass.md').write_text(_AGENT_FIXTURE)
    (ws / '.yaha' / 'Model.md').write_text('')
    eng = YAHAEngine(workspace_dir=str(ws))
    cfg = eng.agent_classes['OutputGuardNoTool']
    agent = Agent(
        agent_config=cfg,
        session=eng.session,
        models_config=eng.models_config,
        agent_classes=eng.agent_classes,
        tool_schemas=eng.tool_schemas,
        access_controller=eng.access_controller,
        unattended=True,
    )
    agent.ui.console = Console(width=100, file=io.StringIO())
    return agent, ws


class BudgetTests(unittest.TestCase):
    """The cap is half of the remaining window, in bytes, with a floor."""

    def test_budget_is_half_of_the_remaining_window(self):
        # 128000 - 28000 = 100000 tokens left; x4 bytes/token = 400000; /2.
        self.assertEqual(compute_output_budget(128000, 28000), 200000)

    def test_budget_of_a_full_window(self):
        self.assertEqual(compute_output_budget(128000, 0), 256000)

    def test_exhausted_window_falls_back_to_the_floor(self):
        self.assertEqual(compute_output_budget(128000, 128000),
                         MIN_OUTPUT_BUDGET_BYTES)
        # defensive: an over-full window (bad accounting upstream) also floors
        self.assertEqual(compute_output_budget(128000, 200000),
                         MIN_OUTPUT_BUDGET_BYTES)

    def test_unknown_window_falls_back_to_the_floor_not_to_unbounded(self):
        self.assertEqual(compute_output_budget(0, 0), MIN_OUTPUT_BUDGET_BYTES)
        self.assertEqual(compute_output_budget(None, 1234),
                         MIN_OUTPUT_BUDGET_BYTES)

    def test_budget_is_monotonically_non_increasing_in_used_tokens(self):
        budgets = [compute_output_budget(128000, used)
                   for used in range(0, 140000, 5000)]
        for earlier, later in zip(budgets, budgets[1:]):
            self.assertGreaterEqual(earlier, later, budgets)
        self.assertGreaterEqual(budgets[0], budgets[-1])

    def test_budget_never_exceeds_half_of_the_window(self):
        for used in (0, 1, 1000, 64000, 127999):
            budget = compute_output_budget(128000, used)
            raw_half = (128000 - used) * output_guard.CHARS_PER_TOKEN // 2
            if raw_half >= MIN_OUTPUT_BUDGET_BYTES:
                self.assertLessEqual(budget, raw_half, used)
            else:
                # Documented carve-out: when half of the window is smaller than
                # the floor, the floor wins (this is the near-exhausted case).
                self.assertEqual(budget, MIN_OUTPUT_BUDGET_BYTES, used)


class GuardOutputTests(unittest.TestCase):
    """The pure guard: exact passthrough, honest marker, UTF-8 safety."""

    def test_small_output_is_returned_untouched(self):
        text = 'LINES READ: 2\n\nhello\nworld\n'
        self.assertIs(guard_output(text, 4096), text)
        self.assertNotIn(TRUNCATION_MARKER_PREFIX, guard_output(text, 4096))

    def test_output_exactly_at_the_budget_is_untouched(self):
        text = 'x' * 1000
        self.assertIs(guard_output(text, 1000), text)

    def test_output_one_byte_over_the_budget_is_truncated(self):
        text = 'x' * 1001
        result = guard_output(text, 1000)
        self.assertIn(TRUNCATION_MARKER_PREFIX, result)
        self.assertLessEqual(len(result.encode('utf-8')), 1000)

    def test_guard_is_disabled_when_no_budget_is_given(self):
        text = 'x' * 50000
        self.assertIs(guard_output(text, None), text)

    def test_empty_text_is_untouched(self):
        self.assertEqual(guard_output('', 8), '')

    def test_marker_reports_actual_bytes_read_and_bytes_truncated(self):
        text = 'y' * 20000
        budget = 4096
        result = guard_output(text, budget)
        shown, original, dropped, limit = _footer_counts(result)
        self.assertEqual(original, len(text.encode('utf-8')))
        self.assertEqual(limit, budget)
        self.assertEqual(shown + dropped, original)
        self.assertEqual(shown, len(_kept_payload(result).encode('utf-8')))
        self.assertTrue(text.startswith(_kept_payload(result)))
        self.assertLessEqual(len(result.encode('utf-8')), budget)

    def test_counts_are_right_for_a_multibyte_payload(self):
        text = 'ab\u6587cd' * 4000           # 3 bytes for the CJK char
        result = guard_output(text, 500)
        shown, original, dropped, limit = _footer_counts(result)
        self.assertEqual(original, len(text.encode('utf-8')))
        self.assertEqual(shown + dropped, original)
        self.assertEqual(shown, len(_kept_payload(result).encode('utf-8')))

    def test_multibyte_payload_is_never_cut_mid_character(self):
        text = '\u6587' * 5000                # 3 bytes per character
        result = guard_output(text, 1000)
        kept = _kept_payload(result)
        self.assertTrue(text.startswith(kept))
        # a partial sequence would blow up here instead of round-tripping
        kept.encode('utf-8').decode('utf-8')
        self.assertEqual(len(kept.encode('utf-8')) % 3, 0)
        result.encode('utf-8').decode('utf-8')

    def test_cut_prefers_a_line_boundary(self):
        text = ''.join('line %04d\n' % i for i in range(2000))
        result = guard_output(text, 1000)
        kept = _kept_payload(result)
        self.assertTrue(text.startswith(kept))
        self.assertTrue(kept.endswith('\n'), repr(kept[-20:]))
        self.assertLessEqual(len(result.encode('utf-8')), 1000)

    def test_budget_smaller_than_the_marker_keeps_the_marker_visible(self):
        text = 'z' * 5000
        result = guard_output(text, 32)
        self.assertIn(TRUNCATION_MARKER_PREFIX, result)
        self.assertEqual(_kept_payload(result), '')


class ReadFileGuardTests(unittest.TestCase):
    """read_file keeps its line-range contract and gains the cap."""

    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix='yaha_outguard_read_'))
        self.big = self.ws / 'big.txt'
        self.big.write_text(''.join('row %05d\n' % i for i in range(3000)),
                            encoding='utf-8')
        self.small = self.ws / 'small.txt'
        self.small.write_text('alpha\nbeta\n', encoding='utf-8')

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def test_small_read_has_no_marker_and_keeps_the_header(self):
        result = file_tools.read_file(str(self.small), 1, 2,
                                      max_output_bytes=8192)
        self.assertEqual(result, 'LINES READ: 2\n\nalpha\nbeta\n')
        self.assertNotIn(TRUNCATION_MARKER_PREFIX, result)

    def test_legacy_behaviour_without_a_budget(self):
        result = file_tools.read_file(str(self.big), 1, 3000)
        self.assertNotIn(TRUNCATION_MARKER_PREFIX, result)
        self.assertIn('LINES READ: 3000', result)
        self.assertIn('row 02999', result)

    def test_oversized_read_is_capped_and_counted(self):
        budget = 2048
        result = file_tools.read_file(str(self.big), 1, 3000,
                                      max_output_bytes=budget)
        shown, original, dropped, limit = _footer_counts(result)
        self.assertEqual(limit, budget)
        self.assertEqual(shown + dropped, original)
        self.assertGreater(original, budget)
        self.assertLessEqual(len(result.encode('utf-8')), budget)
        self.assertIn('LINES READ: 3000', result[:200])

    def test_multibyte_file_truncates_cleanly(self):
        path = self.ws / 'cjk.txt'
        path.write_text('\u6587\u5b57 row\n' * 500, encoding='utf-8')
        result = file_tools.read_file(str(path), 1, 500, max_output_bytes=600)
        self.assertIn(TRUNCATION_MARKER_PREFIX, result)
        result.encode('utf-8').decode('utf-8')
        self.assertLessEqual(len(result.encode('utf-8')), 600)

    def test_missing_file_error_is_unchanged(self):
        missing = str(self.ws / 'nope.txt')
        result = file_tools.read_file(missing, 1, 10, max_output_bytes=8)
        self.assertEqual(result, 'File not found: %s' % missing)


class BashGuardTests(unittest.TestCase):
    """bash_tool caps stdout+stderr as one payload."""

    def _run(self, cmdline, budget=None):
        return system_tools.bash_tool(cmdline, max_output_bytes=budget)

    def test_small_output_is_unchanged(self):
        result = self._run('echo hi', budget=8192)
        self.assertEqual(result.strip(), 'hi')
        self.assertNotIn(TRUNCATION_MARKER_PREFIX, result)

    def test_legacy_behaviour_without_a_budget(self):
        result = self._run('%s -c "print(\'q\' * 20000)"' % sys.executable)
        self.assertNotIn(TRUNCATION_MARKER_PREFIX, result)
        self.assertGreater(len(result.encode('utf-8')), 20000)

    def test_oversized_output_is_capped_and_counted(self):
        budget = 4096
        result = self._run('%s -c "print(\'q\' * 50000)"' % sys.executable,
                           budget=budget)
        shown, original, dropped, limit = _footer_counts(result)
        self.assertEqual(limit, budget)
        self.assertEqual(shown + dropped, original)
        self.assertGreater(original, budget)
        self.assertLessEqual(len(result.encode('utf-8')), budget)

    def test_stdout_and_stderr_are_capped_together(self):
        cmd = ('%s -c "import sys; print(\'o\' * 40000); '
               'print(\'e\' * 40000, file=sys.stderr)"' % sys.executable)
        budget = 4096
        result = self._run(cmd, budget=budget)
        shown, original, dropped, limit = _footer_counts(result)
        # one cap over BOTH streams: the original size counts stdout + stderr
        self.assertGreater(original, 80000, original)
        self.assertEqual(shown + dropped, original)
        self.assertLessEqual(len(result.encode('utf-8')), budget)

    def test_empty_output_is_unchanged(self):
        self.assertEqual(self._run('true', budget=8), '(empty output)')

    def test_short_stderr_is_preserved(self):
        result = self._run('%s -c "import sys; sys.stderr.write(\'boom\'); '
                           'sys.exit(3)"' % sys.executable, budget=8192)
        self.assertIn('boom', result)
        self.assertNotIn(TRUNCATION_MARKER_PREFIX, result)


class SharedGuardAndWiringTests(unittest.TestCase):
    """One implementation, injected by the agent at call time."""

    def test_both_tools_share_the_single_guard(self):
        self.assertIs(file_tools.guard_output, output_guard.guard_output)
        self.assertIs(system_tools.guard_output, output_guard.guard_output)

    def test_agent_budget_tracks_the_prompt_token_count(self):
        agent, ws = _make_agent()
        try:
            agent.context_size = 128000
            agent._last_prompt_tokens = 28000
            self.assertEqual(agent._tool_output_budget(), 200000)
            agent._last_prompt_tokens = 128000
            self.assertEqual(agent._tool_output_budget(),
                             MIN_OUTPUT_BUDGET_BYTES)
            # no real usage yet -> the chars/4 history estimate (empty history)
            agent._last_prompt_tokens = 0
            self.assertEqual(agent._tool_output_budget(),
                             compute_output_budget(128000, 0))
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_agent_dispatch_injects_the_budget_into_read_file(self):
        agent, ws = _make_agent()
        try:
            big = Path(ws) / 'big.txt'
            big.write_text('sentinel-row\n' * 5000, encoding='utf-8')
            agent._tool_output_budget = lambda: 700
            result = agent.tool_map['read_file'](str(big), 1, 5000)
            shown, original, dropped, limit = _footer_counts(result)
            self.assertEqual(limit, 700)          # the injected budget was used
            self.assertLessEqual(len(result.encode('utf-8')), 700)
            self.assertTrue(original > 700)
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_agent_dispatch_injects_the_budget_into_bash(self):
        agent, ws = _make_agent()
        try:
            # No access controller: no permission prompt and no sandbox wrap, so
            # this goes through the real _bash_tool_with_guard -> bash_tool path
            # (a spy would replace the very function the guard lives in).
            agent.access_controller = None
            agent.context_size = 1000          # -> floor budget
            agent._last_prompt_tokens = 0
            expected = agent._tool_output_budget()
            self.assertEqual(expected, MIN_OUTPUT_BUDGET_BYTES)
            big = Path(ws) / 'big.txt'
            big.write_text('bash-row %s\n' % ('x' * 40) * 5000, encoding='utf-8')
            result = agent.tool_map['bash_tool']('cat %s' % big)
            shown, original, dropped, limit = _footer_counts(result)
            self.assertEqual(limit, expected)   # the dispatch injected it
            self.assertLessEqual(len(result.encode('utf-8')), expected)
            self.assertGreater(original, expected)
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_agent_dispatch_injects_the_budget_into_bash_kwargs(self):
        """Belt and braces: the dispatch really passes max_output_bytes."""
        agent, ws = _make_agent()
        try:
            agent.context_size = 64000
            agent._last_prompt_tokens = 1000
            seen = {}

            def spy(cmdline, **kwargs):
                seen['cmdline'] = cmdline
                seen.update(kwargs)
                return 'ok'

            with patch('yaha.core.agent.bash_tool', spy):
                result = agent.tool_map['bash_tool']('echo hi')
            self.assertEqual(result, 'ok')
            self.assertEqual(seen['cmdline'], 'echo hi')
            self.assertEqual(seen.get('max_output_bytes'),
                             agent._tool_output_budget())
            self.assertEqual(seen.get('max_output_bytes'),
                             compute_output_budget(64000, 1000))
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    @staticmethod
    def _schema_path():
        """The shipped tool-schema.md of the package actually under test.

        Anchored on `yaha.__file__` rather than on this test file's location, so
        it resolves identically in the canonical repo, in the selfcoding
        worktree and in a scratch copy - and can never silently skip.
        """
        import yaha
        return Path(yaha.__file__).resolve().parent / 'templates' / 'tool-schema.md'

    def test_shipped_schema_still_exposes_only_the_old_parameters(self):
        schema_path = self._schema_path()
        self.assertTrue(schema_path.exists(),
                        'shipped tool-schema.md missing at %s' % schema_path)
        text = schema_path.read_text(encoding='utf-8')
        for tool, params in (('read_file', ('start_line', 'end_line')),
                             ('bash_tool', ('cmdline',))):
            block = text[text.index('"name": "%s"' % tool):]
            block = block[: block.index('"required"')]
            for param in params:
                self.assertIn('"%s"' % param, block)
            # the budget stays internal: it must not leak into the model-facing
            # schema as a parameter the model could pass.
            self.assertNotIn('max_output_bytes', block)


if __name__ == '__main__':
    unittest.main()