#!/usr/bin/env python3
'''Regression tests for the prompt guard that protects the first keystroke.

The Esc interrupt listener owns stdin in cbreak mode and discards non-ESC bytes
while it is active. Prompts that call ui.prompt_user directly (the
AccessController security question with agent id ACS, and the API retry
wait/break prompt) must suspend the listener around the prompt, otherwise the
operator's first typed character is stolen by the cbreak reader.

These tests assert the deterministic wiring (suspend -> prompt -> resume).
'''
import types
import unittest

from yaha.core.agent import Agent, _PromptGuardUI


def test_prompt_guard_suspends_before_and_resumes_after():
    events = []

    class FakeUI:
        def prompt_user(self, *a, **k):
            events.append('prompt')
            return 'answer'

    guard = _PromptGuardUI(FakeUI(), lambda: events.append('suspend'), lambda: events.append('resume'))
    result = guard.prompt_user('Security', 'ACS', 'Allow?', default='n')
    assert result == 'answer', result
    assert events == ['suspend', 'prompt', 'resume'], events
    print('test_prompt_guard_suspends_before_and_resumes_after OK')


def test_prompt_guard_resumes_on_exception():
    events = []

    class FakeUI:
        def prompt_user(self, *a, **k):
            events.append('prompt')
            raise KeyboardInterrupt()

    guard = _PromptGuardUI(FakeUI(), lambda: events.append('suspend'), lambda: events.append('resume'))
    try:
        guard.prompt_user('Q')
    except KeyboardInterrupt:
        pass
    else:
        raise AssertionError('expected KeyboardInterrupt to propagate')
    assert events == ['suspend', 'prompt', 'resume'], events
    print('test_prompt_guard_resumes_on_exception OK')


def test_prompt_guard_passes_kwargs_and_delegates_other_attrs():
    captured = {}

    class FakeUI:
        def prompt_user(self, *a, **k):
            captured['args'] = a
            captured['kwargs'] = k
            return 'v'

        def display_warning(self, *a, **k):
            return 'warned'

    guard = _PromptGuardUI(FakeUI(), lambda: None, lambda: None)
    assert guard.prompt_user('a', 'b', 'c', default='x', multiline=False) == 'v'
    assert captured['args'] == ('a', 'b', 'c'), captured
    assert captured['kwargs'] == {'default': 'x', 'multiline': False}, captured
    assert guard.display_warning('hi') == 'warned'
    print('test_prompt_guard_passes_kwargs_and_delegates_other_attrs OK')


def test_agent_interrupt_guarded_ui_wires_suspend_resume():
    agent = object.__new__(Agent)
    calls = []
    agent.ui = types.SimpleNamespace(prompt_user=lambda *a, **k: 'x')
    agent._interrupt_suspend = lambda: calls.append('suspend')
    agent._interrupt_resume = lambda: calls.append('resume')
    guard = Agent._interrupt_guarded_ui(agent)
    assert isinstance(guard, _PromptGuardUI), guard
    guard.prompt_user('Security', 'ACS', 'Allow?')
    assert calls == ['suspend', 'resume'], calls
    print('test_agent_interrupt_guarded_ui_wires_suspend_resume OK')


def test_bash_tool_wiring_uses_guarded_ui():
    from yaha.core import agent as agent_mod
    agent = object.__new__(Agent)
    agent.access_controller = 'AC'
    agent.agent_id = 'abc123'
    agent.ui = types.SimpleNamespace(prompt_user=lambda *a, **k: 'x')
    agent._interrupt_suspend = lambda: None
    agent._interrupt_resume = lambda: None
    # _bash_tool_with_guard forwards the remaining-context budget resolved by
    # Agent._tool_output_budget(); stub it so this double needs no context state.
    agent._tool_output_budget = lambda: 4096
    seen = {}

    def fake_bash_tool(cmdline, access_controller=None, agent_id=None, ui=None,
                       max_output_bytes=None):
        seen['cmdline'] = cmdline
        seen['ac'] = access_controller
        seen['aid'] = agent_id
        seen['ui'] = ui
        seen['budget'] = max_output_bytes
        return 'ran'

    orig = agent_mod.bash_tool
    agent_mod.bash_tool = fake_bash_tool
    try:
        out = Agent._bash_tool_with_guard(agent, 'echo hi')
    finally:
        agent_mod.bash_tool = orig
    assert out == 'ran', out
    assert seen['cmdline'] == 'echo hi', seen
    assert seen['ac'] == 'AC', seen
    assert seen['aid'] == 'abc123', seen
    assert isinstance(seen['ui'], _PromptGuardUI), seen
    assert seen['budget'] == 4096, seen
    print('test_bash_tool_wiring_uses_guarded_ui OK')


def test_retry_prompt_suspends_listener():
    agent = object.__new__(Agent)
    agent.config = types.SimpleNamespace(name='TestAgent')
    agent.agent_id = 'abc123'
    agent.llm_retry_limit = 3
    agent.api_retry_prompt_timeout = 5
    events = []
    agent._interrupt_suspend = lambda: events.append('suspend')
    agent._interrupt_resume = lambda: events.append('resume')

    def fake_prompt(*a, **k):
        events.append('prompt')
        return 'wait'

    agent.ui = types.SimpleNamespace(
        prompt_user=fake_prompt,
        display_warning=lambda *a, **k: None,
    )
    result = Agent._prompt_retry_wait_or_break(agent, 'Stage', 'boom')
    assert result == 'wait', result
    assert events == ['suspend', 'prompt', 'resume'], events
    print('test_retry_prompt_suspends_listener OK')


def test_retry_prompt_resumes_on_keyboard_interrupt():
    agent = object.__new__(Agent)
    agent.config = types.SimpleNamespace(name='TestAgent')
    agent.agent_id = 'abc123'
    agent.llm_retry_limit = 3
    agent.api_retry_prompt_timeout = 5
    events = []
    agent._interrupt_suspend = lambda: events.append('suspend')
    agent._interrupt_resume = lambda: events.append('resume')

    def fake_prompt(*a, **k):
        events.append('prompt')
        raise KeyboardInterrupt()

    agent.ui = types.SimpleNamespace(
        prompt_user=fake_prompt,
        display_warning=lambda *a, **k: None,
    )
    result = Agent._prompt_retry_wait_or_break(agent, 'Stage', 'boom')
    assert result == 'break', result
    assert events == ['suspend', 'prompt', 'resume'], events
    print('test_retry_prompt_resumes_on_keyboard_interrupt OK')


class InterruptPromptGuardTests(unittest.TestCase):
    '''Unittest-discoverable wrapper for the regression functions above.'''
    test_prompt_guard_suspends_before_and_resumes_after = staticmethod(test_prompt_guard_suspends_before_and_resumes_after)
    test_prompt_guard_resumes_on_exception = staticmethod(test_prompt_guard_resumes_on_exception)
    test_prompt_guard_passes_kwargs_and_delegates_other_attrs = staticmethod(test_prompt_guard_passes_kwargs_and_delegates_other_attrs)
    test_agent_interrupt_guarded_ui_wires_suspend_resume = staticmethod(test_agent_interrupt_guarded_ui_wires_suspend_resume)
    test_bash_tool_wiring_uses_guarded_ui = staticmethod(test_bash_tool_wiring_uses_guarded_ui)
    test_retry_prompt_suspends_listener = staticmethod(test_retry_prompt_suspends_listener)
    test_retry_prompt_resumes_on_keyboard_interrupt = staticmethod(test_retry_prompt_resumes_on_keyboard_interrupt)


if __name__ == '__main__':
    unittest.main()
