# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

"""Background shell jobs for asynchronous tool calling.

Pure stdlib - no UI, no LLM, no scheduler imports - so the whole module is
unit-testable in isolation, exactly like ``yaha/core/endpoints.py``.

A job runs one shell command on a daemon worker thread and records its result.
The registry hands out short, stable job ids (``async-1``, ``async-2``, ...)
that double as WAITABLE runtime EVENT names: when a job finishes, the registry
invokes its ``wake`` callback with the job id, and the runtime turns that into
``set_event(job_id)`` so any agent parked on the id is released. The callback is
invoked from the WORKER thread, so it must be thread-safe and must never touch
scheduler state directly - see ``AgentRuntime._notify_job``, the mirror of the
endpoint wake mailbox.
"""

import itertools
import re
import threading
import time

PENDING = "pending"
RUNNING = "running"
DONE = "done"
ERROR = "error"
CANCELLED = "cancelled"

# One command may run at most this long before the worker kills it (the same
# 300s cap the synchronous `bash_tool` applies).
JOB_TIMEOUT_S = 300
# How long the scheduler may keep waiting on a parked job id before it declares
# a deadlock anyway. Generous on purpose: a job always answers within
# JOB_TIMEOUT_S, so this only fires when the machinery itself is wedged - and a
# wedged run must end LOUDLY rather than hang forever.
JOB_IDLE_TIMEOUT_S = 600.0
JOB_ID_PREFIX = "async-"
_JOB_ID_RE = re.compile(r"^async-\d+$")


def is_async_job(name):
    """True when ``name`` is an async job event id (``async-<n>``).

    Used by the Wait Event validator exactly like ``is_ep``: an async job
    PRODUCES its own event when it finishes, so a wait on it has no
    ``set_event_*`` prompt literal and must not be reported as missing.
    """
    return bool(_JOB_ID_RE.match(str(name or "").strip()))


class AsyncJob:
    """One background command and its eventual result.

    ``status`` walks PENDING -> RUNNING -> DONE/ERROR/CANCELLED. ``proc`` holds
    the live ``Popen`` (set by the runner) so ``shutdown`` can kill a child that
    would otherwise outlive an aborted run.

    ``cmdline`` is what the worker RUNS; it may be a sandbox wrapper. The
    separate ``display_cmdline`` is the same command AS THE CALLER WROTE IT,
    and it is what the result the agent eventually reads echoes back - so a
    result that outlives history compression still names its command.
    """

    __slots__ = ("job_id", "cmdline", "display_cmdline", "owner", "status",
                 "output", "error", "started_at", "finished_at", "delivered",
                 "proc")

    def __init__(self, job_id, cmdline, owner=None, display_cmdline=None):
        self.job_id = job_id
        self.cmdline = cmdline
        self.display_cmdline = (display_cmdline if display_cmdline is not None
                                else cmdline)
        self.owner = owner
        self.status = PENDING
        self.output = ""
        self.error = None
        self.started_at = None
        self.finished_at = None
        self.delivered = False
        self.proc = None

    def finish(self, status, output="", error=None):
        """Record the terminal state (called once, by the worker thread).

        A CANCELLED job is terminal: ``shutdown`` may kill a child while its
        worker is still unwinding, so the worker's late ``finish`` must not
        resurrect the job as DONE.

        PUBLICATION ORDER: the payload (``output`` / ``error`` /
        ``finished_at``) is written BEFORE the terminal ``status``, because
        ``status`` is the visibility marker every reader polls (``is_live``,
        ``drain_finished``, ``snapshot``). Publishing it last guarantees a
        reader that observes a terminal status also observes the matching
        payload - the reverse order lets a watcher see DONE/ERROR with a stale
        (empty) result.
        """
        if self.status == CANCELLED:
            return self
        self.output = output or ""
        self.error = error
        self.finished_at = time.monotonic()
        # Terminal status LAST - see PUBLICATION ORDER above.
        self.status = status
        return self

    def elapsed(self):
        if self.started_at is None:
            return 0.0
        end = self.finished_at if self.finished_at is not None else time.monotonic()
        return max(0.0, end - self.started_at)

    def snapshot(self):
        return {
            "job_id": self.job_id,
            "cmdline": self.cmdline,
            "display_cmdline": self.display_cmdline,
            "status": self.status,
            "output": self.output,
            "error": self.error,
            "elapsed": self.elapsed(),
            "delivered": self.delivered,
        }

    def __repr__(self):
        return "<AsyncJob %s %s>" % (self.job_id, self.status)


