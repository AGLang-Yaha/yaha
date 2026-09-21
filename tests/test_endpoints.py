#!/usr/bin/env python3
'''Tests for remote agent communication via ENDPOINTS (service EPs and CEPs).

An EP is BOTH an event name and a message queue, addressed as
``http(s)://host[:port]/path/version``. A service EP is the listener side; a CEP
is one conversation and is a LEVEL signal over a FIFO queue (set while messages
are queued, unset when drained) - deliberately NOT the local pulse/latch rules.

Every test binds port 0 (ephemeral) and uses daemon threads only, so nothing
here can collide with another process or hang the suite.
'''
import json
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

from yaha.core import endpoints as E
from yaha.core.agent import Agent
from yaha.core.engine import YAHAEngine
from yaha.core.runtime import AgentRuntime, validate_wait_events

HOME = Path(__file__).resolve().parent.parent
TMP = HOME / 'tmp'

_LT = chr(60)
_GT = chr(62)
_TOOL_OPEN = _LT + 'tool_code' + _GT
_TOOL_CLOSE = _LT + '/tool_code' + _GT


def _tool_call(name, args):
    return _TOOL_OPEN + json.dumps({'tool': name, 'args': args}) + _TOOL_CLOSE


FIXTURE = '''
## Evt
- **Inherited:** None
- **Code:** Evt
- **Model:** m
- **Toolset:**
    - bash_tool
- **Preamble:**
    > do the thing
- **Aftermath:**
    > done
'''


class _MockLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.usage = {'prompt_tokens': 5, 'completion_tokens': 5}

    def generate(self, messages, **kwargs):
        resp = self.responses[self.calls] if self.calls < len(self.responses) else 'ok'
        self.calls += 1
        return resp, dict(self.usage), None


def _make_agent(fixture=None):
    TMP.mkdir(parents=True, exist_ok=True)
    ws = Path(tempfile.mkdtemp(prefix='yaha_ep_', dir=str(TMP)))
    yh = ws / '.yaha'
    yh.mkdir(parents=True)
    (yh / 'memory').mkdir()
    (yh / 'ext').mkdir()
    (yh / 'logs').mkdir()
    (yh / 'tool-schema.md').write_text('[]')
    (yh / 'AgentClass.md').write_text(fixture if fixture is not None else FIXTURE)
    (yh / 'Model.md').write_text('')
    eng = YAHAEngine(workspace_dir=str(ws))
    agent = Agent(
        agent_config=eng.agent_classes['Evt'],
        session=eng.session,
        models_config=eng.models_config,
        agent_classes=eng.agent_classes,
        tool_schemas=eng.tool_schemas,
        access_controller=eng.access_controller,
    )
    return agent, eng, ws


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class EpAddressTests(unittest.TestCase):
    def test_parse_ep_extracts_the_parts(self):
        address = E.parse_ep('http://127.0.0.1:8123/crew/reviewer/v1')
        self.assertEqual(address.scheme, 'http')
        self.assertEqual(address.host, '127.0.0.1')
        self.assertEqual(address.port, 8123)
        self.assertEqual(address.path, '/crew/reviewer')
        self.assertEqual(address.version, 'v1')

    def test_port_zero_is_not_replaced_by_the_default(self):
        '''Ephemeral binding must survive parsing (a real bug I hit).'''
        self.assertEqual(E.parse_ep('http://127.0.0.1:0/crew/v1').port, 0)
        self.assertEqual(E.parse_ep('https://h/crew/v1').port, 443)
        self.assertEqual(E.parse_ep('http://h/crew/v1').port, 80)

    def test_version_segment_is_required(self):
        for bad in ('http://h/v1', 'http://h/crew/nope', 'http://h/crew/v2/x'):
            with self.assertRaises(E.EndpointError):
                E.parse_ep(bad)

    def test_bad_shapes_are_rejected(self):
        for bad in ('', 'notaurl', 'ftp://h/crew/v1', 'http:///crew/v1'):
            with self.assertRaises(E.EndpointError):
                E.parse_ep(bad)

    def test_is_ep(self):
        self.assertTrue(E.is_ep('http://h/crew/v1'))
        self.assertTrue(E.is_ep('https://h/crew/v1'))
        self.assertFalse(E.is_ep('plain'))
        self.assertFalse(E.is_ep('@placeholder'))
        self.assertFalse(E.is_ep(''))

