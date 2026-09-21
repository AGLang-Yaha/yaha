#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

"""Unit tests for the interrupt helpers (no real TTY needed).

The real Esc listener is TTY-only; these tests exercise listener state and the
interrupt command vocabulary headlessly. Parsing and dispatch now live behind
the interrupt diagnose venue: bare text is a QUESTION to the model and only a
leading-slash line is a command, so the old bare-keyword parser is gone.
"""
import io
import unittest

from yaha.core.agent import Agent
from yaha.core import interrupt_commands as ic
from yaha.ui.interrupt import EscInterruptListener


def test_bare_keywords_are_not_commands():
    # Regression pin for the venue model: 'resume' is a question, not a command.
    assert ic.is_command('resume') is False
    assert ic.parse_interrupt_line('resume') == (None, None)
    assert ic.parse_interrupt_line('/resume')[0] is not None
    assert ic.is_command('/rewind') is True
    print('test_bare_keywords_are_not_commands OK')


def test_venue_helpers_exist_and_legacy_parsers_are_gone():
    assert hasattr(Agent, '_serve_interrupt_venue')
    assert hasattr(Agent, '_maybe_boundary_venue')
    assert hasattr(Agent, '_venue_status_report')
    assert not hasattr(Agent, '_serve_interrupt_menu')
    assert not hasattr(Agent, '_parse_interrupt_command')
    assert not hasattr(Agent, '_summarize_interrupt')
    assert not hasattr(Agent, '_interrupt_status_text')
    print('test_venue_helpers_exist_and_legacy_parsers_are_gone OK')


def test_resolve_stage_index():
    fake = object.__new__(Agent)
    chain = [{'name': 'Preamble'}, {'name': 'Coding'}, {'name': 'Aftermath'}]
    assert Agent._resolve_stage_index(fake, chain, 'coding') == 1
    assert Agent._resolve_stage_index(fake, chain, 'Aftermath') == 2
    assert Agent._resolve_stage_index(fake, chain, 'missing') is None
    assert Agent._resolve_stage_index(fake, chain, None) is None
    print('test_resolve_stage_index OK')


def test_esclistener_pending_and_suspend():
    calls = []
    listener = EscInterruptListener(callback=lambda: calls.append(1))
    listener.clear_pending()
    listener._trigger()
    assert listener.is_pending() is True
    assert calls == [1]
    listener.suspend()
    listener.clear_pending()
    assert listener.is_pending() is False
    print('test_esclistener_pending_and_suspend OK')


def test_esclistener_non_stream_no_thread():
    listener = EscInterruptListener(stream=io.StringIO())
    assert listener.fileno is None
    listener.start()
    assert listener._thread is None
    print('test_esclistener_non_stream_no_thread OK')


class UserInterruptTests(unittest.TestCase):
    """Unittest-discoverable wrapper for regression functions above."""
    test_bare_keywords_are_not_commands = staticmethod(test_bare_keywords_are_not_commands)
    test_venue_helpers_exist_and_legacy_parsers_are_gone = staticmethod(test_venue_helpers_exist_and_legacy_parsers_are_gone)
    test_resolve_stage_index = staticmethod(test_resolve_stage_index)
    test_esclistener_pending_and_suspend = staticmethod(test_esclistener_pending_and_suspend)
    test_esclistener_non_stream_no_thread = staticmethod(test_esclistener_non_stream_no_thread)


if __name__ == '__main__':
    unittest.main()