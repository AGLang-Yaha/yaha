#!/usr/bin/env python3
"""Regression tests for chat-style multi-line editing/pasting in ask_user.

`prompt_user` gained an opt-in `multiline` flag. On a TTY with multiline=True
it delegates to `_multiline_editor`:
- Enter always submits the whole buffer (no Mac Alt/Option required to send);
- pasting a block with newlines inserts it, not submits it;
- Control+J and Esc+Enter (two literal keys) are always-available optional
  line breaks; Option+Enter inserts a newline only when the terminal forwards
  Option/Meta as Escape (otherwise it is indistinguishable from Enter).

Default calls (`multiline=False`) and the timed retry path remain unchanged.
"""

import io
import os
import unittest
from unittest.mock import patch

from rich.console import Console

from yaha.core.agent import Agent
from yaha.ui.terminal import YAHAUI, ASK_INTERRUPT_SENTINEL, _multiline_editor


def _ui():
    ui = YAHAUI(verbose=True)
    ui.console = Console(width=100, record=True, file=io.StringIO())
    return ui


def test_prompt_user_multiline_non_tty_falls_back_to_input():
    ui = _ui()
    with patch("sys.stdin.isatty", return_value=False), patch("builtins.input", return_value="hello") as mock_input:
        result = ui.prompt_user("agent", "id", "Go?", default="", multiline=True)
    assert result == "hello", result
    assert mock_input.call_args.args == ("> ",), mock_input.call_args
    print("test_prompt_user_multiline_non_tty_falls_back_to_input OK")


def test_prompt_user_tty_multiline_uses_editor_preserves_newlines():
    ui = _ui()
    # Avoid opening a real prompt_toolkit session: verify prompt_user delegates
    # to the editor helper and preserves embedded newlines.
    with patch("sys.stdin.isatty", return_value=True), patch.object(ui, "_prompt_multiline", return_value="line1\nline2") as editor:
        result = ui.prompt_user("agent", "id", "Go?", default="", multiline=True)
    assert result == "line1\nline2", result
    editor.assert_called_once_with("")
    print("test_prompt_user_tty_multiline_uses_editor_preserves_newlines OK")


def test_prompt_user_default_remains_single_line_input():
    # Default calls (multiline=False) must behave exactly like the old prompt
    # even on a TTY: builtin input(), no editor, no extra hint.
    ui = _ui()
    with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", return_value="one") as mock_input, patch.object(ui, "_prompt_multiline") as editor:
        result = ui.prompt_user("agent", "id", "Go?", default="")
    assert result == "one", result
    assert editor.call_count == 0, editor.call_args
    assert mock_input.call_args.args == ("> ",), mock_input.call_args
    print("test_prompt_user_default_remains_single_line_input OK")


def test_prompt_user_timed_path_ignores_multiline_flag():
    # The unattended/retry path (timeout given) stays select/readline based;
    # multiline=True must never route it to the editor.
    ui = _ui()
    with patch("sys.stdin.isatty", return_value=False), patch("yaha.ui.terminal.time.sleep") as mock_sleep, patch.object(ui, "_prompt_multiline") as editor:
        result = ui.prompt_user("agent", "id", "Wait or break?", default="wait", timeout=0.01, multiline=True)
    assert result is None, result
    assert editor.call_count == 0, editor.call_args
    mock_sleep.assert_called_once()
    print("test_prompt_user_timed_path_ignores_multiline_flag OK")


def test_prompt_multiline_strips_outer_whitespace_keeps_newlines():
    ui = _ui()
    with patch("yaha.ui.terminal._multiline_editor", return_value="\nline1\nline2\n") as editor:
        result = ui._prompt_multiline("fallback")
    assert result == "line1\nline2", result
    editor.assert_called_once_with("fallback")
    print("test_prompt_multiline_strips_outer_whitespace_keeps_newlines OK")


def test_prompt_multiline_empty_editor_result_returns_default():
    ui = _ui()
    with patch("yaha.ui.terminal._multiline_editor", return_value="   ") as editor:
        result = ui._prompt_multiline("fallback")
    assert result == "fallback", result
    editor.assert_called_once_with("fallback")
    print("test_prompt_multiline_empty_editor_result_returns_default OK")


def test_prompt_multiline_eof_or_interrupt_returns_default():
    ui = _ui()
    with patch("yaha.ui.terminal._multiline_editor", side_effect=EOFError):
        assert ui._prompt_multiline("fallback") == "fallback"
    with patch("yaha.ui.terminal._multiline_editor", side_effect=KeyboardInterrupt):
        assert ui._prompt_multiline("fallback") == "fallback"
    print("test_prompt_multiline_eof_or_interrupt_returns_default OK")


