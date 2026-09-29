# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

"""The asynchronous shell-tool family: ``async_bash`` and ``poll_async``.

``async_bash`` launches a command in the background and returns a waitable job
id immediately; ``poll_async`` reads that job's status without blocking. The
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
    if access_controller:
        authorized, msg = access_controller.authorize_bash(agent_id, cmdline, ui=ui)
        if not authorized:
            return msg
        cmdline = access_controller.get_sandbox_command(cmdline)

    def runner(job):
        return _run_async_command(job, max_output_bytes)

    job = registry.start(cmdline, runner=runner, owner=owner)
    return ("ASYNC JOB STARTED: " + job.job_id + "\n"
            + "- Runs in the background; the id is a WAITABLE event name, so"
            + " a stage can declare it as its Wait Event (or bind a"
            + " @placeholder to it).\n"
            + "- Check it any time with poll_async; the finished result is"
            + " also attached automatically to your next message.")


def poll_async_tool(job_id, registry):
    """Non-blocking status/result read for one async job."""
    if registry is None:
        return "Error: poll_async requires a job registry."
    snapshot = registry.poll(job_id)
    if snapshot is None:
        return "Error: no async job with id " + repr(str(job_id)) + "."
    status = snapshot["status"]
    elapsed = snapshot["elapsed"]
    if status in (PENDING, RUNNING):
        return "ASYNC JOB %s: %s (%.1fs)" % (snapshot["job_id"], status, elapsed)
    body = snapshot["output"] or "(no output)"
    if snapshot.get("error"):
        body = body + "\nERROR: " + snapshot["error"]
    return "ASYNC JOB %s: %s (%.1fs)\n%s" % (
        snapshot["job_id"], status, elapsed, body)