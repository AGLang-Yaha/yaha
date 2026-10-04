#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

'''`AgentRuntime.run_group`: the SCOPED driver behind a venue /spawn frame.

A `/spawn`ed frame owns a GROUP of coroutines (the spawned agent and any
parallel peers it starts). `run_group(names)` steps ONLY those handles, so the
caller - itself parked in its own `_step` - is never disturbed, and it NEVER
runs the deadlock detector: a member waiting on a message channel is a normal
pause (`blocked`), not a deadlock.
'''
import types
import unittest

from yaha.core.runtime import AgentRuntime, READY, WAITING


class _FakeAgent:
    '''A minimal cooperative agent: yields scripted tokens then dies.'''

    def __init__(self, steps=None, final='done', spawn_peer=None):
        # `steps` are runtime tokens; a token is ('enter', stage) or ('wait', event).
        self._steps = list(steps if steps is not None else [('enter', 'Stage')])
        self._final = final
        self._peer = spawn_peer
        self._peer_started = False
        self.ui = types.SimpleNamespace(set_agent_tag=lambda *a, **k: None)

    def iter_run(self, task_prompt, seed_history=None):
        if self._peer is not None and not self._peer_started:
            self._peer_started = True
            self.runtime.spawn(self._peer, 'peer task', name='peer-1')
        if not self._steps:
            return self._final
        yield self._steps.pop(0)
        return self._final

    def _shutdown_hooks(self):
        pass


def _parked_on(event):
    return _FakeAgent(steps=[('wait', event), ('enter', 'Stage')], final='resumed')


class RunGroupTests(unittest.TestCase):

    def test_run_group_steps_only_named(self):
        runtime = AgentRuntime()
        group_agent = _FakeAgent()
        other = _FakeAgent()
        runtime.spawn(group_agent, 'g', name='group-1')
        runtime.spawn(other, 'o', name='other-1')

        outcome = runtime.run_group(['group-1'])

        self.assertEqual(outcome, 'empty')
        self.assertNotIn('group-1', runtime.pool)
        # The unrelated handle was NEVER stepped: still live and still ready.
        self.assertIn('other-1', runtime.pool)
        self.assertEqual(runtime.pool['other-1'].status, READY)

    def test_run_group_adopts_new_peers(self):
        runtime = AgentRuntime()
        peer = _FakeAgent(final='peer done')
        parent = _FakeAgent(spawn_peer=peer)
        runtime.spawn(parent, 'p', name='parent-1')

        outcome = runtime.run_group(['parent-1'])

        self.assertEqual(outcome, 'empty')
        self.assertNotIn('parent-1', runtime.pool)
        self.assertNotIn('peer-1', runtime.pool, 'the new peer joined the group')

    def test_run_group_returns_empty(self):
        runtime = AgentRuntime()
        runtime.spawn(_FakeAgent(), 'x', name='a-1')
        self.assertEqual(runtime.run_group(['a-1']), 'empty')

    def test_run_group_returns_blocked_on_channel_wait(self):
        runtime = AgentRuntime()
        runtime.spawn(_parked_on('remote_message'), 'w', name='waiter-1')

        outcome = runtime.run_group(['waiter-1'])

        self.assertEqual(outcome, 'blocked')
        self.assertIn('waiter-1', runtime.pool)
        self.assertEqual(runtime.pool['waiter-1'].status, WAITING)

    def test_blocked_group_keeps_waiting_no_deadlock(self):
        runtime = AgentRuntime()
        runtime.spawn(_parked_on('remote_message'), 'w', name='waiter-1')

        # Repeated drives must NEVER raise RuntimeDeadlockError while the group
        # waits on a message channel: the venue keeps the frame and the operator
        # may /resume later.
        for _ in range(3):
            self.assertEqual(runtime.run_group(['waiter-1']), 'blocked')

    def test_run_group_drains_job_notifications(self):
        runtime = AgentRuntime()
        runtime.spawn(_parked_on('job_done'), 'w', name='waiter-1')
        self.assertEqual(runtime.run_group(['waiter-1']), 'blocked')

        # A worker thread finishing a job posts to the wake mailbox; the scoped
        # driver drains it on the scheduler thread and releases the waiter.
        runtime._job_pending.append('job_done')
        outcome = runtime.run_group(['waiter-1'])
        self.assertEqual(outcome, 'empty')

    def test_run_group_empty_names_is_empty(self):
        runtime = AgentRuntime()
        runtime.spawn(_FakeAgent(), 'x', name='a-1')
        self.assertEqual(runtime.run_group([]), 'empty')


if __name__ == '__main__':
    unittest.main()