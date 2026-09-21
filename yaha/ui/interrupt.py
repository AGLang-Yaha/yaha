#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

"""Esc-to-user-interrupt machinery for the YAHA TUI.

Two pieces:

- ``EscInterruptListener``: a daemon thread watching a TTY stream for a lone
  ASCII ESC byte (escape sequences started by ESC are skipped).
- ``StdinInterruptController``: owns stdin's cbreak mode and the listener for
  a whole run. Exactly one controller exists per engine; it is installed when
  the root run starts and stopped when the run ends, and it is SHARED by every
  agent (root, parallel peers, nested sub-agents). Agents never own stdin:
  they suspend the controller around prompts and poll it for pending Esc, so
  whichever agent is active when Esc lands serves the interrupt venue.

Limitations / notes
-------------------
Outside a prompt, ordinary user type-ahead bytes are consumed and deliberately
not replayed in this implementation: users should type only when a prompt is
visible, otherwise the bytes may disappear.

suspend() is a hard hand-off, not a hint: it sets the suspended flag and then
waits out any in-flight select/read cycle on the I/O lock, so once suspend()
returns the listener provably does not read again until resume(). The reader
also re-checks the flag after every select() wakes, which closes the race
where a keystroke landing while a prompt was taking over stdin got swallowed.
"""

import os
import select
import sys
import threading
import time


class EscInterruptListener:
    """Watch a TTY stream for a lone ESC byte."""

    def __init__(self, callback=None, stream=None):
        self.callback = callback
        self._stream = stream or sys.stdin
        self._stop = threading.Event()
        self._suspended = threading.Event()
        self._pending = threading.Event()
        # Serializes select/read cycles against suspend(): holding it around
        # the whole cycle lets suspend() wait until no read is in flight.
        self._io_lock = threading.Lock()
        self._thread = None
        self.fileno = None
        try:
            if hasattr(self._stream, "fileno"):
                self.fileno = self._stream.fileno()
        except (OSError, ValueError):
            self.fileno = None

    # --- lifecycle --------------------------------------------------------
    def start(self):
        if self.fileno is None or self._thread is not None:
            return
        self._suspended.clear()
        self._stop.clear()
        thread = threading.Thread(target=self._run, daemon=True, name="yaha-esc-listener")
        self._thread = thread
        thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=0.5)
            self._thread = None

    # --- cooperative stdin ownership -------------------------------------
    def suspend(self):
        self._suspended.set()
        # Wait out any in-flight select/read cycle: after suspend() returns,
        # the listener is guaranteed not to consume bytes until resume(), so
        # a foreground prompt gets exclusive stdin ownership.
        with self._io_lock:
            pass

    def resume(self):
        self._suspended.clear()

    # --- pending state ----------------------------------------------------
    def is_pending(self):
        return self._pending.is_set()

    def clear_pending(self):
        self._pending.clear()

    def _trigger(self):
        self._pending.set()
        if self.callback is not None:
            try:
                self.callback()
            except Exception:
                pass

    # --- internal reader --------------------------------------------------
    def _run(self):
        fd = self.fileno
        if fd is None:
            return
        while not self._stop.is_set():
            if self._suspended.is_set():
                time.sleep(0.05)
                continue
            # Hold the I/O lock for the whole select+read cycle: suspend()
            # sets its flag first and then blocks here, so a byte that wakes
            # select() while a prompt is taking over is left for the prompt.
            with self._io_lock:
                if self._suspended.is_set():
                    continue
                try:
                    ready, _, _ = select.select([fd], [], [], 0.1)
                except (OSError, ValueError):
                    return
                if not ready:
                    continue
                if self._suspended.is_set():
                    # A prompt claimed stdin while we were selecting; do not
                    # swallow the keystroke that belongs to it.
                    continue
                try:
                    byte = os.read(fd, 1)
                except (OSError, ValueError):
                    return
                if byte not in (b"\x1b",):
                    # Ignore normal typed data while there is no foreground
                    # prompt. A real prompt owns stdin via suspend(), so this
                    # should not normally contend with ask_user.
                    continue
                # A potentially lone ESC: wait a short window to see whether a
                # terminal escape sequence (arrow key, option+key) follows.
                try:
                    ready2, _, _ = select.select([fd], [], [], 0.005)
                except (OSError, ValueError):
                    ready2 = []
                if ready2:
                    # consume enough of the continuation to avoid polluting a
                    # later prompt, then skip (not an interrupt).
                    try:
                        junk = os.read(fd, 16)
                    except (OSError, ValueError):
                        junk = b""
                    if not junk:
                        self._trigger()
                    continue
                self._trigger()

