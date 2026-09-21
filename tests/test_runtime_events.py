#!/usr/bin/env python3
'''Unit tests for the cooperative multi-agent runtime primitives.

Covers Event, the shared named EventRegistry, AgentHandle lifecycle and the
static Wait Event helpers. Pure module: no engine, no LLM, no TTY needed.
'''
import unittest

from yaha.core import runtime as runtime_mod
from yaha.core.runtime import (
    Event,
    EventRegistry,
    AgentRuntime,
    READY,
    WAITING,
    DEAD,
    stage_wait_event,
    collect_wait_events,
    collect_producer_events,
    validate_wait_events,
)


class EventTests(unittest.TestCase):
    def test_runtime_module_is_the_one_under_test(self):
        path = runtime_mod.__file__.replace(chr(92), '/')
        self.assertTrue(path.endswith('core/runtime.py'), path)

    def test_event_flag_set_and_clear(self):
        ev = Event('go')
        self.assertFalse(ev.is_set())
        self.assertFalse(bool(ev))
        ev.set()
        self.assertTrue(ev.is_set())
        self.assertTrue(bool(ev))
        ev.clear()
        self.assertFalse(ev.is_set())

    def test_event_set_returns_true(self):
        self.assertTrue(Event('x').set())


class EventRegistryTests(unittest.TestCase):
    def test_create_on_first_use_is_identity(self):
        reg = EventRegistry()
        first = reg.get('a')
        self.assertIs(first, reg.get('a'))
        self.assertIn('a', reg)

    def test_get_creates_and_contains_tracks(self):
        reg = EventRegistry()
        self.assertNotIn('n', reg)
        self.assertFalse(reg.is_set('n'))
        self.assertIn('n', reg)

    def test_set_autocreates(self):
        reg = EventRegistry()
        reg.set('go')
        self.assertTrue(reg.is_set('go'))
        self.assertEqual(reg.names(), ['go'])

    def test_names_sorted_and_never_deleted(self):
        reg = EventRegistry()
        reg.set('b')
        reg.set('a')
        reg.get('c')
        self.assertEqual(reg.names(), ['a', 'b', 'c'])
        reg.clear('a')
        self.assertEqual(reg.names(), ['a', 'b', 'c'])

    def test_distinct_names_distinct_objects(self):
        reg = EventRegistry()
        self.assertIsNot(reg.get('a'), reg.get('b'))


class RuntimeNamingTests(unittest.TestCase):
    def test_next_name_is_monotonic(self):
        rt = AgentRuntime()
        self.assertEqual(rt.next_name(), 'agent-1')
        self.assertEqual(rt.next_name(), 'agent-2')
        self.assertEqual(rt.next_name('w'), 'w-3')

    def test_snapshot_shape_is_empty_at_start(self):
        snap = AgentRuntime().snapshot()
        self.assertEqual(snap['live'], [])
        self.assertEqual(snap['ready'], [])
        self.assertEqual(snap['events'], [])


class WaitEventHelperTests(unittest.TestCase):
    class _Cfg:
        def __init__(self, chain, system_prompt=''):
            self.prompt_chain = chain
            self.system_prompt = system_prompt

    def test_stage_wait_event_reads_and_normalizes(self):
        self.assertIsNone(stage_wait_event(None))
        self.assertIsNone(stage_wait_event({}))
        self.assertIsNone(stage_wait_event({'wait_event': None}))
        self.assertIsNone(stage_wait_event({'wait_event': '   '}))
        self.assertEqual(stage_wait_event({'wait_event': ' go '}), 'go')

    def test_collect_wait_events_maps_names_to_stages(self):
        cfgs = {'A': self._Cfg([{'name': 'Work', 'wait_event': 'go'}])}
        self.assertEqual(collect_wait_events(cfgs), {'go': ['A/Work']})

    def test_collect_producer_events_scans_prompt_literals(self):
        cfgs = {'A': self._Cfg(
            [{'name': 'Work',
              'content': '''then call set_event_success('', 'go')'''}])}
        self.assertEqual(collect_producer_events(cfgs), {'go'})

    def test_validate_wait_events_reports_missing_producer(self):
        cfgs = {'A': self._Cfg(
            [{'name': 'Work', 'wait_event': 'go', 'content': 'no producer here'}])}
        self.assertEqual(validate_wait_events(cfgs), ['go'])

    def test_validate_wait_events_empty_when_produced(self):
        cfgs = {'A': self._Cfg(
            [{'name': 'Work', 'wait_event': 'go',
              'content': '''call set_event_success('', 'go')'''}])}
        self.assertEqual(validate_wait_events(cfgs), [])