def test_prompt_multiline_editor_missing_falls_back_to_input():
    ui = _ui()
    with patch("yaha.ui.terminal._multiline_editor", side_effect=ImportError("no pt")), patch("builtins.input", return_value="ok") as mock_input:
        result = ui._prompt_multiline("fallback")
    assert result == "ok", result
    mock_input.assert_called_once_with("> ")
    print("test_prompt_multiline_editor_missing_falls_back_to_input OK")


def test_multiline_editor_binding_contract():
    # Structural test: no Option/Meta key is needed to submit.
    # Enter must accept; only the optional line-break bindings may insert \n.
    bound = {}

    class RecordingKB:
        def add(self, *keys):
            def register(fn):
                bound[tuple(keys)] = fn
                return fn
            return register

    class FakeSession:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def prompt(self, *args, **kwargs):
            return "a\nb"

    with patch("prompt_toolkit.PromptSession", FakeSession):
        with patch("prompt_toolkit.key_binding.KeyBindings", RecordingKB):
            out = _multiline_editor("pre")

    assert out == "a\nb", out
    assert set(bound) == {("enter",), ("c-j",), ("escape", "enter"), ("c-i",)}, set(bound)

    class Buffer:
        def __init__(self):
            self.accepted = 0
            self.inserted = []

        def validate_and_handle(self):
            self.accepted += 1

        def insert_text(self, text):
            self.inserted.append(text)

    class Event:
        def __init__(self, buffer):
            self.current_buffer = buffer

    enter_buf = Buffer()
    ctrlj_buf = Buffer()
    option_buf = Buffer()
    bound[("enter",)](Event(enter_buf))
    bound[("c-j",)](Event(ctrlj_buf))
    bound[("escape", "enter")](Event(option_buf))

    assert enter_buf.accepted == 1, enter_buf.accepted
    assert ctrlj_buf.inserted == ["\n"], ctrlj_buf.inserted
    assert option_buf.inserted == ["\n"], option_buf.inserted
    print("test_multiline_editor_binding_contract OK")


def test_multiline_editor_disables_cpr_probe_env():
    """The editor must set PROMPT_TOOLKIT_NO_CPR before constructing the session.

    Without this, prompt_toolkit probes CPR with ESC[6n; terminals that never
    answer make it print the unsupported-CPR warning and redraw via
    run_in_terminal, which can swallow the first typed character.
    """
    captured = {}

    class FakeSession:
        def __init__(self, **kwargs):
            captured["no_cpr_at_construction"] = os.environ.get("PROMPT_TOOLKIT_NO_CPR")

        def prompt(self, *args, **kwargs):
            return "answer"

    class FakeKB:
        def add(self, *keys):
            def register(fn):
                return fn
            return register

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("PROMPT_TOOLKIT_NO_CPR", None)
        with patch("prompt_toolkit.PromptSession", FakeSession), patch("prompt_toolkit.key_binding.KeyBindings", FakeKB):
            result = _multiline_editor("")

    assert result == "answer", result
    assert captured["no_cpr_at_construction"] == "1", captured
    print("test_multiline_editor_disables_cpr_probe_env OK")


def test_multiline_hint_no_longer_promises_bare_option_enter():
    # Regression: on a multiline TTY the hint must not claim bare
    # Option+Enter adds a line (default macOS Terminal sends plain Return for
    # Option+Enter). Guaranteed helpers Control+J and Esc+Enter are advertised.
    ui = _ui()
    with patch("sys.stdin.isatty", return_value=True), patch.object(ui, "_prompt_multiline", return_value="ok") as editor:
        result = ui.prompt_user("agent", "id", "Go?", default="", multiline=True)
    assert result == "ok", result
    editor.assert_called_once_with("")
    out = ui.console.export_text()
    assert "Control+J or Esc+Enter adds a line" in out, repr(out)
    assert "Option+Enter adds a line" not in out, repr(out)
    print("test_multiline_hint_no_longer_promises_bare_option_enter OK")


def test_agent_ask_user_enables_multiline():
    calls = []

    class FakeUI:
        def prompt_user(self, *args, **kwargs):
            calls.append((args, kwargs))
            return "ok"

    class FakeConfig:
        name = "FooAgent"

    class FakeAgent:
        ui = FakeUI()
        config = FakeConfig()
        agent_id = "agent-1"

    result = Agent.ask_user(FakeAgent(), "Question?")
    assert result == "ok", result
    assert calls == [(("FooAgent", "agent-1", "Question?"), {"multiline": True})], calls
    print("test_agent_ask_user_enables_multiline OK")


