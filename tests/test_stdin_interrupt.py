#!/usr/bin/env python3
'''Regression tests for engine-owned stdin / Esc-interrupt control.

Ownership model: the YAHAEngine owns ONE StdinInterruptController per run
(stdin cbreak mode + the Esc reader thread). Every agent the engine creates -
root, spawn_parallel_agent peers, nested yaha_spawn_agent children - shares it:
agents suspend it around prompts and poll it for a pending Esc, so keystrokes
typed at ANY agent's prompt survive, and Esc is served by whichever agent is
on the CPU (including peers after the root's chain has finished).

Also covered: the I/O-lock guarantee that once suspend() returns the reader
thread provably does not consume bytes until resume() (the old select/suspend
race that swallowed a prompt's first keystroke).
'''
import os
import select
import shutil
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from yaha.core.agent import Agent, VenueResult
from yaha.core import interrupt_commands as ic
from yaha.core.engine import YAHAEngine
from yaha.core.runtime import AgentRuntime
from yaha.ui.interrupt import EscInterruptListener, StdinInterruptController

HOME = Path(__file__).resolve().parent.parent
TMP = HOME / 'tmp'


# --- helpers ---------------------------------------------------------------

class _FakeTty:
    '''File-like stand-in for sys.stdin backed by a real fd.'''

    def __init__(self, fd, tty=True):
        self._fd = fd
        self._tty = tty

    def fileno(self):
        return self._fd

    def isatty(self):
        return self._tty


def _pipe_stream():
    r, w = os.pipe()
    return _FakeTty(r), r, w


def _read_available(fd):
    ready, _, _ = select.select([fd], [], [], 0)
    return bool(ready)


class _FakeController:
    def __init__(self, events=None):
        self._events = events if events is not None else []
        self.pending = False

    def suspend(self):
        self._events.append('suspend')

    def resume(self):
        self._events.append('resume')

    def is_pending(self):
        return self.pending

    def clear_pending(self):
        self._events.append('clear')
        self.pending = False


def _bare_agent(controller=None):
    agent = object.__new__(Agent)
    agent.stdin_interrupt = controller
    agent._serving_interrupt = False
    agent._compressing = False
    return agent


# --- controller lifecycle ----------------------------------------------------

def test_install_non_tty_returns_false_and_stays_inert():
    r, w = os.pipe()
    try:
        ctl = StdinInterruptController(stream=_FakeTty(r, tty=False))
        assert ctl.install() is False
        assert ctl.active is False
        assert ctl.is_pending() is False
        # Everything is a safe no-op on an uninstalled controller.
        ctl.suspend()
        ctl.resume()
        ctl.clear_pending()
        ctl.stop()
    finally:
        os.close(r)
        os.close(w)
    print('test_install_non_tty_returns_false_and_stays_inert OK')


def test_install_starts_listener_and_stop_clears():
    stream, r, w = _pipe_stream()
    ctl = StdinInterruptController(stream=stream)
    try:
        assert ctl.install() is True
        assert ctl.active is True
        thread = ctl.listener._thread
        assert thread is not None and thread.is_alive()
        # termios does not work on a pipe: the listener must still be active.
        assert ctl.cbreak_now is False
        ctl.stop()
        assert ctl.active is False
        assert not thread.is_alive()
    finally:
        ctl.stop()
        os.close(r)
        os.close(w)
    print('test_install_starts_listener_and_stop_clears OK')


def test_install_is_idempotent():
    stream, r, w = _pipe_stream()
    ctl = StdinInterruptController(stream=stream)
    try:
        assert ctl.install() is True
        first = ctl.listener
        assert ctl.install() is True
        assert ctl.listener is first
    finally:
        ctl.stop()
        os.close(r)
        os.close(w)
    print('test_install_is_idempotent OK')


def test_suspend_preserves_bytes_written_after_suspend():
    stream, r, w = _pipe_stream()
    ctl = StdinInterruptController(stream=stream)
    ctl.install()
    try:
        ctl.suspend()
        os.write(w, b'x')
        # Several loop iterations pass; a reading listener would have eaten it.
        time.sleep(0.35)
        assert _read_available(r), 'controller consumed a byte while suspended'
        assert ctl.is_pending() is False
    finally:
        ctl.stop()
        os.close(r)
        os.close(w)
    print('test_suspend_preserves_bytes_written_after_suspend OK')