class QueueSemanticsTests(unittest.TestCase):
    def test_level_follows_occupancy(self):
        q = E.MessageQueue(cap=3)
        self.assertFalse(q.level)
        self.assertTrue(q.put('a'))
        self.assertTrue(q.level)
        self.assertEqual(q.take(), 'a')
        self.assertFalse(q.level)

    def test_fifo_one_message_per_take(self):
        q = E.MessageQueue(cap=5)
        for text in ('one', 'two', 'three'):
            q.put(text)
        self.assertEqual([q.take(), q.take(), q.take()], ['one', 'two', 'three'])
        self.assertIsNone(q.take())

    def test_early_message_survives_for_the_next_wait(self):
        q = E.MessageQueue(cap=5)
        q.put('early')
        self.assertEqual(q.take(), 'early')

    def test_a_cep_is_a_level_not_a_pulse(self):
        cep = E.Cep('http://h/crew/cep/1/v1')
        self.assertFalse(cep.is_set())
        cep.inbox.put('m')
        self.assertTrue(cep.is_set())
        self.assertEqual(cep.take(), 'm')
        self.assertFalse(cep.is_set())
        cep.inbox.put('m2')
        self.assertTrue(cep.is_set())
        self.assertEqual(cep.take(), 'm2')

    def test_the_queue_bound_is_reported_not_silently_dropped(self):
        q = E.MessageQueue(cap=2)
        self.assertTrue(q.put('a'))
        self.assertTrue(q.put('b'))
        self.assertFalse(q.put('c'))
        self.assertEqual(q.take(), 'a')
        self.assertEqual(q.take(), 'b')
        self.assertIsNone(q.take())

class ConversationTests(unittest.TestCase):
    '''A live server registry plus a live client registry, both reclaimed.'''

    def setUp(self):
        self.woken = []
        self.server = E.EndpointRegistry(wake=self.woken.append)
        self.client = E.EndpointRegistry()
        self.service = self.server.service_ep('http://127.0.0.1:0/crew/reviewer/v1')

    def tearDown(self):
        self.client.reclaim()
        self.server.reclaim()

    def _connect(self):
        cep_client = self.client.connect(self.service.url)
        accepted = self.service.pending.take()
        self.assertIsNotNone(accepted)
        return cep_client, self.server.ceps[accepted]

    def test_the_listener_is_only_up_after_the_wait(self):
        address = E.parse_ep(self.service.url)
        self.assertNotEqual(address.port, 0)
        self.assertTrue(self.service.started)
        self.assertFalse(self.service.pending.level)

    def test_connect_creates_one_cep_per_client(self):
        cep_client, cep_server = self._connect()
        self.assertEqual(cep_client.url, cep_server.url)
        self.assertEqual(cep_client.direction, 'outbound')
        self.assertEqual(cep_server.direction, 'inbound')
        self.assertEqual(self.server.count_ceps(), 1)

    def test_client_to_server_delivery(self):
        cep_client, cep_server = self._connect()
        self.client.send(cep_client, 'hello server')
        self.assertTrue(_wait_until(lambda: cep_server.is_set()))
        self.assertEqual(cep_server.take(), 'hello server')
        self.assertFalse(cep_server.is_set())

    def test_server_to_client_delivery_through_the_poller(self):
        cep_client, cep_server = self._connect()
        self.server.send(cep_server, 'hello client')
        self.assertTrue(_wait_until(lambda: cep_client.is_set()))
        self.assertEqual(cep_client.take(), 'hello client')

    def test_two_clients_do_not_cross(self):
        first_client, first_server = self._connect()
        second_client, second_server = self._connect()
        self.assertNotEqual(first_client.url, second_client.url)
        self.client.send(first_client, 'for the first')
        self.assertTrue(_wait_until(lambda: first_server.is_set()))
        self.assertEqual(first_server.take(), 'for the first')
        self.assertFalse(second_server.is_set())

    def test_close_tears_the_conversation_down(self):
        cep_client, cep_server = self._connect()
        self.assertTrue(self.client.close(cep_client))
        self.assertTrue(cep_client.closed)
        self.assertTrue(_wait_until(lambda: cep_server.closed))
        self.assertIsNone(self.server.ceps.get(cep_server.url))
        self.assertEqual(self.server.count_ceps(), 0)
        with self.assertRaises(E.EndpointError):
            self.server.send(cep_server, 'too late')

    def test_the_conversation_cap_is_refused_with_an_error(self):
        small = E.EndpointRegistry(max_ceps=1)
        try:
            service = small.service_ep('http://127.0.0.1:0/crew/tiny/v1')
            small.service_ep('http://127.0.0.1:0/crew/tiny/v1')
            client = E.EndpointRegistry()
            try:
                client.connect(service.url)
                with self.assertRaises(E.EndpointError):
                    client.connect(service.url)
            finally:
                client.reclaim()
        finally:
            small.reclaim()

