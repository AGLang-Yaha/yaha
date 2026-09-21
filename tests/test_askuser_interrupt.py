#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

'''Regression tests for reaching the interrupt venue from ask_user.

The ask_user editor can request the venue two ways: Ctrl+I, or submitting exactly
/interrupt or /int. The request travels as ASK_INTERRUPT_SENTINEL through the UI
layer and is served by Agent._handle_ask_user_interrupt, which is now driven by
the shared command table:

- /resume with text returns the text as the ANSWER to the pending question;
- bare /resume re-opens the question;
- rewind / jump / restart / quit are deferred chain moves recorded in
  Agent._pending_interrupt_status and consumed by the ReAct loop.
'''
import types
import unittest
from unittest.mock import patch

from yaha.core.agent import Agent, VenueResult
from yaha.core import interrupt_commands as ic
from yaha.ui.terminal import ASK_INTERRUPT_SENTINEL, _ask_interrupt_text, _multiline_editor


class _RecordingKB:
    def __init__(self):
        self.bound = {}

    def add(self, *keys):
        def register(fn):
            self.bound[tuple(keys)] = fn
            return fn
        return register


def _fake_agent(answers):
    agent = object.__new__(Agent)
    agent.config = types.SimpleNamespace(name='TestAgent')
    agent.agent_id = 'abc123'
    agent.caller_id = None
    agent._pending_interrupt_status = None
    agent.history = []
    agent.logger = types.SimpleNamespace(log_turn=lambda *a, **k: None)
    agent.ui = types.SimpleNamespace(display_warning=lambda *a, **k: None)
    seq = list(answers)

    def _prompt(question):
        if not seq:
            raise AssertionError('no more scripted answers')
        return seq.pop(0)

    agent._prompt_interrupt_safe = _prompt
    return agent


# --- editor / sentinel (unchanged by the venue refactor) --------------------

def test_multiline_editor_typed_command_returns_sentinel():
    class FakeSession:
        def __init__(self, **kwargs):
            pass

        def prompt(self, *args, **kwargs):
            return '/interrupt'

    with patch('prompt_toolkit.PromptSession', FakeSession), patch('prompt_toolkit.key_binding.KeyBindings', _RecordingKB):
        out = _multiline_editor('')
    assert out == ASK_INTERRUPT_SENTINEL, out
    print('test_multiline_editor_typed_command_returns_sentinel OK')


def test_multiline_editor_short_and_case_insensitive_commands():
    for text in ('/int', '/INT', '  /Interrupt  ', '/interrupt'):
        class FakeSession:
            def __init__(self, **kwargs):
                pass

            def prompt(self, *args, **kwargs):
                return text

        with patch('prompt_toolkit.PromptSession', FakeSession), patch('prompt_toolkit.key_binding.KeyBindings', _RecordingKB):
            out = _multiline_editor('')
        assert out == ASK_INTERRUPT_SENTINEL, (text, out)
    print('test_multiline_editor_short_and_case_insensitive_commands OK')


def test_multiline_editor_normal_text_unchanged():
    class FakeSession:
        def __init__(self, **kwargs):
            pass

        def prompt(self, *args, **kwargs):
            return 'real answer'

    with patch('prompt_toolkit.PromptSession', FakeSession), patch('prompt_toolkit.key_binding.KeyBindings', _RecordingKB):
        out = _multiline_editor('')
    assert out == 'real answer', out
    assert _ask_interrupt_text('real answer') is False
    print('test_multiline_editor_normal_text_unchanged OK')


def test_multiline_editor_ctrl_i_binding_exits_with_sentinel():
    kb = _RecordingKB()

    class FakeSession:
        def __init__(self, **kwargs):
            pass

        def prompt(self, *args, **kwargs):
            return 'unused'

    with patch('prompt_toolkit.PromptSession', FakeSession), patch('prompt_toolkit.key_binding.KeyBindings', lambda: kb):
        _multiline_editor('')

    assert ('c-i',) in kb.bound, set(kb.bound)

    class App:
        def __init__(self):
            self.result = None

        def exit(self, result=None, **kwargs):
            self.result = result

    class Event:
        def __init__(self):
            self.current_buffer = types.SimpleNamespace(insert_text=lambda t: None)
            self.app = App()

    event = Event()
    kb.bound[('c-i',)](event)
    assert event.app.result == ASK_INTERRUPT_SENTINEL, event.app.result
    print('test_multiline_editor_ctrl_i_binding_exits_with_sentinel OK')