class AsyncJobRegistry:
    """Run-scoped registry of background jobs.

    ``wake`` (optional) is called with the job id from the WORKER thread the
    instant a job reaches a terminal state, so a scheduler can release agents
    parked on that id. It MUST be thread-safe and MUST NOT touch scheduler state
    directly. ``runner`` (optional) is the default worker body; ``start`` may
    override it per call.
    """

    def __init__(self, wake=None, runner=None):
        self._jobs = {}
        self._order = []
        self._counter = itertools.count(1)
        self._lock = threading.Lock()
        self._wake = wake
        self._runner = runner

    def allocate(self, cmdline, owner=None, display_cmdline=None):
        """Reserve a fresh job id and return the (not yet started) job.

        ``owner`` is the agent id that started the job; it is only used to
        scope the opportunistic drain, never the wait-by-id (any peer may
        park on any live job id).

        ``display_cmdline`` (see ``AsyncJob``) is the caller-facing form of the
        command, used when the result is shown back to the agent; it defaults
        to ``cmdline``, the command that actually runs.
        """
        with self._lock:
            job_id = "%s%d" % (JOB_ID_PREFIX, next(self._counter))
            job = AsyncJob(job_id, cmdline, owner=owner,
                           display_cmdline=display_cmdline)
            self._jobs[job_id] = job
            self._order.append(job_id)
            return job

    def start(self, cmdline, runner=None, owner=None, display_cmdline=None):
        """Allocate a job and launch it on a daemon worker thread.

        The status is flipped to RUNNING BEFORE the thread starts, so a
        scheduler that checks liveness immediately after this returns can never
        observe the job as finished-and-lost.

        ``display_cmdline`` is the command AS THE CALLER WROTE IT, kept for the
        result the agent eventually reads (see ``AsyncJob.display_cmdline``);
        ``cmdline`` remains the command that actually runs, which may be a
        sandbox wrapper.
        """
        job = self.allocate(cmdline, owner=owner,
                            display_cmdline=display_cmdline)
        run = runner or self._runner
        job.status = RUNNING
        job.started_at = time.monotonic()
        thread = threading.Thread(
            target=self._worker,
            args=(job, run),
            name="yaha-async-%s" % job.job_id,
            daemon=True,
        )
        thread.start()
        return job

    def _worker(self, job, run):
        """Run one job and publish its result; never lets an error escape."""
        try:
            if run is None:
                raise RuntimeError("no runner configured for async job")
            result = run(job)
            if isinstance(result, tuple):
                parts = list(result) + [None, None, None]
                status, output, error = parts[0], parts[1], parts[2]
            else:
                status, output, error = DONE, str(result or ""), None
        except Exception as exc:  # a worker must never die silently
            status, output, error = ERROR, "", str(exc)
        job.finish(status, output, error)
        wake = self._wake
        if wake is not None:
            try:
                wake(job.job_id)
            except Exception:
                pass

    def get(self, job_id):
        with self._lock:
            return self._jobs.get(str(job_id or "").strip())

    def poll(self, job_id):
        """Snapshot of one job, or None when the id is unknown."""
        job = self.get(job_id)
        return job.snapshot() if job is not None else None

    def is_live(self, job_id):
        """True while the named job is still running (the deadlock predicate)."""
        job = self.get(job_id)
        return job is not None and job.status == RUNNING

    def drain_finished(self, owner=None):
        """Return finished, not-yet-delivered jobs once, in start order.

        When ``owner`` is given, ONLY that agent's jobs are returned, so a
        run-global registry can never leak one agent's command output into
        another agent's conversation. ``owner=None`` keeps the old
        (unscoped) behaviour for callers that want everything.
        """
        with self._lock:
            out = []
            for job_id in self._order:
                job = self._jobs.get(job_id)
                if job is None or job.delivered:
                    continue
                if owner is not None and job.owner != owner:
                    continue
                if job.status in (DONE, ERROR):
                    job.delivered = True
                    out.append(job)
            return out

    def shutdown(self):
        """Best effort: kill live children and mark live jobs cancelled."""
        with self._lock:
            jobs = [self._jobs[j] for j in self._order if j in self._jobs]
        cancelled = 0
        for job in jobs:
            if job.status != RUNNING:
                continue
            proc = job.proc
            if proc is not None:
                try:
                    proc.kill()
                except Exception:
                    pass
            job.finish(CANCELLED, job.output, "cancelled at shutdown")
            cancelled += 1
        return cancelled