def _resume(gen):
    '''Drive a wait generator one step; return its StopIteration value.'''
    try:
        next(gen)
    except StopIteration as stop:
        return stop.value
    return None


class AgentEndpointTests(unittest.TestCase):
    def setUp(self):
        self.agent, self.eng, self.ws = _make_agent()
        self.runtime = AgentRuntime(ui=self.agent.ui)
        self.agent.runtime = self.runtime
        # The PEER is a SEPARATE run: a client and a server live in different
        # registries, so their CEPs of the same conversation never collide.
        self.peer = E.EndpointRegistry()

    def tearDown(self):
        self.peer.reclaim()
        self.runtime.endpoints.reclaim()
        shutil.rmtree(self.ws, ignore_errors=True)

    def test_waiting_on_a_cep_is_a_level_read(self):
        cep = self.runtime.endpoints.register_cep(
            E.Cep('http://127.0.0.1:9/crew/self/cep/1/v1', direction='inbound'))
        gen = self.agent._await_endpoint(cep.url)
        self.assertEqual(next(gen), ('wait', cep.url))
        cep.inbox.put('arrived')
        name, error, notice = _resume(gen)
        self.assertIsNone(error)
        self.assertEqual(name, cep.url)
        self.assertTrue(notice.startswith('[REMOTE MESSAGE]'))
        self.assertIn('arrived', notice)
        self.assertFalse(cep.is_set())

    def test_waiting_on_a_service_ep_yields_a_connection_notice(self):
        gen = self.agent._await_endpoint('http://127.0.0.1:0/crew/self2/v1')
        token = next(gen)
        self.assertEqual(token[0], 'wait')
        parked_on = token[1]
        self.assertTrue(parked_on.endswith('/crew/self2/v1'))
        self.peer.connect(parked_on)
        name, error, notice = _resume(gen)
        self.assertIsNone(error)
        self.assertTrue(notice.startswith('[REMOTE CONNECTION]'))
        self.assertIn('/cep/', notice)

    def test_an_endpoint_wait_without_a_runtime_fails_recoverably(self):
        self.agent.runtime = None
        gen = self.agent._await_endpoint('http://h/crew/v1')
        name, error, notice = _resume(gen)
        self.assertIsNotNone(error)
        self.assertIn('multi-agency runtime', error)
        self.assertIsNone(notice)

    def test_connect_send_and_close_round_trip(self):
        service = self.peer.service_ep('http://127.0.0.1:0/crew/rt/v1')
        out = self.agent.connect_tool(service.url)
        self.assertTrue(out.startswith('CONNECTED'), out)
        cep_url = out.split(' ', 1)[1]
        accepted = service.pending.take()
        self.assertEqual(accepted, cep_url)
        server_cep = self.peer.ceps[accepted]
        self.assertEqual(self.agent.send_tool(cep_url, 'from the client'), 'SENT')
        self.assertTrue(_wait_until(lambda: server_cep.is_set()))
        self.assertEqual(server_cep.take(), 'from the client')
        self.assertEqual(self.agent.close_tool(cep_url), 'CLOSED')

    def test_send_tool_accepts_a_bound_placeholder(self):
        service = self.peer.service_ep('http://127.0.0.1:0/crew/ph/v1')
        cep_url = self.agent.connect_tool(service.url).split(' ', 1)[1]
        accepted = service.pending.take()
        server_cep = self.peer.ceps[accepted]
        self.runtime.bind_event('@channel', cep_url)
        self.assertEqual(self.agent.send_tool('@channel', 'via placeholder'), 'SENT')
        self.assertTrue(_wait_until(lambda: server_cep.is_set()))
        self.assertEqual(server_cep.take(), 'via placeholder')

    def test_unbound_placeholder_cep_is_refused(self):
        out = self.agent.send_tool('@never', 'x')
        self.assertTrue(str(out).startswith('Error'), out)
        self.assertIn('unresolved event placeholder', out)

    def test_the_three_tools_need_a_runtime(self):
        self.agent.runtime = None
        for out in (self.agent.connect_tool('http://h/crew/v1'),
                    self.agent.send_tool('http://h/crew/v1', 'x'),
                    self.agent.close_tool('http://h/crew/v1')):
            self.assertIn('multi-agency runtime', out)

    def test_a_bad_service_url_is_an_error_string(self):
        out = self.agent.connect_tool('http://h/crew/nope')
        self.assertTrue(str(out).startswith('Error'), out)


