#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

'''Tests for the set_event_success / set_event_loop stage-exit tools.

They are framework tools (always callable, exempt from the stage exit-tool
list) that set a named runtime event and then finish the stage like
success_tool / loop_tool. Built over a real Agent and a scripted LLM.
'''
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from yaha.core.agent import Agent
from yaha.core.engine import YAHAEngine
from yaha.core.runtime import AgentRuntime

HOME = Path(__file__).resolve().parent.parent
TMP = HOME / 'tmp'

# The harness tool-call markers are assembled from character codes so this
# file never contains a literal marker of its own.
_LT = chr(60)
_GT = chr(62)
_TOOL_OPEN = _LT + 'tool_code' + _GT
_TOOL_CLOSE = _LT + '/tool_code' + _GT

FIXTURE = '''
## Evt
- **Inherited:** None
- **Code:** Evt
- **Model:** m
- **Toolset:**
    - bash_tool
- **Preamble:**
    > do the thing
- **Aftermath:**
    > done
'''


def _tool_call(name, args):
    return _TOOL_OPEN + json.dumps({'tool': name, 'args': args}) + _TOOL_CLOSE


class _MockLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.usage = {'prompt_tokens': 5, 'completion_tokens': 5}

    def generate(self, messages, **kwargs):
        if self.calls < len(self.responses):
            resp = self.responses[self.calls]
        else:
            resp = 'ok'
        self.calls += 1
        return resp, dict(self.usage), None


def _make_agent():
    TMP.mkdir(parents=True, exist_ok=True)
    ws = Path(tempfile.mkdtemp(prefix='yaha_evt_', dir=str(TMP)))
    yh = ws / '.yaha'
    yh.mkdir(parents=True)
    (yh / 'memory').mkdir()
    (yh / 'ext').mkdir()
    (yh / 'logs').mkdir()
    (yh / 'tool-schema.md').write_text('[]')
    (yh / 'AgentClass.md').write_text(FIXTURE)
    (yh / 'Model.md').write_text('')
    eng = YAHAEngine(workspace_dir=str(ws))
    cfg = eng.agent_classes['Evt']
    agent = Agent(
        agent_config=cfg,
        session=eng.session,
        models_config=eng.models_config,
        agent_classes=eng.agent_classes,
        tool_schemas=eng.tool_schemas,
        access_controller=eng.access_controller,
    )
    return agent, eng, ws


class ToolMethodTests(unittest.TestCase):
    def test_set_event_success_sets_event_and_returns_sentinel(self):
        agent, eng, ws = _make_agent()
        try:
            rt = AgentRuntime(ui=agent.ui)
            agent.runtime = rt
            self.assertEqual(agent.set_event_success('', 'go'), 'SET_EVENT_SUCCESS_SIGNALED')
            self.assertTrue(rt.events.is_set('go'))
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_set_event_loop_sets_event_and_returns_sentinel(self):
        agent, eng, ws = _make_agent()
        try:
            rt = AgentRuntime(ui=agent.ui)
            agent.runtime = rt
            self.assertEqual(agent.set_event_loop(summary='', names=['again']), 'LOOP_SIGNALED')
            self.assertTrue(rt.events.is_set('again'))
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_tools_are_no_ops_without_a_runtime(self):
        agent, eng, ws = _make_agent()
        try:
            agent.runtime = None
            self.assertEqual(agent.set_event_success('', 'go'), 'SET_EVENT_SUCCESS_SIGNALED')
            self.assertEqual(agent.set_event_loop(summary='', names=['go']), 'LOOP_SIGNALED')
            self.assertEqual(agent.set_event_success(None), 'SET_EVENT_SUCCESS_SIGNALED')
        finally:
            shutil.rmtree(ws, ignore_errors=True)


class ReactLoopTests(unittest.TestCase):
    def _stage(self):
        return {
            'name': 'Preamble',
            'content': 'x',
            'allowed_tools': [],
            'required_tools': [],
            'exit_tools': [],
            'fail_target': None,
            'max_turns': None,
            'wait_event': None,
        }

    def test_set_event_success_terminates_stage_and_sets_event(self):
        agent, eng, ws = _make_agent()
        try:
            rt = AgentRuntime(ui=agent.ui)
            agent.runtime = rt
            agent.llm = _MockLLM([_tool_call('set_event_success', {'name': 'go', 'summary': 'left'})])
            agent.history = [{'role': 'system', 'content': 's'}]
            resp, status, turn = agent._run_react_loop(1, self._stage(), 'x')
            self.assertEqual(status, 'DONE')
            self.assertTrue(rt.events.is_set('go'))
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_set_event_loop_returns_loop_status(self):
        agent, eng, ws = _make_agent()
        try:
            rt = AgentRuntime(ui=agent.ui)
            agent.runtime = rt
            agent.llm = _MockLLM([_tool_call('set_event_loop', {'name': 'looped'})])
            agent.history = [{'role': 'system', 'content': 's'}]
            resp, status, turn = agent._run_react_loop(1, self._stage(), 'x')
            self.assertEqual(status, 'LOOP')
            self.assertTrue(rt.events.is_set('looped'))
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_full_run_sets_event_and_completes(self):
        agent, eng, ws = _make_agent()
        try:
            rt = AgentRuntime(ui=agent.ui)
            agent.runtime = rt
            agent.llm = _MockLLM([
                _tool_call('set_event_success', {'name': 'ready'}),
                'aftermath done',
            ])
            result = agent.run('task')
            self.assertTrue(rt.events.is_set('ready'))
            self.assertIn('aftermath done', result)
        finally:
            shutil.rmtree(ws, ignore_errors=True)



