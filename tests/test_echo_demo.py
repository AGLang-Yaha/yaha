#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

'''End-to-end tests for the two-agent echo example in ``examples/echo_agent``.

The example is a REAL workspace example: two agent classes that inherit
``BaseAgent`` and are run as ordinary agents (``yaha -a Eco`` / ``yaha -a Ecl``),
not by a Python driver. These tests therefore:

* build their workspace the way ``yaha --init`` builds one - the shipped template
  closure is copied into ``.yaha/`` so ``BaseAgent`` is present, then the two
  example classes are APPENDED to ``.yaha/AgentClass.md``. That is the only
  layout in which inheritance can resolve (see the README's note on ``-I``);
* assert the RESOLVED configuration, so the example cannot silently lose its
  parent's system prompt, toolset, snap policy or detached ``Compression`` stage;
* drive two REAL agents - one ``AgentRuntime`` each, meeting only over real HTTP -
  through the real cooperative scheduler. Nothing here hand-drives a wait
  generator, which is the shortcut that once let a scheduler bug pass a green gate;
* keep the class text under test READ FROM THE SHIPPED FILE, so the text the
  README tells a user to paste is the text that is executed.

The example ships no script, so its scripted test brains live HERE: a brain only
reads the stage prompt and its own tool results, exactly what a model sees.
'''
import re
import shutil
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import json
from pathlib import Path

from yaha.config.agents import parse_agent_class_md
from yaha.core.agent import Agent
from yaha.core.engine import YAHAEngine
from yaha.core.runtime import (AgentRuntime,
                               RuntimeDeadlockError,
                               validate_wait_events)

HOME = Path(__file__).resolve().parent.parent
TMP = HOME / 'tmp'
TEMPLATES = HOME / 'yaha' / 'templates'
EXAMPLE = HOME / 'examples' / 'echo_agent'
FRAGMENT = EXAMPLE / 'echo_agent.md'
DEFAULT_PORT = 8123
DEFAULT_PATH = '/echo/v1'
STOP_WORD = 'quit'
NO_REPLY_MARKER = '[no reply] (user submitted empty input repeatedly)'

_LT = chr(60)
_GT = chr(62)
_TOOL_OPEN = _LT + 'tool_code' + _GT
_TOOL_CLOSE = _LT + '/tool_code' + _GT
CONNECTION_NOTICE = '[REMOTE CONNECTION]:'
MESSAGE_NOTICE = '[REMOTE MESSAGE]:'


def tool_call(name, args):
    return _TOOL_OPEN + json.dumps({'tool': name, 'args': args}) + _TOOL_CLOSE