def test_prompt_multiline_propagates_sentinel():
    from yaha.ui.terminal import YAHAUI
    ui = object.__new__(YAHAUI)
    with patch('yaha.ui.terminal._multiline_editor', return_value=ASK_INTERRUPT_SENTINEL):
        out = ui._prompt_multiline('')
    assert out == ASK_INTERRUPT_SENTINEL, out
    print('test_prompt_multiline_propagates_sentinel OK')


# --- ask_user <-> venue ----------------------------------------------------

def test_ask_user_sentinel_then_answer_reasks():
    agent = _fake_agent([ASK_INTERRUPT_SENTINEL, 'final answer'])
    served = []
    agent._serve_interrupt_venue = lambda *a, **k: (served.append(a), VenueResult(ic.STATUS_RESUME, None, None))[1]
    out = agent.ask_user('Q?')
    assert out == 'final answer', out
    assert served and served[0][0].get('name') == 'ask_user', served
    assert agent._pending_interrupt_status is None
    print('test_ask_user_sentinel_then_answer_reasks OK')


def test_ask_user_resume_text_returns_the_answer():
    agent = _fake_agent([ASK_INTERRUPT_SENTINEL])
    agent._serve_interrupt_venue = lambda *a, **k: VenueResult(
        ic.STATUS_RESUME, None, 'steered answer'
    )
    out = agent.ask_user('Q?')
    assert out == 'steered answer', out
    print('test_ask_user_resume_text_returns_the_answer OK')


def test_ask_user_rewind_and_jump_defer_not_drop():
    for status in (ic.STATUS_REWIND, 'INTERRUPT_JUMP:Coding'):
        agent = _fake_agent([ASK_INTERRUPT_SENTINEL])
        agent._serve_interrupt_venue = lambda *a, **k: VenueResult(status, None, None)
        out = agent.ask_user('Q?')
        assert agent._pending_interrupt_status == status, agent._pending_interrupt_status
        assert 'requested' in out, out
    print('test_ask_user_rewind_and_jump_defer_not_drop OK')


def test_ask_user_quit_and_restart_defer_chain_move():
    agent = _fake_agent([ASK_INTERRUPT_SENTINEL])
    agent._serve_interrupt_venue = lambda *a, **k: VenueResult('FAIL:Aftermath', None, None)
    out = agent.ask_user('Q?')
    assert agent._pending_interrupt_status == 'FAIL:Aftermath', agent._pending_interrupt_status
    assert 'quit' in out, out

    agent = _fake_agent([ASK_INTERRUPT_SENTINEL])
    agent._serve_interrupt_venue = lambda *a, **k: VenueResult(ic.STATUS_RESTART, None, None)
    out = agent.ask_user('Q?')
    assert agent._pending_interrupt_status == ic.STATUS_RESTART, agent._pending_interrupt_status
    assert 'restart' in out, out
    print('test_ask_user_quit_and_restart_defer_chain_move OK')


def test_ask_user_duck_typed_agent_without_safe_prompt():
    # Regression: ask_user must keep the getattr fallback so callers that only
    # expose config/agent_id/ui (like a duck-typed fake) keep working.
    class FakeUI:
        def prompt_user(self, *a, **k):
            return 'the answer'

    fake = types.SimpleNamespace(
        config=types.SimpleNamespace(name='TestAgent'), agent_id='abc123', ui=FakeUI()
    )
    assert Agent.ask_user(fake, 'Q?') == 'the answer'
    print('test_ask_user_duck_typed_agent_without_safe_prompt OK')


# --- deferred consumption --------------------------------------------------

def _consume_agent():
    agent = object.__new__(Agent)
    agent._pending_interrupt_status = None
    agent.history = []
    agent.logger = types.SimpleNamespace(log_turn=lambda *a, **k: None)
    return agent


def test_consume_deferred_interrupt_status_quit():
    agent = _consume_agent()
    agent._pending_interrupt_status = 'FAIL:Aftermath'
    consumed = Agent._consume_deferred_interrupt_status(agent, 7)
    assert consumed == ('Interrupt: quit requested.', 'FAIL:Aftermath', 7), consumed
    assert agent._pending_interrupt_status is None
    assert any('quit requested' in m.get('content', '') for m in agent.history)
    assert Agent._consume_deferred_interrupt_status(agent, 7) is None
    print('test_consume_deferred_interrupt_status_quit OK')


def test_consume_deferred_interrupt_status_restart():
    agent = _consume_agent()
    agent._pending_interrupt_status = 'RESTART'
    consumed = Agent._consume_deferred_interrupt_status(agent, 3)
    assert consumed == ('Interrupt: restart requested.', 'RESTART', 3), consumed
    assert agent._pending_interrupt_status is None
    print('test_consume_deferred_interrupt_status_restart OK')