def _t_missing_name_is_error_with_runtime(self):
    agent, eng, ws = _make_agent()
    try:
        rt = AgentRuntime(ui=agent.ui)
        agent.runtime = rt
        self.assertTrue(agent.set_event_success(None).startswith('Error'))
        self.assertTrue(agent.set_event_loop(summary='').startswith('Error'))
        self.assertEqual(rt.events.names(), [])
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def _t_missing_name_does_not_complete_stage(self):
    agent, eng, ws = _make_agent()
    try:
        rt = AgentRuntime(ui=agent.ui)
        agent.runtime = rt
        agent.llm = _MockLLM([
            _tool_call('set_event_success', {'summary': 'x'}),
            _tool_call('success_tool', {'summary': 'ok'}),
        ])
        agent.history = [{'role': 'system', 'content': 's'}]
        stage = {'name': 'Preamble', 'content': 'x', 'allowed_tools': [],
                 'required_tools': [], 'exit_tools': ['success_tool'],
                 'fail_target': None, 'max_turns': None, 'wait_event': None}
        resp, status, turn = agent._run_react_loop(1, stage, 'x')
        self.assertEqual(status, 'DONE')
        self.assertEqual(rt.events.names(), [])
    finally:
        shutil.rmtree(ws, ignore_errors=True)


ToolMethodTests.test_missing_name_is_error_with_runtime = _t_missing_name_is_error_with_runtime
ReactLoopTests.test_missing_name_does_not_complete_stage = _t_missing_name_does_not_complete_stage



def _t_empty_args_dict_gives_the_friendly_error(self):
    agent, eng, ws = _make_agent()
    try:
        rt = AgentRuntime(ui=agent.ui)
        agent.runtime = rt
        agent.llm = _MockLLM([
            _tool_call('set_event_success', {}),
            _tool_call('success_tool', {'summary': 'ok'}),
        ])
        agent.history = [{'role': 'system', 'content': 's'}]
        stage = {'name': 'Preamble', 'content': 'x', 'allowed_tools': [],
                 'required_tools': [], 'exit_tools': ['success_tool'],
                 'fail_target': None, 'max_turns': None, 'wait_event': None}
        resp, status, turn = agent._run_react_loop(1, stage, 'x')
        self.assertEqual(status, 'DONE')
        self.assertEqual(rt.events.names(), [])
        blob = ' '.join(str(m.get('content') or '') for m in agent.history)
        self.assertIn('requires a non-empty', blob)
        self.assertNotIn('unexpected keyword argument', blob)
    finally:
        shutil.rmtree(ws, ignore_errors=True)


ReactLoopTests.test_empty_args_dict_gives_the_friendly_error = _t_empty_args_dict_gives_the_friendly_error


