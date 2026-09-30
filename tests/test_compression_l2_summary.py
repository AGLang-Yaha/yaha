# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

"""L2 compression: a <=100-line summary + the last n ReAct steps.

A compressed context is rebuilt as system prompt + summary + the last n ReAct
steps (a step = one tool_call-result pair). `Compression Keep Steps` is an
agent-wide scalar (default 10) and the L1 truncation rule still applies to the
retained steps.
"""
import unittest

from _compression_fixture import AgentCase, DIGEST


def _tool_result(tag, size=40):
    return 'TOOL RESULT (bash_tool): ' + tag + ('x' * size)


def _add_step(agent, tag, size=40):
    agent.history.append({'role': 'assistant', 'content': 'call ' + tag})
    agent.history.append({'role': 'user', 'content': _tool_result(tag, size)})


class TestRecentReactSteps(unittest.TestCase):
    def test_returns_last_n_pairs(self):
        with AgentCase() as case:
            agent = case.agent
            agent.history = [{'role': 'system', 'content': 'sys'}]
            for tag in ('s1', 's2', 's3', 's4'):
                _add_step(agent, tag)
            steps = agent._recent_react_steps(2)
            self.assertEqual(len(steps), 4)
            self.assertEqual(steps[0]['content'], 'call s3')
            self.assertTrue(steps[1]['content'].startswith('TOOL RESULT ('))
            self.assertEqual(steps[2]['content'], 'call s4')
            self.assertTrue(steps[3]['content'].startswith('TOOL RESULT ('))

    def test_empty_without_tool_results(self):
        with AgentCase() as case:
            agent = case.agent
            agent.history = [{'role': 'system', 'content': 'sys'},
                             {'role': 'user', 'content': 'hello'}]
            self.assertEqual(agent._recent_react_steps(3), [])

    def test_n_zero_and_negative(self):
        with AgentCase() as case:
            agent = case.agent
            agent.history = [{'role': 'system', 'content': 'sys'}]
            _add_step(agent, 's1')
            self.assertEqual(agent._recent_react_steps(0), [])
            self.assertEqual(agent._recent_react_steps(-1), [])


class TestSummaryCap(unittest.TestCase):
    def test_long_summary_capped_at_100_lines(self):
        with AgentCase() as case:
            agent = case.agent
            text = chr(10).join('line %d' % i for i in range(150))
            lines = agent._cap_summary_lines(text).splitlines()
            self.assertEqual(len(lines), 100)
            self.assertIn('truncated', lines[-1])

    def test_short_summary_passes_through(self):
        with AgentCase() as case:
            agent = case.agent
            text = 'GOAL: g'
            self.assertEqual(agent._cap_summary_lines(text), text)


class TestKeepStepsConfig(unittest.TestCase):
    def test_default_is_ten(self):
        with AgentCase() as case:
            agent = case.agent
            agent.config.compression_keep_steps = None
            self.assertEqual(agent._compression_keep_steps(), 10)

    def test_configured_value_is_used(self):
        with AgentCase() as case:
            agent = case.agent
            agent.config.compression_keep_steps = 4
            self.assertEqual(agent._compression_keep_steps(), 4)


class TestRebuildShape(unittest.TestCase):
    def _history_with_steps(self, agent, n):
        agent.history = [{'role': 'system', 'content': 'sys'}]
        for i in range(n):
            _add_step(agent, 'step%d' % i)

    def test_rebuild_is_system_summary_then_steps(self):
        with AgentCase() as case:
            agent = case.agent
            agent.config.compression_keep_steps = 2
            self._history_with_steps(agent, 2)
            agent._rebuild_compressed_history(DIGEST)
            self.assertEqual(agent.history[0]['role'], 'system')
            self.assertTrue(agent.history[1]['content'].startswith('CONTEXT DIGEST'))
            self.assertIn('GOAL: g', agent.history[1]['content'])
            self.assertEqual(agent.history[2]['content'], 'call step0')
            self.assertTrue(agent.history[3]['content'].startswith('TOOL RESULT ('))
            self.assertEqual(agent.history[4]['content'], 'call step1')
            self.assertTrue(agent.history[5]['content'].startswith('TOOL RESULT ('))

    def test_only_last_n_steps_are_kept(self):
        with AgentCase() as case:
            agent = case.agent
            agent.config.compression_keep_steps = 1
            self._history_with_steps(agent, 3)
            agent._rebuild_compressed_history(DIGEST)
            joined = chr(10).join(m['content'] for m in agent.history)
            self.assertIn('call step2', joined)
            self.assertNotIn('call step0', joined)

    def test_l1_rule_applies_to_retained_steps(self):
        with AgentCase() as case:
            agent = case.agent
            agent.history = [{'role': 'system', 'content': 'sys'}]
            for i in range(6):
                _add_step(agent, 'big%d' % i, size=6000)
            agent.config.compression_keep_steps = 6
            agent._rebuild_compressed_history(DIGEST)
            joined = chr(10).join(m['content'] for m in agent.history)
            self.assertIn('COMPRESSED TOOL RESULT', joined)