def free_port():
    sock = socket.socket()
    try:
        sock.bind(('127.0.0.1', 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def service_url(port):
    return 'http://127.0.0.1:%d%s' % (int(port), DEFAULT_PATH)


def fragment_text(port):
    return FRAGMENT.read_text().replace(':%d/' % DEFAULT_PORT, ':%d/' % int(port))


def make_workspace(port):
    '''A workspace the way ``--init`` makes one, plus the example classes.'''
    TMP.mkdir(parents=True, exist_ok=True)
    ws = Path(tempfile.mkdtemp(prefix='yaha_echo_', dir=str(TMP)))
    yh = ws / '.yaha'
    yh.mkdir(parents=True)
    for sub in ('memory', 'ext', 'logs'):
        (yh / sub).mkdir()
    # The shipped template closure: this is what makes `BaseAgent` resolvable.
    for template in sorted(TEMPLATES.glob('*.md')):
        shutil.copy(template, yh / template.name)
    with (yh / 'AgentClass.md').open('a') as handle:
        handle.write('\n\n' + fragment_text(port))
    return ws


def workspace_classes(workspace):
    return parse_agent_class_md(str(Path(workspace) / '.yaha' / 'AgentClass.md'))


def build_agents(workspace):
    engine = YAHAEngine(workspace_dir=str(workspace))
    agents = {}
    for name in ('EchoServer', 'EchoClient'):
        agents[name] = Agent(
            agent_config=engine.agent_classes[name],
            session=engine.session,
            models_config=engine.models_config,
            agent_classes=engine.agent_classes,
            tool_schemas=engine.tool_schemas,
            access_controller=engine.access_controller,
        )
    return engine, agents


def _wait_until(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _http(url, payload=None, method='POST', timeout=10.0):
    data = None if payload is None else json.dumps(payload).encode('utf-8')
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header('Content-Type', 'application/json')
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode('utf-8')
    return json.loads(body) if body else None


# --- the scripted brains (the example ships none) --------------------------

def last_user_text(messages):
    for message in reversed(list(messages or [])):
        if isinstance(message, dict) and message.get('role') == 'user':
            return str(message.get('content') or '')
    return ''


def last_notice(text, marker):
    '''The LAST marker occurrence: the runtime APPENDS the notice to the prompt.'''
    found = re.findall(re.escape(marker) + r'[ \t]*(.*)', text or '')
    return found[-1].strip() if found else None


def tool_result(text):
    if not str(text or '').startswith('TOOL RESULT ('):
        return None, ''
    name = str(text).split('(', 1)[1].split(')', 1)[0]
    body = str(text).split('```')
    if len(body) < 2:
        body = str(text).split('~~~')
    return name, (body[1].strip() if len(body) > 1 else str(text))


class ScriptedBrain:
    def __init__(self):
        self.seen = []
        self.calls = 0

    def generate(self, messages, **kwargs):
        text = last_user_text(messages)
        self.seen.append(text)
        self.calls += 1
        return self.reply(text), {'prompt_tokens': 5, 'completion_tokens': 5}, None

    def saw(self, needle):
        return any(needle in text for text in self.seen)

    def reply(self, text):                    # pragma: no cover
        return 'done'


class ServerBrain(ScriptedBrain):
    '''Binds the conversation, then echoes every message back.'''

    def __init__(self):
        ScriptedBrain.__init__(self)
        self.bound = ''
        self.echoes = []
        self.closed = False

    def reply(self, text):
        name, content = tool_result(text)
        if name == 'bind_tool':
            return tool_call('success_tool', {'summary': 'conversation bound'})
        if name == 'send_tool':
            return tool_call('loop_tool', {})
        if name == 'close_tool':
            self.closed = True
            return tool_call('success_tool', {'summary': 'session closed'})
        connected = last_notice(text, CONNECTION_NOTICE)
        if connected:
            self.bound = connected
            return tool_call('bind_tool', {'placeholder': '@peer',
                                           'real_event': connected})
        message = last_notice(text, MESSAGE_NOTICE)
        if message is not None:
            if message.strip() == STOP_WORD:
                return tool_call('close_tool', {'cep': '@peer'})
            self.echoes.append(message)
            return tool_call('send_tool', {'cep': '@peer',
                                           'message': 'echo: ' + message})
        return 'done'


class ClientBrain(ScriptedBrain):
    '''Connects, asks the operator, sends the answer verbatim.'''

    def __init__(self):
        ScriptedBrain.__init__(self)
        self.cep = ''
        self.answers = []
        self.received = []
        self.quitting = False

    def reply(self, text):
        name, content = tool_result(text)
        if name == 'connect_tool':
            match = re.search(r'CONNECTED\s+(\S+)', content)
            self.cep = match.group(1) if match else ''
            return tool_call('bind_tool', {'placeholder': '@reply',
                                           'real_event': self.cep})
        if name == 'bind_tool':
            return tool_call('success_tool', {'summary': 'conversation bound'})
        if name == 'ask_user':
            answer = content.strip()
            self.answers.append(answer)
            if answer == STOP_WORD or answer == NO_REPLY_MARKER or not answer:
                self.quitting = True
                return tool_call('send_tool', {'cep': '@reply',
                                               'message': STOP_WORD})
            return tool_call('send_tool', {'cep': '@reply', 'message': answer})
        if name == 'send_tool':
            if self.quitting:
                return tool_call('exit_to', {'name': 'Aftermath'})
            return tool_call('exit_to', {'name': 'Receive'})
        message = last_notice(text, MESSAGE_NOTICE)
        if message is not None:
            self.received.append(message)
            return tool_call('exit_to', {'name': 'Ask'})
        match = re.search(r"connect_tool\('([^']+)'\)", text)
        if match:
            return tool_call('connect_tool', {'service_ep': match.group(1)})
        if 'ask_user' in text:
            return tool_call('ask_user', {
                'question': 'What should the echo server send back? '
                            '(type quit to end the session)'})
        return 'done'


class InheritanceTests(unittest.TestCase):
    '''T1: the shipped classes are REAL workspace agents.'''

    @classmethod
    def setUpClass(cls):
        cls.workspace = make_workspace(DEFAULT_PORT)
        cls.classes = workspace_classes(cls.workspace)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.workspace, ignore_errors=True)

    def test_both_classes_inherit_baseagent_and_resolve_its_wiring(self):
        parent = self.classes['BaseAgent']
        for name in ('EchoServer', 'EchoClient'):
            child = self.classes[name]
            self.assertEqual(child.inherited, 'BaseAgent')
            # model + snap policy come from the parent
            self.assertEqual(child.model, parent.model)
            self.assertEqual(child.snap_handler, parent.snap_handler)
            self.assertEqual(child.snap_to, parent.snap_to)
            # the WORKER PROTOCOL system prompt is inherited, not replaced
            self.assertTrue(child.system_prompt.strip())
            self.assertEqual(child.system_prompt, parent.system_prompt)

    def test_the_toolset_is_extended_not_replaced(self):
        parent_tools = set(self.classes['BaseAgent'].toolset)
        server_tools = set(self.classes['EchoServer'].toolset)
        client_tools = set(self.classes['EchoClient'].toolset)
        self.assertTrue(parent_tools <= server_tools)
        self.assertTrue(parent_tools <= client_tools)
        self.assertTrue({'bind_tool', 'send_tool', 'close_tool'} <= server_tools)
        self.assertTrue({'connect_tool', 'bind_tool', 'send_tool'} <= client_tools)
        self.assertIn('bash_tool', server_tools)   # proof of the parent's tools

    def test_the_parents_detached_snap_stage_survives(self):
        for name in ('EchoServer', 'EchoClient'):
            self.assertIn('Compression', self.classes[name].detached_stages)

    def test_the_authored_stages_are_present_with_their_gates(self):
        server = {s['name']: s for s in self.classes['EchoServer'].prompt_chain}
        self.assertEqual(server['Preamble']['wait_event'],
                         'http://127.0.0.1:8123/echo/v1')
        self.assertEqual(server['Echo']['wait_event'], '@peer')
        self.assertEqual(server['Echo']['exit_tools'], ['loop_tool', 'success_tool'])
        client = {s['name']: s for s in self.classes['EchoClient'].prompt_chain}
        self.assertEqual(client['Receive']['wait_event'], '@reply')
        self.assertEqual(client['Ask']['exit_tools'], ['exit_to'])

    def test_every_wait_has_a_legal_producer(self):
        watched = {k: self.classes[k] for k in ('EchoServer', 'EchoClient')}
        self.assertEqual(validate_wait_events(watched), [])

    def test_the_client_can_only_send_the_answer_or_the_stop_word(self):
        text = FRAGMENT.read_text()
        payloads = re.findall(r"send_tool\('@reply', '([^']*)'\)", text)
        self.assertTrue(payloads)
        self.assertEqual(sorted(set(payloads)), ['<the answer>', STOP_WORD])

    def test_the_include_flag_resolves_inheritance_like_an_include(self):
        '''``-I`` appends the class file to the workspace text before resolution.

        Passing the shipped example via ``include_agent_files`` must yield the
        same fully-resolved class as installing it in the workspace: the parent is
        named AND applied (non-empty system prompt, the parent's tools and
        model). A bare single-file parse is unchanged: with no extras there is no
        parent in the text to resolve.
        '''
        TMP.mkdir(parents=True, exist_ok=True)
        workspace = Path(tempfile.mkdtemp(prefix='yaha_echo_iflag_', dir=str(TMP)))
        try:
            yaha_dir = workspace / '.yaha'
            for sub in ('memory', 'ext', 'logs'):
                (yaha_dir / sub).mkdir(parents=True)
            for template in sorted(TEMPLATES.glob('*.md')):
                shutil.copy(template, yaha_dir / template.name)
            engine = YAHAEngine(workspace_dir=str(workspace),
                                include_agent_files=[str(FRAGMENT)])
            resolved = engine.agent_classes['EchoServer']
        finally:
            shutil.rmtree(workspace, ignore_errors=True)
        self.assertEqual(resolved.inherited, 'BaseAgent')
        self.assertTrue(resolved.system_prompt.strip(),
                        'the parent was named but not applied')
        self.assertEqual(resolved.model, 'DefaultModel')
        self.assertIn('bash_tool', resolved.toolset)

        alone = parse_agent_class_md(str(FRAGMENT))
        self.assertEqual(alone['EchoServer'].inherited, 'BaseAgent')
        self.assertEqual(alone['EchoServer'].system_prompt, '')
        self.assertNotIn('bash_tool', alone['EchoServer'].toolset)


class TwoAgentRoundTripTests(unittest.TestCase):
    '''T2/T3: two real agents, two real runtimes, one real conversation.'''

    def setUp(self):
        self.port = free_port()
        self.workspace = make_workspace(self.port)
        _, agents = build_agents(self.workspace)
        self.server, self.client = agents['EchoServer'], agents['EchoClient']

    def tearDown(self):
        shutil.rmtree(self.workspace, ignore_errors=True)

    def _drive_both(self, answers):
        pending = list(answers)
        self.client._prompt_interrupt_safe = lambda _q: (pending.pop(0)
                                                         if pending else STOP_WORD)
        self.server.llm = ServerBrain()
        self.client.llm = ClientBrain()

        server_runtime = AgentRuntime(ui=self.server.ui)
        client_runtime = AgentRuntime(ui=self.client.ui)
        server_runtime.spawn(self.server, 'serve the echo endpoint')
        client_runtime.spawn(self.client, 'talk to the echo server')
        outcome = {'server': {}, 'client': {}}

        def _run(runtime, box):
            try:
                box['value'] = runtime.run()
            except BaseException as exc:                 # noqa: BLE001
                box['error'] = exc

        workers = {
            'server': threading.Thread(
                target=_run, args=(server_runtime, outcome['server']), daemon=True),
            'client': threading.Thread(
                target=_run, args=(client_runtime, outcome['client']), daemon=True),
        }
        workers['server'].start()
        self.assertTrue(_wait_until(lambda: any(
            getattr(svc, 'started', False)
            for svc in server_runtime.endpoints.services.values())),
            'the server never bound its service endpoint')
        workers['client'].start()
        for worker in workers.values():
            worker.join(timeout=30.0)
        return server_runtime, client_runtime, workers, outcome

    def test_the_operators_message_makes_the_round_trip(self):
        message = 'a purple elephant'
        server_runtime, client_runtime, workers, outcome = self._drive_both(
            [message, STOP_WORD])
        for name, worker in workers.items():
            self.assertFalse(worker.is_alive(), 'the %s side never finished' % name)
        for name, box in outcome.items():
            self.assertNotIn('error', box, box.get('error'))
        self.assertEqual(self.client.llm.answers, [message, STOP_WORD])
        self.assertEqual(self.server.llm.echoes, [message])
        self.assertEqual(self.client.llm.received, ['echo: ' + message])
        self.assertIn('/cep/', self.server.llm.bound)
        self.assertTrue(self.server.llm.closed, 'the session was never closed')
        empty = {'services': [], 'ceps': [], 'channels': [], 'listeners': []}
        self.assertEqual(server_runtime.endpoints.snapshot(), empty)
        self.assertEqual(client_runtime.endpoints.snapshot(), empty)

    def test_an_empty_answer_ends_the_session_instead_of_hanging(self):
        server_runtime, client_runtime, workers, outcome = self._drive_both([''])
        for name, worker in workers.items():
            self.assertFalse(worker.is_alive(), 'the %s side never finished' % name)
        for name, box in outcome.items():
            self.assertNotIn('error', box, box.get('error'))
        self.assertTrue(self.server.llm.closed)
        self.assertEqual(client_runtime.endpoints.snapshot(),
                         {'services': [], 'ceps': [], 'channels': [], 'listeners': []})


class IdleGuardTests(unittest.TestCase):
    '''T4: no peer at all must still end LOUDLY, never hang.'''

    def test_a_server_with_no_client_ends_loudly(self):
        workspace = make_workspace(free_port())
        try:
            _, agents = build_agents(workspace)
            server = agents['EchoServer']
            server.llm = ServerBrain()
            runtime = AgentRuntime(ui=server.ui, endpoint_idle_timeout=0.3)
            runtime.spawn(server, 'serve the echo endpoint')
            with self.assertRaises(RuntimeDeadlockError) as caught:
                runtime.run()
            self.assertIn('no remote endpoint answered', str(caught.exception))
        finally:
            shutil.rmtree(workspace, ignore_errors=True)


class RawPeerTests(unittest.TestCase):
    '''T5: a peer needs no yaha at all - plain HTTP reaches the parked server.'''

    def test_a_plain_http_client_can_connect_send_and_receive(self):
        port = free_port()
        workspace = make_workspace(port)
        try:
            _, agents = build_agents(workspace)
            server = agents['EchoServer']
            server.llm = ServerBrain()
            runtime = AgentRuntime(ui=server.ui)
            runtime.spawn(server, 'serve the echo endpoint')
            outcome = {}

            def _run():
                try:
                    outcome['value'] = runtime.run()
                except BaseException as exc:             # noqa: BLE001
                    outcome['error'] = exc

            worker = threading.Thread(target=_run, daemon=True)
            worker.start()
            self.assertTrue(_wait_until(lambda: any(
                getattr(svc, 'started', False)
                for svc in runtime.endpoints.services.values())),
                'the server never bound its service endpoint')

            url = service_url(port)
            cep = _http(url, {'connect': True})['cep']
            self.assertIn('/cep/', cep)
            self.assertEqual(_http(cep, {'message': 'hello'}), {'ok': True})
            answer, deadline = None, time.monotonic() + 15.0
            while answer is None and time.monotonic() < deadline:
                answer = (_http(cep + '?timeout=1', method='GET') or {}).get('message')
            self.assertEqual(answer, 'echo: hello')
            with self.assertRaises(urllib.error.HTTPError) as caught:
                _http(url + '/nope', {'connect': True})
            self.assertEqual(caught.exception.code, 404)
            _http(cep, {'message': STOP_WORD})
            worker.join(timeout=15.0)
            self.assertFalse(worker.is_alive(), 'the server never finished')
            self.assertNotIn('error', outcome, outcome.get('error'))
            self.assertEqual(runtime.endpoints.snapshot(),
                             {'services': [], 'ceps': [], 'channels': [], 'listeners': []})
        finally:
            shutil.rmtree(workspace, ignore_errors=True)


class DocumentationTests(unittest.TestCase):
    '''T6: the README's commands match the shipped classes (anti-drift).'''

    @classmethod
    def setUpClass(cls):
        cls.workspace = make_workspace(DEFAULT_PORT)
        cls.classes = workspace_classes(cls.workspace)
        cls.readme = (EXAMPLE / 'README.md').read_text()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.workspace, ignore_errors=True)

    def test_the_documented_agent_codes_exist(self):
        codes = {name: self.classes[name].code for name in ('EchoServer', 'EchoClient')}
        self.assertEqual(codes, {'EchoServer': 'Eco', 'EchoClient': 'Ecl'})
        for code in codes.values():
            self.assertIn('yaha -a %s' % code, self.readme)

    def test_the_readme_does_not_point_at_the_removed_script(self):
        self.assertFalse((EXAMPLE / 'demo.py').exists())
        # A whole-word check: 'demo.py' must not appear as a file of its own
        # ('test_echo_demo.py' legitimately contains it).
        self.assertIsNone(re.search(r'(?<![A-Za-z0-9_])demo\.py', self.readme))

    def test_the_documented_include_layout_really_resolves_inheritance(self):
        '''The include method must WORK, not merely be described.

        The include path resolves relative to the INCLUDING file, so the layout
        the README documents is: the example copied into ``.yaha/`` and ONE
        include line appended to ``.yaha/AgentClass.md``. Building it here means
        the install method a user is most likely to get wrong cannot rot.
        '''
        TMP.mkdir(parents=True, exist_ok=True)
        workspace = Path(tempfile.mkdtemp(prefix='yaha_echo_inc_', dir=str(TMP)))
        try:
            yaha_dir = workspace / '.yaha'
            yaha_dir.mkdir(parents=True)
            for sub in ('memory', 'ext', 'logs'):
                (yaha_dir / sub).mkdir()
            for template in sorted(TEMPLATES.glob('*.md')):
                shutil.copy(template, yaha_dir / template.name)
            shutil.copy(FRAGMENT, yaha_dir / 'echo_agent.md')
            with (yaha_dir / 'AgentClass.md').open('a') as handle:
                handle.write('\n\n<!--include path="echo_agent.md" -->\n')
            classes = parse_agent_class_md(str(yaha_dir / 'AgentClass.md'))
        finally:
            shutil.rmtree(workspace, ignore_errors=True)
        for name in ('EchoServer', 'EchoClient'):
            child = classes[name]
            self.assertEqual(child.inherited, 'BaseAgent')
            self.assertTrue(child.system_prompt.strip(),
                            '%s lost the inherited system prompt' % name)
            self.assertIn('bash_tool', child.toolset)

    def test_the_readme_states_where_the_included_file_must_live(self):
        self.assertIn('AgentClass.md', self.readme)
        self.assertIn('-I', self.readme)      # the -I install path is documented too
        self.assertIn('relative to the file that contains the line', self.readme)


if __name__ == '__main__':
    unittest.main()
