#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

'''Tests for the async tools: ``async_bash`` and ``poll_async``.

No agent and no LLM: the tools are called directly with a real registry and,
where needed, a fake AccessController. Every wait is bounded.
'''
import time
import unittest

from yaha.core.async_jobs import AsyncJobRegistry, DONE
from yaha.tools.async_tools import async_bash_tool, poll_async_tool


class _AllowController:
    def __init__(self, sandbox=None):
        self.sandbox = sandbox
        self.calls = []

    def authorize_bash(self, agent_id, cmdline, ui=None):
        self.calls.append(('authorize', cmdline))
        return True, 'ok'

    def get_sandbox_command(self, cmdline):
        self.calls.append(('sandbox', cmdline))
        return self.sandbox if self.sandbox is not None else cmdline


class _DenyController:
    def authorize_bash(self, agent_id, cmdline, ui=None):
        return False, 'DENIED: nope'

    def get_sandbox_command(self, cmdline):
        raise AssertionError('sandbox must not be computed for a denied command')


def _wait_status(registry, job_id, status, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = registry.poll(job_id)
        if snap and snap['status'] == status:
            return snap
        time.sleep(0.01)
    return registry.poll(job_id)


class AsyncBashToolTests(unittest.TestCase):
    def test_returns_immediately_with_id(self):
        reg = AsyncJobRegistry()
        started = time.monotonic()
        text = async_bash_tool('sleep 3', registry=reg)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.0)
        self.assertIn('ASYNC JOB STARTED', text)
        self.assertIn('async-1', text)
        reg.shutdown()

    def test_hint_names_wait_and_poll(self):
        reg = AsyncJobRegistry()
        text = async_bash_tool('true', registry=reg)
        self.assertIn('WAITABLE', text)
        self.assertIn('poll_async', text)
        reg.shutdown()

    def test_denied_command_starts_no_job(self):
        reg = AsyncJobRegistry()
        text = async_bash_tool('rm -rf /', registry=reg,
                               access_controller=_DenyController())
        self.assertEqual(text, 'DENIED: nope')
        self.assertEqual(reg.drain_finished(), [])

    def test_sandbox_command_is_what_runs(self):
        reg = AsyncJobRegistry()
        ctrl = _AllowController(sandbox='printf sandboxed')
        async_bash_tool('printf origin', registry=reg, access_controller=ctrl)
        snap = _wait_status(reg, 'async-1', DONE)
        self.assertIn('sandboxed', snap['output'])
        self.assertNotIn('origin', snap['output'])

    def test_missing_registry_is_an_error(self):
        self.assertTrue(async_bash_tool('true', registry=None).startswith('Error'))

    def test_output_is_captured(self):
        reg = AsyncJobRegistry()
        async_bash_tool('printf hi', registry=reg)
        snap = _wait_status(reg, 'async-1', DONE)
        self.assertIn('hi', snap['output'])

    def test_nonzero_exit_is_noted(self):
        reg = AsyncJobRegistry()
        async_bash_tool('exit 3', registry=reg)
        snap = _wait_status(reg, 'async-1', DONE)
        self.assertIn('exit code 3', snap['output'])


class PollAsyncToolTests(unittest.TestCase):
    def test_running_then_done(self):
        reg = AsyncJobRegistry()
        async_bash_tool('sleep 0.2; printf done-ok', registry=reg)
        first = poll_async_tool('async-1', reg)
        self.assertIn('running', first)
        _wait_status(reg, 'async-1', DONE)
        second = poll_async_tool('async-1', reg)
        self.assertIn('done', second)
        self.assertIn('done-ok', second)

    def test_unknown_job_text(self):
        text = poll_async_tool('async-9', AsyncJobRegistry())
        self.assertIn('no async job', text)

    def test_missing_registry_is_an_error(self):
        self.assertTrue(poll_async_tool('async-1', None).startswith('Error'))




class OwnerTagTests(unittest.TestCase):
    def test_owner_is_recorded_on_the_job(self):
        reg = AsyncJobRegistry()
        async_bash_tool('true', registry=reg, owner='a1')
        self.assertEqual(reg.get('async-1').owner, 'a1')
        reg.shutdown()

    def test_owner_defaults_to_none(self):
        reg = AsyncJobRegistry()
        async_bash_tool('true', registry=reg)
        self.assertIsNone(reg.get('async-1').owner)
        reg.shutdown()

    def test_never_started_job_polls_as_pending(self):
        from yaha.core.async_jobs import PENDING
        reg = AsyncJobRegistry()
        reg.allocate('true')
        text = poll_async_tool('async-1', reg)
        self.assertIn(PENDING, text)


if __name__ == '__main__':
    unittest.main()
