#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

'''Unit tests for the async job registry and job objects.

Pure stdlib: jobs run tiny shell commands on daemon threads and every wait is
bounded, so nothing here can hang the suite. No agent, no runtime, no TTY.
'''
import subprocess
import threading
import time
import unittest

from yaha.core import async_jobs as J


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _subprocess_runner(job):
    '''A real runner: Popen the command and capture it (like the tool does).'''
    proc = subprocess.Popen(job.cmdline, shell=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    job.proc = proc
    out, err = proc.communicate(timeout=10)
    return J.DONE, (out or '') + (err or ''), None


def _echo_runner(job):
    return J.DONE, 'hello', None


class IsAsyncJobTests(unittest.TestCase):
    def test_recognizes_job_ids(self):
        self.assertTrue(J.is_async_job('async-1'))
        self.assertTrue(J.is_async_job('async-42'))

    def test_rejects_non_job_names(self):
        for bad in ('async-x', 'agent-1', '@p', 'async-', '', None, 7):
            self.assertFalse(J.is_async_job(bad), bad)


class RegistryTests(unittest.TestCase):
    def test_allocate_ids_are_unique_and_monotonic(self):
        reg = J.AsyncJobRegistry()
        ids = [reg.allocate('true').job_id for _ in range(3)]
        self.assertEqual(ids, ['async-1', 'async-2', 'async-3'])

    def test_start_flips_running_before_returning(self):
        gate = threading.Event()

        def runner(job):
            gate.wait(5)
            return J.DONE, 'ok', None

        reg = J.AsyncJobRegistry()
        job = reg.start('x', runner=runner)
        self.assertEqual(job.status, J.RUNNING)
        self.assertTrue(reg.is_live(job.job_id))
        gate.set()
        self.assertTrue(_wait_for(lambda: job.status == J.DONE))

    def test_job_runs_and_reaches_done(self):
        reg = J.AsyncJobRegistry()
        job = reg.start('true', runner=_echo_runner)
        self.assertTrue(_wait_for(lambda: job.status == J.DONE))
        snap = reg.poll(job.job_id)
        self.assertEqual(snap['output'], 'hello')
        self.assertIsNone(snap['error'])
        self.assertGreaterEqual(snap['elapsed'], 0.0)

    def test_real_command_output_is_captured(self):
        reg = J.AsyncJobRegistry()
        job = reg.start('printf async-ok', runner=_subprocess_runner)
        self.assertTrue(_wait_for(lambda: job.status == J.DONE))
        self.assertIn('async-ok', job.output)

    def test_failing_runner_reaches_error(self):
        def boom(job):
            raise RuntimeError('kaboom')
        reg = J.AsyncJobRegistry()
        job = reg.start('x', runner=boom)
        self.assertTrue(_wait_for(lambda: job.status == J.ERROR))
        self.assertIn('kaboom', job.error)

    def test_poll_unknown_id_returns_none(self):
        self.assertIsNone(J.AsyncJobRegistry().poll('nope'))

    def test_is_live_false_for_unknown_id(self):
        self.assertFalse(J.AsyncJobRegistry().is_live('async-999'))

    def test_is_live_flips_on_completion(self):
        gate = threading.Event()

        def runner(job):
            gate.wait(5)
            return J.DONE, 'done', None

        reg = J.AsyncJobRegistry()
        job = reg.start('x', runner=runner)
        self.assertTrue(reg.is_live(job.job_id))
        gate.set()
        self.assertTrue(_wait_for(lambda: not reg.is_live(job.job_id)))

    def test_wake_callback_fires_on_completion(self):
        seen = []
        reg = J.AsyncJobRegistry(wake=seen.append)
        job = reg.start('x', runner=_echo_runner)
        self.assertTrue(_wait_for(lambda: job.job_id in seen))

    def test_wake_error_is_swallowed(self):
        def bad_wake(name):
            raise RuntimeError('wake failed')
        reg = J.AsyncJobRegistry(wake=bad_wake)
        job = reg.start('x', runner=_echo_runner)
        self.assertTrue(_wait_for(lambda: job.status == J.DONE))

    def test_drain_finished_delivers_once(self):
        reg = J.AsyncJobRegistry()
        job = reg.start('x', runner=_echo_runner)
        self.assertTrue(_wait_for(lambda: job.status == J.DONE))
        first = reg.drain_finished()
        self.assertEqual([j.job_id for j in first], [job.job_id])
        self.assertEqual(reg.drain_finished(), [])

    def test_drain_never_returns_running_job(self):
        gate = threading.Event()

        def runner(job):
            gate.wait(5)
            return J.DONE, 'o', None

        reg = J.AsyncJobRegistry()
        job = reg.start('x', runner=runner)
        self.assertEqual(reg.drain_finished(), [])
        gate.set()
        self.assertTrue(_wait_for(lambda: job.status == J.DONE))

    def test_shutdown_cancels_live_job(self):
        reg = J.AsyncJobRegistry()
        job = reg.start('sleep 30', runner=_subprocess_runner)
        self.assertTrue(_wait_for(lambda: job.proc is not None))
        reg.shutdown()
        self.assertEqual(job.status, J.CANCELLED)
        # A late worker finish must NOT resurrect a cancelled job.
        job.finish(J.DONE, 'late', None)
        self.assertEqual(job.status, J.CANCELLED)




class OwnerDrainTests(unittest.TestCase):
    def test_allocate_records_owner(self):
        reg = J.AsyncJobRegistry()
        job = reg.allocate('true', owner='a1')
        self.assertEqual(job.owner, 'a1')

    def test_allocate_default_owner_is_none(self):
        self.assertIsNone(J.AsyncJobRegistry().allocate('true').owner)

    def test_drain_finished_filters_by_owner(self):
        reg = J.AsyncJobRegistry()
        a = reg.start('x', runner=_echo_runner, owner='a')
        b = reg.start('x', runner=_echo_runner, owner='b')
        self.assertTrue(_wait_for(lambda: a.status == J.DONE and b.status == J.DONE))
        mine = reg.drain_finished(owner='a')
        self.assertEqual([j.job_id for j in mine], [a.job_id])
        rest = reg.drain_finished()
        self.assertEqual([j.job_id for j in rest], [b.job_id])

    def test_drain_finished_unscoped_returns_all(self):
        reg = J.AsyncJobRegistry()
        a = reg.start('x', runner=_echo_runner, owner='a')
        b = reg.start('x', runner=_echo_runner, owner='b')
        self.assertTrue(_wait_for(lambda: a.status == J.DONE and b.status == J.DONE))
        got = sorted(j.job_id for j in reg.drain_finished())
        self.assertEqual(got, sorted([a.job_id, b.job_id]))


if __name__ == '__main__':
    unittest.main()
