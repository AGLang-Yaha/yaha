#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

'''SNAP MARKER plus the AGLang stage-level event-signal gate.'''
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock

from yaha.cli import init_workspace
from yaha.core.engine import YAHAEngine
from yaha.core.agent import Agent

TOOLS = '### Toolset\n\t- read_file\n'
MD = '\n'.join([
    '## Xpa', '- **Code:** Xpa', '- **Model:** DefaultModel',
    '- **System Prompt:** > sys prom',
    '- **A:**', '    - **Exit Tools:**', '        - set_event_success',
    '    > STAGE_PROMPT_A',
]) + '\n' + TOOLS


class _Rt:
    def __init__(self):
        self.calls = []

    def set_event(self, name):
        self.calls.append(str(name))
        return True

    def consume_event(self, name):
        return False

    def resolve_event(self, name):
        return str(name or '')

    def record_blocked_signal(self, *a, **k):
        pass


class SnapSignalGuardTests(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp(prefix='yaha_snapguard_')
        d = os.path.join(self.ws, '.yaha')
        for sub in ('memory', 'ext', 'logs'):
            os.makedirs(os.path.join(d, sub))
        open(os.path.join(d, 'tool-schema.md'), 'w').write('[]')
        open(os.path.join(d, 'Model.md'), 'w').write('')
        open(os.path.join(d, 'AgentClass.md'), 'w').write(MD)

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def _agent(self):
        eng = YAHAEngine(workspace_dir=self.ws)
        cfg = eng.agent_classes.get('Xpa')
        ag = Agent(agent_config=cfg, session=eng.session,
                   models_config=eng.models_config,
                   agent_classes=eng.agent_classes,
                   tool_schemas=eng.tool_schemas,
                   access_controller=eng.access_controller)
        ag.unattended = True
        ag.runtime = _Rt()
        return ag

    def _shipped_agent(self):
        """BaseAgent built from the SHIPPED templates via the real init flow,
        so template, include/macro expansion and stage parsing are all under test."""
        ws = tempfile.mkdtemp(prefix='yaha_shipped_')
        self.addCleanup(shutil.rmtree, ws, ignore_errors=True)
        cwd = os.getcwd()
        os.chdir(ws)
        try:
            init_workspace()
        finally:
            os.chdir(cwd)
        eng = YAHAEngine(workspace_dir=ws)
        cfg = eng.agent_classes.get('BaseAgent')
        ag = Agent(agent_config=cfg, session=eng.session,
                   models_config=eng.models_config,
                   agent_classes=eng.agent_classes,
                   tool_schemas=eng.tool_schemas,
                   access_controller=eng.access_controller)
        ag.unattended = True
        ag.runtime = _Rt()
        return ag

    def _call(self, ag, tool, names):
        return ag._execute_tool_with_hooks(
            {}, tool, {'names': names}, 1, 1, 'Aftermath', exits_stage=True)

    def _llm(self, reply, then='done'):
        calls = {'n': 0}

        def fake(messages, context_size=None, **kw):
            calls['n'] += 1
            if calls['n'] > 8:
                raise RuntimeError('react loop did not terminate')
            return (reply if calls['n'] == 1 else then), {}, None

        llm = MagicMock()
        llm.generate = MagicMock(side_effect=fake)
        return llm

    def _gate_stage(self, allowed):
        return {
            'name': 'A', 'prompt': 'STAGE_PROMPT_A',
            'required_tools': [], 'exit_tools': [],
            'allowed_tools': list(allowed), 'detached': False,
            'final_answer': True,
        }

    def _event_code(self, name):
        tag_open = '<tool' + '_code>'
        tag_close = '</tool' + '_code>'
        payload = {'tool': 'set_event_success', 'args': {'names': [name]}}
        return tag_open + json.dumps(payload) + tag_close

    def test_stage_gate_refuses_the_event_tool(self):
        ag = self._agent()
        ag.llm = self._llm(self._event_code('code_ready'))
        ag._run_react_loop(1, self._gate_stage(['read_file']), 'x')
        self.assertEqual(ag.runtime.calls, [])
        joined = ' '.join(str(m.get('content', '')) for m in ag.history)
        self.assertIn('is not allowed in stage', joined)

    def test_stage_without_allowed_tools_fires_the_event_tool(self):
        ag = self._agent()
        ag.llm = self._llm(self._event_code('code_ready'))
        ag._run_react_loop(1, self._gate_stage([]), 'x')
        self.assertEqual(ag.runtime.calls, ['code_ready'])

    def test_shipped_compression_config_excludes_event_tools(self):
        # Parsed config, not markdown text: fails if the template drops the
        # Allowed Tools list OR the detached-stage parser stops populating it.
        ag = self._shipped_agent()
        comp = (ag.config.detached_stages or {}).get('Compression')
        self.assertIsNotNone(comp)
        allowed = set(comp.get('allowed_tools') or [])
        self.assertIn('read_file', allowed)
        for tool in ('set_event_success', 'set_event_loop', 'set_event_jump'):
            self.assertNotIn(tool, allowed)

    def test_snap_handler_refuses_event_tools_end_to_end(self):
        # The REAL handler path: shipped template -> parsed detached
        # Compression stage -> _maybe_snap -> _run_react_loop -> gate.
        ag = self._shipped_agent()
        seen = []

        def fake(messages, context_size=None, **kw):
            # The snap REPLACES history on success, so the refusal would be
            # gone afterwards; capture what the handler is fed while it runs.
            seen.append([str(m.get('content', '')) for m in messages])
            if len(seen) > 8:
                raise RuntimeError('react loop did not terminate')
            if len(seen) == 1:
                return self._event_code('code_ready'), {}, None
            return 'GOAL: probe\nCOMPLETED: nothing', {}, None

        llm = MagicMock()
        llm.generate = MagicMock(side_effect=fake)
        ag.llm = llm
        ag._maybe_snap({'name': 'Coding'}, 1, force=True)
        # The event tool must NEVER fire inside the snap handler...
        self.assertEqual(ag.runtime.calls, [])
        # ...the refusal is fed back to the handler while it runs...
        self.assertTrue(any('is not allowed in stage' in c
                            for turn_msgs in seen for c in turn_msgs))
        self.assertEqual(ag._last_tool_result, ('set_event_success', 'ERROR'))
        # ...and the handler still completes the snap with a plain-text digest.
        self.assertTrue(ag._last_snap_rebuilt)
        self.assertEqual(ag._snap_marker['stage'], 'Coding')

    def test_mark_snap_records_and_surfaces(self):
        ag = self._agent()
        ag._stage_signalled_events = {'code_ready'}
        text = ag._mark_snap('Coding', 'mid-stage')
        self.assertIn('[SNAP MARKER]', text)
        self.assertIn('code_ready', text)
        self.assertEqual(ag._snap_marker['stage'], 'Coding')
        self.assertEqual(ag._snap_marker['phase'], 'mid-stage')
        self.assertEqual(ag._snap_marker['signalled'], ['code_ready'])
        joined = '\n'.join(str(m.get('content', '')) for m in ag.history)
        self.assertIn('[SNAP MARKER]', joined)

    def test_l2_cold_restart_marks_the_marker(self):
        ag = self._agent()
        ag.build_system_prompt = MagicMock(return_value='SYS')
        ag._reflect_for_compression = MagicMock(return_value=('DIGEST', 1))
        ag._l1_sufficient = MagicMock(return_value=False)
        restarted, turn = ag._compress_context({'name': 'Xpa'}, 'x', 1)
        self.assertTrue(restarted)
        self.assertEqual(ag._snap_marker['phase'], 'l2-cold-restart')
        self.assertEqual(ag._snap_marker['stage'], 'Xpa')

    def test_status_dump_surfaces_the_snap_marker(self):
        ag = self._agent()
        self.assertIn('snap', ag._venue_status_report({}, 1, set()))
        ag._snap_marker = {'when': '2026-01-02 03:04:05', 'stage': 'Coding',
                           'phase': 'mid-stage', 'signalled': ['code_ready']}
        dump = ag._venue_status_report({}, 1, set())
        self.assertIn('snap', dump)
        self.assertIn('Coding', dump)
        self.assertIn('code_ready', dump)


if __name__ == '__main__':
    unittest.main()
