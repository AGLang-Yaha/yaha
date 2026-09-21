#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

'''Scheduler tests for the cooperative, non-preemptive multi-agent runtime.

Drives fake scripted agents so round-robin order, run-to-block, event
wake-ups, the all-agents-dead termination rule, deadlock detection and the
non-TTY unattended path are deterministic. No LLM, engine or TTY required.
'''
import sys
import unittest
from unittest.mock import patch

from yaha.core.runtime import (
    AgentRuntime,
    RuntimeDeadlockError,
    READY,
    WAITING,
    DEAD,
)


class _Recorder:
    '''Captures the runtime's active-agent publications in global order.'''

    def __init__(self):
        self.events = []

    def set_active_agent(self, name, tag, stage, live):
        self.events.append((name, stage, live))

    def set_agent_tag(self, tag):
        pass


class _FakeAgent:
    '''A scripted cooperative agent.

    program items:
      ('enter', stage)  -> yield control at a stage boundary
      ('wait', event)   -> yield, parking on a runtime event
      ('set', event)    -> set an event (no yield)
      ('return', value) -> finish the chain with value
    '''

    def __init__(self, program, ui=None):
        self.program = list(program)
        self.ui = ui
        self.runtime = None
        self.history = []
        self.entered = []
        self.task_prompt = None

    def iter_run(self, task_prompt, seed_history=None):
        self.task_prompt = task_prompt
        for item in self.program:
            kind = item[0]
            if kind == 'enter':
                self.entered.append(item[1])
                yield ('enter', item[1])
            elif kind == 'wait':
                # Mirror the real (check-once-then-yield-once) protocol: take
                # a latched pulse if one is waiting, otherwise park exactly
                # once. On resume we continue past the wait; the runtime only
                # re-queues a parked handle from set_event, so a resume IS
                # satisfaction and the event must never be re-checked here.
                name = str(item[1])
                if self.runtime is not None and self.runtime.consume_event(name):
                    pass
                else:
                    yield ('wait', item[1])
            elif kind == 'set':
                self.runtime.set_event(item[1])
            elif kind == 'return':
                return item[1]