class ValidationTests(unittest.TestCase):
    def _cfg(self, chain, system_prompt=''):
        class _Cfg(object):
            pass
        cfg = _Cfg()
        cfg.prompt_chain = chain
        cfg.system_prompt = system_prompt
        return cfg

    def test_an_endpoint_wait_is_exempt(self):
        cfg = self._cfg([{'name': 'Preamble', 'content': '',
                          'wait_event': 'http://127.0.0.1:9/crew/v1'}])
        self.assertEqual(validate_wait_events({'C': cfg}), [])

    def test_a_misspelt_local_wait_is_still_refused(self):
        cfg = self._cfg([{'name': 'Preamble', 'content': '',
                          'wait_event': 'ghost'}])
        self.assertEqual(validate_wait_events({'C': cfg}), ['ghost'])


class SchemaTests(unittest.TestCase):
    def test_the_three_remote_tools_are_declared(self):
        from yaha.config.tools import load_tool_schema
        tools = load_tool_schema(str(HOME / 'yaha' / 'templates' / 'tool-schema.md'))
        by_name = {t['name']: t for t in tools}
        for name in ('connect_tool', 'send_tool', 'close_tool'):
            self.assertIn(name, by_name)
        self.assertEqual(by_name['connect_tool']['parameters']['required'],
                         ['service_ep'])
        self.assertEqual(by_name['send_tool']['parameters']['required'],
                         ['cep', 'message'])
        self.assertEqual(by_name['close_tool']['parameters']['required'], ['cep'])


if __name__ == '__main__':
    unittest.main()

WAIT_EP_FIXTURE = '''
## Evt
- **Inherited:** None
- **Code:** Evt
- **Model:** m
- **Toolset:**
    - bash_tool
- **Preamble:**
    - **Wait Event:** http://127.0.0.1:0/crew/srv/v1
    - **Exit Tools:**
        - success_tool
    > do the thing
- **Aftermath:**
    > done
'''