def test_suspend_during_inflight_select_preserves_byte():
    # The regression: suspend() raced a listener already blocked in select(),
    # and the keystroke that woke select() was read and discarded. With the
    # I/O lock, suspend() waits the cycle out and the reader re-checks the
    # flag, so the byte survives for the prompt.
    stream, r, w = _pipe_stream()
    ctl = StdinInterruptController(stream=stream)
    ctl.install()
    try:
        time.sleep(0.05)  # listener is almost surely inside select(0.1) now
        ctl.suspend()
        os.write(w, b'h')
        time.sleep(0.35)
        assert _read_available(r), 'in-flight select swallowed the keystroke'
    finally:
        ctl.stop()
        os.close(r)
        os.close(w)
    print('test_suspend_during_inflight_select_preserves_byte OK')


def test_resume_restores_esc_interrupt():
    stream, r, w = _pipe_stream()
    ctl = StdinInterruptController(stream=stream)
    ctl.install()
    try:
        ctl.suspend()
        ctl.resume()
        os.write(w, b'\x1b')
        deadline = time.time() + 1.5
        while time.time() < deadline and not ctl.is_pending():
            time.sleep(0.02)
        assert ctl.is_pending(), 'lone ESC after resume did not trigger'
        ctl.clear_pending()
        assert ctl.is_pending() is False
    finally:
        ctl.stop()
        os.close(r)
        os.close(w)
    print('test_resume_restores_esc_interrupt OK')


def test_non_esc_bytes_still_discarded_while_active():
    # The deliberate type-ahead policy is unchanged: outside a prompt the
    # active listener consumes (and drops) ordinary bytes.
    stream, r, w = _pipe_stream()
    ctl = StdinInterruptController(stream=stream)
    ctl.install()
    try:
        os.write(w, b'q')
        deadline = time.time() + 1.5
        while time.time() < deadline and _read_available(r):
            time.sleep(0.02)
        assert not _read_available(r), 'active listener did not drain type-ahead'
        assert ctl.is_pending() is False
    finally:
        ctl.stop()
        os.close(r)
        os.close(w)
    print('test_non_esc_bytes_still_discarded_while_active OK')


def test_pty_cbreak_cooked_transitions():
    import pty
    import termios
    master, slave = pty.openpty()
    ctl = StdinInterruptController(stream=_FakeTty(slave))
    try:
        assert termios.tcgetattr(slave)[3] & termios.ICANON
        assert ctl.install() is True
        assert ctl.cbreak_now is True
        assert not (termios.tcgetattr(slave)[3] & termios.ICANON), 'cbreak not set'
        ctl.suspend()
        assert termios.tcgetattr(slave)[3] & termios.ICANON, 'cooked not restored'
        ctl.resume()
        assert not (termios.tcgetattr(slave)[3] & termios.ICANON), 'cbreak not re-armed'
        ctl.stop()
        assert termios.tcgetattr(slave)[3] & termios.ICANON, 'orig not restored'
        assert ctl.active is False
    finally:
        ctl.stop()
        os.close(master)
        os.close(slave)
    print('test_pty_cbreak_cooked_transitions OK')


# --- agent delegation --------------------------------------------------------

def test_agent_suspend_resume_delegate_to_controller():
    events = []
    agent = _bare_agent(controller=_FakeController(events))
    Agent._interrupt_suspend(agent)
    Agent._interrupt_resume(agent)
    assert events == ['suspend', 'resume'], events
    print('test_agent_suspend_resume_delegate_to_controller OK')


def test_agent_without_controller_suspend_resume_are_noops():
    agent = _bare_agent(controller=None)
    Agent._interrupt_suspend(agent)  # must not raise
    Agent._interrupt_resume(agent)
    print('test_agent_without_controller_suspend_resume_are_noops OK')


def test_ask_user_suspends_controller_around_prompt():
    events = []
    agent = _bare_agent(controller=_FakeController(events))
    agent.config = types.SimpleNamespace(name='Peer')
    agent.agent_id = 'peer0001'

    def fake_prompt(*a, **k):
        events.append('prompt')
        return 'the answer'

    agent.ui = types.SimpleNamespace(
        prompt_user=fake_prompt,
        display_warning=lambda *a, **k: None,
    )
    # The real _prompt_interrupt_safe must route the prompt through the
    # shared controller: suspend -> prompt -> resume.
    out = Agent.ask_user(agent, 'Q?')
    assert out == 'the answer', out
    assert events == ['suspend', 'prompt', 'resume'], events
    print('test_ask_user_suspends_controller_around_prompt OK')


