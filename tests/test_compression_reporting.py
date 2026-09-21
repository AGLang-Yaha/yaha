"""B2/B6: the occupancy report must mark estimates, must not hide an over-window
reading, and must express before/after in ONE instrument.

B2 drives `_ctx_display_text` (the section-bar provider). B6 asserts the invariant
behind the diagnosis' arithmetic proof: the drop reported across an L1 pass must equal
the drop actually attributable to the removed characters at the agent's own rate -
not the unit switch from a provider count to a chars/4 reading.
"""
import unittest

from _compression_fixture import AgentCase, history_with_old_tool

SNAP_DISABLED_STAGE = {'name': 'Coding', 'snap': 'disabled'}
EST_MARKER = 'est.'


def _clear_predicted_flag(agent):
    """Post-fix hook; a no-op before the fix exists."""
    try:
        setattr(agent, '_usage_is_predicted', False)
    except Exception:
        pass


def _tokens_per_char(agent):
    cal = getattr(agent, '_calibration', None)
    if cal is not None and hasattr(cal, 'tokens_per_char'):
        return cal.tokens_per_char()
    return 0.25


class TestGaugeMarksEstimates(unittest.TestCase):
    """B2."""

    def test_estimate_is_marked_before_any_measurement(self):
        with AgentCase(context_size=1000) as case:
            agent = case.agent
            agent._last_prompt_tokens = 0
            text = agent._ctx_display_text()
            self.assertIn(EST_MARKER, text,
                          'an occupancy without a provider measurement must be marked: %r' % text)

    def test_estimate_is_marked_after_a_compression(self):
        with AgentCase(context_size=1000) as case:
            agent = case.agent
            agent.history = history_with_old_tool()
            case.anchor(960)
            agent._mid_loop_protection(SNAP_DISABLED_STAGE, 'cont', 1)
            text = agent._ctx_display_text()
            self.assertIn(EST_MARKER, text,
                          'a post-compression reading is a prediction and must be marked: %r' % text)

    def test_a_real_measurement_carries_no_marker(self):
        with AgentCase(context_size=1000) as case:
            agent = case.agent
            agent.history = history_with_old_tool()
            case.anchor(500)
            _clear_predicted_flag(agent)
            text = agent._ctx_display_text()
            self.assertNotIn(EST_MARKER, text, 'a provider-measured reading must stay unmarked')

    def test_over_window_reading_is_not_hidden(self):
        with AgentCase(context_size=1000) as case:
            agent = case.agent
            agent._last_prompt_tokens = 1200
            _clear_predicted_flag(agent)
            text = agent._ctx_display_text()
            self.assertIn('120%', text,
                          'an at-or-over-window occupancy must not be flattened to 99%%: %r' % text)


class TestReportUsesOneInstrument(unittest.TestCase):
    """B6."""

    def test_reported_drop_matches_the_characters_actually_removed(self):
        with AgentCase(context_size=420000) as case:
            agent = case.agent
            agent.history = history_with_old_tool(size=600000)
            case.anchor(400000)                       # ratio 0.952 -> above the watermark

            chars_before = case.chars()
            before_tokens = int(agent._context_usage_ratio() * agent.context_size)

            agent._mid_loop_protection(SNAP_DISABLED_STAGE, 'cont', 1)

            chars_after = case.chars()
            after_tokens = int(agent._context_usage_ratio() * agent.context_size)
            removed = chars_before - chars_after
            self.assertGreater(removed, 1000, 'the guard must exercise a real truncation')

            expected = removed * _tokens_per_char(agent)
            self.assertAlmostEqual(before_tokens - after_tokens, expected,
                                   delta=max(500.0, 0.03 * expected),
                                   msg='the before/after pair mixes units: removed=%d expected=%.0f got=%d'
                                       % (removed, expected, before_tokens - after_tokens))


if __name__ == '__main__':
    unittest.main()