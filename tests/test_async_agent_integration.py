#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

# Agent-level wiring tests for the async tools (async_bash / poll_async).

import pathlib
import threading
import time
import unittest

from yaha.config.tools import load_tool_schema
from yaha.core.agent import Agent
from yaha.core.async_jobs import AsyncJobRegistry, DONE, ERROR


class _FakeUI:
    def display_tool_result(self, *args):
        pass


class _Cfg:
    name = 'T'


def _bare_agent(runtime=None):
    ag = Agent.__new__(Agent)
    ag.runtime = runtime
    ag._async_jobs_local = None
    ag.history = []
    ag.ui = _FakeUI()
    ag.config = _Cfg()
    ag.agent_id = 'a1'
    return ag


def _await_status(job, status, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if job.status == status:
            return True
        time.sleep(0.01)
    return job.status == status


class AsyncAgentWiringTests(unittest.TestCase):
    def test_local_registry_outside_a_run(self):
        ag = _bare_agent()
        self.assertIsInstance(ag._async_registry(), AsyncJobRegistry)

    def test_runtime_registry_used_inside_a_run(self):
        class _Runtime:
            def __init__(self):
                self.async_jobs = AsyncJobRegistry()
        rt = _Runtime()
        ag = _bare_agent(runtime=rt)
        self.assertIs(ag._async_registry(), rt.async_jobs)

    def test_drain_appends_finished_result_once(self):
        ag = _bare_agent()
        job = ag._async_registry().start(
            'x', runner=lambda j: (DONE, 'ok-drain', None), owner=ag.agent_id)
        self.assertTrue(_await_status(job, DONE))
        self.assertEqual(ag._drain_async_results(), 1)
        content = ag.history[0]['content']
        self.assertIn('ASYNC BASH RESULT', content)
        self.assertIn('ok-drain', content)
        self.assertEqual(ag._drain_async_results(), 0)
        self.assertEqual(len(ag.history), 1)

    def test_drain_ignores_running_job(self):
        ag = _bare_agent()
        gate = threading.Event()
        ag._async_registry().start(
            'x', runner=lambda j: (gate.wait(5), (DONE, 'late', None))[1])
        self.assertEqual(ag._drain_async_results(), 0)
        self.assertEqual(ag.history, [])
        gate.set()

    def test_drain_marks_error_status(self):
        ag = _bare_agent()

        def boom(job):
            raise RuntimeError('nope')

        job = ag._async_registry().start('x', runner=boom, owner=ag.agent_id)
        self.assertTrue(_await_status(job, ERROR))
        ag._drain_async_results()
        self.assertIn('ERROR', ag.history[0]['content'])
        self.assertIn('nope', ag.history[0]['content'])

    def test_schema_loads_both_new_tools(self):
        names = [s.get('name') for s in load_tool_schema('.yaha/tool-schema.md')]
        self.assertIn('async_bash', names)
        self.assertIn('poll_async', names)
        self.assertNotIn('wait_async', names)




class OwnerScopeTests(unittest.TestCase):
    def test_drain_is_owner_scoped(self):
        class _Runtime:
            def __init__(self):
                self.async_jobs = AsyncJobRegistry()
        rt = _Runtime()
        a = _bare_agent(runtime=rt)
        a.agent_id = 'A'
        b = _bare_agent(runtime=rt)
        b.agent_id = 'B'
        job = rt.async_jobs.start('x', runner=lambda j: (DONE, 'for-A', None),
                                  owner='A')
        self.assertTrue(_await_status(job, DONE))
        self.assertEqual(b._drain_async_results(), 0)
        self.assertEqual(b.history, [])
        self.assertEqual(a._drain_async_results(), 1)
        self.assertIn('for-A', a.history[0]['content'])


# Resolve the tree under test from THIS file, never from the CWD: a relative
# path would read the MAIN checkout (or raise FileNotFoundError) when the
# suite is launched from anywhere but the worktree root.
_AGENT_PY = (pathlib.Path(__file__).resolve().parents[1]
             / 'yaha' / 'core' / 'agent.py')


class GuardWiringTests(unittest.TestCase):
    def test_async_bash_tool_map_entry_is_guarded(self):
        src = _AGENT_PY.read_text(encoding='utf-8')
        self.assertIn('"async_bash": self._async_bash_tool_with_guard', src)

    def test_async_bash_guard_uses_the_guarded_ui(self):
        import inspect
        src = inspect.getsource(Agent._async_bash_tool_with_guard)
        self.assertIn('_interrupt_guarded_ui', src)
        self.assertIn('owner=self.agent_id', src)


if __name__ == '__main__':
    unittest.main()