def test_agent_ask_user_reasks_on_empty_once():
    calls = []

    class FakeUI:
        def __init__(self):
            self.warnings = []

        def prompt_user(self, *args, **kwargs):
            calls.append((args, kwargs))
            if len(calls) == 1:
                return ""
            return "real task"

        def display_warning(self, *args, **kwargs):
            self.warnings.append((args, kwargs))

    class FakeConfig:
        name = "FooAgent"

    class FakeAgent:
        ui = FakeUI()
        config = FakeConfig()
        agent_id = "agent-1"

    result = Agent.ask_user(FakeAgent(), "What task?")
    assert result == "real task", result
    assert len(calls) == 2, calls
    expected = (("FooAgent", "agent-1", "What task?"), {"multiline": True})
    assert calls[0] == expected, calls
    assert calls[1] == expected, calls
    assert FakeAgent.ui.warnings, FakeAgent.ui.warnings
    print("test_agent_ask_user_reasks_on_empty_once OK")


def test_agent_ask_user_returns_no_reply_marker_after_empty_loop():
    calls = []

    class FakeUI:
        def __init__(self):
            self.warnings = []

        def prompt_user(self, *args, **kwargs):
            calls.append((args, kwargs))
            return ""

        def display_warning(self, *args, **kwargs):
            self.warnings.append((args, kwargs))

    class FakeConfig:
        name = "FooAgent"

    class FakeAgent:
        ui = FakeUI()
        config = FakeConfig()
        agent_id = "agent-1"

    result = Agent.ask_user(FakeAgent(), "Question?")
    assert "no reply" in result, result
    assert "ya" not in result, result
    assert len(calls) == 3, calls
    assert len(FakeAgent.ui.warnings) == 2, FakeAgent.ui.warnings
    print("test_agent_ask_user_returns_no_reply_marker_after_empty_loop OK")


def test_agent_ask_user_preserves_normal_non_empty_answer():
    calls = []

    class FakeUI:
        def prompt_user(self, *args, **kwargs):
            calls.append((args, kwargs))
            return "debug regression"

        def display_warning(self, *args, **kwargs):
            raise AssertionError("warning should not be called for a non-empty answer")

    class FakeConfig:
        name = "FooAgent"

    class FakeAgent:
        ui = FakeUI()
        config = FakeConfig()
        agent_id = "agent-1"

    result = Agent.ask_user(FakeAgent(), "Question?")
    assert result == "debug regression", result
    assert len(calls) == 1, calls
    print("test_agent_ask_user_preserves_normal_non_empty_answer OK")


class AskUserMultilineTests(unittest.TestCase):
    """Unittest-discoverable wrapper for the regression functions above."""
    test_prompt_user_multiline_non_tty_falls_back_to_input = staticmethod(test_prompt_user_multiline_non_tty_falls_back_to_input)
    test_prompt_user_tty_multiline_uses_editor_preserves_newlines = staticmethod(test_prompt_user_tty_multiline_uses_editor_preserves_newlines)
    test_prompt_user_default_remains_single_line_input = staticmethod(test_prompt_user_default_remains_single_line_input)
    test_prompt_user_timed_path_ignores_multiline_flag = staticmethod(test_prompt_user_timed_path_ignores_multiline_flag)
    test_prompt_multiline_strips_outer_whitespace_keeps_newlines = staticmethod(test_prompt_multiline_strips_outer_whitespace_keeps_newlines)
    test_prompt_multiline_empty_editor_result_returns_default = staticmethod(test_prompt_multiline_empty_editor_result_returns_default)
    test_prompt_multiline_eof_or_interrupt_returns_default = staticmethod(test_prompt_multiline_eof_or_interrupt_returns_default)
    test_prompt_multiline_editor_missing_falls_back_to_input = staticmethod(test_prompt_multiline_editor_missing_falls_back_to_input)
    test_multiline_editor_binding_contract = staticmethod(test_multiline_editor_binding_contract)
    test_multiline_editor_disables_cpr_probe_env = staticmethod(test_multiline_editor_disables_cpr_probe_env)
    test_multiline_hint_no_longer_promises_bare_option_enter = staticmethod(test_multiline_hint_no_longer_promises_bare_option_enter)
    test_agent_ask_user_enables_multiline = staticmethod(test_agent_ask_user_enables_multiline)
    test_agent_ask_user_reasks_on_empty_once = staticmethod(test_agent_ask_user_reasks_on_empty_once)
    test_agent_ask_user_returns_no_reply_marker_after_empty_loop = staticmethod(test_agent_ask_user_returns_no_reply_marker_after_empty_loop)
    test_agent_ask_user_preserves_normal_non_empty_answer = staticmethod(test_agent_ask_user_preserves_normal_non_empty_answer)


if __name__ == "__main__":
    unittest.main()
