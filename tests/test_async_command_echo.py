#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

'''The async tools echo the FULL command they were started with.

A job may outlive the ``async_bash`` call that launched it, and history
compression can drop that call in the meantime. Every result the agent reads -
an explicit ``poll_async`` and the opportunistic drain - must therefore name the
command, and it must name the command AS THE AGENT WROTE IT, not the sandbox
wrapper the worker actually executes.
'''
import time
import unittest

from yaha.core.async_jobs import AsyncJobRegistry, DONE
from yaha.core.agent import Agent
from yaha.tools.async_tools import async_bash_tool, poll_async_tool


class _AllowController:
    def __init__(self, sandbox=None):
        self.sandbox = sandbox

    def authorize_bash(self, agent_id, cmdline, ui=None):
        return True, 'ok'

    def get_sandbox_command(self, cmdline):
        return self.sandbox if self.sandbox is not None else cmdline


def _wait_status(registry, job_id, status, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = registry.poll(job_id)
        if snap and snap['status'] == status:
            return snap
        time.sleep(0.01)
    return registry.poll(job_id)


class _FakeUI:
    def display_tool_result(self, *args):
        pass


class _Cfg:
    name = 'T'


def _bare_agent():
    ag = Agent.__new__(Agent)
    ag.runtime = None
    ag._async_jobs_local = None
    ag.history = []
    ag.ui = _FakeUI()
    ag.config = _Cfg()
    ag.agent_id = 'a1'
    return ag


class RegistryDisplayCommandTests(unittest.TestCase):
    def test_start_records_display_cmdline(self):
        reg = AsyncJobRegistry()
        reg.start('run-me', runner=lambda j: (DONE, 'ok', None),
                  display_cmdline='shown-me')
        self.assertEqual(reg.get('async-1').display_cmdline, 'shown-me')
        self.assertEqual(reg.poll('async-1')['display_cmdline'], 'shown-me')

    def test_display_cmdline_defaults_to_cmdline(self):
        reg = AsyncJobRegistry()
        reg.start('run-me', runner=lambda j: (DONE, 'ok', None))
        self.assertEqual(reg.get('async-1').display_cmdline, 'run-me')
        self.assertEqual(reg.poll('async-1')['cmdline'], 'run-me')

    def test_allocate_accepts_display_cmdline(self):
        reg = AsyncJobRegistry()
        job = reg.allocate('run-me', display_cmdline='shown-me')
        self.assertEqual(job.display_cmdline, 'shown-me')


class PollEchoTests(unittest.TestCase):
    def test_done_result_echoes_the_full_command(self):
        reg = AsyncJobRegistry()
        async_bash_tool('printf hi-from-async', registry=reg)
        _wait_status(reg, 'async-1', DONE)
        text = poll_async_tool('async-1', reg)
        self.assertIn('COMMAND:', text)
        self.assertIn('printf hi-from-async', text)
        self.assertIn('hi-from-async', text)

    def test_running_result_echoes_the_command(self):
        reg = AsyncJobRegistry()
        async_bash_tool('sleep 0.3; printf late', registry=reg)
        text = poll_async_tool('async-1', reg)
        self.assertIn('running', text)
        self.assertIn('sleep 0.3; printf late', text)
        _wait_status(reg, 'async-1', DONE)

    def test_echoes_the_original_not_the_sandbox_wrapper(self):
        reg = AsyncJobRegistry()
        ctrl = _AllowController(sandbox='printf sandboxed')
        async_bash_tool('printf origin', registry=reg, access_controller=ctrl)
        _wait_status(reg, 'async-1', DONE)
        text = poll_async_tool('async-1', reg)
        self.assertIn('printf origin', text)
        self.assertNotIn('printf sandboxed', text)


class DrainEchoTests(unittest.TestCase):
    def test_drain_message_carries_the_command(self):
        ag = _bare_agent()
        ag._async_registry().start('echo drained-here', owner='a1',
                                   runner=lambda j: (DONE, 'out', None))
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and ag._drain_async_results() == 0:
            time.sleep(0.01)
        content = ag.history[0]['content']
        self.assertIn('- **Command**:', content)
        self.assertIn('echo drained-here', content)

    def test_drain_prefers_the_display_command(self):
        ag = _bare_agent()
        ag._async_registry().start('sandbox-runner', owner='a1',
                                   display_cmdline='the-real-command',
                                   runner=lambda j: (DONE, 'out', None))
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and ag._drain_async_results() == 0:
            time.sleep(0.01)
        content = ag.history[0]['content']
        self.assertIn('the-real-command', content)
        self.assertNotIn('sandbox-runner', content)


if __name__ == '__main__':
    unittest.main()