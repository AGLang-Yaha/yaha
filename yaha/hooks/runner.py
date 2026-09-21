#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

"""Hook execution engine.

One :class:`HookEngine` per agent. It runs the four hook kinds declared on a
stage, in declaration order, and reports the outcome to the caller, which owns
the failure routing (stage entry blocked, tool aborted, stage failed, exit
action blocked) and the context injection.

Fail-fast rule: the list stops at the first failing hook whose ``required``
modifier is set. Running later hooks against an already-invalid stage would be
misleading, and gating is the whole point of ``(required)``. A non-required
failure is recorded and the list continues, so telemetry can never be lethal.
"""

import os
import subprocess
import time

from .dom import build_snapshot
from .kernel import PythonKernel

CAPTURE_LIMIT = 4096
TRUNCATION_TAIL = "\n[...truncated]"


def truncate(text, limit=CAPTURE_LIMIT):
    if text is None:
        return ""
    text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + TRUNCATION_TAIL


class HookOutcome:
    """Result of running one hook kind for one stage."""

    __slots__ = ("ok", "failures", "observations", "blocked_label", "detail")

    def __init__(self, ok=True, failures=None, observations=None,
                 blocked_label=None, detail=None):
        self.ok = ok
        self.failures = list(failures or [])
        self.observations = list(observations or [])
        self.blocked_label = blocked_label
        self.detail = detail

    def observation_text(self):
        return "\n".join(text for _label, text in self.observations)


