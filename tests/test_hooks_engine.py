#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

"""HookEngine: ordering, bash/python execution, gating, capture, reporting.

The engine is deliberately easy to drive without an agent: it only needs the
handful of attributes a real Agent exposes (config, session, ui, logger,
log_error). These tests use a small fake so the failure routing can be asserted
without an LLM in the loop.
"""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaha.hooks.runner as hook_runner
from yaha.hooks.runner import CAPTURE_LIMIT, HookEngine, HookOutcome, truncate

FENCE = chr(96) * 3


class FakeUI:
    def __init__(self):
        self.hooks = []

    def display_hook(self, agent_name, agent_id, label, output, status="ok"):
        self.hooks.append((label, output, status))


class FakeSession:
    def __init__(self, workspace):
        self.workspace_dir = workspace
        self.folder_name = "sess-1"
        self.log_dir = os.path.join(workspace, ".yaha", "logs", "sess-1")

    def get_error_log_path(self):
        return os.path.join(self.log_dir, "error.log")


class FakeAgent:
    """The minimum an Agent must expose for the hook engine."""

    def __init__(self, workspace, hook_timeout=30.0):
        self.config = SimpleNamespace(name="TestAgent", code="Tst",
                                      model="m", provider="p",
                                      hook_timeout=hook_timeout)
        self.agent_id = "abcd1234"
        self.session = FakeSession(workspace)
        self.ui = FakeUI()
        self.logger = SimpleNamespace(log_path=os.path.join(workspace, "agent.log"))
        self.model_cfg = SimpleNamespace(model="m", provider="p")
        self.history = []
        self.runtime = None
        self.agent_classes = {}
        self._hook_engine_obj = None
        self._current_turn = 1
        self._last_stage_turn = 2
        self._chain_index = 0
        self._chain_len = 3
        self._last_prompt_tokens = 0
        self.context_size = 1000
        self.errors = []

    def log_error(self, category, message, **kwargs):
        self.errors.append((category, message, kwargs))


def bash_hook(code, required=False, capture=False, label=None, index=1):
    return {"kind": "bash", "code": code, "required": required,
            "capture": capture, "index": index,
            "label": label or ("preenter[%d]" % index)}


def python_hook(code, required=False, capture=False, label=None, index=1):
    return {"kind": "python", "code": code, "required": required,
            "capture": capture, "index": index,
            "label": label or ("preenter[%d]" % index)}


def stage(hooks=None, name="Coding"):
    return {"name": name, "hooks": hooks or {}, "allowed_tools": [],
            "required_tools": [], "exit_tools": []}