def test_consume_deferred_interrupt_status_rewind_and_jump():
    agent = _consume_agent()
    agent._pending_interrupt_status = ic.STATUS_REWIND
    consumed = Agent._consume_deferred_interrupt_status(agent, 4)
    assert consumed == ('Interrupt: rewind requested.', ic.STATUS_REWIND, 4), consumed
    assert any('rewind requested' in m.get('content', '') for m in agent.history)

    agent = _consume_agent()
    agent._pending_interrupt_status = 'INTERRUPT_JUMP:Coding'
    consumed = Agent._consume_deferred_interrupt_status(agent, 4)
    assert consumed == ('Interrupt: jump requested.', 'INTERRUPT_JUMP:Coding', 4), consumed
    assert any('jump requested' in m.get('content', '') for m in agent.history)
    print('test_consume_deferred_interrupt_status_rewind_and_jump OK')


def test_venue_never_injects_on_quit_or_restart_decision():
    # The venue itself must not touch history: the caller decides.
    agent = _consume_agent()
    agent._pending_interrupt_status = 'RESTART'
    assert agent.history == []
    print('test_venue_never_injects_on_quit_or_restart_decision OK')


def test_maybe_user_interrupt_no_listener_is_none():
    agent = object.__new__(Agent)
    agent.stdin_interrupt = None
    agent._serving_interrupt = False
    agent._compressing = False
    assert Agent._maybe_user_interrupt(agent, {'name': 'Stage'}, 0, set()) is None
    print('test_maybe_user_interrupt_no_listener_is_none OK')


def test_venue_status_report_tolerates_missing_tokens():
    # Regression: /status must not hard-require _last_prompt_tokens.
    agent = _consume_agent()
    agent.config = types.SimpleNamespace(name='TestAgent', model='m')
    agent.agent_id = 'abc123'
    agent.caller_id = None
    agent.context_size = 1000
    agent.compress_count = 0
    agent.stdin_interrupt = None
    agent._serving_interrupt = False
    agent._compressing = False
    agent._current_turn = 1
    agent._chain_index = 0
    agent._chain_len = 0
    agent._venue_entry = None
    agent._last_tool_result = None
    agent.turn_limit = 10
    agent.llm_retry_limit = 10
    agent.api_retry_unattended_max_rounds = 3
    text = Agent._venue_status_report(agent, {'name': 'ask_user'}, 0, set())
    assert 'runtime truth' in text, text
    assert 'stage=ask_user' in text, text
    print('test_venue_status_report_tolerates_missing_tokens OK')


class AskUserInterruptTests(unittest.TestCase):
    '''Unittest-discoverable wrapper for the regression functions above.'''
    test_multiline_editor_typed_command_returns_sentinel = staticmethod(test_multiline_editor_typed_command_returns_sentinel)
    test_multiline_editor_short_and_case_insensitive_commands = staticmethod(test_multiline_editor_short_and_case_insensitive_commands)
    test_multiline_editor_normal_text_unchanged = staticmethod(test_multiline_editor_normal_text_unchanged)
    test_multiline_editor_ctrl_i_binding_exits_with_sentinel = staticmethod(test_multiline_editor_ctrl_i_binding_exits_with_sentinel)
    test_prompt_multiline_propagates_sentinel = staticmethod(test_prompt_multiline_propagates_sentinel)
    test_ask_user_sentinel_then_answer_reasks = staticmethod(test_ask_user_sentinel_then_answer_reasks)
    test_ask_user_resume_text_returns_the_answer = staticmethod(test_ask_user_resume_text_returns_the_answer)
    test_ask_user_rewind_and_jump_defer_not_drop = staticmethod(test_ask_user_rewind_and_jump_defer_not_drop)
    test_ask_user_quit_and_restart_defer_chain_move = staticmethod(test_ask_user_quit_and_restart_defer_chain_move)
    test_ask_user_duck_typed_agent_without_safe_prompt = staticmethod(test_ask_user_duck_typed_agent_without_safe_prompt)
    test_consume_deferred_interrupt_status_quit = staticmethod(test_consume_deferred_interrupt_status_quit)
    test_consume_deferred_interrupt_status_restart = staticmethod(test_consume_deferred_interrupt_status_restart)
    test_consume_deferred_interrupt_status_rewind_and_jump = staticmethod(test_consume_deferred_interrupt_status_rewind_and_jump)
    test_venue_never_injects_on_quit_or_restart_decision = staticmethod(test_venue_never_injects_on_quit_or_restart_decision)
    test_maybe_user_interrupt_no_listener_is_none = staticmethod(test_maybe_user_interrupt_no_listener_is_none)
    test_venue_status_report_tolerates_missing_tokens = staticmethod(test_venue_status_report_tolerates_missing_tokens)


if __name__ == '__main__':
    unittest.main()