class SetEventJumpTests(unittest.TestCase):
    '''set_event_jump: set events, then move the chain to a target stage.

    The tool produces the SAME EXIT_TO status as exit_to, with the target
    validated BEFORE the events are set, so a bad target (or a self-jump)
    keeps the stage alive and fires nothing.
    '''

    FIXTURE_JUMP = '''
## Jmp
- **Inherited:** None
- **Code:** Jmp
- **Model:** m
- **Toolset:**
    - bash_tool
- **Preamble:**
    > do the thing
- **Second:**
    > next thing
- **Aftermath:**
    > done
'''

    def _make_jump_agent(self):
        TMP.mkdir(parents=True, exist_ok=True)
        ws = Path(tempfile.mkdtemp(prefix='yaha_jmp_', dir=str(TMP)))
        yh = ws / '.yaha'
        yh.mkdir(parents=True)
        (yh / 'memory').mkdir()
        (yh / 'ext').mkdir()
        (yh / 'logs').mkdir()
        (yh / 'tool-schema.md').write_text('[]')
        (yh / 'AgentClass.md').write_text(self.FIXTURE_JUMP)
        (yh / 'Model.md').write_text('')
        eng = YAHAEngine(workspace_dir=str(ws))
        cfg = eng.agent_classes['Jmp']
        agent = Agent(
            agent_config=cfg,
            session=eng.session,
            models_config=eng.models_config,
            agent_classes=eng.agent_classes,
            tool_schemas=eng.tool_schemas,
            access_controller=eng.access_controller,
        )
        return agent, eng, ws

    def _stage(self, name='Preamble'):
        return {
            'name': name,
            'content': 'x',
            'allowed_tools': [],
            'required_tools': [],
            'exit_tools': [],
            'fail_target': None,
            'max_turns': None,
            'wait_event': None,
        }

    def test_jump_sets_events_and_returns_exit_to_status(self):
        agent, eng, ws = self._make_jump_agent()
        try:
            rt = AgentRuntime(ui=agent.ui)
            agent.runtime = rt
            agent.llm = _MockLLM([_tool_call(
                'set_event_jump',
                {'summary': 'a summary', 'target': 'Second', 'names': ['go']})])
            agent.history = [{'role': 'system', 'content': 's'}]
            resp, status, turn = agent._run_react_loop(1, self._stage(), 'x')
            self.assertEqual(status, 'EXIT_TO:Second')
            self.assertTrue(rt.events.is_set('go'))
            self.assertEqual(resp, 'a summary')
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_self_jump_is_refused_and_sets_nothing(self):
        agent, eng, ws = self._make_jump_agent()
        try:
            rt = AgentRuntime(ui=agent.ui)
            agent.runtime = rt
            agent.llm = _MockLLM([
                _tool_call('set_event_jump',
                           {'target': 'Preamble', 'names': ['go']}),
                _tool_call('success_tool', {'summary': 'ok'}),
            ])
            agent.history = [{'role': 'system', 'content': 's'}]
            resp, status, turn = agent._run_react_loop(1, self._stage(), 'x')
            self.assertEqual(status, 'DONE')
            self.assertEqual(rt.events.names(), [])
            blob = ' '.join(str(m.get('content') or '') for m in agent.history)
            self.assertIn('CURRENT stage', blob)
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_unknown_target_is_refused_and_sets_nothing(self):
        agent, eng, ws = self._make_jump_agent()
        try:
            rt = AgentRuntime(ui=agent.ui)
            agent.runtime = rt
            agent.llm = _MockLLM([
                _tool_call('set_event_jump',
                           {'target': 'Nowhere', 'names': ['go']}),
                _tool_call('success_tool', {'summary': 'ok'}),
            ])
            agent.history = [{'role': 'system', 'content': 's'}]
            resp, status, turn = agent._run_react_loop(1, self._stage(), 'x')
            self.assertEqual(status, 'DONE')
            self.assertEqual(rt.events.names(), [])
            blob = ' '.join(str(m.get('content') or '') for m in agent.history)
            self.assertIn('neither a stage', blob)
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_missing_target_is_an_error_and_sets_nothing(self):
        agent, eng, ws = self._make_jump_agent()
        try:
            rt = AgentRuntime(ui=agent.ui)
            agent.runtime = rt
            self.assertTrue(agent.set_event_jump(
                summary='', names=['go']).startswith('Error'))
            self.assertEqual(rt.events.names(), [])
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_missing_event_name_is_an_error(self):
        agent, eng, ws = self._make_jump_agent()
        try:
            rt = AgentRuntime(ui=agent.ui)
            agent.runtime = rt
            self.assertTrue(agent.set_event_jump(
                summary='', target='Second').startswith('Error'))
            self.assertEqual(rt.events.names(), [])
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_full_run_jumps_and_curates_the_stage_result(self):
        agent, eng, ws = self._make_jump_agent()
        try:
            rt = AgentRuntime(ui=agent.ui)
            agent.runtime = rt
            agent.llm = _MockLLM([
                _tool_call('set_event_jump',
                           {'summary': 'jumping', 'target': 'Second',
                            'names': ['go']}),
                'second stage answer',
                'aftermath done',
            ])
            result = agent.run('task')
            self.assertTrue(rt.events.is_set('go'))
            self.assertIn('aftermath done', result)
            blob = ' '.join(str(m.get('content') or '') for m in agent.history)
            self.assertIn('second stage answer', blob)
            # No prompt prefix: the jump summary never becomes a stage prompt.
            prompts = [str(m.get('content') or '') for m in agent.history
                       if m.get('role') == 'user']
            self.assertFalse(
                [p for p in prompts if p.startswith('jumping')],
                'the jump summary must never become a prompt prefix')
        finally:
            shutil.rmtree(ws, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