class SchedulerTests(unittest.TestCase):
    def test_round_robin_order_is_creation_order(self):
        rec = _Recorder()
        rt = AgentRuntime(ui=rec)
        a = _FakeAgent([('enter', 'A1'), ('return', 'ra')])
        b = _FakeAgent([('enter', 'B1'), ('return', 'rb')])
        ha = rt.spawn(a, 'ta', name='a', tag='a')
        hb = rt.spawn(b, 'tb', name='b', tag='b')
        rt.run()
        entered = [e[1] for e in rec.events if e[1]]
        self.assertEqual(entered, ['A1', 'B1'])
        self.assertEqual(ha.result, 'ra')
        self.assertEqual(hb.result, 'rb')
        self.assertEqual(rt.pool, {})

    def test_one_stage_per_step_non_preemptive(self):
        rt = AgentRuntime()
        agent = _FakeAgent([('enter', 'S1'), ('enter', 'S2'), ('return', 'done')])
        handle = rt.spawn(agent, 't', name='a', tag='a')
        rt._step(handle)
        self.assertEqual(handle.entered, ['S1'])
        self.assertEqual(handle.status, READY)
        rt._step(handle)
        self.assertEqual(handle.entered, ['S1', 'S2'])
        rt._step(handle)
        self.assertEqual(handle.status, DEAD)
        self.assertEqual(handle.result, 'done')
        self.assertEqual(rt.pool, {})

    def test_waiter_wakes_when_peer_sets_event(self):
        rt = AgentRuntime()
        a = _FakeAgent([('enter', 'A1'), ('set', 'go'), ('return', 'ra')])
        b = _FakeAgent([('wait', 'go'), ('enter', 'B1'), ('return', 'rb')])
        rt.spawn(a, 'ta', name='a', tag='a')
        hb = rt.spawn(b, 'tb', name='b', tag='b')
        rt.run()
        self.assertEqual(b.entered, ['B1'])
        self.assertEqual(hb.result, 'rb')

    def test_one_set_releases_all_parked_waiters_and_rearms(self):
        rt = AgentRuntime()
        a = _FakeAgent([('wait', 'go'), ('enter', 'A1'), ('return', 'ra')])
        b = _FakeAgent([('wait', 'go'), ('enter', 'B1'), ('return', 'rb')])
        c = _FakeAgent([('enter', 'C1'), ('set', 'go'), ('return', 'rc')])
        rt.spawn(a, 'ta', name='a', tag='a')
        rt.spawn(b, 'tb', name='b', tag='b')
        rt.spawn(c, 'tc', name='c', tag='c')
        rt.run()
        self.assertEqual(a.entered, ['A1'])
        self.assertEqual(b.entered, ['B1'])
        # The pulse was consumed by this generation of waiters, so the flag
        # is re-armed rather than left set for whoever arrives next.
        self.assertFalse(rt.event_is_set('go'))

    def test_rearmed_event_blocks_again_on_the_next_round(self):
        """CORE regression for the auto-reset bug.

        ONE ``set_event`` satisfies ONE generation of waiters. A second Wait
        Event on the same name must block AGAIN (auto-reset) instead of
        passing straight through, which a sticky latch would allow.
        """
        rt = AgentRuntime()
        a = _FakeAgent([
            ('wait', 'go'), ('enter', 'A1'),
            ('wait', 'go'), ('enter', 'A2'),
            ('return', 'ra'),
        ])
        prod = _FakeAgent([('set', 'go'), ('return', 'rc')])
        ha = rt.spawn(a, 'ta', name='a', tag='a')
        hp = rt.spawn(prod, 'tc', name='c', tag='c')

        rt._step(ha)                 # first wait -> a parks
        self.assertEqual(ha.status, WAITING)
        self.assertEqual(ha.wait_event, 'go')

        rt._step(hp)                 # producer pulses go, then dies
        self.assertEqual(ha.status, READY)

        rt._step(ha)                 # a enters A1 at the stage boundary
        self.assertEqual(a.entered, ['A1'])

        rt._step(ha)                 # SECOND wait -> must block again
        self.assertEqual(a.entered, ['A1'])
        self.assertEqual(ha.status, WAITING)
        self.assertEqual(ha.wait_event, 'go')

        # A fresh pulse lets the agent through to the end of its chain.
        rt.set_event('go')
        rt.run()
        self.assertEqual(a.entered, ['A1', 'A2'])
        self.assertEqual(ha.result, 'ra')
        self.assertEqual(rt.pool, {})

    def test_rearmed_event_does_not_leak_to_a_later_waiter(self):
        """After a handshake completes, a LATER waiter still parks.

        No leftover latched flag from a consumed pulse may leak into an agent
        that only arrives after the earlier generation has moved on.
        """
        rt = AgentRuntime()
        a = _FakeAgent([('wait', 'go'), ('enter', 'A1'), ('return', 'ra')])
        prod = _FakeAgent([('set', 'go'), ('return', 'rc')])
        late = _FakeAgent([('wait', 'go'), ('enter', 'L1'), ('return', 'rl')])
        ha = rt.spawn(a, 'ta', name='a', tag='a')
        hp = rt.spawn(prod, 'tc', name='c', tag='c')
        hl = rt.spawn(late, 'td', name='d', tag='d')

        rt._step(ha)                 # a parks on go
        rt._step(hp)                 # c pulses go (releases a) and dies
        rt._step(ha)                 # a enters A1
        rt._step(ha)                 # a finishes and dies
        self.assertIsNone(rt.pool.get('a'))

        # A newcomer that arrives after the handshake sees a CLEAR flag.
        rt._step(hl)
        self.assertEqual(late.entered, [])
        self.assertEqual(hl.status, WAITING)
        self.assertEqual(hl.wait_event, 'go')

    def test_runtime_ends_only_when_all_agents_are_dead(self):
        rt = AgentRuntime()
        root = _FakeAgent([('enter', 'R1'), ('return', 'root-done')])
        child = _FakeAgent([('enter', 'C1'), ('return', 'child-done')])
        hroot = rt.spawn(root, 't', name='main', tag='main')
        rt.spawn(child, 't2', name='agent-1', tag='agent-1')
        result = rt.run(hroot)
        self.assertEqual(result, 'root-done')
        self.assertEqual(child.entered, ['C1'])
        self.assertEqual(rt.pool, {})

    def test_deadlock_raises_with_diagnostic(self):
        rt = AgentRuntime()
        rt.spawn(_FakeAgent([('wait', 'x')]), 't', name='a', tag='a')
        rt.spawn(_FakeAgent([('wait', 'y')]), 't', name='b', tag='b')
        with self.assertRaises(RuntimeDeadlockError) as ctx:
            rt.run()
        msg = str(ctx.exception)
        self.assertIn('x', msg)
        self.assertIn('y', msg)
        self.assertIn('a', msg)
        self.assertIn('b', msg)

    def test_empty_runtime_run_returns_none(self):
        self.assertIsNone(AgentRuntime().run())

    def test_statuses_transition(self):
        rt = AgentRuntime()
        agent = _FakeAgent([('wait', 'go'), ('enter', 'A1'), ('return', 'ra')])
        handle = rt.spawn(agent, 't', name='a', tag='a')
        rt._step(handle)
        self.assertEqual(handle.status, WAITING)
        self.assertEqual(handle.wait_event, 'go')
        rt.set_event('go')
        self.assertEqual(handle.status, READY)


class _NonTTYStdin:
    def isatty(self):
        return False

    def readline(self):
        return ''

    def fileno(self):
        raise OSError('no fileno for a non-tty stdin')


class UnattendedTests(unittest.TestCase):
    def test_non_tty_run_synchronises_without_prompting(self):
        rt = AgentRuntime()
        a = _FakeAgent([('wait', 'go'), ('enter', 'A1'), ('return', 'ra')])
        b = _FakeAgent([('enter', 'B1'), ('set', 'go'), ('return', 'rb')])
        rt.spawn(a, 't', name='a', tag='a')
        rt.spawn(b, 't', name='b', tag='b')

        def _boom(*args, **kwargs):
            raise AssertionError('input() must not run in an unattended run')

        with patch('sys.stdin', _NonTTYStdin()):
            self.assertFalse(sys.stdin.isatty())
            with patch('builtins.input', _boom):
                rt.run()
        self.assertEqual(a.entered, ['A1'])
        self.assertEqual(rt.pool, {})


if __name__ == '__main__':
    unittest.main()
