#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

'''Tests for the interrupt diagnose venue (chat + slash commands).

The venue replaced the old bare-keyword scratchpad. The contract under test:

- a line WITHOUT a leading slash is a question to the model: it never moves the
  chain and never mutates chain history;
- only a slash command changes the execution flow;
- the command table in core/interrupt_commands.py is the single source of truth
  for names, arg arity plus the /help text;
- the only history mutation is /resume with steer text;
- ask_user honours /resume <text> as the answer and defers chain moves.

No TTY exists in the sandbox, so sys.stdin is faked and ui.interrupt_read is
scripted with a list of lines.
'''
import contextlib
import sys
import types
import unittest
from unittest.mock import patch

from yaha.core.agent import Agent, VenueResult
from yaha.core import interrupt_commands as ic
from yaha.core.md_loader import MarkdownLoadError


class _FakeTTY:
    def __init__(self, is_tty=True):
        self._is_tty = is_tty

    def isatty(self):
        return self._is_tty


class _FakeListener:
    '''Counts the REAL suspend/resume transitions of a shared controller.'''

    def __init__(self):
        self.suspends = 0
        self.resumes = 0

    def suspend(self):
        self.suspends += 1

    def resume(self):
        self.resumes += 1

    def stop(self):
        pass

    def start(self):
        pass


class _ScriptedLLM:
    '''Deterministic provider: returns scripted answers, records the messages.'''

    def __init__(self, answers=None, usage=True):
        self.answers = list(answers or [])
        self.calls = []
        self.usage = {'prompt_tokens': 7, 'completion_tokens': 7} if usage else None

    def generate(self, messages, **kwargs):
        self.calls.append(messages)
        if not self.answers:
            raise AssertionError('no scripted answers left')
        return self.answers.pop(0), self.usage, None


class _MixedLLM:
    '''Provider that can raise, return an empty string, or answer.'''

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def generate(self, messages, **kwargs):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item, {'prompt_tokens': 5, 'completion_tokens': 5}, None


def _make_agent(lines, llm=None, stage=None):
    '''Build a venue-ready Agent with a scripted input box.'''
    agent = object.__new__(Agent)
    agent.config = types.SimpleNamespace(
        name='TestAgent', model='mock-model', prompt_chain=[],
        detached_stages={}, fail_target=None, system_prompt='sys', toolset=['ask_user'],
    )
    agent.agent_id = 'abc123'
    agent.caller_id = None
    agent.soul_prompt = 'soul'
    agent.history = [{'role': 'system', 'content': 'sys prompt'}]
    agent.context_size = 128000
    agent.compress_count = 0
    agent._last_prompt_tokens = 0
    agent.agent_classes = {}
    agent.runtime = None
    agent.yaha_load_classes = None
    agent.yaha_spawn_frame = None
    agent._venue_stack = []
    agent.current_task_prompt = 'the task'
    agent.stdin_interrupt = None
    agent._serving_interrupt = False
    agent._compressing = False
    agent._pending_interrupt_status = None
    agent._current_turn = 3
    agent._last_stage_turn = 1
    agent._last_stage_tools_called = set()
    agent._chain_index = 0
    agent._chain_len = 2
    agent._venue_entry = None
    agent._last_tool_result = None
    agent.llm_retry_limit = 10
    agent.api_retry_unattended_max_rounds = 3
    agent.turn_limit = 200
    agent.logger = types.SimpleNamespace(log_turn=lambda *a, **k: None)
    # Use the REAL suspend/resume: they are no-ops when stdin_interrupt is
    # None, and they exercise a supplied controller (the rev1 nesting test).
    agent._interrupt_suspend = Agent._interrupt_suspend.__get__(agent)
    agent._interrupt_resume = Agent._interrupt_resume.__get__(agent)
    agent.llm = llm or _ScriptedLLM(['a model answer'])
    box = list(lines)
    printed = {'answers': [], 'help': [], 'status': [], 'notes': [], 'warnings': [], 'info': [], 'list': []}
    agent.ui = types.SimpleNamespace(
        interrupt_banner=lambda *a, **k: None,
        interrupt_read=lambda *a, **k: (box.pop(0) if box else None),
        interrupt_answer=lambda *a, **k: printed['answers'].append(a[-1]),
        interrupt_help=lambda *a, **k: printed['help'].append(a[-1]),
        interrupt_status=lambda *a, **k: printed['status'].append(a[-1]),
        interrupt_note=lambda *a, **k: printed['notes'].append(a[-1]),
        interrupt_info=lambda *a, **k: printed['info'].append(a[-1]),
        interrupt_list=lambda *a, **k: printed['list'].append((a[-3], a[-2], a[-1])),
        display_warning=lambda *a, **k: printed['warnings'].append(a[-1]),
        spinner=lambda *a, **k: contextlib.nullcontext(),
    )
    agent._printed = printed
    agent._box = box
    return agent


def _run_venue(agent, stage=None, is_tty=True, frames=None, spawned=False):
    stage = stage if stage is not None else {'name': 'Coding'}
    with patch('sys.stdin', _FakeTTY(is_tty)):
        return Agent._serve_interrupt_venue(agent, stage, 2, set(),
                                            frames=frames, spawned=spawned)


# --- table / help ----------------------------------------------------------

def test_table_well_formed():
    seen = set()
    for spec in ic.INTERRUPT_COMMANDS:
        assert spec.name and spec.name not in seen, spec.name
        seen.add(spec.name)
        assert spec.help, spec.name
        assert spec.arg in (ic.ARG_NONE, ic.ARG_REQUIRED, ic.ARG_OPTIONAL), spec.arg
        assert spec.kind in (ic.KIND_LOOP, ic.KIND_FINAL), spec.kind
        if spec.kind == ic.KIND_FINAL:
            assert spec.status, spec.name
        else:
            assert spec.status == '', spec.name
    assert 'resume' in seen and 'quit' in seen and 'help' in seen
    print('test_table_well_formed OK')


def test_help_lists_exactly_the_table():
    menu = ic.render_help()
    for spec in ic.INTERRUPT_COMMANDS:
        assert ic.usage_of(spec) in menu, (spec.name, menu)
    assert ic.PREFIX + 'details' not in menu
    print('test_help_lists_exactly_the_table OK')


def test_is_command_and_parse():
    assert ic.is_command('/resume') is True
    assert ic.is_command('  /resume ') is True
    assert ic.is_command('resume') is False
    assert ic.is_command('') is False
    assert ic.is_command(None) is False

    spec, arg = ic.parse_interrupt_line('/jump Coding')
    assert spec is not None and spec.name == 'jump' and arg == 'Coding'
    spec, arg = ic.parse_interrupt_line('  /JUMP   Coding  ')
    assert spec is not None and spec.name == 'jump' and arg == 'Coding'
    spec, arg = ic.parse_interrupt_line('/resume')
    assert spec is not None and spec.name == 'resume' and arg is None
    spec, arg = ic.parse_interrupt_line('/nope x')
    assert spec is None and arg == 'x'
    assert ic.parse_interrupt_line('just a question') == (None, None)
    assert ic.parse_interrupt_line('') == (None, None)
    assert ic.command_token('/jump Coding') == 'jump'
    print('test_is_command_and_parse OK')