# --- Esc service by any agent ------------------------------------------------

def test_peer_serves_pending_esc_via_loop_poll():
    # The gap fix: a peer owns no listener, but the shared controller's
    # pending flag is polled by EVERY agent, so a peer services the Esc that
    # landed while it held the CPU.
    ctl = _FakeController()
    ctl.pending = True
    peer = _bare_agent(controller=ctl)
    served = []
    peer._serve_interrupt_venue = lambda *a, **k: (
        served.append(a), VenueResult(ic.STATUS_RESUME, None, None))[1]
    result = Agent._maybe_user_interrupt(peer, {'name': 'Review'}, 1, {'ask_user'})
    assert result is not None and result.status == ic.STATUS_RESUME, result
    assert served and served[0][0].get('name') == 'Review', served
    assert ctl.pending is False, 'pending flag must be consumed'
    # A second poll with no new Esc is a no-op.
    assert Agent._maybe_user_interrupt(peer, {'name': 'Review'}, 1, set()) is None
    print('test_peer_serves_pending_esc_via_loop_poll OK')


def test_peer_serves_pending_esc_via_boundary_poll():
    ctl = _FakeController()
    ctl.pending = True
    peer = _bare_agent(controller=ctl)
    peer._last_stage_turn = 3
    peer._last_stage_tools_called = {'bash_tool'}
    served = []
    peer._serve_interrupt_venue = lambda *a, **k: (
        served.append(a), VenueResult(ic.STATUS_RESUME, None, None))[1]
    result = Agent._maybe_boundary_venue(peer, {'name': 'Coding'})
    assert result is not None and result.status == ic.STATUS_RESUME, result
    assert served and served[0][1] == 3 and served[0][2] == {'bash_tool'}, served
    assert ctl.pending is False
    print('test_peer_serves_pending_esc_via_boundary_poll OK')


def test_pending_esc_not_served_while_serving_or_compressing():
    ctl = _FakeController()
    ctl.pending = True
    agent = _bare_agent(controller=ctl)
    agent._serving_interrupt = True
    assert Agent._maybe_user_interrupt(agent, {'name': 'S'}, 0, set()) is None
    agent._serving_interrupt = False
    agent._compressing = True
    assert Agent._maybe_user_interrupt(agent, {'name': 'S'}, 0, set()) is None
    assert ctl.pending is True, 'guards must not consume the pending flag'
    print('test_pending_esc_not_served_while_serving_or_compressing OK')


def test_maybe_user_interrupt_without_controller_is_none():
    agent = _bare_agent(controller=None)
    assert Agent._maybe_user_interrupt(agent, {'name': 'S'}, 0, set()) is None
    assert Agent._maybe_boundary_venue(agent, {'name': 'S'}) is None
    print('test_maybe_user_interrupt_without_controller_is_none OK')


# --- engine wiring -----------------------------------------------------------

FIXTURE = '''
## Solo
- **Inherited:** None
- **Code:** Sol
- **Model:** m
- **Toolset:**
    - bash_tool
- **Preamble:**
    > solo
- **Aftermath:**
    > solo done
'''


def _make_engine():
    TMP.mkdir(parents=True, exist_ok=True)
    ws = Path(tempfile.mkdtemp(prefix='yaha_stdin_', dir=str(TMP)))
    yh = ws / '.yaha'
    yh.mkdir(parents=True)
    (yh / 'memory').mkdir()
    (yh / 'ext').mkdir()
    (yh / 'logs').mkdir()
    (yh / 'tool-schema.md').write_text('[]')
    (yh / 'AgentClass.md').write_text(FIXTURE)
    (yh / 'Model.md').write_text('')
    return YAHAEngine(workspace_dir=str(ws)), ws