class _EngineCase(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.mkdtemp(prefix="yaha_engine_")
        self.agent = FakeAgent(self.workspace)
        self.addCleanup(lambda: shutil.rmtree(self.workspace, ignore_errors=True))

    def engine(self):
        eng = HookEngine(self.agent)
        self.addCleanup(eng.close)
        return eng


class BashHooks(_EngineCase, unittest.TestCase):
    def test_successful_bash_hook_captures_output(self):
        eng = self.engine()
        out = eng.run("preenter", stage({"preenter": [
            bash_hook("echo hello")]}))
        self.assertTrue(out.ok)
        self.assertEqual(out.observations, [])
        self.assertEqual(self.agent.errors, [])
        self.assertEqual(self.agent.ui.hooks[0][0], "Coding/preenter[1]")
        self.assertIn("hello", self.agent.ui.hooks[0][1])
        self.assertEqual(self.agent.ui.hooks[0][2], "ok")

    def test_capture_returns_an_observation(self):
        eng = self.engine()
        out = eng.run("preenter", stage({"preenter": [
            bash_hook("echo captured", capture=True)]}))
        self.assertTrue(out.ok)
        self.assertEqual(len(out.observations), 1)
        label, text = out.observations[0]
        self.assertEqual(label, "Coding/preenter[1]")
        self.assertIn("captured", text)

    def test_nonzero_exit_is_a_failure_but_not_gating(self):
        eng = self.engine()
        out = eng.run("preenter", stage({"preenter": [
            bash_hook("exit 3")]}))
        self.assertTrue(out.ok)                 # not required -> only logged
        self.assertEqual(len(out.failures), 1)
        self.assertFalse(out.failures[0]["required"])
        self.assertIn("code 3", out.failures[0]["detail"])
        self.assertEqual(self.agent.errors[0][0], "hook")
        self.assertEqual(self.agent.ui.hooks[0][2], "error")

    def test_required_failure_blocks_and_stops_the_list(self):
        eng = self.engine()
        stage_spec = stage({"preenter": [
            bash_hook("exit 2", required=True, index=1, label="preenter[1]"),
            bash_hook("echo should-not-run", index=2, label="preenter[2]")]})
        out = eng.run("preenter", stage_spec)
        self.assertFalse(out.ok)
        self.assertEqual(out.blocked_label, "Coding/preenter[1]")
        self.assertIn("code 2", out.detail)
        # fail-fast: the second hook never executed
        self.assertEqual(len(self.agent.ui.hooks), 1)

    def test_optional_failure_then_success_runs_both(self):
        eng = self.engine()
        stage_spec = stage({"preenter": [
            bash_hook("exit 1", index=1, label="preenter[1]"),
            bash_hook("echo ran", capture=True, index=2, label="preenter[2]")]})
        out = eng.run("preenter", stage_spec)
        self.assertTrue(out.ok)
        self.assertEqual(len(out.failures), 1)
        self.assertEqual(len(out.observations), 1)
        self.assertEqual(len(self.agent.ui.hooks), 2)

    def test_hook_runs_in_the_workspace_with_env_vars(self):
        eng = self.engine()
        out = eng.run("preenter", stage({"preenter": [
            bash_hook("pwd; echo $YAHA_AGENT $YAHA_STAGE $YAHA_AGENT_ID",
                      capture=True)]}))
        self.assertTrue(out.ok)
        text = out.observations[0][1]
        self.assertIn(os.path.realpath(self.workspace), os.path.realpath(text.split("\n")[0]))
        self.assertIn("TestAgent", text)
        self.assertIn("Coding", text)
        self.assertIn("abcd1234", text)

    def test_tool_context_is_exposed_to_a_pretool_hook(self):
        eng = self.engine()
        tool = {"name": "bash_tool", "args": {"cmdline": "ls"}}
        out = eng.run("pretool", stage({"pretool": [
            bash_hook("echo $YAHA_TOOL", capture=True)]}), tool=tool)
        self.assertTrue(out.ok)
        self.assertIn("bash_tool", out.observations[0][1])

    def test_tool_context_is_exposed_to_a_posttool_hook(self):
        eng = self.engine()
        tool = {"name": "bash_tool", "args": {"cmdline": "ls"}, "ok": False,
                "error": "Error: nope"}
        out = eng.run("posttool", stage({"posttool": [
            bash_hook("echo $YAHA_TOOL; echo $YAHA_TOOL_ARGS",
                      capture=True)]}), tool=tool)
        self.assertTrue(out.ok)
        text = out.observations[0][1]
        self.assertIn("bash_tool", text)
        self.assertIn("cmdline", text)


class PythonHooks(_EngineCase, unittest.TestCase):
    def test_python_hook_runs_and_captures(self):
        eng = self.engine()
        code = "x = 21\nprint(x * 2)\nemit('extra')"
        out = eng.run("preenter", stage({"preenter": [
            python_hook(code, capture=True)]}))
        self.assertTrue(out.ok, out.detail)
        text = out.observations[0][1]
        self.assertIn("42", text)
        self.assertIn("extra", text)

    def test_python_hook_sees_the_live_stage_via_the_dom(self):
        eng = self.engine()
        out = eng.run("preenter", stage({"preenter": [
            python_hook("print(yaha.stage.name)", capture=True)]}))
        self.assertTrue(out.ok)
        self.assertIn("Coding", out.observations[0][1])

    def test_python_failure_is_reported_with_the_traceback(self):
        eng = self.engine()
        out = eng.run("preenter", stage({"preenter": [
            python_hook("raise KeyError('nope')", required=True)]}))
        self.assertFalse(out.ok)
        self.assertIn("KeyError", out.detail)
        self.assertIn("KeyError", out.failures[0]["detail"])

    # -- yaha.tool inside python hooks -------------------------------------
    # Regression suite for the review BLOCKER: the python execution path used
    # to hard-code `tool=None`, so the feature's own documented posttool
    # example raised `AttributeError: 'NoneType' object has no attribute 'ok'`.
    # Only a *bash* hook was covered before, which is why this slipped through.

    TOOL_IN_FLIGHT = {"name": "bash_tool", "args": {"cmdline": "ls"},
                      "ok": None, "error": None, "duration_ms": None,
                      "stage_attempt": 2}
    TOOL_SUCCEEDED = {"name": "bash_tool", "args": {"cmdline": "ls"},
                      "ok": True, "error": None, "duration_ms": 7,
                      "stage_attempt": 2}
    TOOL_FAILED = {"name": "bash_tool", "args": {"cmdline": "ls"},
                   "ok": False, "error": "Error: nope", "duration_ms": 5,
                   "stage_attempt": 2}

    def test_python_pretool_sees_the_tool_name_and_args(self):
        eng = self.engine()
        out = eng.run("pretool", stage({"pretool": [python_hook(
            "print(yaha.tool.name)\nprint(yaha.tool.args.cmdline)",
            capture=True)]}), tool=self.TOOL_IN_FLIGHT)
        self.assertTrue(out.ok, out.detail)
        text = out.observations[0][1]
        self.assertIn("bash_tool", text)
        self.assertIn("ls", text)

    def test_python_pretool_outcome_fields_are_none(self):
        # The call has not happened yet, so these three are necessarily None.
        eng = self.engine()
        out = eng.run("pretool", stage({"pretool": [python_hook(
            "emit('ok=%r err=%r ms=%r' % (yaha.tool.ok, yaha.tool.error, "
            "yaha.tool.duration_ms))", capture=True)]}),
            tool=self.TOOL_IN_FLIGHT)
        self.assertTrue(out.ok, out.detail)
        text = out.observations[0][1]
        self.assertIn("ok=None", text)
        self.assertIn("err=None", text)
        self.assertIn("ms=None", text)

    def test_python_posttool_sees_a_successful_call(self):
        eng = self.engine()
        out = eng.run("posttool", stage({"posttool": [python_hook(
            "emit('ok=%r err=%r ms=%d' % (yaha.tool.ok, yaha.tool.error, "
            "yaha.tool.duration_ms))", capture=True)]}),
            tool=self.TOOL_SUCCEEDED)
        self.assertTrue(out.ok, out.detail)
        text = out.observations[0][1]
        self.assertIn("ok=True", text)
        self.assertIn("err=None", text)
        self.assertIn("ms=7", text)

    def test_python_posttool_flagship_failure_example_runs(self):
        # The EXACT block printed in yaha/docs/hooks.md and in the
        # AgentClass.md comment block. Before the fix this raised
        # AttributeError instead of reporting the failure.
        eng = self.engine()
        code = ("if not yaha.tool.ok:\n"
                "    emit(\"tool failed: \" + str(yaha.tool.error))")
        out = eng.run("posttool", stage({"posttool": [
            python_hook(code, capture=True)]}), tool=self.TOOL_FAILED)
        self.assertTrue(out.ok, out.detail)
        self.assertEqual(len(out.failures), 0)
        self.assertIn("tool failed: Error: nope", out.observations[0][1])

    def test_python_posttool_shape_matches_the_bash_path(self):
        # Both execution backends must see the same tool context.
        eng = self.engine()
        python_out = eng.run("posttool", stage({"posttool": [python_hook(
            "emit(str(yaha.tool.name))", capture=True)]}),
            tool=self.TOOL_SUCCEEDED)
        bash_out = eng.run("posttool", stage({"posttool": [
            bash_hook("echo $YAHA_TOOL", capture=True)]}),
            tool=self.TOOL_SUCCEEDED)
        self.assertTrue(python_out.ok, python_out.detail)
        self.assertTrue(bash_out.ok, bash_out.detail)
        self.assertIn("bash_tool", python_out.observations[0][1])
        self.assertIn("bash_tool", bash_out.observations[0][1])

    def test_python_preenter_and_preexit_have_no_tool_context(self):
        # The other side of the contract: yaha.tool stays None outside
        # pretool/posttool even though the runner now always forwards it.
        eng = self.engine()
        for kind in ("preenter", "preexit"):
            out = eng.run(kind, stage({kind: [
                python_hook("emit('tool=%r' % (yaha.tool,))", capture=True)]}))
            self.assertTrue(out.ok, out.detail)
            self.assertIn("tool=None", out.observations[0][1])


class OrderingAndReporting(_EngineCase, unittest.TestCase):
    def test_hooks_run_in_declaration_order(self):
        eng = self.engine()
        stage_spec = stage({"preenter": [
            bash_hook("echo first", capture=True, index=1, label="preenter[1]"),
            bash_hook("echo second", capture=True, index=2, label="preenter[2]"),
            bash_hook("echo third", capture=True, index=3, label="preenter[3]")]})
        out = eng.run("preenter", stage_spec)
        self.assertEqual([label for label, _t in out.observations],
                         ["Coding/preenter[1]", "Coding/preenter[2]",
                          "Coding/preenter[3]"])
        self.assertEqual([t.strip() for _l, t in out.observations],
                         ["first", "second", "third"])

    def test_no_hooks_is_a_silent_fast_path(self):
        eng = self.engine()
        out = eng.run("preenter", stage({"preenter": []}))
        self.assertTrue(out.ok)
        self.assertEqual(out.observations, [])
        self.assertEqual(self.agent.ui.hooks, [])
        self.assertIsNone(eng._kernel)          # no kernel was started
        self.assertTrue(eng.has_hooks(stage({"pretool": [bash_hook("echo")]}), "pretool"))
        self.assertFalse(eng.has_hooks(stage({}), "pretool"))

    def test_log_error_carries_the_hook_metadata(self):
        eng = self.engine()
        eng.run("preexit", stage({"preexit": [
            bash_hook("exit 7", required=True, label="preexit[1]")]}))
        category, message, kwargs = self.agent.errors[0]
        self.assertEqual(category, "hook")
        self.assertEqual(kwargs["stage"], "Coding")
        self.assertEqual(kwargs["attempt"], 1)
        self.assertEqual(kwargs["extra"]["hook"], "Coding/preexit[1]")
        self.assertEqual(kwargs["extra"]["kind"], "bash")
        self.assertTrue(kwargs["extra"]["required"])

    def test_truncate_caps_capture_output(self):
        long_text = "x" * (CAPTURE_LIMIT + 50)
        out = truncate(long_text)
        self.assertLessEqual(len(out), CAPTURE_LIMIT + len("\n[...truncated]"))
        self.assertTrue(out.endswith("[...truncated]"))
        self.assertEqual(truncate("short"), "short")


class HookTimeoutResolution(_EngineCase, unittest.TestCase):
    """F1: `YAHA_HOOK_TIMEOUT` must really override the class value.

    The class value is always resolved to a float during inheritance, so the
    env branch was unreachable while the docs advertised the override. These
    tests pin the documented precedence: env > `Hook Timeout` > 300 s.
    """

    def setUp(self):
        super().setUp()
        self._saved = os.environ.pop("YAHA_HOOK_TIMEOUT", None)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        if self._saved is None:
            os.environ.pop("YAHA_HOOK_TIMEOUT", None)
        else:
            os.environ["YAHA_HOOK_TIMEOUT"] = self._saved

    def engine_with_class_timeout(self, timeout):
        self.agent.config.hook_timeout = timeout
        return self.engine()

    def test_class_timeout_is_used_when_the_env_var_is_absent(self):
        eng = self.engine_with_class_timeout(42.0)
        self.assertEqual(eng._timeout(), 42.0)

    def test_env_var_overrides_the_resolved_class_timeout(self):
        os.environ["YAHA_HOOK_TIMEOUT"] = "5"
        eng = self.engine_with_class_timeout(300.0)
        self.assertEqual(eng._timeout(), 5.0)

    def test_env_var_reaches_the_kernel_constructor(self):
        os.environ["YAHA_HOOK_TIMEOUT"] = "7"
        eng = self.engine_with_class_timeout(300.0)
        self.assertEqual(eng.kernel().timeout, 7.0)

    def test_env_var_zero_disables_the_timeout(self):
        os.environ["YAHA_HOOK_TIMEOUT"] = "0"
        eng = self.engine_with_class_timeout(300.0)
        self.assertEqual(eng._timeout(), 0.0)

    def test_invalid_env_var_falls_back_to_the_class_timeout(self):
        for raw in ("abc", "", "   "):
            os.environ["YAHA_HOOK_TIMEOUT"] = raw
            eng = self.engine_with_class_timeout(42.0)
            self.assertEqual(eng._timeout(), 42.0, repr(raw))

    def test_bash_hook_passes_the_env_timeout_to_subprocess(self):
        os.environ["YAHA_HOOK_TIMEOUT"] = "5"
        eng = self.engine_with_class_timeout(300.0)
        seen = {}

        def fake_run(*args, **kwargs):
            seen.update(kwargs)
            return SimpleNamespace(stdout="ok", stderr="", returncode=0)

        with mock.patch.object(hook_runner.subprocess, "run", fake_run):
            outcome = eng.run("preenter", stage({"preenter": [bash_hook("echo hi")]}))
        self.assertTrue(outcome.ok)
        self.assertEqual(seen.get("timeout"), 5.0)


class OutcomeObject(unittest.TestCase):
    def test_outcome_defaults_are_ok(self):
        out = HookOutcome()
        self.assertTrue(out.ok)
        self.assertEqual(out.observations, [])
        self.assertEqual(out.observation_text(), "")

    def test_outcome_joins_observations(self):
        out = HookOutcome(observations=[("a", "one"), ("b", "two")])
        self.assertEqual(out.observation_text(), "one\ntwo")


if __name__ == "__main__":
    unittest.main(verbosity=2)