def test_arg_rules():
    agent = _make_agent(['/jump', '/rewind now', '/resume'])
    result = _run_venue(agent)
    assert result.status == ic.STATUS_RESUME, result
    assert len(agent._printed['warnings']) == 2, agent._printed['warnings']
    assert 'Usage: /jump <stage>' in agent._printed['warnings'][0]
    assert 'Usage: /rewind' in agent._printed['warnings'][1]
    print('test_arg_rules OK')


# --- chat mode -------------------------------------------------------------

def test_free_text_calls_the_model_and_stays_in_venue():
    llm = _ScriptedLLM(['we are in Coding'])
    agent = _make_agent(['what stage are we in?', '/resume'], llm=llm)
    before = list(agent.history)
    result = _run_venue(agent)
    assert result.status == ic.STATUS_RESUME, result
    assert result.text is None, result
    assert len(llm.calls) == 1, llm.calls
    assert agent._printed['answers'] == ['we are in Coding'], agent._printed['answers']
    assert agent.history == before, 'chat must not mutate history'
    print('test_free_text_calls_the_model_and_stays_in_venue OK')


def test_chat_never_mutates_history():
    llm = _ScriptedLLM(['one', 'two', 'three'])
    agent = _make_agent(['q1', 'q2', 'q3', '/resume'], llm=llm)
    snapshot = list(agent.history)
    last_identity = agent.history[-1]
    _run_venue(agent)
    assert agent.history == snapshot, agent.history
    assert agent.history[-1] is last_identity, 'history objects must be untouched'
    assert len(llm.calls) == 3
    print('test_chat_never_mutates_history OK')


def test_chat_failure_keeps_venue_alive():
    llm = _MixedLLM([RuntimeError('boom'), '', 'recovered'])
    agent = _make_agent(['q1', 'q2', 'q3', '/resume'], llm=llm)
    result = _run_venue(agent)
    assert result.status == ic.STATUS_RESUME, result
    answers = agent._printed['answers']
    assert len(answers) == 3, answers
    assert 'chat failed' in answers[0], answers[0]
    assert 'chat failed' in answers[1], answers[1]
    assert answers[2] == 'recovered', answers[2]
    assert llm.calls == 3
    print('test_chat_failure_keeps_venue_alive OK')


def test_chat_transcript_gives_followup_context():
    llm = _ScriptedLLM(['first answer', 'second answer'])
    agent = _make_agent(['first?', 'and why?', '/resume'], llm=llm)
    _run_venue(agent)
    assert len(llm.calls) == 2
    second = llm.calls[1]
    joined = ' '.join(str(m.get('content') or '') for m in second)
    assert 'first?' in joined, joined
    assert 'first answer' in joined, joined
    assert second[0].get('role') == 'system'
    assert 'INTERRUPT VENUE' in second[0].get('content'), second[0]
    print('test_chat_transcript_gives_followup_context OK')


# --- flow commands ---------------------------------------------------------

def test_resume_bare_vs_text():
    agent = _make_agent(['/resume'])
    result = _run_venue(agent)
    assert result == VenueResult(ic.STATUS_RESUME, None, None), result
    assert len(agent.history) == 1, 'bare resume injects nothing'

    agent = _make_agent(['/resume look at the logs'])
    result = _run_venue(agent)
    assert result == VenueResult(ic.STATUS_RESUME, None, 'look at the logs'), result
    assert agent._printed['notes'], 'resume prints a confirmation'
    assert len(agent.history) == 1, 'the venue itself never injects history'
    print('test_resume_bare_vs_text OK')


def test_rewind_restart_quit_jump_statuses():
    agent = _make_agent(['/rewind'])
    assert _run_venue(agent) == VenueResult(ic.STATUS_REWIND, None, None)

    agent = _make_agent(['/restart'])
    assert _run_venue(agent) == VenueResult(ic.STATUS_RESTART, None, None)

    agent = _make_agent(['/jump Coding'])
    assert _run_venue(agent) == VenueResult('INTERRUPT_JUMP:Coding', 'Coding', None)

    agent = _make_agent(['/quit'])
    result = _run_venue(agent, stage={'name': 'Coding', 'fail_target': 'Plan'})
    assert result == VenueResult('FAIL:Plan', None, None), result

    agent = _make_agent(['/quit'])
    result = _run_venue(agent, stage={'name': 'ask_user'})
    assert result == VenueResult('FAIL:aftermath', None, None), result
    print('test_rewind_restart_quit_jump_statuses OK')


def test_status_report_fields():
    agent = _make_agent(['/status', '/resume'])
    _run_venue(agent, stage={'name': 'Coding', 'max_turns': 40})
    report = agent._printed['status'][0]
    assert 'runtime truth' in report, report
    assert 'stage=Coding' in report, report
    assert 'chain 1/2' in report, report
    assert 'global 3' in report, report
    assert 'TestAgent' in report, report
    assert 'mock-model' in report, report
    assert 'history     1 messages' in report, report
    print('test_status_report_fields OK')


def test_status_report_shows_workspace_and_log():
    agent = _make_agent(['/status', '/resume'])
    agent.session = types.SimpleNamespace(workspace_dir='/tmp/ws')
    agent.logger = types.SimpleNamespace(
        log_turn=lambda *a, **k: None,
        log_path='.yaha/logs/sess-1/TestAgent-abc123.md',
    )
    _run_venue(agent)
    report = agent._printed['status'][0]
    assert 'workspace   /tmp/ws' in report, report
    assert 'log         /tmp/ws/.yaha/logs/sess-1/TestAgent-abc123.md' in report, report

    # An already-absolute logger path is printed untouched.
    absolute = _make_agent(['/status', '/resume'])
    absolute.session = types.SimpleNamespace(workspace_dir='/tmp/ws')
    absolute.logger = types.SimpleNamespace(
        log_turn=lambda *a, **k: None,
        log_path='/elsewhere/other.md',
    )
    _run_venue(absolute)
    assert 'log         /elsewhere/other.md' in absolute._printed['status'][0]

    # Graceful degradation: no session and no logger path at all.
    bare = _make_agent(['/status', '/resume'])
    bare.__dict__.pop('session', None)
    bare.logger = types.SimpleNamespace(log_turn=lambda *a, **k: None)
    _run_venue(bare)
    bare_report = bare._printed['status'][0]
    assert 'workspace   (unknown)' in bare_report, bare_report
    assert 'log         (none)' in bare_report, bare_report
    print('test_status_report_shows_workspace_and_log OK')


