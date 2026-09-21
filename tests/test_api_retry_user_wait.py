#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

"""Regression tests for API retry exhaustion: user wait/break flow.

Covers both the original fix (retry round 10, wait/break prompt, provider errors
never become final answers) and the review-fix additions:
- unattended no-reply rounds are capped (api_retry_unattended_max_rounds)
- Ctrl+C at the timed prompt maps to break
- timed prompt prints a prompt glyph and returns None on timeout / absent stdin
- no-timeout prompt_user path remains unchanged

This file lives in selfcoding/tests (local reftest runner); a copy also lives in
worktree/yaha/tests so patchman exports it as canonical <repo>/tests/... .
"""
import io
import shutil
import sys
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from rich.console import Console

from yaha.core.agent import Agent
from yaha.core.engine import YAHAEngine
from yaha.ui.terminal import YAHAUI



_AGENT_FIXTURE = """
## RetryNoTool
- **Inherited:** None
- **Code:** RN
- **Toolset:**
> preamble
"""

_STAGE = {
    "name": "Preamble",
    "allowed_tools": [],
    "exit_tools": [],
    "required_tools": [],
}


class _FlakyLLM:
    """Scripted mock: `errors` provider failures, then `final` if provided."""

    def __init__(self, errors=3, final=None, error_text="Error calling MockProvider: 500 boom"):
        self.errors = errors
        self.final = final
        self.error_text = error_text
        self.calls = 0
        self.usage = {"prompt_tokens": 5, "completion_tokens": 5}

    def generate(self, messages, **kwargs):
        self.calls += 1
        if self.calls <= self.errors:
            return self.error_text, dict(self.usage), None
        if self.final is not None:
            return self.final, dict(self.usage), None
        return self.error_text, dict(self.usage), None


def _make_agent(errors=99, final=None, answers=None, unattended_rounds=3, unattended=True):
    """Build a no-tool Agent with verbose UI suppressed and small retry values."""
    ws = Path(tempfile.mkdtemp(prefix="yaha_retry_", ))
    (ws / ".yaha").mkdir(parents=True)
    for sub in ("memory", "ext", "logs"):
        (ws / ".yaha" / sub).mkdir()
    (ws / ".yaha" / "tool-schema.md").write_text("[]")
    (ws / ".yaha" / "AgentClass.md").write_text(_AGENT_FIXTURE)
    (ws / ".yaha" / "Model.md").write_text("")
    eng = YAHAEngine(workspace_dir=str(ws))
    cfg = eng.agent_classes["RetryNoTool"]
    agent = Agent(
        agent_config=cfg,
        session=eng.session,
        models_config=eng.models_config,
        agent_classes=eng.agent_classes,
        tool_schemas=eng.tool_schemas,
        access_controller=eng.access_controller,
    )
    # Small test knobs + muted UI.
    agent.llm_retry_limit = 3
    agent.api_retry_prompt_timeout = 0.001
    agent.api_retry_wait_seconds = 0.001
    agent.api_retry_unattended_max_rounds = unattended_rounds
    agent.unattended = unattended
    agent.ui.console = Console(width=100, file=io.StringIO())
    agent.llm = _FlakyLLM(errors=errors, final=final)
    if answers is not None:
        box = list(answers)
        agent.ui.prompt_user = lambda *a, **k: box.pop(0) if box else None
    return agent, ws


def _run(agent):
    return agent._run_react_loop(1, _STAGE)


def _runner(fn):
    fn()
    print(f"{fn.__name__} OK")


def test_break_returns_graceful_error():
    agent, ws = _make_agent(errors=99, answers=["break"])
    try:
        with patch("time.sleep"):
            result, status, _ = _run(agent)
        assert status == "ERROR", (result, status)
        assert result.startswith("Error:"), result
        assert "user chose to break" in result, result
        assert "Error calling" not in result, result
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def test_break_synonyms():
    for word in ("b", "quit", "abort", "stop", "exit", "no", "n", "q"):
        agent, ws = _make_agent(errors=99, answers=[word])
        try:
            with patch("time.sleep"):
                result, status, _ = _run(agent)
            assert status == "ERROR", (word, result, status)
            assert "user chose to break" in result, (word, result)
        finally:
            shutil.rmtree(ws, ignore_errors=True)


