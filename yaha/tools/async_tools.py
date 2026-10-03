# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

"""The asynchronous shell-tool family: ``async_bash`` and ``poll_async``.

``async_bash`` launches a command in the background and returns a waitable job
id immediately; ``poll_async`` reads that job's status without blocking, or
-- with a positive ``timeout`` -- parks the caller until it finishes. The
finished result is ALSO attached to the agent's next message by the
opportunistic drain in ``Agent._drain_async_results``.
"""

import subprocess

from ..core.async_jobs import DONE, ERROR, JOB_TIMEOUT_S, PENDING, RUNNING
from .system_tools import format_shell_output


def _run_async_command(job, max_output_bytes=None):
    """Worker body: run one already-sandboxed command and format its output.

    Uses ``Popen`` rather than ``subprocess.run`` so the live handle can be
    stored on the job and killed by ``AsyncJobRegistry.shutdown`` when a run is
    aborted. Returns ``(status, output, error)``; never raises.
    """
    try:
        proc = subprocess.Popen(
            job.cmdline,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except Exception as exc:
        return ERROR, "", "failed to start command: %s" % exc
    job.proc = proc
    try:
        out, err = proc.communicate(timeout=JOB_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
            out, err = proc.communicate(timeout=5)
        except Exception:
            out, err = "", ""
        return (ERROR, format_shell_output(out, err, max_output_bytes),
                "timed out after %d seconds" % JOB_TIMEOUT_S)
    text = format_shell_output(out, err, max_output_bytes)
    if proc.returncode not in (0, None):
        text = text + "\n(exit code %s)" % proc.returncode
    return DONE, text, None


def async_bash_tool(cmdline, registry, access_controller=None,
                    agent_id="Unknown", ui=None, max_output_bytes=None,
                    owner=None):
    """Launch ``cmdline`` in the background; return a waitable job id.

    Authorization and sandboxing happen HERE, on the CALLING thread, so the
    interactive security prompt stays synchronous and a denial returns
    immediately (exactly like ``bash_tool``). Only an authorized command reaches
    the background worker, and the returned id is a valid event name a stage can
    wait on. ``owner`` (the calling agent id) is recorded so the finished
    result is drained back to THAT agent only.
    """
    if registry is None:
        return "Error: async_bash requires a job registry."
    # Keep the command AS THE AGENT WROTE IT. The sandbox wrapper computed
    # below is what actually RUNS, but the result the agent eventually reads
    # (possibly after history compression during a long run) must name the
    # command the agent asked for.
    display_cmdline = cmdline
    if access_controller:
        authorized, msg = access_controller.authorize_bash(agent_id, cmdline, ui=ui)
        if not authorized:
            return msg
        cmdline = access_controller.get_sandbox_command(cmdline)

    def runner(job):
        return _run_async_command(job, max_output_bytes)

    job = registry.start(cmdline, runner=runner, owner=owner,
                        display_cmdline=display_cmdline)
    return ("ASYNC JOB STARTED: " + job.job_id + "\n"
            + "- Runs in the background; the id is a WAITABLE event name, so"
            + " a stage can declare it as its Wait Event (or bind a"
            + " @placeholder to it).\n"
            + "- Check it any time with poll_async; the finished result is"
            + " also attached automatically to your next message.")


def _coerce_wait_seconds(timeout):
    """Normalize a poll_async timeout into (seconds, error).

    None means do not wait (the historical behaviour) and yields
    (None, None). A number, or a numeric string, is clamped to
    [0, JOB_TIMEOUT_S]: zero or negative means do not wait, and the cap
    matches the longest a single async job can run. A non-numeric value is a
    loud error, never a silent zero.
    """
    if timeout is None:
        return None, None
    try:
        seconds = float(timeout)
    except (TypeError, ValueError):
        return None, ("Error: poll_async timeout must be a number of seconds, "
                      "got " + repr(timeout) + ".")
    if seconds != seconds:  # NaN is not a usable wait.
        return None, ("Error: poll_async timeout must be a number of seconds, "
                      "got " + repr(timeout) + ".")
    if seconds <= 0:
        return None, None
    return min(seconds, float(JOB_TIMEOUT_S)), None


def poll_async_tool(job_id, registry, timeout=None):
    """Status/result read for one async job; optionally park until it ends.

    With no ``timeout`` (or a non-positive one) this is the historical
    non-blocking snapshot. With a positive ``timeout`` the caller is parked for
    up to that many seconds (capped at ``JOB_TIMEOUT_S``) and released the
    moment the job reaches a terminal state; if the wait elapses first the
    reply says so and the job KEEPS RUNNING - a timeout never cancels it.

    Every answer echoes the command the job was started with. The result may
    arrive long after the ``async_bash`` call that launched it, and history
    compression can drop that call in the meantime; naming the command here
    keeps the result self-describing.
    """
    if registry is None:
        return "Error: poll_async requires a job registry."
    seconds, error = _coerce_wait_seconds(timeout)
    if error:
        return error
    if seconds:
        snapshot = registry.wait(job_id, timeout=seconds)
    else:
        snapshot = registry.poll(job_id)
    if snapshot is None:
        return "Error: no async job with id " + repr(str(job_id)) + "."
    status = snapshot["status"]
    elapsed = snapshot["elapsed"]
    command = snapshot.get("display_cmdline") or snapshot.get("cmdline") or ""
    header = "ASYNC JOB %s: %s (%.1fs)" % (snapshot["job_id"], status, elapsed)
    if status in (PENDING, RUNNING):
        if seconds:
            return (header + "\nCOMMAND: " + command
                    + "\n(wait timed out after %.1fs; the job is still running "
                    "and was NOT cancelled)" % seconds)
        return header + "\nCOMMAND: " + command
    body = snapshot["output"] or "(no output)"
    if snapshot.get("error"):
        body = body + "\nERROR: " + snapshot["error"]
    return header + "\nCOMMAND: " + command + "\n" + body
