#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

'''SNAP MARKER and the compression-handler event-signal ban.'''
import os
import shutil
import tempfile
import unittest

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

    def _call(self, ag, tool, names):
        return ag._execute_tool_with_hooks(
            {}, tool, {'names': names}, 1, 1, 'Aftermath', exits_stage=True)

    def test_event_tool_refused_while_compressing(self):
        ag = self._agent()
        ag._compressing = True
        res, failure, ran = self._call(ag, 'set_event_success', ['code_ready'])
        self.assertTrue(str(res).startswith('Error'), res)
        self.assertIn('compression', str(res))
        self.assertEqual(ag.runtime.calls, [])

    def test_loop_and_jump_refused_while_compressing(self):
        ag = self._agent()
        ag._compressing = True
        for tool, args in (('set_event_loop', {'names': ['review_done']}),
                           ('set_event_jump', {'names': ['commit_ready'],
                                               'target': 'Aftermath'})):
            res, failure, ran = ag._execute_tool_with_hooks(
                {}, tool, args, 1, 1, 'Aftermath', exits_stage=True)
            self.assertTrue(str(res).startswith('Error'), (tool, res))
        self.assertEqual(ag.runtime.calls, [])

    def test_event_tool_fires_when_not_compressing(self):
        ag = self._agent()
        ag._compressing = False
        self._call(ag, 'set_event_success', ['code_ready'])
        self.assertEqual(ag.runtime.calls, ['code_ready'])

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

    def test_mark_snap_covers_the_l2_cold_restart_phase(self):
        ag = self._agent()
        ag._mark_snap('Xpa', 'l2-cold-restart')
        self.assertEqual(ag._snap_marker['phase'], 'l2-cold-restart')

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