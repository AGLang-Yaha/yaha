#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

'''Multi-agency in unattended / non-TTY runs.

The event/wait machinery is deterministic and needs no TTY; only the TUI tag
layer is TTY-specific and is simply skipped. A root run must complete without
ever blocking on input when stdin is not a TTY, and a spawned peer must run to
completion before the runtime ends.
'''
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from yaha.core.engine import YAHAEngine

HOME = Path(__file__).resolve().parent.parent
TMP = HOME / 'tmp'

_LT = chr(60)
_GT = chr(62)
_TOOL_OPEN = _LT + 'tool_code' + _GT
_TOOL_CLOSE = _LT + '/tool_code' + _GT

FIXTURE = '''
## Root
- **Inherited:** None
- **Code:** Roo
- **Model:** m
- **Toolset:**
    - bash_tool
    - spawn_parallel_agent
- **Preamble:**
    > start and spawn a peer agent
- **Aftermath:**
    > finish

### Agentset
- Child

## Child
- **Inherited:** None
- **Code:** Chd
- **Model:** m
- **Toolset:**
    - bash_tool
- **Preamble:**
    > child work
- **Aftermath:**
    > child done
'''


class _NonTTY:
    def isatty(self):
        return False

    def readline(self):
        return ''

    def fileno(self):
        raise OSError('no fileno')


class _FakeProvider:
    script = []
    calls = 0
    usage = {'prompt_tokens': 1, 'completion_tokens': 1}

    def __init__(self, cfg=None):
        pass

    def generate(self, messages, **kwargs):
        _FakeProvider.calls += 1
        if _FakeProvider.script:
            return _FakeProvider.script.pop(0), dict(_FakeProvider.usage), None
        return 'ok', dict(_FakeProvider.usage), None


def _make_engine():
    TMP.mkdir(parents=True, exist_ok=True)
    ws = Path(tempfile.mkdtemp(prefix='yaha_una_', dir=str(TMP)))
    yh = ws / '.yaha'
    yh.mkdir(parents=True)
    (yh / 'memory').mkdir()
    (yh / 'ext').mkdir()
    (yh / 'logs').mkdir()
    (yh / 'tool-schema.md').write_text('[]')
    (yh / 'AgentClass.md').write_text(FIXTURE)
    (yh / 'Model.md').write_text('')
    return YAHAEngine(workspace_dir=str(ws)), ws


def _spawn_call():
    payload = {'tool': 'spawn_parallel_agent',
               'args': {'agent_class': 'Child',
                        'clone_context': 'new',
                        'prompt': 'child task'}}
    return _TOOL_OPEN + json.dumps(payload) + _TOOL_CLOSE


def _boom(*args, **kwargs):
    raise AssertionError('input() must not be called in an unattended run')


class UnattendedRootRunTests(unittest.TestCase):
    def setUp(self):
        _FakeProvider.script = []
        _FakeProvider.calls = 0

    def test_root_run_completes_non_tty_without_prompting(self):
        eng, ws = _make_engine()
        try:
            with patch('yaha.llm.ollama.OllamaProvider', _FakeProvider):
                with patch('sys.stdin', _NonTTY()):
                    with patch('builtins.input', _boom):
                        result = eng.run_agent('Root', 'do the task')
            self.assertIsInstance(result, str)
            self.assertIn('ok', result)
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_unattended_root_spawns_peer_and_peer_finishes(self):
        eng, ws = _make_engine()
        try:
            _FakeProvider.script = [_spawn_call()]
            with patch('yaha.llm.ollama.OllamaProvider', _FakeProvider):
                with patch('sys.stdin', _NonTTY()):
                    with patch('builtins.input', _boom):
                        result = eng.run_agent('Root', 'do the task')
            self.assertIsInstance(result, str)
            # root Preamble (spawn + finish) , root Aftermath, child Preamble and
            # child Aftermath all call the provider -> the peer really ran.
            self.assertGreaterEqual(_FakeProvider.calls, 4)
        finally:
            shutil.rmtree(ws, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
