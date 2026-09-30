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


class _FakeTTY:
    def __init__(self, is_tty=True):
        self._is_tty = is_tty

    def isatty(self):
        return self._is_tty


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
    agent._interrupt_suspend = lambda: None
    agent._interrupt_resume = lambda: None
    agent.llm = llm or _ScriptedLLM(['a model answer'])
    box = list(lines)
    printed = {'answers': [], 'help': [], 'status': [], 'notes': [], 'warnings': [], 'info': []}
    agent.ui = types.SimpleNamespace(
        interrupt_banner=lambda *a, **k: None,
        interrupt_read=lambda *a, **k: (box.pop(0) if box else None),
        interrupt_answer=lambda *a, **k: printed['answers'].append(a[-1]),
        interrupt_help=lambda *a, **k: printed['help'].append(a[-1]),
        interrupt_status=lambda *a, **k: printed['status'].append(a[-1]),
        interrupt_note=lambda *a, **k: printed['notes'].append(a[-1]),
        interrupt_info=lambda *a, **k: printed['info'].append(a[-1]),
        display_warning=lambda *a, **k: printed['warnings'].append(a[-1]),
        spinner=lambda *a, **k: contextlib.nullcontext(),
    )
    agent._printed = printed
    agent._box = box
    return agent


def _run_venue(agent, stage=None, is_tty=True):
    stage = stage if stage is not None else {'name': 'Coding'}
    with patch('sys.stdin', _FakeTTY(is_tty)):
        return Agent._serve_interrupt_venue(agent, stage, 2, set())


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

    agent = _make_agent(['/spawn NoAgent do it', '/resume'])
    agent.yaha_spawn_agent = lambda system, instruction, agent_class=None: 'spawn digest'
    _run_venue(agent)
    assert agent._printed['answers'] == ['spawn digest'], agent._printed['answers']

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


if __name__ == '__main__':
    unittest.main()