def test_help_and_status_are_loop_commands():
    llm = _ScriptedLLM(['unused'])
    agent = _make_agent(['/help', '/status', '/resume'], llm=llm)
    result = _run_venue(agent)
    assert result.status == ic.STATUS_RESUME, result
    assert agent._printed['help'], 'help output missing'
    assert agent._printed['status'], 'status output missing'
    assert llm.calls == [], 'loop commands must not call the model'
    print('test_help_and_status_are_loop_commands OK')


def test_call_and_spawn_display_output():
    llm = _ScriptedLLM(['detached digest'])
    agent = _make_agent(['/call Diag', '/resume'], llm=llm)
    agent.config.detached_stages = {'Diag': {'content': 'diagnose this'}}
    _run_venue(agent)
    assert agent._printed['answers'] == ['detached digest'], agent._printed['answers']

    # `/spawn` now PUSHES a frame and enters the child's venue (Round 2); the
    # one-shot digest behaviour lives on `/call`.
    child = _make_agent(['/quit', '/resume'])
    agent = _make_agent(['/spawn NoAgent do it', '/resume'])
    agent.yaha_spawn_frame = lambda agent_class, runtime=None: child
    _run_venue(agent)
    assert any('pushed frame' in n for n in agent._printed['notes']), agent._printed['notes']
    assert any('popped frame' in n for n in agent._printed['notes']), agent._printed['notes']

    agent = _make_agent(['/call Missing', '/resume'])
    _run_venue(agent)
    assert agent._printed['warnings'], 'missing detached stage must warn'
    print('test_call_and_spawn_display_output OK')


# --- ask_user --------------------------------------------------------------

def test_ask_user_resume_text_is_the_answer():
    agent = _make_agent([])
    agent._serve_interrupt_venue = lambda *a, **k: VenueResult(
        ic.STATUS_RESUME, None, 'the answer text'
    )
    action, payload = Agent._handle_ask_user_interrupt(agent, 'Q?')
    assert action == 'answer', (action, payload)
    assert payload == 'the answer text', payload

    agent._serve_interrupt_venue = lambda *a, **k: VenueResult(ic.STATUS_RESUME, None, None)
    assert Agent._handle_ask_user_interrupt(agent, 'Q?') == ('reask', None)
    print('test_ask_user_resume_text_is_the_answer OK')


def test_ask_user_deferred_matrix():
    cases = [
        (ic.STATUS_REWIND, 'rewind'),
        ('INTERRUPT_JUMP:Coding', 'jump'),
        (ic.STATUS_RESTART, 'restart'),
        ('FAIL:Plan', 'quit'),
    ]
    for status, label in cases:
        agent = _make_agent([])
        agent._serve_interrupt_venue = lambda *a, **k: VenueResult(status, None, None)
        action, marker = Agent._handle_ask_user_interrupt(agent, 'Q?')
        assert action == 'marker', (status, action)
        assert label in marker, (status, marker)
        assert agent._pending_interrupt_status == status, agent._pending_interrupt_status

        consumed = Agent._consume_deferred_interrupt_status(agent, 5)
        assert consumed is not None and consumed[1] == status, (status, consumed)
        assert consumed[2] == 5, consumed
        assert agent._pending_interrupt_status is None
        assert agent.history, 'a synthetic stand-in message must be recorded'
        assert Agent._consume_deferred_interrupt_status(agent, 5) is None
    print('test_ask_user_deferred_matrix OK')


# --- environment -----------------------------------------------------------

def test_non_tty_resumes_immediately():
    llm = _ScriptedLLM(['should not be called'])
    agent = _make_agent(['/quit'], llm=llm)
    result = _run_venue(agent, is_tty=False)
    assert result == VenueResult(ic.STATUS_RESUME, None, None), result
    assert llm.calls == [], 'non-TTY must not call the model'
    assert agent.history == [{'role': 'system', 'content': 'sys prompt'}], agent.history
    print('test_non_tty_resumes_immediately OK')


def test_empty_line_reprints_prompt_without_model_call():
    llm = _ScriptedLLM(['unused'])
    agent = _make_agent(['', '   ', '/resume'], llm=llm)
    result = _run_venue(agent)
    assert result.status == ic.STATUS_RESUME, result
    assert llm.calls == [], 'an empty line is not a question'
    assert agent._printed['warnings'] == [], 'an empty line is not an error'
    print('test_empty_line_reprints_prompt_without_model_call OK')


def test_unknown_slash_does_not_call_model():
    llm = _ScriptedLLM(['unused'])
    agent = _make_agent(['/foo bar', '/resume'], llm=llm)
    result = _run_venue(agent)
    assert result.status == ic.STATUS_RESUME, result
    assert llm.calls == [], 'a slash line is never a question'
    assert agent._printed['warnings'], 'unknown slash must warn'
    assert '/help' in agent._printed['warnings'][0], agent._printed['warnings'][0]
    print('test_unknown_slash_does_not_call_model OK')


def test_eof_resumes():
    agent = _make_agent([])
    result = _run_venue(agent)
    assert result == VenueResult(ic.STATUS_RESUME, None, None), result
    print('test_eof_resumes OK')


def test_boundary_poll_drains_pending_interrupt():
    agent = _make_agent(['/resume'])
    agent._last_stage_turn = 4
    agent._last_stage_tools_called = {'bash_tool'}
    cleared = []
    agent.stdin_interrupt = types.SimpleNamespace(
        is_pending=lambda: True,
        clear_pending=lambda: cleared.append(True),
    )
    result = Agent._maybe_boundary_venue(agent, {'name': 'Coding'})
    assert result is not None and result.status == ic.STATUS_RESUME, result
    assert cleared == [True], 'the pending flag must be consumed exactly once'

    agent.stdin_interrupt = types.SimpleNamespace(
        is_pending=lambda: False, clear_pending=lambda: None,
    )
    assert Agent._maybe_boundary_venue(agent, {'name': 'Coding'}) is None
    print('test_boundary_poll_drains_pending_interrupt OK')


