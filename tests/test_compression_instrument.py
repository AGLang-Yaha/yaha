"""B1/B3/B4: one instrument for occupancy, an independent strict watermark, and no
per-turn L1 thrash.

These guards are written to be world-agnostic so they can be run RED before the fix
and GREEN after it: they only touch `_last_prompt_tokens` (the real anchor) and, when
the attribute exists, `_chars_at_last_usage` (the anchor's character baseline).
"""
import unittest

from yaha.core import agent as agent_mod

from _compression_fixture import (AgentCase, MockLLM, history_with_old_tool,
                                  realistic_window, set_max_tokens)

SNAP_DISABLED_STAGE = {'name': 'Coding', 'snap': 'disabled'}
SNAP_ENABLED_STAGE = {'name': 'Preamble', 'snap': 'enabled'}
COMPRESSED_MARK = 'COMPRESSED TOOL RESULT'


def _compressed(agent):
    return any(COMPRESSED_MARK in (m.get('content') or '') for m in agent.history)


def _chars_derived_estimate(agent):
    """The chars/4 reading of the CURRENT history - the value the buggy code wrote
    into `_last_prompt_tokens`."""
    total = sum(len(m.get('content') or '') for m in agent.history)
    return int((total / 4) / agent.context_size * agent.context_size)


class TestStrictWatermark(unittest.TestCase):
    """B3: the emergency watermark must not be derived from snap_threshold."""

    def test_constant_exists_and_is_the_documented_value(self):
        self.assertTrue(hasattr(agent_mod, 'HARD_FORCE_RATIO'),
                        'core/agent.py must define the strict emergency watermark constant')
        self.assertAlmostEqual(agent_mod.HARD_FORCE_RATIO, 0.95, places=6)

    def test_below_the_watermark_never_fires_even_with_a_tiny_snap_threshold(self):
        with AgentCase() as case:
            agent = case.agent
            realistic_window(agent)                 # shipped 128K/8192 -> bar ~0.904
            agent.config.snap_threshold = 0.01      # the value that used to force the net
            agent.history = history_with_old_tool()
            case.anchor(110000)                     # real occupancy ~86%, below the bar
            before = agent.compress_count

            rebuilt, _ = agent._mid_loop_protection(SNAP_DISABLED_STAGE, 'cont', 1)

            self.assertFalse(rebuilt)
            self.assertEqual(agent.compress_count, before,
                             'the emergency net fired below the strict watermark')
            self.assertFalse(_compressed(agent), 'L1 truncated below the strict watermark')
            self.assertEqual(agent.llm.calls, 0, 'no handler may run below the watermark')

    def test_above_the_watermark_still_fires(self):
        with AgentCase() as case:
            agent = case.agent
            realistic_window(agent)                 # bar ~0.904
            # A history big enough that L1 can genuinely move the window: 400k chars against
            # a 124k-token anchor leaves plenty of headroom below the bar afterwards.
            agent.history = history_with_old_tool(size=400000)
            case.anchor(124000)
            before = agent.compress_count

            rebuilt, _ = agent._mid_loop_protection(SNAP_DISABLED_STAGE, 'cont', 1)

            self.assertFalse(rebuilt)
            self.assertTrue(_compressed(agent), 'L1 must still protect above the watermark')
            self.assertEqual(agent.compress_count, before + 1)

    def test_the_bar_comes_from_the_constant_not_from_snap_threshold(self):
        original = getattr(agent_mod, 'HARD_FORCE_RATIO', None)
        agent_mod.HARD_FORCE_RATIO = 0.5
        try:
            with AgentCase() as case:
                agent = case.agent
                # A 512K window puts the window formula at ~0.984, so the patched CAP is the
                # binding value: this shows the cap itself moves the trigger, deliberately
                # rather than by accident of the small-window floor.
                realistic_window(agent, context_size=512000, max_tokens=8192)
                agent.config.snap_threshold = 0.99   # would NOT fire at 86%
                agent.history = history_with_old_tool()
                case.anchor(440320)                  # 86% of the 512K window

                agent._mid_loop_protection(SNAP_DISABLED_STAGE, 'cont', 1)

                # The point is WHICH bar moved the net, not which level it then chose: with a
                # patched 0.5 cap the net acts (L1 when that suffices, otherwise escalating to
                # the handler), whereas the unpatched 0.95 cap would have left 86% alone.
                self.assertTrue(_compressed(agent) or agent.llm.calls > 0,
                                'the emergency bar must follow HARD_FORCE_RATIO, not snap_threshold')
        finally:
            if original is None:
                try:
                    delattr(agent_mod, 'HARD_FORCE_RATIO')
                except AttributeError:
                    pass
            else:
                agent_mod.HARD_FORCE_RATIO = original


class TestLastPromptTokensStaysReal(unittest.TestCase):
    """B1: `_last_prompt_tokens` must be written only from provider usage."""

    def test_l1_pass_does_not_overwrite_the_real_anchor(self):
        # Keep the anchor consistent with the history (~0.25 tokens/char); an anchor that
        # no truncation could move would legitimately escalate instead of taking L1.
        with AgentCase(context_size=2000) as case:
            agent = case.agent
            agent.history = history_with_old_tool()      # ~6.1k chars ~ 1.5k tokens
            anchor = case.anchor(1917)                   # ratio 0.958 -> above the watermark

            agent._mid_loop_protection(SNAP_DISABLED_STAGE, 'cont', 1)

            self.assertTrue(_compressed(agent), 'the guard must exercise a real L1 pass')
            self.assertEqual(agent._last_prompt_tokens, anchor,
                             'an L1 pass wrote a characters-derived value into the real anchor')
            self.assertNotEqual(agent._last_prompt_tokens, _chars_derived_estimate(agent))

    def test_forced_snap_keeps_only_provider_reported_values(self):
        with AgentCase(context_size=460000) as case:
            agent = case.agent
            agent.llm = MockLLM(usage={'prompt_tokens': 4242, 'completion_tokens': 5})
            agent.history = history_with_old_tool()
            anchor = case.anchor(440617)

            agent._maybe_snap(SNAP_ENABLED_STAGE, 1, force=True)

            self.assertGreater(agent.llm.calls, 0, 'the handler must have run')
            self.assertIn(agent._last_prompt_tokens, (anchor, 4242),
                          'a cold restart left a characters-derived value in the real anchor')


class TestNoThrash(unittest.TestCase):
    """B4: a stable real occupancy below the watermark must not compress per turn."""

    def test_repeated_turns_do_not_compress(self):
        with AgentCase() as case:
            agent = case.agent
            realistic_window(agent)                 # shipped 128K/8192 -> bar ~0.904
            agent.history = history_with_old_tool()
            before = agent.compress_count

            for turn in range(1, 6):
                case.anchor(110000)                 # the provider keeps reporting ~86%
                rebuilt, _ = agent._mid_loop_protection(SNAP_DISABLED_STAGE, 'cont', turn)
                self.assertFalse(rebuilt)

            self.assertEqual(agent.compress_count, before,
                             'compression thrashed while the real occupancy stayed stable')
            self.assertFalse(_compressed(agent))
            self.assertEqual(agent.llm.calls, 0)


if __name__ == '__main__':
    unittest.main()