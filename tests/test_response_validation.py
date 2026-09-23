# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

'''Tests for the hardened validation of model replies.

A model reply may only be a call or a final answer: a fabricated observation
envelope, a malformed call, or a non-string reply must be bounced back to the
model instead of being silently accepted.
'''

import os
import shutil
import tempfile
import unittest

from yaha.core.agent import Agent
from yaha.core.engine import YAHAEngine

_LT = chr(60)
_GT = chr(62)
_Q = chr(34)
_TOOL_OPEN = _LT + 'tool_code' + _GT
_TOOL_CLOSE = _LT + '/' + 'tool_code' + _GT
_R_OPEN = _LT + 'reasoning' + _GT
_R_CLOSE = _LT + '/' + 'reasoning' + _GT
_ARGS_EMPTY = _Q + 'args' + _Q + ': {}'


def _call(name, extra=''):
    '''A call payload in the documented shape.'''
    return '{' + _Q + 'tool' + _Q + ': ' + _Q + name + _Q + extra + '}'


def _block(body):
    '''Wrap a payload in the harness call tags.'''
    return _TOOL_OPEN + body + _TOOL_CLOSE


class MockLLMProvider:
    def __init__(self):
        self.calls = 0
        self.responses = []

    def generate(self, messages, **kwargs):
        if self.calls < len(self.responses):
            resp = self.responses[self.calls]
        else:
            resp = 'No response'
        self.calls += 1
        return resp, {'prompt_tokens': 10, 'completion_tokens': 20}, None