def test_engine_agents_share_one_controller():
    eng, ws = _make_engine()
    try:
        ctl = eng._stdin_controller()
        root = eng._make_agent(eng.agent_classes['Solo'], None, 0)
        nested = eng._make_agent(eng.agent_classes['Solo'], root.agent_id, 1)
        rt = AgentRuntime(ui=root.ui)
        eng._spawn_parallel_agent(rt, root, root.config, 'Solo', 'new', 'p')
        peer = list(rt.pool.values())[0].agent
        assert root.stdin_interrupt is ctl
        assert nested.stdin_interrupt is ctl
        assert peer.stdin_interrupt is ctl
        assert eng._stdin_controller() is ctl, 'controller must be cached'
    finally:
        shutil.rmtree(ws, ignore_errors=True)
    print('test_engine_agents_share_one_controller OK')


def test_root_run_installs_and_stops_controller():
    eng, ws = _make_engine()
    events = []
    try:
        with patch.object(StdinInterruptController, 'install',
                          lambda self: events.append('install') or True), \
             patch.object(StdinInterruptController, 'stop',
                          lambda self: events.append('stop')), \
             patch.object(AgentRuntime, 'run',
                          lambda self, h=None: events.append('run') or 'done'):
            out = eng.run_agent('Solo', 'do it')
        assert out == 'done', out
        assert events == ['install', 'run', 'stop'], events
    finally:
        shutil.rmtree(ws, ignore_errors=True)
    print('test_root_run_installs_and_stops_controller OK')


def test_nested_run_does_not_install_controller():
    eng, ws = _make_engine()
    events = []
    try:
        with patch.object(StdinInterruptController, 'install',
                          lambda self: events.append('install') or True), \
             patch.object(StdinInterruptController, 'stop',
                          lambda self: events.append('stop')), \
             patch.object(Agent, 'run',
                          lambda self, p, seed_history=None: 'nested-done'):
            out = eng.run_agent('Solo', 'do it', caller_id='root1234')
        assert out == 'nested-done', out
        assert events == [], events
    finally:
        shutil.rmtree(ws, ignore_errors=True)
    print('test_nested_run_does_not_install_controller OK')


class StdinInterruptTests(unittest.TestCase):
    '''Unittest-discoverable wrapper for the regression functions above.'''
    test_install_non_tty_returns_false_and_stays_inert = staticmethod(test_install_non_tty_returns_false_and_stays_inert)
    test_install_starts_listener_and_stop_clears = staticmethod(test_install_starts_listener_and_stop_clears)
    test_install_is_idempotent = staticmethod(test_install_is_idempotent)
    test_suspend_preserves_bytes_written_after_suspend = staticmethod(test_suspend_preserves_bytes_written_after_suspend)
    test_suspend_during_inflight_select_preserves_byte = staticmethod(test_suspend_during_inflight_select_preserves_byte)
    test_resume_restores_esc_interrupt = staticmethod(test_resume_restores_esc_interrupt)
    test_non_esc_bytes_still_discarded_while_active = staticmethod(test_non_esc_bytes_still_discarded_while_active)
    test_pty_cbreak_cooked_transitions = staticmethod(test_pty_cbreak_cooked_transitions)
    test_agent_suspend_resume_delegate_to_controller = staticmethod(test_agent_suspend_resume_delegate_to_controller)
    test_agent_without_controller_suspend_resume_are_noops = staticmethod(test_agent_without_controller_suspend_resume_are_noops)
    test_ask_user_suspends_controller_around_prompt = staticmethod(test_ask_user_suspends_controller_around_prompt)
    test_peer_serves_pending_esc_via_loop_poll = staticmethod(test_peer_serves_pending_esc_via_loop_poll)
    test_peer_serves_pending_esc_via_boundary_poll = staticmethod(test_peer_serves_pending_esc_via_boundary_poll)
    test_pending_esc_not_served_while_serving_or_compressing = staticmethod(test_pending_esc_not_served_while_serving_or_compressing)
    test_maybe_user_interrupt_without_controller_is_none = staticmethod(test_maybe_user_interrupt_without_controller_is_none)
    test_engine_agents_share_one_controller = staticmethod(test_engine_agents_share_one_controller)
    test_root_run_installs_and_stops_controller = staticmethod(test_root_run_installs_and_stops_controller)
    test_nested_run_does_not_install_controller = staticmethod(test_nested_run_does_not_install_controller)


if __name__ == '__main__':
    unittest.main()