WAIT_CEP_FIXTURE = '''
## Evt
- **Inherited:** None
- **Code:** Evt
- **Model:** m
- **Toolset:**
    - bash_tool
- **Preamble:**
    - **Wait Event:** @channel
    - **Exit Tools:**
        - success_tool
    > do the thing
- **Aftermath:**
    > done
'''

WAIT_LOCAL_FIXTURE = '''
## Evt
- **Inherited:** None
- **Code:** Evt
- **Model:** m
- **Toolset:**
    - bash_tool
- **Preamble:**
    - **Wait Event:** ghost-event
    > do the thing
- **Aftermath:**
    > done
'''


class _RecordingLLM(_MockLLM):
    '''A mock LLM that remembers every prompt it was shown.'''

    def __init__(self, responses):
        _MockLLM.__init__(self, responses)
        self.seen = []

    def generate(self, messages, **kwargs):
        try:
            self.seen.append(json.dumps(messages))
        except Exception:
            self.seen.append(str(messages))
        return _MockLLM.generate(self, messages, **kwargs)

    def saw(self, needle):
        return any(needle in text for text in self.seen)


def _terminating_responses():
    '''Complete the waiting stage, then end the chain from Aftermath.'''
    return [_tool_call('success_tool', {'summary': 'handled'}),
            _tool_call('fail_tool', {'reason': 'end of test'})]


class SchedulerTests(unittest.TestCase):
    '''The tests that go through a REAL AgentRuntime.run.

    The BLOCKER the reviewer found lived exactly here: every other endpoint test
    drives the wait generator by hand, so none of them ever entered the
    scheduler loop, which used to abort the run the instant the last agent parked
    on a remote endpoint.
    '''

    def setUp(self):
        self.peer = E.EndpointRegistry()

    def tearDown(self):
        self.peer.reclaim()

    def _driver(self, agent, runtime, handle=None):
        result = {}

        def _run():
            try:
                result['value'] = runtime.run(handle or runtime.spawn(agent, 'task'))
            except BaseException as exc:            # noqa: BLE001 - reported below
                result['error'] = exc

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        return thread, result

    def _service_url(self, runtime):
        for service in list(runtime.endpoints.services.values()):
            if service.started:
                return service.url
        return None

    def test_a_parked_service_ep_wait_does_not_deadlock(self):
        '''S1: the reviewer's reproduction, turned green.'''
        agent, eng, ws = _make_agent(WAIT_EP_FIXTURE)
        try:
            runtime = AgentRuntime(ui=agent.ui)
            agent.runtime = runtime
            agent.llm = _RecordingLLM(_terminating_responses())
            thread, result = self._driver(agent, runtime)

            url = _wait_until(lambda: self._service_url(runtime) or False,
                              timeout=10.0)
            service_url = self._service_url(runtime)
            self.assertIsNotNone(service_url, 'the listener never started')
            self.peer.connect(service_url)

            thread.join(timeout=15.0)
            self.assertFalse(thread.is_alive(), 'the run never finished')
            self.assertNotIn('error', result, result.get('error'))
            self.assertTrue(agent.llm.saw('[REMOTE CONNECTION]'),
                            'the stage was not prompted with the connection')
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_a_parked_cep_wait_does_not_deadlock(self):
        '''S2: a parked conversation wait survives until the peer speaks.'''
        agent, eng, ws = _make_agent(WAIT_CEP_FIXTURE)
        try:
            runtime = AgentRuntime(ui=agent.ui)
            agent.runtime = runtime
            agent.llm = _RecordingLLM(_terminating_responses())
            cep = runtime.endpoints.register_cep(
                E.Cep('http://127.0.0.1:9/crew/self/cep/1/v1', direction='inbound'))
            runtime.bind_event('@channel', cep.url)
            thread, result = self._driver(agent, runtime)

            _wait_until(lambda: any(name == cep.url for name in runtime.waiting),
                        timeout=10.0)
            cep.inbox.put('peer says hello')
            runtime._notify_endpoint(cep.url)

            thread.join(timeout=15.0)
            self.assertFalse(thread.is_alive(), 'the run never finished')
            self.assertNotIn('error', result, result.get('error'))
            self.assertTrue(agent.llm.saw('[REMOTE MESSAGE]'))
            self.assertTrue(agent.llm.saw('peer says hello'))
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_an_unanswerable_endpoint_wait_still_ends_loudly(self):
        '''S3: the detector is DELAYED for endpoints, never disabled.'''
        from yaha.core.runtime import RuntimeDeadlockError
        agent, eng, ws = _make_agent(WAIT_EP_FIXTURE)
        try:
            runtime = AgentRuntime(ui=agent.ui, endpoint_idle_timeout=0.3)
            agent.runtime = runtime
            agent.llm = _RecordingLLM(_terminating_responses())
            with self.assertRaises(RuntimeDeadlockError) as caught:
                runtime.run(runtime.spawn(agent, 'task'))
            self.assertIn('no remote endpoint answered', str(caught.exception))
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_a_stuck_local_wait_still_deadlocks_as_before(self):
        '''S4: no endpoint in play, so the new branch must be inert.'''
        from yaha.core.runtime import RuntimeDeadlockError
        agent, eng, ws = _make_agent(WAIT_LOCAL_FIXTURE)
        try:
            runtime = AgentRuntime(ui=agent.ui)
            agent.runtime = runtime
            agent.llm = _RecordingLLM(_terminating_responses())
            with self.assertRaises(RuntimeDeadlockError) as caught:
                runtime.run(runtime.spawn(agent, 'task'))
            self.assertIn('no event can be set', str(caught.exception))
        finally:
            shutil.rmtree(ws, ignore_errors=True)