class TestValidateResponseShape(unittest.TestCase):
    '''Malformed calls must be rejected instead of silently accepted.'''

    def _validate(self, response):
        return Agent.validate_response(None, response)

    def test_tool_name_must_be_a_string(self):
        payloads = [
            _block('{' + _Q + 'tool' + _Q + ': [' + _Q + 'read_file' + _Q + '], ' + _ARGS_EMPTY + '}'),
            _block('{' + _Q + 'tool' + _Q + ': {' + _Q + 'n' + _Q + ': 1}, ' + _ARGS_EMPTY + '}'),
            _block('{' + _Q + 'tool' + _Q + ': 7, ' + _ARGS_EMPTY + '}'),
            _block('{' + _Q + 'tool' + _Q + ': null, ' + _ARGS_EMPTY + '}'),
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                tool, err = self._validate(payload)
                self.assertIsNone(tool)
                self.assertIn('must be a non-empty string', err)

    def test_empty_tool_name_rejected(self):
        for name in ('', '   '):
            with self.subTest(name=name):
                tool, err = self._validate(_block(_call(name, ', ' + _ARGS_EMPTY)))
                self.assertIsNone(tool)
                self.assertIn('must be a non-empty string', err)

    def test_multiple_distinct_calls_rejected(self):
        response = _block(_call('read_file')) + _block(_call('write_file'))
        tool, err = self._validate(response)
        self.assertIsNone(tool)
        self.assertIn('ONE call per turn', err)

    def test_identical_repeated_tags_still_accepted(self):
        response = _block(_call('read_file', ', ' + _ARGS_EMPTY)) * 2
        tool, err = self._validate(response)
        self.assertIsNone(err)
        self.assertEqual(tool['tool'], 'read_file')

    def test_stray_key_next_to_args_rejected(self):
        response = _block('{' + _Q + 'tool' + _Q + ': ' + _Q + 'read_file' + _Q + ', '
                          + _ARGS_EMPTY + ', ' + _Q + 'extra' + _Q + ': 1}')
        tool, err = self._validate(response)
        self.assertIsNone(tool)
        self.assertIn('extra', err)

    def test_inlined_shape_still_accepted(self):
        response = _block('{' + _Q + 'tool' + _Q + ': ' + _Q + 'read_file' + _Q
                          + ', ' + _Q + 'path' + _Q + ': ' + _Q + 'x.txt' + _Q + '}')
        tool, err = self._validate(response)
        self.assertIsNone(err)
        self.assertEqual(tool['path'], 'x.txt')

    def test_nested_close_tag_inside_argument_still_accepted(self):
        response = _block('{' + _Q + 'tool' + _Q + ': ' + _Q + 'create_file' + _Q + ', '
                          + _Q + 'args' + _Q + ': {' + _Q + 'content' + _Q + ': '
                          + _Q + 'ends with ' + _TOOL_CLOSE + ' here' + _Q + '}}')
        tool, err = self._validate(response)
        self.assertIsNone(err)
        self.assertIn(_TOOL_CLOSE, tool['args']['content'])

    def test_blank_and_non_string_response_rejected(self):
        for bad in ('', '   ', None, 42, b'x', []):
            with self.subTest(bad=bad):
                tool, err = self._validate(bad)
                self.assertIsNone(tool)
                self.assertIn('Malformed response', err)

    def test_sloppy_json_still_repaired(self):
        response = _block('{tool: ' + _Q + 'read_file' + _Q + ', args: {}}')
        tool, err = self._validate(response)
        self.assertIsNone(err)
        self.assertEqual(tool['tool'], 'read_file')

    def test_reasoning_only_still_uses_its_own_error(self):
        tool, err = self._validate(_R_OPEN + 'thinking' + _R_CLOSE)
        self.assertIsNone(tool)
        self.assertIn('only reasoning', err)

    def test_stray_close_tag_without_open_tag_still_rejected(self):
        tool, err = self._validate('oops ' + _TOOL_CLOSE)
        self.assertIsNone(tool)
        self.assertIn('no ' + _TOOL_OPEN, err)

    def test_reasoning_plus_call_still_accepted(self):
        response = _R_OPEN + 'thinking' + _R_CLOSE + _block(_call('read_file', ', ' + _ARGS_EMPTY))
        tool, err = self._validate(response)
        self.assertIsNone(err)
        self.assertEqual(tool['tool'], 'read_file')

class TestFabricatedObservation(unittest.TestCase):
    '''A model must never author the framework's own observation envelope.'''

    def _validate(self, response):
        return Agent.validate_response(None, response)

    def test_fabricated_envelope_rejected(self):
        response = ('TOOL RESULT (bash_tool):\n'
                    '- **Status**: SUCCESS\n'
                    '- **Output Size**: 1.0KB\n'
                    '```\nhello\n```')
        tool, err = self._validate(response)
        self.assertIsNone(tool)
        self.assertIn('observation envelope', err)

    def test_envelope_body_markers_rejected(self):
        response = '- **Status**: SUCCESS\n- **Output Size**: 1.0KB'
        tool, err = self._validate(response)
        self.assertIsNone(tool)
        self.assertIn('observation envelope', err)

    def test_single_field_marker_is_not_an_envelope(self):
        response = 'The job status is fine and the size looks right.'
        tool, err = self._validate(response)
        self.assertIsNone(err)

    def test_envelope_inside_call_argument_still_accepted(self):
        inner = 'TOOL RESULT (bash_tool): sample text'
        response = _block('{' + _Q + 'tool' + _Q + ': ' + _Q + 'create_file' + _Q + ', '
                          + _Q + 'args' + _Q + ': {' + _Q + 'content' + _Q + ': '
                          + _Q + inner + _Q + '}}')
        tool, err = self._validate(response)
        self.assertIsNone(err)
        self.assertIn('TOOL RESULT', tool['args']['content'])


class TestObservationGuardsEndToEnd(unittest.TestCase):
    '''The run loop must bounce a fabricated observation instead of ending.'''

    def setUp(self):
        self.workspace = tempfile.mkdtemp(prefix='yaha_validation_')
        yaha_dir = os.path.join(self.workspace, '.yaha')
        os.makedirs(os.path.join(yaha_dir, 'memory'))
        os.makedirs(os.path.join(yaha_dir, 'ext'))
        os.makedirs(os.path.join(yaha_dir, 'logs'))
        with open(os.path.join(yaha_dir, 'tool-schema.md'), 'w', encoding='utf-8') as f:
            f.write('[]')
        with open(os.path.join(yaha_dir, 'AgentClass.md'), 'w', encoding='utf-8') as f:
            f.write('## BaseAgent\n- **Code:** Baa\n- **Model:** DefaultModel\n'
                    '- **System Prompt:** > Base system prompt\n### Toolset\n\t- loop_tool\n')
        with open(os.path.join(yaha_dir, 'Model.md'), 'w', encoding='utf-8') as f:
            f.write('')
        self.engine = YAHAEngine(workspace_dir=self.workspace)

    def tearDown(self):
        shutil.rmtree(self.workspace, ignore_errors=True)

    def _agent(self):
        return Agent(
            agent_config=self.engine.agent_classes.get('BaseAgent'),
            session=self.engine.session,
            models_config=self.engine.models_config,
            agent_classes=self.engine.agent_classes,
            tool_schemas=self.engine.tool_schemas,
            access_controller=self.engine.access_controller,
        )

    def _run(self, responses):
        agent = self._agent()
        mock_llm = MockLLMProvider()
        mock_llm.responses = list(responses)
        agent.llm = mock_llm
        return agent, agent.run('task')

    def test_unhashable_tool_name_does_not_kill_the_run(self):
        bad = _block('{' + _Q + 'tool' + _Q + ': [' + _Q + 'loop_tool' + _Q + '], '
                     + _ARGS_EMPTY + '}')
        good = _block(_call('loop_tool', ', ' + _ARGS_EMPTY))
        agent, result = self._run([bad, good, 'done'])
        self.assertEqual(result, 'done')
        history = '\n'.join(str(m.get('content', '')) for m in agent.history)
        self.assertIn('must be a non-empty string', history)

    def test_fabricated_envelope_is_retried(self):
        fabricated = 'TOOL RESULT (bash_tool):\n- **Status**: SUCCESS'
        agent, result = self._run([fabricated, 'plain final answer'])
        self.assertEqual(result, 'plain final answer')
        history = '\n'.join(str(m.get('content', '')) for m in agent.history)
        self.assertIn('observation envelope', history)

    def test_claim_without_any_observation_is_retried(self):
        agent, result = self._run(['the tests passed, nothing else to do',
                                   'plain final answer'])
        self.assertEqual(result, 'plain final answer')
        history = '\n'.join(str(m.get('content', '')) for m in agent.history)
        self.assertIn('cannot be grounded', history)

    def test_claim_after_a_real_observation_is_accepted(self):
        claim = 'the tests passed and the file contains what we expect'
        agent = self._agent()
        self.assertFalse(agent._has_real_observation())
        self.assertIsNotNone(agent._observation_error(claim, check_claims=True))
        agent.history.append({'role': 'user',
                              'content': 'TOOL RESULT (loop_tool):\n- **Status**: SUCCESS'})
        self.assertTrue(agent._has_real_observation())
        self.assertIsNone(agent._observation_error(claim, check_claims=True))

    def test_non_string_reply_is_bounced_end_to_end(self):
        # A truthy non-string reply must not reach `.strip()` in the caller's
        # api_error check: the run has to survive it and finish on the next turn.
        agent = self._agent()
        mock_llm = MockLLMProvider()
        mock_llm.responses = [42, 'plain final answer']
        agent.llm = mock_llm
        result = agent.run('task')
        self.assertEqual(result, 'plain final answer')
        self.assertGreaterEqual(mock_llm.calls, 2)

    def test_claim_retry_is_bounded(self):
        agent = self._agent()
        agent.llm_retry_limit = 2
        claim = 'the tests passed, nothing else to do'
        mock_llm = MockLLMProvider()
        mock_llm.responses = [claim] * 6
        agent.llm = mock_llm
        try:
            agent.run('task')
        except Exception:
            # A bounded failure may surface as an exception; what matters is that
            # the loop terminated instead of spinning on the same claim.
            pass
        self.assertLessEqual(mock_llm.calls, 4)
        history = '\n'.join(str(m.get('content', '')) for m in agent.history)
        self.assertIn('kept claiming', history)

    def test_compressed_observation_counts_as_real(self):
        agent = self._agent()
        self.assertFalse(agent._has_real_observation())
        agent.history.append({'role': 'user',
                              'content': 'COMPRESSED TOOL RESULT (loop_tool): kept for context'})
        self.assertTrue(agent._has_real_observation())
        self.assertIsNone(agent._observation_error('the tests passed', check_claims=True))

    def test_multiple_distinct_calls_rejected_end_to_end(self):
        both = _block(_call('read_file')) + _block(_call('write_file'))
        agent, result = self._run([both, 'plain final answer'])
        self.assertEqual(result, 'plain final answer')
        history = '\n'.join(str(m.get('content', '')) for m in agent.history)
        self.assertIn('ONE call per turn', history)

    def test_nested_open_tag_limitation_is_pinned(self):
        # Pre-existing limitation, unchanged by this round: when a call argument
        # quotes a literal OPEN tag, the parser starts from that inner tag, the
        # JSON cannot be recovered, and the reply is bounced as malformed. Pinned
        # here so a future fix must update this expectation deliberately.
        inner = 'quote ' + _TOOL_OPEN + ' inside'
        response = _block('{' + _Q + 'tool' + _Q + ': ' + _Q + 'create_file' + _Q + ', '
                          + _Q + 'args' + _Q + ': {' + _Q + 'content' + _Q + ': '
                          + _Q + inner + _Q + '}}')
        tool, err = Agent.validate_response(None, response)
        self.assertIsNone(tool)
        self.assertIn('Malformed tool block', err)
