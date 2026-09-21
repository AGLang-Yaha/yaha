#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

'''Tests for late binding of events: the `@placeholder` + `bind_tool` feature.

A placeholder (`@name`) is bound at runtime to a REAL event name by
`bind_tool(placeholder, real_event)`. The binding is run-global and 1:1, the
LAST bind wins, placeholder chaining is refused, and an UNBOUND placeholder in
either the wait position or a set_event_* position is a recoverable per-stage
FAILURE routed to that stage's Fail Target.
'''
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from yaha.core.agent import Agent
from yaha.core.engine import YAHAEngine
from yaha.core.runtime import (AgentRuntime, BindingRegistry, is_placeholder,
                               validate_wait_events)

HOME = Path(__file__).resolve().parent.parent
TMP = HOME / 'tmp'

# Assemble the harness tool-call markers from character codes so this file
# never contains a literal marker of its own.
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

FIXTURE_WAIT = '''
## Evt
- **Inherited:** None
- **Code:** Evt
- **Model:** m
- **Toolset:**
    - bash_tool
- **Preamble:**
    - **Wait Event:** @gate
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


def _make_agent(fixture=FIXTURE):
    TMP.mkdir(parents=True, exist_ok=True)
    ws = Path(tempfile.mkdtemp(prefix='yaha_bind_', dir=str(TMP)))
    yh = ws / '.yaha'
    yh.mkdir(parents=True)
    (yh / 'memory').mkdir()
    (yh / 'ext').mkdir()
    (yh / 'logs').mkdir()
    (yh / 'tool-schema.md').write_text('[]')
    (yh / 'AgentClass.md').write_text(fixture)
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

class RegistryTests(unittest.TestCase):
    def test_is_placeholder(self):
        self.assertTrue(is_placeholder('@x'))
        self.assertTrue(is_placeholder('  @gate '))
        self.assertFalse(is_placeholder('x'))
        self.assertFalse(is_placeholder(''))
        self.assertFalse(is_placeholder(None))

    def test_bind_and_resolve(self):
        reg = BindingRegistry()
        self.assertIsNone(reg.resolve('@x'))
        self.assertEqual(reg.bind('@x', 'real'), 'real')
        self.assertEqual(reg.resolve('@x'), 'real')
        self.assertIn('@x', reg)
        self.assertEqual(reg.names(), ['@x'])

    def test_last_bind_wins(self):
        reg = BindingRegistry()
        reg.bind('@x', 'first')
        reg.bind('@x', 'second')
        self.assertEqual(reg.resolve('@x'), 'second')

    def test_runtime_resolve_event_passes_plain_names_through(self):
        rt = AgentRuntime()
        self.assertEqual(rt.resolve_event('plain'), 'plain')
        self.assertIsNone(rt.resolve_event('@bound-later'))
        rt.bind_event('@gate', 'real-gate')
        self.assertEqual(rt.resolve_event('@gate'), 'real-gate')


class BindToolTests(unittest.TestCase):
    def test_requires_a_runtime(self):
        agent, eng, ws = _make_agent()
        try:
            agent.runtime = None
            out = agent.bind_tool('@gate', 'real')
            self.assertTrue(str(out).startswith('Error'))
            self.assertIn('runtime', str(out))
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_first_argument_must_be_a_placeholder(self):
        agent, eng, ws = _make_agent()
        try:
            agent.runtime = AgentRuntime(ui=agent.ui)
            out = agent.bind_tool('gate', 'real')
            self.assertTrue(str(out).startswith('Error'))
            self.assertIn('@', str(out))
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_second_argument_must_not_be_empty(self):
        agent, eng, ws = _make_agent()
        try:
            agent.runtime = AgentRuntime(ui=agent.ui)
            out = agent.bind_tool('@gate', '   ')
            self.assertTrue(str(out).startswith('Error'))
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_placeholder_chaining_is_refused(self):
        agent, eng, ws = _make_agent()
        try:
            rt = AgentRuntime(ui=agent.ui)
            agent.runtime = rt
            out = agent.bind_tool('@a', '@b')
            self.assertTrue(str(out).startswith('Error'))
            self.assertIn('chaining', str(out))
            self.assertEqual(rt.bindings.names(), [])
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_successful_bind_is_run_global_and_returns_confirmation(self):
        agent, eng, ws = _make_agent()
        try:
            rt = AgentRuntime(ui=agent.ui)
            agent.runtime = rt
            out = agent.bind_tool('@gate', 'real-gate')
            self.assertIn('BOUND_EVENT', str(out))
            self.assertEqual(rt.resolve_event('@gate'), 'real-gate')
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_resolve_event_name_reports_unbound_placeholder(self):
        agent, eng, ws = _make_agent()
        try:
            agent.runtime = AgentRuntime(ui=agent.ui)
            real, err = agent._resolve_event_name('plain')
            self.assertEqual(real, 'plain')
            self.assertIsNone(err)
            real, err = agent._resolve_event_name('@nope')
            self.assertIsNone(real)
            self.assertIn('unresolved event placeholder', err)
            agent.runtime.bind_event('@nope', 'yes')
            real, err = agent._resolve_event_name('@nope')
            self.assertEqual(real, 'yes')
            self.assertIsNone(err)
        finally:
            shutil.rmtree(ws, ignore_errors=True)

def _drain(agent, task='task', limit=60):
    '''Collect up to `limit` tokens from iter_run without exhausting it.'''
    tokens = []
    gen = agent.iter_run(task)
    try:
        for _ in range(limit):
            tokens.append(next(gen))
    except StopIteration:
        pass
    return tokens


def _entered(tokens, stage='Preamble'):
    return [t for t in tokens if t and t[0] == 'enter' and t[1] == stage]


def _history_text(agent):
    return ' '.join(str(m.get('content') or '') for m in (agent.history or []))


class WaitGateTests(unittest.TestCase):
    def test_unbound_placeholder_does_not_enter_the_stage(self):
        agent, eng, ws = _make_agent(FIXTURE_WAIT)
        try:
            agent.runtime = AgentRuntime(ui=agent.ui)
            agent.llm = _MockLLM([])
            tokens = _drain(agent)
            self.assertEqual(_entered(tokens), [])
            self.assertIn('unresolved event placeholder', _history_text(agent))
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_bound_placeholder_wait_consumes_the_real_event(self):
        agent, eng, ws = _make_agent(FIXTURE_WAIT)
        try:
            rt = AgentRuntime(ui=agent.ui)
            agent.runtime = rt
            rt.bind_event('@gate', 'real-gate')
            rt.set_event('real-gate')
            agent.llm = _MockLLM([])
            tokens = _drain(agent)
            self.assertEqual(len(_entered(tokens)), 1)
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_bound_placeholder_parks_on_the_real_event_name(self):
        agent, eng, ws = _make_agent(FIXTURE_WAIT)
        try:
            rt = AgentRuntime(ui=agent.ui)
            agent.runtime = rt
            rt.bind_event('@gate', 'real-gate')
            agent.llm = _MockLLM([])
            tokens = _drain(agent, limit=5)
            waits = [t for t in tokens if t and t[0] == 'wait']
            self.assertEqual(waits, [('wait', 'real-gate')])
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_plain_wait_event_is_unchanged(self):
        '''A literal Wait Event keeps the old behaviour exactly.'''
        agent, eng, ws = _make_agent(FIXTURE_WAIT.replace('@gate', 'gate'))
        try:
            rt = AgentRuntime(ui=agent.ui)
            agent.runtime = rt
            rt.set_event('gate')
            agent.llm = _MockLLM([])
            tokens = _drain(agent)
            self.assertEqual(len(_entered(tokens)), 1)
        finally:
            shutil.rmtree(ws, ignore_errors=True)


class ProducerSideTests(unittest.TestCase):
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

    def _run(self, agent, tool_name, args):
        agent.llm = _MockLLM([_tool_call(tool_name, args)])
        agent.history = [{'role': 'system', 'content': 's'}]
        return agent._run_react_loop(1, self._stage(), 'x')

    def test_bound_placeholder_signals_the_real_event(self):
        agent, eng, ws = _make_agent()
        try:
            rt = AgentRuntime(ui=agent.ui)
            agent.runtime = rt
            rt.bind_event('@go', 'go')
            resp, status, turn = self._run(
                agent, 'set_event_success', {'name': '@go', 'summary': 'left'})
            self.assertEqual(status, 'DONE')
            self.assertTrue(rt.events.is_set('go'))
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_unbound_placeholder_fails_the_stage(self):
        agent, eng, ws = _make_agent()
        try:
            agent.runtime = AgentRuntime(ui=agent.ui)
            resp, status, turn = self._run(
                agent, 'set_event_success', {'name': '@go', 'summary': 'left'})
            self.assertTrue(str(status).startswith('FAIL'), status)
            self.assertIn('unresolved event placeholder', str(resp))
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_unbound_placeholder_fails_via_loop_and_jump_too(self):
        for tool_name, args in (('set_event_loop', {'name': '@go'}),
                                ('set_event_jump', {'name': '@go', 'target': 'Aftermath'})):
            agent, eng, ws = _make_agent()
            try:
                agent.runtime = AgentRuntime(ui=agent.ui)
                resp, status, turn = self._run(agent, tool_name, args)
                self.assertTrue(str(status).startswith('FAIL'), (tool_name, status))
                self.assertIn('unresolved event placeholder', str(resp))
            finally:
                shutil.rmtree(ws, ignore_errors=True)

class SingleAgentTests(unittest.TestCase):
    def test_wait_on_placeholder_fails_without_raising(self):
        '''No runtime => nothing can bind, so the wait fails the stage entry.'''
        agent, eng, ws = _make_agent(FIXTURE_WAIT)
        try:
            agent.runtime = None
            agent.llm = _MockLLM([])
            tokens = _drain(agent)
            self.assertEqual(_entered(tokens), [])
            self.assertIn('unresolved event placeholder', _history_text(agent))
        finally:
            shutil.rmtree(ws, ignore_errors=True)


class _Cfg:
    '''Minimal stand-in for an AgentClassConfig (validation reads two fields).'''

    def __init__(self, chain, system_prompt=''):
        self.prompt_chain = chain
        self.system_prompt = system_prompt


def _vstage(name, wait_event=None, content=''):
    stage = {'name': name, 'content': content}
    if wait_event:
        stage['wait_event'] = wait_event
    return stage


class ValidationTests(unittest.TestCase):
    def test_no_placeholder_keeps_the_old_typo_net(self):
        cfg = _Cfg([_vstage('Preamble', wait_event='ghost')])
        self.assertEqual(validate_wait_events({'C': cfg}), ['ghost'])

    def test_literal_producer_satisfies_a_plain_wait(self):
        cfg = _Cfg([_vstage('Preamble', wait_event='go',
                            content="set_event_success('', 'go')")])
        self.assertEqual(validate_wait_events({'C': cfg}), [])

    def test_placeholder_producer_accepts_an_unproduced_wait(self):
        cfg = _Cfg([_vstage('Preamble', wait_event='go')],
                   system_prompt="set_event_success('', '@g') binds go")
        self.assertEqual(validate_wait_events({'C': cfg}), [])

    def test_placeholder_wait_is_always_exempt(self):
        cfg = _Cfg([_vstage('Preamble', wait_event='@gate')])
        self.assertEqual(validate_wait_events({'C': cfg}), [])


class SchemaTests(unittest.TestCase):
    def test_bind_tool_is_declared_in_the_shipped_schema(self):
        from yaha.config.tools import load_tool_schema
        path = HOME / 'yaha' / 'templates' / 'tool-schema.md'
        tools = load_tool_schema(str(path))
        matches = [t for t in tools if t.get('name') == 'bind_tool']
        self.assertEqual(len(matches), 1)
        params = matches[0]['parameters']
        self.assertEqual(sorted(params['required']),
                         ['placeholder', 'real_event'])
        self.assertIn('placeholder', params['properties'])
        self.assertIn('real_event', params['properties'])

    def test_bind_tool_is_not_a_framework_tool(self):
        '''It must stay opt-in: the Toolset privilege cap must keep applying.'''
        agent, eng, ws = _make_agent()
        try:
            self.assertIn('bind_tool', agent.tool_map)
            self.assertNotIn('bind_tool', agent.config.toolset)
        finally:
            shutil.rmtree(ws, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