class HookEngine:
    """Runs the hook blocks of a stage for one agent."""

    def __init__(self, agent):
        self.agent = agent
        self._kernel = None

    # -- kernel ------------------------------------------------------------

    def _timeout(self):
        """Per-block timeout in seconds (``0`` disables it).

        Precedence: ``YAHA_HOOK_TIMEOUT`` (process environment) > the class's
        ``- **Hook Timeout:**`` value > the 300 s default. The environment is
        read FIRST so the documented runtime override really is one: the class
        value is always resolved to a float during inheritance, so a
        config-first order would leave the env branch unreachable.

        An empty or unparsable environment value is ignored (never fatal) and
        the class value applies. ``"0"`` is a real value, not an absent one, and
        keeps its documented meaning: no timeout.
        """
        raw = os.environ.get("YAHA_HOOK_TIMEOUT")
        if raw is not None and str(raw).strip() != "":
            try:
                return float(raw)
            except (TypeError, ValueError):
                pass
        value = getattr(getattr(self.agent, "config", None), "hook_timeout", None)
        try:
            return float(value)
        except (TypeError, ValueError):
            return 300.0

    def kernel(self):
        if self._kernel is None:
            session = getattr(self.agent, "session", None)
            workspace = getattr(session, "workspace_dir", None) or os.getcwd()
            self._kernel = PythonKernel(workspace=workspace, timeout=self._timeout())
        return self._kernel

    def kernel_view(self):
        if self._kernel is None:
            return {"pid": None, "running": False, "resets": 0, "last_error": None}
        return self._kernel.view()

    def close(self):
        if self._kernel is not None:
            self._kernel.close()
            self._kernel = None

    # -- introspection -----------------------------------------------------

    @staticmethod
    def specs(stage, kind):
        return list(((stage or {}).get("hooks") or {}).get(kind) or [])

    def has_hooks(self, stage, kind=None):
        hooks = (stage or {}).get("hooks") or {}
        if kind is not None:
            return bool(hooks.get(kind))
        return any(hooks.get(name) for name in hooks)

    # -- execution ---------------------------------------------------------

    def run(self, kind, stage, tool=None, turn=None):
        """Run every hook of ``kind``; never raises."""
        specs = self.specs(stage, kind)
        if not specs:
            return HookOutcome()

        stage_name = (stage or {}).get("name") or "stage"
        failures = []
        observations = []
        blocked_label = None
        detail = None

        for spec in specs:
            label = "%s/%s" % (stage_name, spec.get("label", kind))
            started = time.time()
            output, failure = self._run_one(kind, spec, stage, tool, label)
            duration_ms = int((time.time() - started) * 1000)
            required = bool(spec.get("required"))

            self._display(label, output, failure is not None)
            if failure is not None:
                self._log_failure(label, spec, failure, stage_name, turn)
                failures.append({"label": label, "detail": failure,
                                 "required": required})
                if required:
                    blocked_label = label
                    detail = failure
                    break
                continue
            if spec.get("capture") and output:
                observations.append((label, output))

        return HookOutcome(
            ok=blocked_label is None,
            failures=failures,
            observations=observations,
            blocked_label=blocked_label,
            detail=detail,
        )

    def _run_one(self, kind, spec, stage, tool, label):
        """Return ``(output_text, failure_text_or_None)``."""
        try:
            if spec.get("kind") == "python":
                return self._run_python(spec, stage, tool, label)
            return self._run_bash(spec, stage, tool, label)
        except Exception as exc:  # pragma: no cover - defensive
            return "", "hook runner error: %s: %s" % (type(exc).__name__, exc)

    def _run_python(self, spec, stage, tool, label):
        snapshot = build_snapshot(self.agent, stage=stage, hook=spec, tool=tool)
        result = self.kernel().run(spec.get("code") or "", snapshot)
        parts = []
        if result.note:
            parts.append("[kernel] %s" % result.note)
        if result.restarted:
            parts.append(
                "[kernel] namespace RESET - variables from earlier hook blocks "
                "are gone"
            )
        parts.append(result.output)
        output = "".join(part for part in parts if part)
        if not result.ok:
            failure = result.exc_value or result.note or "python hook failed"
            detail = "%s: %s" % (result.exc_type or "Error", failure)
            if result.traceback:
                detail += "\n" + result.traceback.strip()
            return output, detail
        return output, None

    def _run_bash(self, spec, stage, tool, label):
        session = getattr(self.agent, "session", None)
        workspace = getattr(session, "workspace_dir", None) or os.getcwd()
        env = dict(os.environ)
        env.update(self._env(stage, tool, workspace))
        timeout = self._timeout()
        try:
            proc = subprocess.run(
                ["/bin/bash", "-c", spec.get("code") or ""],
                cwd=workspace,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout if timeout > 0 else None,
            )
        except subprocess.TimeoutExpired:
            return "", "bash hook timed out after %gs" % timeout
        except Exception as exc:
            return "", "bash hook could not run: %s" % exc

        output = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode != 0:
            return output, "bash hook exited with code %d" % proc.returncode
        return output, None

    def _env(self, stage, tool, workspace):
        agent = self.agent
        session = getattr(agent, "session", None)
        config = getattr(agent, "config", None)
        values = {
            "YAHA_AGENT": getattr(config, "name", "") or "",
            "YAHA_AGENT_CLASS": getattr(config, "code", "") or "",
            "YAHA_AGENT_ID": getattr(agent, "agent_id", "") or "",
            "YAHA_STAGE": (stage or {}).get("name") or "",
            "YAHA_TURN": str(getattr(agent, "_current_turn", "")),
            "YAHA_WORKSPACE": workspace or "",
            "YAHA_SESSION_DIR": getattr(session, "folder_name", "") or "",
        }
        if tool:
            values["YAHA_TOOL"] = str(tool.get("name") or "")
            values["YAHA_TOOL_ARGS"] = repr(tool.get("args"))
        return values

    # -- reporting ---------------------------------------------------------

    def _display(self, label, output, failed):
        ui = getattr(self.agent, "ui", None)
        display = getattr(ui, "display_hook", None)
        if display is None:
            return
        config = getattr(self.agent, "config", None)
        try:
            display(
                getattr(config, "name", "") or "",
                getattr(self.agent, "agent_id", "") or "",
                label,
                output,
                "error" if failed else "ok",
            )
        except Exception:
            pass

    def _log_failure(self, label, spec, failure, stage_name, turn):
        log_error = getattr(self.agent, "log_error", None)
        if log_error is None:
            return
        try:
            log_error(
                "hook",
                failure,
                stage=stage_name,
                turn=turn,
                attempt=spec.get("index"),
                extra={
                    "hook": label,
                    "kind": spec.get("kind"),
                    "required": bool(spec.get("required")),
                },
            )
        except Exception:
            pass
