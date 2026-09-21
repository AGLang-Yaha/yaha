#!/usr/bin/env python3
"""Parent side of the persistent python kernel used by stage hooks.

One kernel per agent, spawned lazily on the first python hook, kept for the
agent's whole life (every stage, and across ``loop_tool`` re-entries),
terminated when the agent dies. Nothing is shared between agents.

Robustness rules
----------------
* Per-block timeout (default 300 s, ``0`` disables). On expiry the kernel is
  killed, restarted empty, and the block counts as failed.
* Unexpected death (native crash, ``os._exit``, OOM) is detected via the reader
  thread's EOF: the next python hook lazily starts a fresh, EMPTY kernel and the
  namespace reset is reported explicitly. A hook can therefore never leave a
  half-dead child behind, and variables are never silently carried over.
* Failures never raise out of :meth:`run`; a broken hook must not be able to
  break the run other than by failing the hook.
"""

import collections
import json
import os
import queue
import subprocess
import sys
import threading

RECORD_SEPARATOR = "\x1e"

# How long to wait on the response queue before re-checking whether the child
# is still alive. Keeps a mid-block death cheap to detect.
_POLL_SLICE = 0.2


class KernelResult:
    """Outcome of one python hook block."""

    __slots__ = ("ok", "stdout", "stderr", "emitted", "exc_type", "exc_value",
                 "traceback", "note", "resets", "restarted")

    def __init__(self, ok=True, stdout="", stderr="", emitted=None,
                 exc_type=None, exc_value=None, traceback=None, note=None,
                 resets=0, restarted=False):
        self.ok = ok
        self.stdout = stdout
        self.stderr = stderr
        self.emitted = list(emitted or [])
        self.exc_type = exc_type
        self.exc_value = exc_value
        self.traceback = traceback
        self.note = note
        self.resets = resets
        self.restarted = restarted

    @property
    def output(self):
        parts = [self.stdout, self.stderr, "\n".join(self.emitted)]
        return "".join(part for part in parts if part)


class PythonKernel:
    """A lazily started, restartable persistent python subprocess."""

    def __init__(self, workspace=None, timeout=300.0, python=None):
        self.workspace = workspace or os.getcwd()
        self.timeout = float(timeout or 0)
        self.python = python or sys.executable
        self.resets = 0
        self.last_error = None
        self._proc = None
        self._queue = None
        self._counter = 0
        self._stderr_tail = collections.deque(maxlen=100)
        self._lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------

    @property
    def pid(self):
        proc = self._proc
        return proc.pid if proc is not None else None

    @property
    def running(self):
        proc = self._proc
        return bool(proc is not None and proc.poll() is None)

    def view(self):
        """Read-only description for the ``yaha`` DOM."""
        return {
            "pid": self.pid,
            "running": self.running,
            "resets": self.resets,
            "last_error": self.last_error,
        }

    def _spawn(self):
        pkg_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = dict(os.environ)
        env["PYTHONPATH"] = os.path.dirname(pkg_dir) + os.pathsep + env.get("PYTHONPATH", "")
        env["YAHA_HOOK_KERNEL"] = "1"
        proc = subprocess.Popen(
            [self.python, "-u", "-m", "yaha.hooks.kernel_main"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.workspace,
            env=env,
            text=True,
            bufsize=1,
        )
        responses = queue.Queue()
        self._proc = proc
        self._queue = responses
        threading.Thread(
            target=self._pump_stdout, args=(proc, responses), daemon=True
        ).start()
        threading.Thread(target=self._pump_stderr, args=(proc,), daemon=True).start()
        return proc

    def _pump_stdout(self, proc, responses):
        try:
            for line in proc.stdout:
                if not line:
                    break
                if not line.startswith(RECORD_SEPARATOR):
                    self._stderr_tail.append("stray kernel stdout: %s" % line.rstrip())
                    continue
                try:
                    payload = json.loads(line[len(RECORD_SEPARATOR):])
                except Exception:
                    self._stderr_tail.append("unparsable kernel record: %s" % line.rstrip())
                    continue
                responses.put(payload)
        except Exception:
            pass

    def _pump_stderr(self, proc):
        try:
            for line in proc.stderr:
                if not line:
                    break
                self._stderr_tail.append(line.rstrip())
        except Exception:
            pass

    def _kill(self):
        proc = self._proc
        self._proc = None
        self._queue = None
        if proc is None:
            return
        try:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
        except Exception:
            pass
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass

    def _restart(self, reason):
        self._kill()
        self.resets += 1
        self.last_error = reason
        return self.resets

    def close(self):
        self._kill()

    def _tail(self):
        return " | ".join(list(self._stderr_tail)[-5:])

    # -- execution ---------------------------------------------------------

    def run(self, code, snapshot=None):
        """Execute one block; never raises."""
        with self._lock:
            for attempt in (0, 1):
                if self._proc is None:
                    try:
                        self._spawn()
                    except Exception as exc:
                        self.last_error = "kernel failed to start: %s" % exc
                        return KernelResult(
                            ok=False, note=self.last_error,
                            resets=self.resets, restarted=bool(attempt),
                        )
                result = self._attempt(code, snapshot)
                if result is not None:
                    return result
                # The kernel died between or during the request: restart once.
                self._restart("kernel died while executing a hook block")
            return KernelResult(
                ok=False,
                note="kernel died twice; hook not executed%s"
                     % (" [%s]" % self._tail() if self._tail() else ""),
                resets=self.resets, restarted=True,
            )

    def _attempt(self, code, snapshot):
        """Send one request and wait for its response.

        Returns a :class:`KernelResult`, or ``None`` when the kernel died and
        the caller should restart and retry once.
        """
        proc = self._proc
        responses = self._queue
        self._counter += 1
        request_id = self._counter
        payload = json.dumps({
            "id": request_id,
            "code": code,
            "snapshot": snapshot or {},
        }) + "\n"
        try:
            proc.stdin.write(payload)
            proc.stdin.flush()
        except Exception:
            return None

        if self.timeout > 0:
            deadline = _now() + self.timeout
        else:
            deadline = None
        while True:
            remaining = None if deadline is None else deadline - _now()
            if remaining is not None and remaining <= 0:
                self._restart(
                    "hook block timed out after %gs" % self.timeout
                )
                return KernelResult(
                    ok=False,
                    note="hook block timed out after %gs; the kernel was "
                         "killed and restarted with an EMPTY namespace"
                         % self.timeout,
                    resets=self.resets, restarted=True,
                )
            # Poll in short slices: a kernel that dies mid-block must be
            # detected as a death (and restarted), NOT waited out to the full
            # per-block timeout as if it were merely slow.
            slice_timeout = _POLL_SLICE
            if remaining is not None:
                slice_timeout = min(_POLL_SLICE, remaining)
            try:
                response = responses.get(timeout=slice_timeout)
            except queue.Empty:
                if proc.poll() is not None:
                    return None
                continue
            if response.get("id") != request_id:
                # A stale response from a previous generation: ignore it.
                continue
            return KernelResult(
                ok=bool(response.get("ok")),
                stdout=response.get("stdout") or "",
                stderr=response.get("stderr") or "",
                emitted=response.get("emitted") or [],
                exc_type=response.get("exc_type"),
                exc_value=response.get("exc_value"),
                traceback=response.get("traceback"),
                resets=self.resets,
            )


def _now():
    import time
    return time.monotonic()