class TestL2Path(unittest.TestCase):
    def test_l2_rebuild_keeps_steps_and_stage_prompt(self):
        with AgentCase(context_size=400) as case:
            agent = case.agent
            agent.config.compression_keep_steps = 1
            agent.history = [
                {'role': 'system', 'content': 'sys'},
                {'role': 'user', 'content': 'THE TASK'},
                {'role': 'assistant', 'content': 'call a'},
                {'role': 'user', 'content': _tool_result('small')},
                {'role': 'assistant', 'content': 'z' * 3000},
                {'role': 'user', 'content': 'w' * 3000},
            ]
            agent.logger.set_task('task')
            rebuilt, _ = agent._compress_context({'name': 'Coding'}, 'STAGE PROMPT', 1)
            self.assertTrue(rebuilt)
            self.assertTrue(agent.history[1]['content'].startswith('CONTEXT DIGEST'))
            self.assertEqual(agent.history[2]['content'], 'call a')
            self.assertTrue(agent.history[3]['content'].startswith('TOOL RESULT ('))
            self.assertIn('CONTEXT RESTORED', agent.history[-1]['content'])
            self.assertIn('STAGE PROMPT', agent.history[-1]['content'])


class TestPrompts(unittest.TestCase):
    def test_reflection_prompt_asks_for_under_100_lines(self):
        with AgentCase(context_size=400) as case:
            agent = case.agent
            agent.history = [
                {'role': 'system', 'content': 'sys'},
                {'role': 'assistant', 'content': 'z' * 3000},
                {'role': 'user', 'content': 'w' * 3000},
            ]
            agent.logger.set_task('task')
            agent._compress_context({'name': 'Coding'}, 'STAGE PROMPT', 1)
            prompt = agent.llm.sent[0][-1]['content']
            self.assertIn('100', prompt)
            self.assertIn('DESIGN & ARCHITECTURE DECISIONS', prompt)

    def test_snap_request_states_the_summary_spec(self):
        with AgentCase(context_size=400) as case:
            agent = case.agent
            agent.history = [
                {'role': 'system', 'content': 'sys'},
                {'role': 'user', 'content': 'x' * 3000},
            ]
            agent._maybe_snap({'name': 'Preamble', 'snap': 'enabled'}, 1, force=True)
            prompt = agent.llm.sent[0][-1]['content']
            self.assertIn('DESIGN & ARCHITECTURE DECISIONS', prompt)
            self.assertIn('100', prompt)


class TestStepEndRecognition(unittest.TestCase):
    def test_recognises_compressed_step_ends(self):
        with AgentCase() as case:
            agent = case.agent
            agent.history = [{'role': 'system', 'content': 'sys'}]
            for tag in ('s1', 's2', 's3'):
                agent.history.append({'role': 'assistant', 'content': 'call ' + tag})
                agent.history.append({
                    'role': 'user',
                    'content': 'COMPRESSED TOOL RESULT (bash_tool): ' + tag})
            self.assertEqual(len(agent._recent_react_steps(2)), 4)


class TestL2Retention(unittest.TestCase):
    def test_l2_retains_n_steps_after_l1_compress(self):
        with AgentCase(context_size=8000) as case:
            agent = case.agent
            agent.config.compression_keep_steps = 10
            agent.history = [{'role': 'system', 'content': 'sys'},
                             {'role': 'user', 'content': 'THE TASK'}]
            for i in range(12):
                _add_step(agent, 'big%d' % i, size=6000)
            # A big non-tool tail so L1 alone cannot reach the target and L2 runs.
            agent.history.append({'role': 'assistant', 'content': 'z' * 40000})
            agent.logger.set_task('task')
            rebuilt, _ = agent._compress_context({'name': 'Coding'}, 'STAGE PROMPT', 1)
            self.assertTrue(rebuilt)
            kept = [m for m in agent.history
                    if m['role'] == 'user' and 'TOOL RESULT (' in m['content']]
            self.assertEqual(len(kept), 10)


if __name__ == '__main__':
    unittest.main()