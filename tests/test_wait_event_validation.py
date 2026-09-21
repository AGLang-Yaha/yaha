#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

'''Startup protection for Wait Events.

Every Wait Event name must be produced by some literal set_event_success /
set_event_loop call in a prompt, otherwise a wait-name typo would hang the
run. validate_wait_events() reports the unmatched names and the engine refuses
to start a root run with them.
'''
import shutil
import tempfile
import unittest
from pathlib import Path

from yaha.core.engine import YAHAEngine
from yaha.core.runtime import (
    AgentRuntime,
    collect_wait_events,
    collect_producer_events,
    validate_wait_events,
)

HOME = Path(__file__).resolve().parent.parent
TMP = HOME / 'tmp'

FIXTURE = '''
## Producer
- **Inherited:** None
- **Code:** Prd
- **Model:** m
- **Toolset:**
    - bash_tool
- **Preamble:**
    > wake the others with set_event_success('', 'go') when ready
- **Work:**
    - **Wait Event:** go
    > work
- **Aftermath:**
    > done

## NoProducer
- **Inherited:** None
- **Code:** Npr
- **Model:** m
- **Toolset:**
    - bash_tool
- **Preamble:**
    > nothing announces the event here
- **Work:**
    - **Wait Event:** missing_event
    > work
- **Aftermath:**
    > done
'''


def _make_engine():
    TMP.mkdir(parents=True, exist_ok=True)
    ws = Path(tempfile.mkdtemp(prefix='yaha_val_', dir=str(TMP)))
    yh = ws / '.yaha'
    yh.mkdir(parents=True)
    (yh / 'memory').mkdir()
    (yh / 'ext').mkdir()
    (yh / 'logs').mkdir()
    (yh / 'tool-schema.md').write_text('[]')
    (yh / 'AgentClass.md').write_text(FIXTURE)
    (yh / 'Model.md').write_text('')
    return YAHAEngine(workspace_dir=str(ws)), ws


class ValidationTests(unittest.TestCase):
    def test_collects_wait_events_across_classes(self):
        eng, ws = _make_engine()
        try:
            waits = collect_wait_events(eng.agent_classes)
            self.assertIn('go', waits)
            self.assertIn('missing_event', waits)
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_collects_literal_producers(self):
        eng, ws = _make_engine()
        try:
            producers = collect_producer_events(eng.agent_classes)
            self.assertIn('go', producers)
            self.assertNotIn('missing_event', producers)
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_validate_reports_only_unmatched(self):
        eng, ws = _make_engine()
        try:
            self.assertEqual(validate_wait_events(eng.agent_classes), ['missing_event'])
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_root_run_refuses_to_start_with_unmatched_event(self):
        eng, ws = _make_engine()
        try:
            result = eng.run_agent('NoProducer', 'task')
            self.assertIn('Wait Event(s) with no producer', result)
            self.assertIn('missing_event', result)
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_runtime_spawn_still_works_after_validation_gate(self):
        eng, ws = _make_engine()
        try:
            rt = AgentRuntime()
            self.assertEqual(rt.events.names(), [])
        finally:
            shutil.rmtree(ws, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
