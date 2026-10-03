#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

'''Tests for the ``poll_async`` ``timeout`` parameter.

``poll_async(job_id, timeout=N)`` parks the caller for up to N seconds,
returning the moment the job reaches a terminal state. If N elapses first the
reply says the wait timed out and the job KEEPS RUNNING - a timeout never
cancels it. Omitting ``timeout`` (or passing 0) keeps the historical instant,
non-blocking snapshot.

No agent and no LLM: the registry and the tools are driven directly. Every wait
is bounded, and the timing assertions carry generous slack so they cannot flake.
'''
import pathlib
import threading
import time
import unittest

from yaha.core.async_jobs import (
    AsyncJobRegistry, DONE, JOB_TIMEOUT_S, PENDING, RUNNING,
)
from yaha.tools.async_tools import (
    _coerce_wait_seconds, async_bash_tool, poll_async_tool,
)


def _wait_status(registry, job_id, status, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = registry.poll(job_id)
        if snap and snap['status'] == status:
            return snap
        time.sleep(0.01)
    return registry.poll(job_id)


def _gated(gate):
    '''A runner that blocks until ``gate`` is set, then returns DONE.'''
    return lambda j: (gate.wait(5), (DONE, 'late', None))[1]


class CoerceWaitSecondsTests(unittest.TestCase):
    def test_none_is_no_wait(self):
        self.assertEqual(_coerce_wait_seconds(None), (None, None))

    def test_zero_and_negative_are_no_wait(self):
        self.assertEqual(_coerce_wait_seconds(0), (None, None))
        self.assertEqual(_coerce_wait_seconds(-3), (None, None))
        self.assertEqual(_coerce_wait_seconds('-3'), (None, None))

    def test_positive_is_clamped_to_job_timeout(self):
        self.assertEqual(_coerce_wait_seconds(2), (2.0, None))
        self.assertEqual(_coerce_wait_seconds(100000),
                         (float(JOB_TIMEOUT_S), None))

    def test_numeric_string_is_accepted(self):
        self.assertEqual(_coerce_wait_seconds('1.5'), (1.5, None))

    def test_non_numeric_errors(self):
        seconds, error = _coerce_wait_seconds('soon')
        self.assertIsNone(seconds)
        self.assertTrue(error.startswith('Error'))


class RegistryWaitTests(unittest.TestCase):
    def test_returns_terminal_snapshot(self):
        reg = AsyncJobRegistry()
        reg.start('x', runner=lambda j: (DONE, 'done-ok', None))
        snap = reg.wait('async-1', timeout=5.0)
        self.assertEqual(snap['status'], DONE)
        self.assertIn('done-ok', snap['output'])

    def test_wakes_promptly_on_finish(self):
        reg = AsyncJobRegistry()
        reg.start('x', runner=_gated(threading.Event()))
        gate = threading.Event()
        reg.start('y', runner=lambda j: (time.sleep(0.2), (DONE, 'ok', None))[1])
        started = time.monotonic()
        snap = reg.wait('async-2', timeout=10.0)
        self.assertEqual(snap['status'], DONE)
        self.assertLess(time.monotonic() - started, 5.0)

    def test_times_out_while_running(self):
        reg = AsyncJobRegistry()
        gate = threading.Event()
        reg.start('x', runner=_gated(gate))
        started = time.monotonic()
        snap = reg.wait('async-1', timeout=0.3)
        elapsed = time.monotonic() - started
        self.assertEqual(snap['status'], RUNNING)
        self.assertGreaterEqual(elapsed, 0.15)
        self.assertLess(elapsed, 2.0)
        gate.set()

    def test_unknown_id_returns_none(self):
        self.assertIsNone(AsyncJobRegistry().wait('async-9', timeout=0.1))

    def test_pending_job_times_out(self):
        reg = AsyncJobRegistry()
        reg.allocate('x')
        snap = reg.wait('async-1', timeout=0.1)
        self.assertEqual(snap['status'], PENDING)


class PollAsyncTimeoutTests(unittest.TestCase):
    def test_timeout_returns_done_result(self):
        reg = AsyncJobRegistry()
        async_bash_tool('printf hi', registry=reg)
        text = poll_async_tool('async-1', reg, timeout=5)
        self.assertIn('done', text)
        self.assertIn('COMMAND:', text)
        self.assertIn('hi', text)

    def test_already_finished_returns_immediately(self):
        reg = AsyncJobRegistry()
        async_bash_tool('printf hi', registry=reg)
        _wait_status(reg, 'async-1', DONE)
        started = time.monotonic()
        text = poll_async_tool('async-1', reg, timeout=5)
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertIn('done', text)

    def test_timeout_expires_and_reports_running(self):
        reg = AsyncJobRegistry()
        gate = threading.Event()
        reg.start('sleep 9; echo late', runner=_gated(gate),
                  display_cmdline='sleep 9; echo late')
        started = time.monotonic()
        text = poll_async_tool('async-1', reg, timeout=0.3)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertIn('running', text)
        self.assertIn('COMMAND:', text)
        self.assertIn('timed out', text)
        # A timeout must NOT cancel the job: it is still live and still runnable.
        self.assertTrue(reg.is_live('async-1'))
        gate.set()
        self.assertEqual(_wait_status(reg, 'async-1', DONE)['status'], DONE)

    def test_timeout_zero_is_nonblocking(self):
        reg = AsyncJobRegistry()
        gate = threading.Event()
        reg.start('x', runner=_gated(gate))
        started = time.monotonic()
        text = poll_async_tool('async-1', reg, timeout=0)
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertIn('running', text)
        self.assertNotIn('timed out', text)
        gate.set()

    def test_timeout_none_is_unchanged(self):
        reg = AsyncJobRegistry()
        gate = threading.Event()
        reg.start('x', runner=_gated(gate))
        started = time.monotonic()
        text = poll_async_tool('async-1', reg)
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertIn('running', text)
        self.assertNotIn('timed out', text)
        gate.set()

    def test_numeric_string_timeout_waits(self):
        reg = AsyncJobRegistry()
        async_bash_tool('printf hi', registry=reg)
        text = poll_async_tool('async-1', reg, timeout='5')
        self.assertIn('done', text)
        self.assertIn('hi', text)

    def test_negative_timeout_is_nonblocking(self):
        reg = AsyncJobRegistry()
        gate = threading.Event()
        reg.start('x', runner=_gated(gate))
        started = time.monotonic()
        text = poll_async_tool('async-1', reg, timeout=-1)
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertIn('running', text)
        gate.set()

    def test_non_numeric_timeout_errors(self):
        reg = AsyncJobRegistry()
        async_bash_tool('true', registry=reg)
        text = poll_async_tool('async-1', reg, timeout='soon')
        self.assertTrue(text.startswith('Error'))

    def test_unknown_job_with_timeout_errors(self):
        text = poll_async_tool('async-9', AsyncJobRegistry(), timeout=0.1)
        self.assertIn('no async job', text)

    def test_missing_registry_still_errors(self):
        self.assertTrue(poll_async_tool('async-1', None, timeout=1).startswith('Error'))
        self.assertTrue(poll_async_tool('async-1', None).startswith('Error'))


# Resolve the tree under test from THIS file, never from the CWD: a relative
# path would read the MAIN checkout when the suite runs anywhere else.
_AGENT_PY = (pathlib.Path(__file__).resolve().parents[1]
             / 'yaha' / 'core' / 'agent.py')


class AgentWiringTests(unittest.TestCase):
    def test_poll_async_tool_map_accepts_timeout(self):
        src = _AGENT_PY.read_text(encoding='utf-8')
        self.assertIn('"poll_async": lambda job_id, timeout=None: poll_async_tool(', src)

    def test_shipped_schema_exposes_poll_async_timeout(self):
        from yaha.config.tools import load_tool_schema
        import yaha
        tmpl = (pathlib.Path(yaha.__file__).resolve().parent
                / 'templates' / 'tool-schema.md')
        entries = load_tool_schema(str(tmpl))
        entry = next(e for e in entries if e.get('name') == 'poll_async')
        props = entry['parameters']['properties']
        self.assertIn('timeout', props)
        self.assertEqual(entry['parameters']['required'], ['job_id'])


if __name__ == '__main__':
    unittest.main()