def test_wait_resets_and_retries_success():
    agent, ws = _make_agent(errors=3, final="final ok", answers=["wait"])
    try:
        with patch("time.sleep") as sleep_mock:
            result, status, _ = _run(agent)
        assert agent.llm.calls == 4, agent.llm.calls
        assert status == "DONE", (result, status)
        assert result == "final ok", result
        assert "Error calling" not in result
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def test_no_reply_round_caps_unattended_after_limit():
    # cap=2 * limit=3 calls = 6 attempts, then graceful unattended abort.
    agent, ws = _make_agent(errors=99, answers=None, unattended_rounds=2)
    try:
        with patch("time.sleep"):
            result, status, _ = _run(agent)
        assert agent.llm.calls == 6, agent.llm.calls
        assert status == "ERROR", (result, status)
        assert "No user response after 2 unattended retry rounds" in result, result
        assert "Error calling" not in result, result
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def test_explicit_wait_resets_unattended_counter():
    # no-reply round 1, explicit wait round 2 resets counter, more errors then
    # a no-reply round 3 should NOT abort (counter reset by the wait).
    agent, ws = _make_agent(errors=9, final="final ok", answers=[None, "wait"], unattended_rounds=2)
    try:
        with patch("time.sleep"):
            result, status, _ = _run(agent)
        # rounds: [err err err -> None], [err err err -> wait],
        #         [err err err -> None] count = 1, [next -> final ok]
        assert agent.llm.calls == 10, agent.llm.calls
        assert status == "DONE", (result, status)
        assert result == "final ok", result
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def test_api_error_never_returned_as_final_in_one_pass():
    agent, ws = _make_agent(errors=99, answers=None, unattended_rounds=2)
    try:
        with patch("time.sleep"):
            result, status, _ = _run(agent)
        assert status == "ERROR", (result, status)
        assert "Error calling" not in result, result
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def test_partial_errors_then_success_without_exhaustion():
    agent, ws = _make_agent(errors=2, final="final ok")
    try:
        with patch("time.sleep"):
            result, status, _ = _run(agent)
        assert agent.llm.calls == 3, agent.llm.calls
        assert status == "DONE", (result, status)
        assert result == "final ok", result
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def test_keyboard_interrupt_at_prompt_maps_to_break():
    agent, ws = _make_agent(errors=99)

    def interrupting_prompt(*args, **kwargs):
        raise KeyboardInterrupt

    agent.ui.prompt_user = interrupting_prompt
    try:
        with patch("time.sleep"):
            result, status, _ = _run(agent)
        assert status == "ERROR", (result, status)
        assert "user chose to break" in result, result
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def test_prompt_user_timed_timeout_returns_none():
    ui = YAHAUI(verbose=True)
    old_stdin = sys.stdin

    class _FakeStdin:
        def isatty(self):
            return True

        def readline(self):
            raise AssertionError("readline should not be reached on timeout")

    sys.stdin = _FakeStdin()
    try:
        with patch("select.select", return_value=([], [], [])), patch("time.sleep") as sleep_mock:
            result = ui.prompt_user("a", "i", "Q?", default="wait", timeout=0.05)
    finally:
        sys.stdin = old_stdin
    assert result is None, result


def test_prompt_user_timed_typed_answer_returned():
    ui = YAHAUI(verbose=True)
    old_stdin = sys.stdin

    class _FakeStdin:
        def isatty(self):
            return True

        def readline(self):
            return "break\n"

    fake = _FakeStdin()
    sys.stdin = fake
    try:
        with patch("select.select", return_value=([fake], [], [])):
            result = ui.prompt_user("a", "i", "Q?", default="wait", timeout=0.05)
    finally:
        sys.stdin = old_stdin
    assert result == "break", result


def test_prompt_user_timed_keyboard_interrupt_propagates():
    ui = YAHAUI(verbose=True)
    old_stdin = sys.stdin

    class _FakeStdin:
        def isatty(self):
            return True

        def readline(self):
            raise KeyboardInterrupt

    fake = _FakeStdin()
    sys.stdin = fake
    raised = False
    try:
        with patch("select.select", return_value=([fake], [], [])):
            try:
                ui.prompt_user("a", "i", "Q?", default="wait", timeout=0.05)
            except KeyboardInterrupt:
                raised = True
    finally:
        sys.stdin = old_stdin
    assert raised, "KeyboardInterrupt should propagate from timed prompt"