def _t_keyword_producer_is_accepted(self):
    text = 'call set_event_success(name=' + chr(39) + 'go' + chr(39) + ')'
    cfg = self._Cfg([{'name': 'Work', 'wait_event': 'go', 'content': text}])
    self.assertEqual(collect_producer_events({'A': cfg}), {'go'})
    self.assertEqual(validate_wait_events({'A': cfg}), [])


def _t_keyword_producer_with_loop_is_accepted(self):
    text = 'call set_event_loop(name=' + chr(39) + 'go' + chr(39) + ')'
    cfg = self._Cfg([{'name': 'Work', 'wait_event': 'go', 'content': text}])
    self.assertEqual(validate_wait_events({'A': cfg}), [])


def _t_non_literal_producer_is_not_accepted(self):
    cfg = self._Cfg([{'name': 'Work', 'wait_event': 'go',
                      'content': 'call set_event_success(name=some_var)'}])
    self.assertEqual(collect_producer_events({'A': cfg}), set())
    self.assertEqual(validate_wait_events({'A': cfg}), ['go'])


WaitEventHelperTests.test_keyword_producer_is_accepted = _t_keyword_producer_is_accepted
WaitEventHelperTests.test_keyword_producer_with_loop_is_accepted = _t_keyword_producer_with_loop_is_accepted
WaitEventHelperTests.test_non_literal_producer_is_not_accepted = _t_non_literal_producer_is_not_accepted


def _t_jump_producer_is_accepted(self):
    text = ('call set_event_jump(summary=' + chr(39) + chr(39)
            + ', target=' + chr(39) + 'Next' + chr(39)
            + ', names=[' + chr(39) + 'go' + chr(39) + '])')
    cfg = self._Cfg([{'name': 'Work', 'wait_event': 'go', 'content': text}])
    self.assertEqual(collect_producer_events({'A': cfg}), {'go'})
    self.assertEqual(validate_wait_events({'A': cfg}), [])


WaitEventHelperTests.test_jump_producer_is_accepted = _t_jump_producer_is_accepted



class _NoopAgent:
    def __init__(self):
        self.runtime = None
        self.ui = None

    def iter_run(self, task_prompt, seed_history=None):
        yield ('enter', 'S')


def _t_snapshot_exposes_max_agents(self):
    rt = AgentRuntime()
    self.assertEqual(rt.snapshot()['max_agents'], 0)
    rt.spawn(_NoopAgent(), 't', name='a', tag='a')
    rt.spawn(_NoopAgent(), 't', name='b', tag='b')
    snap = rt.snapshot()
    self.assertGreaterEqual(snap['max_agents'], 2)
    for key in ('live', 'ready', 'waiting', 'events'):
        self.assertIn(key, snap)


def _t_fan_out_policy_is_documented(self):
    import yaha.core.runtime as rt_mod
    doc = rt_mod.__doc__ or ''
    self.assertIn('UNBOUNDED', doc)
    self.assertIn('max_agents', doc)


RuntimeNamingTests.test_snapshot_exposes_max_agents = _t_snapshot_exposes_max_agents
RuntimeNamingTests.test_fan_out_policy_is_documented = _t_fan_out_policy_is_documented



def _t_consume_event_clears_and_reports(self):
    rt = AgentRuntime()
    self.assertFalse(rt.consume_event('x'))
    rt.events.set('x')
    self.assertTrue(rt.event_is_set('x'))
    self.assertTrue(rt.consume_event('x'))
    self.assertFalse(rt.event_is_set('x'))
    self.assertFalse(rt.consume_event('x'))


def _t_set_with_no_waiter_latches_for_the_next_consumer(self):
    rt = AgentRuntime()
    rt.set_event('go')
    self.assertTrue(rt.event_is_set('go'))
    self.assertTrue(rt.consume_event('go'))
    self.assertFalse(rt.event_is_set('go'))


RuntimeNamingTests.test_consume_event_clears_and_reports = _t_consume_event_clears_and_reports
RuntimeNamingTests.test_set_with_no_waiter_latches_for_the_next_consumer = _t_set_with_no_waiter_latches_for_the_next_consumer


if __name__ == '__main__':
    unittest.main()