class InterruptVenueTests(unittest.TestCase):
    '''Unittest-discoverable wrapper for the regression functions above.'''
    test_table_well_formed = staticmethod(test_table_well_formed)
    test_help_lists_exactly_the_table = staticmethod(test_help_lists_exactly_the_table)
    test_is_command_and_parse = staticmethod(test_is_command_and_parse)
    test_arg_rules = staticmethod(test_arg_rules)
    test_free_text_calls_the_model_and_stays_in_venue = staticmethod(test_free_text_calls_the_model_and_stays_in_venue)
    test_chat_never_mutates_history = staticmethod(test_chat_never_mutates_history)
    test_chat_failure_keeps_venue_alive = staticmethod(test_chat_failure_keeps_venue_alive)
    test_chat_transcript_gives_followup_context = staticmethod(test_chat_transcript_gives_followup_context)
    test_resume_bare_vs_text = staticmethod(test_resume_bare_vs_text)
    test_rewind_restart_quit_jump_statuses = staticmethod(test_rewind_restart_quit_jump_statuses)
    test_status_report_fields = staticmethod(test_status_report_fields)
    test_status_report_shows_workspace_and_log = staticmethod(test_status_report_shows_workspace_and_log)
    test_help_and_status_are_loop_commands = staticmethod(test_help_and_status_are_loop_commands)
    test_call_and_spawn_display_output = staticmethod(test_call_and_spawn_display_output)
    test_ask_user_resume_text_is_the_answer = staticmethod(test_ask_user_resume_text_is_the_answer)
    test_ask_user_deferred_matrix = staticmethod(test_ask_user_deferred_matrix)
    test_non_tty_resumes_immediately = staticmethod(test_non_tty_resumes_immediately)
    test_empty_line_reprints_prompt_without_model_call = staticmethod(test_empty_line_reprints_prompt_without_model_call)
    test_unknown_slash_does_not_call_model = staticmethod(test_unknown_slash_does_not_call_model)
    test_eof_resumes = staticmethod(test_eof_resumes)
    test_boundary_poll_drains_pending_interrupt = staticmethod(test_boundary_poll_drains_pending_interrupt)


class InterruptDebugCommandsTests(unittest.TestCase):
    '''New read-only venue commands: /history browser, /memory, /chain, /compress.'''

    def _history(self, n):
        history = [{'role': 'system', 'content': 'sys'}]
        for i in range(n):
            history.append({'role': 'user', 'content': 'm%d' % i})
        return history

    def test_history_opens_pager_and_quits(self):
        agent = _make_agent(['/history', 'q', '/resume'])
        agent.history = self._history(4)
        before = list(agent.history)
        result = _run_venue(agent)
        self.assertEqual(result.status, ic.STATUS_RESUME)
        self.assertTrue(agent._printed['info'])
        self.assertIn('history:', agent._printed['info'][0])
        self.assertEqual(agent.llm.calls, [])
        self.assertEqual(agent.history, before)

    def test_history_header_and_indices(self):
        agent = _make_agent(['/history', 'q', '/resume'])
        agent.history = self._history(24)
        _run_venue(agent)
        text = agent._printed['info'][0]
        self.assertIn('25 messages', text)
        self.assertIn('[24]', text)

    def test_history_page_navigation(self):
        agent = _make_agent(['/history 5', 'p', 'p', 'q', '/resume'])
        agent.history = self._history(24)
        _run_venue(agent)
        pages = [t.split('page ')[1].split('/')[0]
                 for t in agent._printed['info'] if 'page ' in t]
        self.assertEqual(pages, ['5', '4', '3'])

    def test_history_goto_page_and_clamp(self):
        agent = _make_agent(['/history 5', 'g 1', 'g 999', 'q', '/resume'])
        agent.history = self._history(24)
        _run_venue(agent)
        pages = [t.split('page ')[1].split('/')[0]
                 for t in agent._printed['info'] if 'page ' in t]
        self.assertEqual(pages, ['5', '1', '5'])
        self.assertTrue(agent._printed['warnings'])

    def test_history_detail_expands_one_message(self):
        long_text = 'X' * 500
        agent = _make_agent(['/history', 'd 0', 'q', '/resume'])
        agent.history = [{'role': 'system', 'content': long_text}]
        _run_venue(agent)
        self.assertTrue([t for t in agent._printed['info'] if long_text in t])

    def test_history_bad_page_size_falls_back(self):
        agent = _make_agent(['/history abc', 'q', '/resume'])
        agent.history = self._history(25)
        _run_venue(agent)
        self.assertTrue(agent._printed['info'])

    def test_history_empty_history(self):
        agent = _make_agent(['/history', 'q', '/resume'])
        agent.history = []
        _run_venue(agent)
        self.assertIn('0 messages', agent._printed['info'][0])

    def test_history_unknown_key_warns_and_repages(self):
        agent = _make_agent(['/history', 'zzz', 'q', '/resume'])
        agent.history = self._history(4)
        _run_venue(agent)
        self.assertTrue(agent._printed['warnings'])

    def test_memory_command_lists_index(self):
        agent = _make_agent(['/memory', '/resume'])
        agent._memory_index_entries = lambda: ['- notes.md' + chr(10) + '  - Description: venue memory note']
        agent.session = types.SimpleNamespace(workspace_dir='/tmp/ws')
        _run_venue(agent)
        text = agent._printed['info'][0]
        self.assertIn('notes.md', text)
        self.assertIn('venue memory note', text)

    def test_chain_command_lists_stages(self):
        agent = _make_agent(['/chain', '/resume'])
        agent.config.prompt_chain = [{'name': 'Preamble'}, {'name': 'Coding'}]
        agent.config.detached_stages = {'Compression': {'content': 'x'}}
        agent._chain_index = 1
        _run_venue(agent)
        text = agent._printed['info'][0]
        self.assertIn('Preamble', text)
        self.assertIn('Coding', text)
        self.assertIn('position 2/2', text)
        self.assertIn('Compression', text)

    def test_compress_dispatches_forcing_mid_loop(self):
        agent = _make_agent(['/compress', '/resume'])
        calls = []
        agent._mid_loop_protection = lambda stage, prompt, turn, force=False: (
            calls.append((stage, prompt, turn, force)), (False, turn))[1]
        _run_venue(agent, stage={'name': 'Coding'})
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0][3])
        self.assertTrue(any('compress:' in t for t in agent._printed['info']))

    def test_force_bypasses_the_watermark(self):
        agent = _make_agent(['/resume'])
        agent.history = [{'role': 'system', 'content': 'sys'},
                         {'role': 'assistant', 'content': 'a'},
                         {'role': 'user', 'content': 'b'}]
        agent.config.snap_handler = 'Compression'
        agent.config.detached_stages = {'Compression': {'content': 'x'}}
        calls = []
        agent._truncate_old_tool_results = lambda *a, **k: (calls.append(True), 0)[1]
        agent._last_prompt_tokens = 1
        agent._chars_at_last_usage = 1
        agent._mid_loop_protection({'name': 'Coding'}, 'p', 1, force=False)
        self.assertEqual(calls, [])
        agent._mid_loop_protection({'name': 'Coding'}, 'p', 1, force=True)
        self.assertEqual(calls, [True])

    def test_table_lists_new_commands(self):
        names = [c.name for c in ic.INTERRUPT_COMMANDS]
        for n in ('history', 'memory', 'chain', 'compress'):
            self.assertIn(n, names)
        menu = ic.render_help()
        for n in ('history', 'memory', 'chain', 'compress'):
            self.assertIn('/' + n, menu)

    def test_new_commands_are_read_only_loop_commands(self):
        history = [{'role': 'system', 'content': 'sys'},
                   {'role': 'user', 'content': 'hi'}]
        agent = _make_agent(['/history', 'q', '/memory', '/chain', '/resume'])
        agent.history = list(history)
        result = _run_venue(agent)
        self.assertEqual(result.status, ic.STATUS_RESUME)
        self.assertEqual(agent.llm.calls, [])
        self.assertEqual(agent.history, history)


