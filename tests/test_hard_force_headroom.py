"""M0 + M3 guards for the revision round.

M0: the strict emergency watermark must leave room for one MAXIMUM request, otherwise the
net cannot complete its own compression (it must call the model over a near-full history).
Asserted over every model the tree actually ships, not over hard-coded numbers.

M3: the anchored prediction must not collapse to 0, because the tool-output budget reads
it and would then hand out the largest budget it can produce.
"""
import unittest
from pathlib import Path

import yaha

from _compression_fixture import AgentCase, history_with_old_tool

MODEL_MD = Path(yaha.__file__).resolve().parent / 'templates' / 'Model.md'


def _shipped_models():
    from yaha.config.models import parse_model_md
    return parse_model_md(str(MODEL_MD))


def _bar_for(context_size, max_tokens, margin, floor, cap):
    """The documented bar formula, evaluated independently of the agent."""
    if not context_size:
        return cap
    bar = 1.0 - (max_tokens + margin) / context_size
    return min(cap, max(floor, bar))


class TestHardForceHeadroom(unittest.TestCase):
    def test_every_shipped_model_leaves_room_for_one_maximum_request(self):
        from yaha.core import agent as agent_mod
        models = _shipped_models()
        self.assertTrue(models, 'templates/Model.md must define at least one model')
        for name, cfg in models.items():
            window = cfg.context_size or 0
            if not window:
                continue
            max_tokens = cfg.max_tokens or agent_mod.HARD_FORCE_DEFAULT_MAX_TOKENS
            bar = _bar_for(window, max_tokens,
                           agent_mod.HARD_FORCE_OUTPUT_MARGIN,
                           agent_mod.HARD_FORCE_FLOOR,
                           agent_mod.HARD_FORCE_RATIO)
            headroom = (1.0 - bar) * window
            if bar > agent_mod.HARD_FORCE_FLOOR + 1e-9:
                self.assertGreaterEqual(
                    headroom, max_tokens + agent_mod.HARD_FORCE_OUTPUT_MARGIN - 1e-6,
                    'model %r: bar %.4f on a %d window leaves %.0f tokens, less than one '
                    'maximum request (%d + margin)' % (name, bar, window, headroom, max_tokens))
            else:
                self.assertAlmostEqual(
                    bar, agent_mod.HARD_FORCE_FLOOR, places=9,
                    msg='model %r: a window too small for the invariant must sit on the '
                        'documented floor, not somewhere arbitrary' % name)

    def test_the_named_cases_from_the_review(self):
        from yaha.core import agent as agent_mod
        bar_128k = _bar_for(128000, 8192, agent_mod.HARD_FORCE_OUTPUT_MARGIN,
                            agent_mod.HARD_FORCE_FLOOR, agent_mod.HARD_FORCE_RATIO)
        self.assertAlmostEqual(bar_128k, 0.904, places=3)
        self.assertLess(bar_128k, 0.95,
                        'a 128K window with 8192 max_tokens must NOT keep the 0.95 bar: '
                        '0.05 * 128000 = 6400 < 8192')
        bar_512k = _bar_for(512000, 8192, agent_mod.HARD_FORCE_OUTPUT_MARGIN,
                            agent_mod.HARD_FORCE_FLOOR, agent_mod.HARD_FORCE_RATIO)
        self.assertAlmostEqual(bar_512k, agent_mod.HARD_FORCE_RATIO, places=9)

    def test_the_agent_bar_follows_its_own_model_config(self):
        from yaha.core import agent as agent_mod
        with AgentCase(context_size=128000) as case:
            self.assertTrue(hasattr(case.agent, '_hard_force_ratio'),
                            'core/agent.py must expose the window-aware bar')
            case.agent.model_cfg.max_tokens = 8192
            self.assertAlmostEqual(case.agent._hard_force_ratio(), 0.904, places=3)
            case.agent.context_size = 512000
            self.assertAlmostEqual(case.agent._hard_force_ratio(),
                                   agent_mod.HARD_FORCE_RATIO, places=9)
            case.agent.context_size = 0
            self.assertAlmostEqual(case.agent._hard_force_ratio(),
                                   agent_mod.HARD_FORCE_RATIO, places=9)

    def test_the_net_does_not_fire_below_the_computed_bar(self):
        # The shipped 128K configuration: 0.86 is below the ~0.904 bar, so the net must
        # stand down even though it is far above a naive constant like the floor.
        with AgentCase(context_size=128000) as case:
            agent = case.agent
            agent.model_cfg.max_tokens = 8192
            agent.history = history_with_old_tool()
            case.anchor(110000)                      # 0.859
            before = agent.compress_count
            agent._mid_loop_protection({'name': 'Coding', 'snap': 'disabled'}, 'cont', 1)
            self.assertEqual(agent.compress_count, before,
                             'the net fired below its own window-derived bar')


class TestPredictionNeverCollapses(unittest.TestCase):
    def test_prediction_stays_positive_while_content_exists(self):
        with AgentCase(context_size=1000) as case:
            agent = case.agent
            agent.history = history_with_old_tool()
            case.anchor(960)                         # above any sane bar
            agent._mid_loop_protection({'name': 'Coding', 'snap': 'disabled'}, 'cont', 1)

            rate = agent._get_calibration().tokens_per_char()
            floor = int(rate * agent._history_chars())
            self.assertGreater(agent._predicted_tokens(), 0,
                               'the prediction collapsed to 0 while content exists')
            self.assertGreaterEqual(agent._predicted_tokens(), floor,
                                    'the prediction fell below the density lower bound')
            self.assertGreater(agent._context_usage_ratio(), 0.0)


if __name__ == '__main__':
    unittest.main()