class StdinInterruptController:
    """Owns stdin's cbreak mode and the Esc listener for a whole run.

    One controller per engine, shared by every agent it creates (root,
    parallel peers, nested sub-agents). The engine installs it when a root
    run starts and stops it when the run ends; agents only consume it:

    - around every prompt they call ``suspend()`` / ``resume()`` so the live
      cbreak reader cannot steal keystrokes;
    - at interrupt poll points they check ``is_pending()`` and serve the
      venue, whichever agent happens to be on the CPU.

    All methods are safe no-ops when the controller was never installed
    (non-TTY / unattended runs), so agents need no special-casing.
    """

    def __init__(self, stream=None):
        # The stream is bound at install() time (default: sys.stdin THEN), so
        # tests can patch sys.stdin before installing.
        self._stream = stream
        self._bound = None
        self.listener = None
        self._term_orig = None
        self._cbreak = False

    # --- diagnostics ------------------------------------------------------
    @property
    def active(self):
        return self.listener is not None

    @property
    def cbreak_now(self):
        return self._cbreak

    # --- lifecycle --------------------------------------------------------
    def install(self):
        """Put stdin in cbreak and start the Esc listener; return active state.

        Idempotent: an already-installed controller is a no-op. Returns False
        when stdin is not a TTY (unattended / redirected runs stay
        listenerless). The listener is installed even when the termios switch
        fails: suspending the reader thread alone already stops keystroke
        stealing.
        """
        if self.listener is not None:
            return True
        stream = self._stream or sys.stdin
        try:
            if not stream.isatty():
                return False
        except Exception:
            return False
        self._bound = stream
        try:
            import termios
            import tty
            self._term_orig = termios.tcgetattr(stream)
            tty.setcbreak(stream.fileno())
            self._cbreak = True
        except Exception:
            self._term_orig = None
            self._cbreak = False
        self.listener = EscInterruptListener(stream=stream)
        self.listener.resume()
        self.listener.start()
        return True

    def stop(self):
        if self.listener is not None:
            self.listener.stop()
            self.listener = None
        if self._term_orig is not None:
            try:
                import termios
                termios.tcsetattr(self._bound, termios.TCSANOW, self._term_orig)
            except Exception:
                pass
        self._term_orig = None
        self._cbreak = False

    # --- cooperative stdin ownership -------------------------------------
    def suspend(self):
        """Hand stdin to a foreground prompt: stop reading, restore cooked."""
        if self.listener is not None:
            self.listener.suspend()
        if self._term_orig is not None and self._cbreak:
            try:
                import termios
                termios.tcsetattr(self._bound, termios.TCSANOW, self._term_orig)
            except Exception:
                pass
            self._cbreak = False

    def resume(self):
        """Take stdin back after a prompt: restore cbreak, resume reading."""
        if self.listener is not None:
            self.listener.resume()
        if self._term_orig is not None and not self._cbreak:
            try:
                import termios
                import tty
                tty.setcbreak(self._bound.fileno())
            except Exception:
                pass
            self._cbreak = True

    # --- pending state ----------------------------------------------------
    def is_pending(self):
        return self.listener is not None and self.listener.is_pending()

    def clear_pending(self):
        if self.listener is not None:
            self.listener.clear_pending()