class InterruptListLoadHistoryTests(unittest.TestCase):
    '''New venue commands: /list agents, /list instance, /load, /history [ID].'''

    def _class_cfg(self, code='X', model='m', description='', system_prompt=''):
        return types.SimpleNamespace(code=code, model=model,
                                     description=description, system_prompt=system_prompt)

    def _handle(self, name, cls, aid, status='ready', wait=None,
                ratio=0.0, window=100000, broken=False):
        if broken:
            peer = types.SimpleNamespace(
                config=types.SimpleNamespace(name=cls), agent_id=aid,
                context_size=window)
        else:
            peer = types.SimpleNamespace(
                config=types.SimpleNamespace(name=cls), agent_id=aid,
                context_size=window, _context_usage_ratio=lambda: ratio)
        return types.SimpleNamespace(name=name, status=status,
                                     wait_event=wait, agent=peer)

    def _runtime(self, pool, active='main', max_agents=None):
        return types.SimpleNamespace(
            pool=pool, active_name=lambda: active,
            max_agents=max_agents if max_agents is not None else len(pool))

    # --- table / help -----------------------------------------------------

    def test_table_lists_new_commands(self):
        names = [c.name for c in ic.INTERRUPT_COMMANDS]
        for n in ('list', 'load'):
            self.assertIn(n, names)
        spec = ic.lookup('list')
        self.assertEqual(spec.kind, ic.KIND_LOOP)
        self.assertEqual(spec.arg, ic.ARG_REQUIRED)
        self.assertEqual(spec.status, '')

    def test_help_lists_new_usage(self):
        menu = ic.render_help()
        self.assertIn('/list <instance|agents>', menu)
        self.assertIn('/load <path>', menu)
        self.assertIn('/history [ID] [page-size]', menu)

    def test_parse_new_commands(self):
        spec, arg = ic.parse_interrupt_line('/list agents')
        self.assertIsNotNone(spec)
        self.assertEqual((spec.name, arg), ('list', 'agents'))
        spec, arg = ic.parse_interrupt_line('/load /x/y.md')
        self.assertEqual((spec.name, arg), ('load', '/x/y.md'))

    # --- /list agents -----------------------------------------------------

    def test_list_agents_rows(self):
        agent = _make_agent(['/list agents', '/resume'])
        agent.agent_classes = {
            'BaseAgent': self._class_cfg(code='Base', model='m1', description='root class'),
            'NoAgent': self._class_cfg(code='No', model='m2', description='no-op'),
        }
        _run_venue(agent)
        self.assertTrue(agent._printed['list'])
        title, items, columns = agent._printed['list'][0]
        self.assertEqual(title, 'Agent Classes')
        self.assertEqual(columns, ['Name', 'Code', 'Model', 'Description'])
        self.assertEqual(len(items), 2)
        joined = ' '.join(str(x) for row in items for x in row)
        self.assertIn('BaseAgent', joined)
        self.assertIn('NoAgent', joined)
        self.assertIn('root class', joined)

    def test_list_agents_description_fallback(self):
        agent = _make_agent(['/list agents', '/resume'])
        agent.agent_classes = {
            'X': self._class_cfg(description='', system_prompt='first line here\nsecond'),
        }
        _run_venue(agent)
        title, items, columns = agent._printed['list'][0]
        self.assertIn('first line here', str(items[0]))

    def test_list_agents_marks_current(self):
        agent = _make_agent(['/list agents', '/resume'])
        agent.agent_classes = {'TestAgent': self._class_cfg()}
        _run_venue(agent)
        title, items, columns = agent._printed['list'][0]
        self.assertIn('(current)', str(items[0][0]))

    def test_list_agents_empty(self):
        agent = _make_agent(['/list agents', '/resume'])
        agent.agent_classes = {}
        _run_venue(agent)
        title, items, columns = agent._printed['list'][0]
        self.assertEqual(items, [])
        self.assertEqual(agent.llm.calls, [])

    # --- /list instance ---------------------------------------------------

    def test_list_instances_rows(self):
        agent = _make_agent(['/list instance', '/resume'])
        agent.runtime = self._runtime({
            'main': self._handle('main', 'TestAgent', 'abc123', status='running'),
            'agent-1': self._handle('agent-1', 'NoAgent', 'def456',
                                    status='waiting', wait='review_done'),
        })
        _run_venue(agent)
        self.assertTrue(agent._printed['list'])
        title, items, columns = agent._printed['list'][0]
        self.assertEqual(
            columns, ['Instance', 'Class', 'ID', 'Status', 'Wait Event', 'Context'])
        self.assertEqual(len(items), 2)
        joined = str(items)
        self.assertIn('agent-1', joined)
        self.assertIn('review_done', joined)
        self.assertIn('NoAgent', joined)

    def test_list_instances_context_pct(self):
        agent = _make_agent(['/list instance', '/resume'])
        agent.runtime = self._runtime({
            'main': self._handle('main', 'TestAgent', 'abc123', ratio=0.25, window=100000),
        })
        _run_venue(agent)
        title, items, columns = agent._printed['list'][0]
        cell = items[0][5]
        self.assertIn('25%', cell)
        self.assertIn('25000/100000', cell)

    def test_list_instances_no_runtime(self):
        agent = _make_agent(['/list instance', '/resume'])
        agent.runtime = None
        _run_venue(agent)
        self.assertTrue(agent._printed['list'])
        self.assertTrue(any('single agent' in t for t in agent._printed['info']))
        title, items, columns = agent._printed['list'][0]
        self.assertTrue(items)

    def test_list_instances_broken_peer(self):
        agent = _make_agent(['/list instance', '/resume'])
        agent.runtime = self._runtime({
            'bad': self._handle('bad', 'X', 'id1', broken=True),
        })
        _run_venue(agent)
        title, items, columns = agent._printed['list'][0]
        self.assertEqual(items[0][5], '-')

    def test_list_instances_read_only(self):
        agent = _make_agent(['/list instance', '/resume'])
        before = list(agent.history)
        agent.runtime = None
        _run_venue(agent)
        self.assertEqual(agent.history, before)
        self.assertEqual(agent.llm.calls, [])

    # --- /list argument handling ------------------------------------------

    def test_list_unknown_subcommand_warns(self):
        agent = _make_agent(['/list bogus', '/resume'])
        result = _run_venue(agent)
        self.assertEqual(result.status, ic.STATUS_RESUME)
        self.assertTrue(any('Usage: /list' in w for w in agent._printed['warnings']))

    def test_list_missing_arg_warns(self):
        agent = _make_agent(['/list', '/resume'])
        result = _run_venue(agent)
        self.assertEqual(result.status, ic.STATUS_RESUME)
        self.assertTrue(agent._printed['warnings'])
        self.assertEqual(agent.llm.calls, [])

    # --- /load ------------------------------------------------------------

    def test_load_calls_callback_and_reports(self):
        agent = _make_agent(['/load /tmp/extra.md', '/resume'])
        seen = []

        def loader(path):
            seen.append(path)
            return 'load: /tmp/extra.md\nadded: 1  NewAgent\nclasses now: 3'

        agent.yaha_load_classes = loader
        _run_venue(agent)
        self.assertEqual(len(seen), 1)
        self.assertTrue(any('classes now: 3' in t for t in agent._printed['info']))

    def test_load_missing_file_warns(self):
        agent = _make_agent(['/load /nope/missing.md', '/resume'])
        agent.yaha_load_classes = lambda p: 'load: file not found: %s' % p
        result = _run_venue(agent)
        self.assertEqual(result.status, ic.STATUS_RESUME)
        self.assertTrue(any('not found' in w for w in agent._printed['warnings']))

    def test_load_parse_error_warns(self):
        agent = _make_agent(['/load /tmp/bad.md', '/resume'])

        def boom(path):
            raise MarkdownLoadError('boom')

        agent.yaha_load_classes = boom
        result = _run_venue(agent)
        self.assertEqual(result.status, ic.STATUS_RESUME)
        self.assertTrue(any('load failed' in w for w in agent._printed['warnings']))

    def test_load_unavailable_warns(self):
        agent = _make_agent(['/load /tmp/x.md', '/resume'])
        agent.yaha_load_classes = None
        _run_venue(agent)
        self.assertTrue(any('not available' in w for w in agent._printed['warnings']))

    def test_load_requires_arg(self):
        agent = _make_agent(['/load', '/resume'])
        result = _run_venue(agent)
        self.assertEqual(result.status, ic.STATUS_RESUME)
        self.assertTrue(agent._printed['warnings'])

    def test_load_read_only_history(self):
        agent = _make_agent(['/load /tmp/x.md', '/resume'])
        before = list(agent.history)
        agent.yaha_load_classes = lambda p: 'load: ok'
        _run_venue(agent)
        self.assertEqual(agent.history, before)

    # --- /history [ID] ----------------------------------------------------

    def _peer_agent(self, aid='def456', history=None):
        return types.SimpleNamespace(
            agent_id=aid,
            history=history or [{'role': 'user', 'content': 'peer msg'}],
            config=types.SimpleNamespace(name='NoAgent'))

    def _peer_runtime(self, peer, name='agent-1'):
        return types.SimpleNamespace(
            pool={name: types.SimpleNamespace(
                name=name, agent=peer, status='waiting', wait_event='e')},
            active_name=lambda: '', max_agents=1)

    def test_history_default_is_own(self):
        agent = _make_agent(['/history', 'q', '/resume'])
        agent.history = [{'role': 'system', 'content': 'sys'},
                         {'role': 'user', 'content': 'mine'}]
        _run_venue(agent)
        self.assertTrue(any('mine' in t for t in agent._printed['info']))

    def test_history_by_instance_name(self):
        agent = _make_agent(['/history agent-1', 'q', '/resume'])
        peer = self._peer_agent(history=[{'role': 'user', 'content': 'peer msg'}])
        agent.runtime = self._peer_runtime(peer)
        _run_venue(agent)
        self.assertTrue(any('peer msg' in t for t in agent._printed['info']))

    def test_history_by_agent_id(self):
        agent = _make_agent(['/history def456', 'q', '/resume'])
        peer = self._peer_agent(aid='def456', history=[{'role': 'user', 'content': 'peer msg'}])
        agent.runtime = self._peer_runtime(peer)
        _run_venue(agent)
        self.assertTrue(any('peer msg' in t for t in agent._printed['info']))

    def test_history_id_and_page_size(self):
        agent = _make_agent(['/history agent-1 5', 'q', '/resume'])
        hist = [{'role': 'user', 'content': 'm%d' % i} for i in range(24)]
        peer = self._peer_agent(history=hist)
        agent.runtime = self._peer_runtime(peer)
        _run_venue(agent)
        pages = [t.split('page ')[1].split('/')[0]
                 for t in agent._printed['info'] if 'page ' in t]
        self.assertEqual(pages[0], '5')

    def test_history_numeric_is_page_size(self):
        agent = _make_agent(['/history 20', 'q', '/resume'])
        agent.history = [{'role': 'user', 'content': 'm%d' % i} for i in range(25)]
        _run_venue(agent)
        self.assertTrue(any('page ' in t for t in agent._printed['info']))

    def test_history_unknown_id_warns(self):
        agent = _make_agent(['/history nope', 'q', '/resume'])
        _run_venue(agent)
        self.assertTrue(any('No agent matches' in w for w in agent._printed['warnings']))

    def test_history_other_agent_read_only(self):
        agent = _make_agent(['/history agent-1', 'q', '/resume'])
        peer = self._peer_agent()
        before = list(peer.history)
        agent.runtime = self._peer_runtime(peer)
        _run_venue(agent)
        self.assertEqual(peer.history, before)