def test_prompt_user_timed_prints_glyph():
    stream = io.StringIO()
    ui = YAHAUI(verbose=True)
    ui.console = Console(width=100, file=stream)
    old_stdin = sys.stdin

    class _FakeStdin:
        def isatty(self):
            return True

        def readline(self):
            return "wait\n"

    fake = _FakeStdin()
    sys.stdin = fake
    try:
        with patch("select.select", return_value=([fake], [], [])):
            ui.prompt_user("a", "i", "Q?", default="wait", timeout=0.05)
    finally:
        sys.stdin = old_stdin
    assert "> " in stream.getvalue(), stream.getvalue()


def test_prompt_user_without_timeout_is_unchanged():
    ui = YAHAUI(verbose=True)
    ui.console = Console(width=100, file=io.StringIO())
    with patch("builtins.input", return_value="  answer  ") as input_mock:
        result = ui.prompt_user("a", "i", "Q?", default="d")
    assert result == "answer", result
    assert input_mock.call_args.args == ("> ",), input_mock.call_args


def test_attended_no_reply_does_not_auto_abort_after_cap():
    # Attended mode (unattended=False): no reply rounds must NOT hit the
    # unattended round cap. The run only stops when the user finally breaks.
    agent, ws = _make_agent(errors=99, answers=[None, None, "break"], unattended_rounds=1)
    agent.unattended = False
    try:
        with patch("time.sleep"):
            result, status, _ = _run(agent)
        assert status == "ERROR", (result, status)
        assert "user chose to break" in result, result
        assert "No user response after" not in result, result
        assert agent.llm.calls == 9, agent.llm.calls
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def test_explicit_unattended_flag_still_caps():
    # Explicit automation mode: cap remains enforced independently of TTY mock.
    agent, ws = _make_agent(errors=99, answers=None, unattended_rounds=2)
    agent.unattended = True
    try:
        with patch("time.sleep"):
            result, status, _ = _run(agent)
        assert agent.llm.calls == 6, agent.llm.calls
        assert status == "ERROR", (result, status)
        assert "No user response after 2 unattended retry rounds" in result, result
    finally:
        shutil.rmtree(ws, ignore_errors=True)


class ApiRetryUserWaitTests(unittest.TestCase):
    """Retry regression functions exposed to unittest discovery."""
    test_break_returns_graceful_error = staticmethod(test_break_returns_graceful_error)
    test_break_synonyms = staticmethod(test_break_synonyms)
    test_wait_resets_and_retries_success = staticmethod(test_wait_resets_and_retries_success)
    test_no_reply_round_caps_unattended_after_limit = staticmethod(test_no_reply_round_caps_unattended_after_limit)
    test_explicit_wait_resets_unattended_counter = staticmethod(test_explicit_wait_resets_unattended_counter)
    test_api_error_never_returned_as_final_in_one_pass = staticmethod(test_api_error_never_returned_as_final_in_one_pass)
    test_partial_errors_then_success_without_exhaustion = staticmethod(test_partial_errors_then_success_without_exhaustion)
    test_keyboard_interrupt_at_prompt_maps_to_break = staticmethod(test_keyboard_interrupt_at_prompt_maps_to_break)
    test_prompt_user_timed_timeout_returns_none = staticmethod(test_prompt_user_timed_timeout_returns_none)
    test_prompt_user_timed_typed_answer_returned = staticmethod(test_prompt_user_timed_typed_answer_returned)
    test_prompt_user_timed_keyboard_interrupt_propagates = staticmethod(test_prompt_user_timed_keyboard_interrupt_propagates)
    test_prompt_user_timed_prints_glyph = staticmethod(test_prompt_user_timed_prints_glyph)
    test_prompt_user_without_timeout_is_unchanged = staticmethod(test_prompt_user_without_timeout_is_unchanged)
    test_attended_no_reply_does_not_auto_abort_after_cap = staticmethod(test_attended_no_reply_does_not_auto_abort_after_cap)
    test_explicit_unattended_flag_still_caps = staticmethod(test_explicit_unattended_flag_still_caps)


if __name__ == "__main__":
    unittest.main()