class ReParkTests(unittest.TestCase):
    '''A wake that produced no item must RE-PARK, never fail the stage (F2).'''

    def setUp(self):
        self.agent, self.eng, self.ws = _make_agent()
        self.runtime = AgentRuntime(ui=self.agent.ui)
        self.agent.runtime = self.runtime
        self.peer = E.EndpointRegistry()

    def tearDown(self):
        self.peer.reclaim()
        self.runtime.endpoints.reclaim()
        shutil.rmtree(self.ws, ignore_errors=True)

    def test_a_spurious_service_wake_re_parks(self):
        gen = self.agent._await_endpoint('http://127.0.0.1:0/crew/sp/v1')
        first = next(gen)
        self.assertEqual(first[0], 'wait')
        again = next(gen)                 # no connection arrived: re-park
        self.assertEqual(again, first)
        self.peer.connect(first[1])       # now a real one
        name, error, notice = _resume(gen)
        self.assertIsNone(error)
        self.assertTrue(notice.startswith('[REMOTE CONNECTION]'))

    def test_two_waiters_on_one_service_ep(self):
        first_gen = self.agent._await_endpoint('http://127.0.0.1:0/crew/two/v1')
        first_token = next(first_gen)
        second_gen = self.agent._await_endpoint('http://127.0.0.1:0/crew/two/v1')
        second_token = next(second_gen)
        self.assertEqual(first_token, second_token)   # same service EP
        self.peer.connect(first_token[1])             # ONE connection
        name, error, notice = _resume(first_gen)
        self.assertIsNone(error)
        self.assertTrue(notice.startswith('[REMOTE CONNECTION]'))
        loser = next(second_gen)                      # the loser re-parks
        self.assertEqual(loser, second_token)

    def test_a_closed_conversation_still_fails_the_stage(self):
        cep = self.runtime.endpoints.register_cep(
            E.Cep('http://127.0.0.1:9/crew/self/cep/2/v1', direction='inbound'))
        cep.close()
        gen = self.agent._await_endpoint(cep.url)
        name, error, notice = _resume(gen)
        self.assertIsNotNone(error)
        self.assertIn('closed', error)
        self.assertIsNone(notice)
