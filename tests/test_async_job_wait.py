#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

# Real-runtime tests: an async job id is a WAITABLE event.
#
# These drive the REAL scheduler (AgentRuntime.run) with scripted agents,
# because a job wake arrives from a WORKER thread and only the scheduler own
# mailbox drain can turn it into a release. Hand-driving the generator would
# bypass exactly the code under test.

import time
import unittest

from yaha.core.async_jobs import DONE
from yaha.core.runtime import AgentRuntime, RuntimeDeadlockError, validate_wait_events


class _FakeAgent:
    # A scripted cooperative agent (same protocol as the scheduler tests).

    def __init__(self, program):
        self.program = list(program)
        self.ui = None
        self.runtime = None
        self.entered = []

    def iter_run(self, task_prompt, seed_history=None):
        for item in self.program:
            kind = item[0]
            if kind == 'enter':
                self.entered.append(item[1])
                yield ('enter', item[1])
            elif kind == 'wait':
                name = str(item[1])
                if self.runtime is not None and self.runtime.consume_event(name):
                    pass
                else:
                    yield ('wait', item[1])
            elif kind == 'return':
                return item[1]


class _Cfg:
    def __init__(self, chain, toolset=None):
        self.prompt_chain = chain
        self.system_prompt = ''
        # None (the default) means "this double declares no toolset", which
        # keeps the validator's legacy blanket exemption in force; pass a list
        # to model a class that really authored its tools.
        self.toolset = toolset


def _sleep_runner(seconds, output='job-output'):
    def runner(job):
        time.sleep(seconds)
        return DONE, output, None
    return runner


def _wait_done(rt, job_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = rt.async_jobs.poll(job_id)
        if snap and snap['status'] == DONE:
            return True
        time.sleep(0.01)
    return False


class JobWaitTests(unittest.TestCase):
    def test_parked_agent_released_when_job_finishes(self):
        rt = AgentRuntime()
        job = rt.async_jobs.start('long', runner=_sleep_runner(0.3))
        agent = _FakeAgent([('wait', job.job_id), ('enter', 'B1'), ('return', 'rb')])
        handle = rt.spawn(agent, 't', name='b', tag='b')
        rt.run()
        self.assertEqual(agent.entered, ['B1'])
        self.assertEqual(handle.result, 'rb')

    def test_live_job_wait_is_not_a_deadlock(self):
        rt = AgentRuntime()
        job = rt.async_jobs.start('long', runner=_sleep_runner(0.3))
        agent = _FakeAgent([('wait', job.job_id), ('return', 'ok')])
        handle = rt.spawn(agent, 't', name='b', tag='b')
        rt.run()
        self.assertEqual(handle.result, 'ok')

    def test_unknown_wait_still_deadlocks(self):
        rt = AgentRuntime()
        agent = _FakeAgent([('wait', 'ghost'), ('return', 'never')])
        rt.spawn(agent, 't', name='b', tag='b')
        with self.assertRaises(RuntimeDeadlockError):
            rt.run()

    def test_finished_job_latches_for_a_late_waiter(self):
        rt = AgentRuntime()
        job = rt.async_jobs.start('quick', runner=_sleep_runner(0.01))
        self.assertTrue(_wait_done(rt, job.job_id))
        agent = _FakeAgent([('wait', job.job_id), ('return', 'latched')])
        handle = rt.spawn(agent, 't', name='b', tag='b')
        rt.run()
        self.assertEqual(handle.result, 'latched')

    def test_validator_accepts_async_job_wait(self):
        cfg = _Cfg([{'name': 'B', 'wait_event': 'async-1'}])
        self.assertEqual(validate_wait_events({'B': cfg}), [])

    def test_validator_still_rejects_a_typo_wait(self):
        cfg = _Cfg([{'name': 'B', 'wait_event': 'ghost'}])
        self.assertEqual(validate_wait_events({'B': cfg}), ['ghost'])

    def test_validator_rejects_async_wait_when_no_async_tool_declared(self):
        # A class that authored a toolset WITHOUT the async family can never
        # start a job, so an ``async-<n>`` wait is a typo - not an exemption.
        cfg = _Cfg([{'name': 'B', 'wait_event': 'async-1'}],
                   toolset=['bash_tool'])
        self.assertEqual(validate_wait_events({'B': cfg}), ['async-1'])

    def test_validator_accepts_async_wait_when_async_tool_declared(self):
        cfg = _Cfg([{'name': 'B', 'wait_event': 'async-1'}],
                   toolset=['bash_tool', 'async_bash'])
        self.assertEqual(validate_wait_events({'B': cfg}), [])

    def test_validator_keeps_exemption_when_toolset_unknown(self):
        # No toolset info anywhere -> cannot tell -> keep the legacy exemption
        # (this is also what keeps the pre-existing test above passing).
        cfg = _Cfg([{'name': 'B', 'wait_event': 'async-1'}])
        self.assertEqual(validate_wait_events({'B': cfg}), [])




class AgedIdleTimerTests(unittest.TestCase):
    def test_aged_job_activity_does_not_abort_a_live_job_wait(self):
        rt = AgentRuntime()
        # Simulate a long-lived runtime whose job timer is stale.
        rt._job_activity = time.monotonic() - 10_000
        job = rt.async_jobs.start('long', runner=_sleep_runner(0.3))
        agent = _FakeAgent([('wait', job.job_id), ('return', 'ok')])
        handle = rt.spawn(agent, 't', name='b', tag='b')
        rt.run()  # must NOT raise RuntimeDeadlockError
        self.assertEqual(handle.result, 'ok')

    def test_park_on_live_job_rearms_the_idle_timer(self):
        rt = AgentRuntime()
        job = rt.async_jobs.start('long', runner=_sleep_runner(0.3))
        rt._job_activity = time.monotonic() - 10_000
        agent = _FakeAgent([('wait', job.job_id), ('return', 'ok')])
        rt.spawn(agent, 't', name='b', tag='b')
        handle = rt.ready.popleft()
        rt._step(handle)  # parks on the live job id
        self.assertGreater(rt._job_activity, time.monotonic() - 2.0)


if __name__ == '__main__':
    unittest.main()