class InterruptSpawnStackTests(unittest.TestCase):
    '''Round 2: /spawn + /quit push/pop venue stack, /agent_stack, /resume.'''

    # --- table / help -----------------------------------------------------

    def test_spawn_quit_agent_stack_table(self):
        names = [c.name for c in ic.INTERRUPT_COMMANDS]
        for n in ('spawn', 'quit', 'agent_stack'):
            self.assertIn(n, names)
        spec = ic.lookup('agent_stack')
        self.assertEqual(spec.kind, ic.KIND_LOOP)
        self.assertEqual(spec.arg, ic.ARG_NONE)
        self.assertTrue(ic.STATUS_POP)

    def test_help_mentions_push_pop_and_stack(self):
        menu = ic.render_help()
        self.assertIn('/spawn <AgentClass> <task>', menu)
        self.assertIn('/agent_stack', menu)
        self.assertIn('pop one venue frame', menu)

    def test_label_for_status_pop(self):
        self.assertEqual(ic.label_for_status(ic.STATUS_POP), 'pop')

    # --- push / pop -------------------------------------------------------

    def _child(self, lines, name='NoAgent', aid='kid001'):
        child = _make_agent(lines, stage={'name': 'Spawned'})
        child.config.name = name
        child.agent_id = aid
        return child

    def test_spawn_pushes_frame_and_enters_child_venue(self):
        child = self._child(['/quit'])
        agent = _make_agent(['/spawn NoAgent go', '/resume'])
        seen = []

        def factory(cls, runtime=None):
            seen.append((cls, runtime))
            return child

        agent.yaha_spawn_frame = factory
        result = _run_venue(agent)
        self.assertEqual(result.status, ic.STATUS_RESUME)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0], 'NoAgent')
        self.assertTrue(any('pushed frame' in n for n in agent._printed['notes']))
        self.assertTrue(any('popped frame' in n for n in agent._printed['notes']))

    def test_spawn_factory_error_warns(self):
        agent = _make_agent(['/spawn Ghost go', '/resume'])
        agent.yaha_spawn_frame = lambda cls, runtime=None: (
            "Error: Agent class 'Ghost' is not authorized for this agent.")
        _run_venue(agent)
        self.assertTrue(any('not authorized' in w for w in agent._printed['warnings']))

    def test_spawn_missing_class_warns(self):
        agent = _make_agent(['/spawn', '/resume'])
        _run_venue(agent)
        # ARG_REQUIRED fires the usage warning before the handler.
        self.assertTrue(any('Usage: /spawn' in w for w in agent._printed['warnings']))

    def test_spawn_unavailable_warns(self):
        agent = _make_agent(['/spawn NoAgent go', '/resume'])
        agent.yaha_spawn_frame = None
        _run_venue(agent)
        self.assertTrue(any('not available' in w for w in agent._printed['warnings']))

    def test_quit_at_root_routes_to_fail_target(self):
        agent = _make_agent(['/quit'])
        result = _run_venue(agent, stage={'name': 'Coding', 'fail_target': 'Plan'})
        self.assertEqual(result.status, 'FAIL:Plan')

    def test_quit_in_spawned_frame_pops(self):
        agent = _make_agent(['/quit'])
        frames = [{'agent': agent, 'task': 't', 'handle': None,
                   'name': 'TestAgent/abc123', 'root': True}]
        result = _run_venue(agent, frames=frames, spawned=True)
        self.assertEqual(result.status, ic.STATUS_POP)

    def test_nested_spawn_pushes_two_frames(self):
        grandchild = self._child(['/quit'], name='Deep', aid='gc0001')
        child = self._child(['/spawn Deep go', '/quit'], name='NoAgent', aid='kid001')
        child.yaha_spawn_frame = lambda cls, runtime=None: grandchild
        agent = _make_agent(['/spawn NoAgent go', '/resume'])
        agent.yaha_spawn_frame = lambda cls, runtime=None: child
        _run_venue(agent)
        root_notes = ' | '.join(agent._printed['notes'])
        child_notes = ' | '.join(child._printed['notes'])
        # The root pushed the child (depth 2) and the child pushed the grandchild
        # (depth 3): "pushed frame N" is the shared stack length at push time.
        self.assertIn('pushed frame 2', root_notes)
        self.assertIn('pushed frame 3', child_notes)
        self.assertIn('popped frame', root_notes)
        self.assertIn('popped frame', child_notes)

    def test_pop_returns_to_parent_and_notes(self):
        child = self._child(['/quit'])
        agent = _make_agent(['/spawn NoAgent go', '/resume'])
        agent.yaha_spawn_frame = lambda cls, runtime=None: child
        result = _run_venue(agent)
        self.assertEqual(result.status, ic.STATUS_RESUME)
        self.assertTrue(any('popped frame' in n for n in agent._printed['notes']))

    def test_spawn_exception_pops_frame(self):
        child = self._child(['/quit'])

        def boom(*a, **k):
            raise RuntimeError('child blew up')

        child._serve_interrupt_venue = boom
        agent = _make_agent(['/spawn NoAgent go', '/resume'])
        agent.yaha_spawn_frame = lambda cls, runtime=None: child
        with self.assertRaises(RuntimeError):
            _run_venue(agent)
        # The child frame is popped in the finally; only the seeded root remains.
        self.assertEqual(len(agent._venue_stack), 1)
        self.assertTrue(agent._venue_stack[0].get('root'))

    # --- /agent_stack -----------------------------------------------------

    def test_agent_stack_at_root_only(self):
        agent = _make_agent(['/agent_stack', '/resume'])
        _run_venue(agent)
        head = agent._printed['info'][0]
        self.assertIn('root only', head)
        title, items, columns = agent._printed['list'][0]
        self.assertEqual(columns, ['#', 'Frame', 'Class', 'ID', 'State', 'Wait'])
        self.assertEqual(items[0][4], 'root')
        self.assertIn('<- current', items[0][1])

    def test_agent_stack_depth_and_current_marker(self):
        agent = _make_agent(['/agent_stack', 'q', '/resume'])
        child = self._child(['/agent_stack', '/quit'], name='NoAgent', aid='kid001')
        frames = [
            {'agent': agent, 'task': 't', 'handle': None,
             'name': 'TestAgent/abc123', 'root': True},
            {'agent': child, 'task': 'go', 'handle': None, 'name': 'NoAgent/kid001'},
        ]
        agent._venue_stack = frames
        # Serve the CHILD's venue so it is the current frame.
        _run_venue(child, frames=frames, spawned=True)
        title, items, columns = child._printed['list'][0]
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0][4], 'root')
        self.assertEqual(items[1][4], 'pending')
        self.assertIn('<- current', items[1][1])
        self.assertNotIn('<- current', items[0][1])

    def test_agent_stack_shows_parked_wait(self):
        runtime = types.SimpleNamespace(
            pool={},
            waiting={'review_done': [types.SimpleNamespace(name='agent-7')]},
            active_name=lambda: '', max_agents=1)
        root = _make_agent(['/resume'])
        child = self._child(['/agent_stack', '/quit'], name='NoAgent', aid='kid001')
        child.runtime = runtime
        frames = [
            {'agent': root, 'task': 't', 'handle': None,
             'name': 'TestAgent/abc123', 'root': True},
            {'agent': child, 'task': 'go', 'handle': 'agent-7', 'name': 'NoAgent/kid001'},
        ]
        _run_venue(child, frames=frames, spawned=True)
        title, items, columns = child._printed['list'][0]
        self.assertEqual(items[1][4], 'handle:agent-7')
        self.assertIn('review_done', items[1][5])

    def test_agent_stack_read_only(self):
        agent = _make_agent(['/agent_stack', '/resume'])
        before = list(agent.history)
        _run_venue(agent)
        self.assertEqual(agent.history, before)
        self.assertEqual(agent.llm.calls, [])

    # --- /resume of a spawned frame ---------------------------------------

    def test_resume_sync_runs_child_without_runtime(self):
        child = self._child(['/resume'])
        calls = []
        child.run = lambda task, seed_history=None: (calls.append(task), 'child answer')[1]
        frames = [
            {'agent': child, 'task': 'do the thing', 'handle': None,
             'name': 'NoAgent/kid001'},
        ]
        result = _run_venue(child, frames=frames, spawned=True)
        self.assertEqual(result.status, ic.STATUS_POP)
        self.assertEqual(calls, ['do the thing'])
        self.assertTrue(any('finished' in n for n in child._printed['notes']))

    def test_resume_group_empty_pops(self):
        child = self._child(['/resume'])
        ran = []
        child.runtime = types.SimpleNamespace(
            pool={},
            spawn=lambda agent, task, name=None, tag=None: (
                ran.append(task), types.SimpleNamespace(name='agent-1'))[1],
            run_group=lambda names: 'empty',
        )
        frames = [{'agent': child, 'task': 'do it', 'handle': None, 'name': 'NoAgent/kid001'}]
        result = _run_venue(child, frames=frames, spawned=True)
        self.assertEqual(result.status, ic.STATUS_POP)
        self.assertEqual(ran, ['do it'])

    def test_resume_group_blocked_stays(self):
        child = self._child(['/resume', '/quit'])
        child.runtime = types.SimpleNamespace(
            pool={},
            spawn=lambda agent, task, name=None, tag=None: types.SimpleNamespace(name='agent-1'),
            run_group=lambda names: 'blocked',
            waiting={'endpoint_a': [types.SimpleNamespace(name='agent-1')]},
        )
        frames = [{'agent': child, 'task': 'do it', 'handle': None, 'name': 'NoAgent/kid001'}]
        result = _run_venue(child, frames=frames, spawned=True)
        self.assertEqual(result.status, ic.STATUS_POP)
        self.assertTrue(any('blocked' in n for n in child._printed['notes']),
                        child._printed['notes'])

    def test_resume_steer_text_prepended(self):
        child = self._child(['/resume focus on logs'])
        calls = []
        child.run = lambda task, seed_history=None: (calls.append(task), 'ok')[1]
        frames = [{'agent': child, 'task': 'do it', 'handle': None, 'name': 'NoAgent/kid001'}]
        _run_venue(child, frames=frames, spawned=True)
        self.assertTrue(calls)
        self.assertIn('do it', calls[0])
        self.assertIn('focus on logs', calls[0])


    # --- rev1: nest-safe stdin hold --------------------------------------

    def test_controller_suspend_is_reentrant(self):
        from yaha.ui.interrupt import StdinInterruptController
        ctl = StdinInterruptController()
        fake = _FakeListener()
        ctl.listener = fake
        ctl.suspend()
        ctl.suspend()
        self.assertEqual(fake.suspends, 1, 'a nested suspend must not suspend twice')
        self.assertTrue(ctl.suspended)
        ctl.resume()
        self.assertEqual(fake.resumes, 0, 'an inner resume must not release the hold')
        self.assertTrue(ctl.suspended)
        ctl.resume()
        self.assertEqual(fake.resumes, 1)
        self.assertFalse(ctl.suspended)

    def test_stdin_hold_survives_a_child_frame(self):
        from yaha.ui.interrupt import StdinInterruptController
        ctl = StdinInterruptController()
        fake = _FakeListener()
        ctl.listener = fake
        child = self._child(['/quit'])
        child.stdin_interrupt = ctl
        agent = _make_agent(['/spawn NoAgent go', '/resume'])
        agent.stdin_interrupt = ctl
        agent.yaha_spawn_frame = lambda cls, runtime=None: child
        _run_venue(agent)
        # The parent suspended once and resumed once; the nested child neither
        # suspended nor released the shared controller.
        self.assertEqual(fake.suspends, 1)
        self.assertEqual(fake.resumes, 1)
        self.assertEqual(ctl.suspend_depth, 0)

    # --- rev1: chain moves are rejected inside a spawned frame -----------

    def test_rewind_restart_jump_rejected_in_spawned_frame(self):
        agent = _make_agent(['/rewind', '/restart', '/jump X', '/quit'])
        frames = [{'agent': agent, 'task': 't', 'handle': None,
                   'name': 'TestAgent/abc123', 'root': True}]
        result = _run_venue(agent, frames=frames, spawned=True)
        self.assertEqual(result.status, ic.STATUS_POP)
        warnings = ' | '.join(agent._printed['warnings'])
        for cmd in ('/rewind', '/restart', '/jump'):
            self.assertIn(cmd, warnings)

    def test_root_frame_still_allows_chain_moves(self):
        agent = _make_agent(['/rewind'])
        result = _run_venue(agent)
        self.assertEqual(result.status, ic.STATUS_REWIND)

    # --- rev1: runtime result + factory call shape ------------------------

    def test_resume_group_empty_reports_child_answer(self):
        child = self._child(['/resume'])
        handle = types.SimpleNamespace(name='agent-1', result='the child answer')
        child.runtime = types.SimpleNamespace(
            pool={},
            spawn=lambda agent, task, name=None, tag=None: handle,
            run_group=lambda names: 'empty',
        )
        frames = [{'agent': child, 'task': 'do it', 'handle': None, 'name': 'NoAgent/kid001'}]
        _run_venue(child, frames=frames, spawned=True)
        self.assertTrue(any('the child answer' in n for n in child._printed['notes']),
                        child._printed['notes'])

    def test_spawn_calls_factory_with_runtime(self):
        child = self._child(['/quit'])
        agent = _make_agent(['/spawn NoAgent go', '/resume'])
        agent.runtime = types.SimpleNamespace(pool={}, active_name=lambda: '', max_agents=1)
        calls = []

        def factory(cls, runtime=None):
            calls.append((cls, runtime))
            return child

        agent.yaha_spawn_frame = factory
        _run_venue(agent)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], 'NoAgent')
        self.assertIs(calls[0][1], agent.runtime)

    def test_spawn_factory_typeerror_reported_once(self):
        calls = []

        def boom(cls, runtime=None):
            calls.append(cls)
            raise TypeError('internal boom')

        agent = _make_agent(['/spawn NoAgent go', '/resume'])
        agent.yaha_spawn_frame = boom
        _run_venue(agent)
        self.assertEqual(len(calls), 1, 'a factory TypeError must not double-invoke')
        self.assertTrue(any('spawn failed' in w for w in agent._printed['warnings']))


class TestInterruptPromptChar(unittest.TestCase):
    '''The interrupt venue uses a `!` prompt char, distinct from `>`.'''

    def test_interrupt_read_default_prompt_is_bang(self):
        from yaha.ui.terminal import YAHAUI
        ui = YAHAUI()
        with patch('builtins.input', return_value='hello') as m:
            result = ui.interrupt_read()
        self.assertEqual(result, 'hello')
        self.assertEqual(m.call_args.args[0], '! ')

    def test_history_pager_prompt_uses_bang(self):
        calls = []
        agent = _make_agent(['/history', 'q'])

        def spy(*a, **k):
            calls.append(a)
            return agent._box.pop(0) if agent._box else None

        agent.ui.interrupt_read = spy
        _run_venue(agent)
        self.assertIn(('history! ',), calls)

    def test_normal_ask_user_prompt_unchanged(self):
        from yaha.ui.terminal import YAHAUI
        ui = YAHAUI()
        with patch('builtins.input', return_value='ans') as m:
            ui.prompt_user('A', 'id1', 'q?')
        self.assertEqual(m.call_args.args[0], '> ')


if __name__ == '__main__':
    unittest.main()