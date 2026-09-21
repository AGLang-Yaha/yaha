#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

"""End-to-end stage hooks through the real Agent, engine and tool plumbing.

A scripted provider and probe AgentClasses drive the hook points. Hooks append
to one ordered file in the workspace and the provider appends to the SAME file,
so one sequence proves the ordering:

    preenter -> prompt/LLM -> pretool -> tool -> posttool -> ... -> preexit

`ask_user` is excluded from pretool/posttool only; because a real interactive
prompt drags in the whole interrupt/venue machinery (irrelevant here), the
exclusion is pinned by driving the tool wrapper directly instead of prompting.
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaha
from yaha.core.engine import YAHAEngine

HOME = Path(__file__).resolve().parent.parent
TMP = HOME / "tmp"

LT = chr(60)
GT = chr(62)
TOOL_OPEN = LT + "tool_code" + GT
TOOL_CLOSE = LT + "/tool_code" + GT

# A class with NO Toolset block inherits an EMPTY toolset, which makes the agent
# take the "no tools -> one-pass final answer" shortcut and return the first
# response verbatim. The probe classes below therefore declare their toolset.
TOOLSET = (
    "\n### Toolset (Available Tools)\n"
    "    - list_files\n"
    "    - ask_user\n"
)

BASE_CLASS = (
    "## BaseAgent\n"
    "- **Inherited:** None\n"
    "- **Code:** Baa\n"
    "- **Description:** foundation\n"
    "- **Model:** DefaultModel\n"
    "- **Preamble:**\n"
    "    - **Inherited:** None\n"
    "    - **Final Answer:** True\n"
    "- **Aftermath:**\n"
    "    - **Inherited:** None\n"
)

_HOOKS = (
    "    - **preenter:**\n"
    "        - bash (capture): echo preenter >> order.txt; echo PREENTER-OUTPUT\n"
    "    - **pretool:**\n"
    "        - bash: echo pretool-$YAHA_TOOL >> order.txt\n"
    "    - **posttool:**\n"
    "        - bash: echo posttool-$YAHA_TOOL >> order.txt\n"
    "    - **preexit:**\n"
    "        - bash: echo preexit >> order.txt\n"
)

PROBE_CLASS = (
    "\n## HookProbe\n"
    "- **Inherited:** BaseAgent\n"
    "- **Code:** Hkp\n"
    "- **Description:** hook probe\n"
    "- **Model:** DefaultModel\n"
    "- **Preamble:**\n"
    "    - **Inherited:** None\n"
    "    - **Allowed Tools:**\n"
    "        - list_files\n"
    "        - ask_user\n"
    "        - success_tool\n"
    "    - **Exit Tools:**\n"
    "        - success_tool\n"
    + _HOOKS +
    "    > PROBE STAGE PROMPT\n"
    "\n"
    "- **Aftermath:**\n"
    "    - **Inherited:** None\n"
    + TOOLSET
)

PLAIN_CLASS = PROBE_CLASS.replace(_HOOKS, "").replace(
    "## HookProbe", "## PlainProbe").replace("- **Code:** Hkp", "- **Code:** Plp")

GATE_CLASS = (
    "\n## GateProbe\n"
    "- **Inherited:** BaseAgent\n"
    "- **Code:** Gtp\n"
    "- **Description:** gating probe\n"
    "- **Model:** DefaultModel\n"
    "- **Preamble:**\n"
    "    - **Inherited:** None\n"
    "    - **Allowed Tools:**\n"
    "        - list_files\n"
    "        - success_tool\n"
    "    - **Exit Tools:**\n"
    "        - success_tool\n"
    "    - **pretool:**\n"
    "        - bash (required): test \"$YAHA_TOOL\" != \"list_files\"\n"
    "    - **posttool:**\n"
    "        - bash: echo posttool-$YAHA_TOOL >> order.txt\n"
    "    > GATE STAGE PROMPT\n"
    "\n"
    "- **Aftermath:**\n"
    "    - **Inherited:** None\n"
    + TOOLSET
)


class _NonTTY:
    def isatty(self):
        return False

    def fileno(self):
        raise OSError("no fileno")


def _tool(name, args):
    return TOOL_OPEN + json.dumps({"tool": name, "args": args}) + TOOL_CLOSE


class _OrderedProvider:
    """Stage-aware provider that also appends to the shared order file."""

    usage = {"prompt_tokens": 1, "completion_tokens": 1}
    workspace = None
    calls = 0

    def __init__(self, cfg=None):
        self.turn = {}

    def generate(self, messages, **kwargs):
        marker = "?"
        for known in ("PROBE STAGE PROMPT", "GATE STAGE PROMPT"):
            if known in str(messages[-1].get("content", "")):
                marker = known
                break
        index = self.turn.get(marker, 0) + 1
        self.turn[marker] = index
        # A single, process-wide call counter so the order file reads as one
        # timeline even though the marker is only visible on the first call of
        # each stage (later calls see the TOOL RESULT as the last message).
        type(self).calls += 1
        with Path(self.workspace, "order.txt").open("a") as handle:
            handle.write("llm-%d\n" % type(self).calls)
        if marker == "GATE STAGE PROMPT" and index == 1:
            return _tool("list_files", {"path": "."}), dict(self.usage), None
        if marker == "PROBE STAGE PROMPT" and index == 1:
            return _tool("list_files", {"path": "."}), dict(self.usage), None
        return _tool("success_tool", {"summary": "done"}), dict(self.usage), None


class _WiringCase(unittest.TestCase):
    def _workspace(self, agent_md):
        TMP.mkdir(parents=True, exist_ok=True)
        ws = Path(tempfile.mkdtemp(prefix="yaha_hooks_", dir=str(TMP)))
        yh = ws / ".yaha"
        yh.mkdir(parents=True)
        (yh / "memory").mkdir()
        (yh / "ext").mkdir()
        (yh / "logs").mkdir()
        (yh / "tool-schema.md").write_text("[]")
        (yh / "AgentClass.md").write_text(agent_md, encoding="utf-8")
        (yh / "Model.md").write_text("")
        return ws

    def _run(self, ws, agent_class):
        _OrderedProvider.workspace = str(ws)
        _OrderedProvider.calls = 0
        with patch("yaha.llm.ollama.OllamaProvider", _OrderedProvider):
            with patch("yaha.core.agent.Agent.ask_user",
                       lambda self, question: "ok"):
                with patch("sys.stdin", _NonTTY()):
                    engine = YAHAEngine(workspace_dir=str(ws))
                    result = engine.run_agent(agent_class, "probe task")
        order_file = ws / "order.txt"
        order = order_file.read_text().split() if order_file.exists() else []
        return engine, result, order

    def _transcript(self, engine, agent_class):
        log_dir = Path(engine.session.log_dir)
        if not log_dir.exists():
            return ""
        files = sorted(log_dir.glob("%s-*.md" % agent_class))
        return "\n".join(path.read_text(encoding="utf-8", errors="replace")
                         for path in files)


class OrderingTests(_WiringCase, unittest.TestCase):
    def test_full_ordering_of_all_four_hook_points(self):
        ws = self._workspace(BASE_CLASS + PROBE_CLASS)
        try:
            _engine, result, order = self._run(ws, "HookProbe")
            self.assertIsInstance(result, str)
            self.assertFalse(result.startswith("Error"), result)
            self.assertEqual(order, [
                "preenter",
                "llm-1",
                "pretool-list_files",
                "posttool-list_files",
                "llm-2",
                "pretool-success_tool",
                "posttool-success_tool",
                "preexit",
            ], "unexpected hook order: %r" % (order,))
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_capture_output_is_injected_as_an_attributed_observation(self):
        ws = self._workspace(BASE_CLASS + PROBE_CLASS)
        try:
            engine, _result, _order = self._run(ws, "HookProbe")
            transcript = self._transcript(engine, "HookProbe")
            self.assertIn("[HOOK Preamble/preenter[1]]", transcript)
            self.assertIn("PREENTER-OUTPUT", transcript)
            # injected BEFORE the stage prompt, so the model reads it first
            self.assertLess(transcript.index("PREENTER-OUTPUT"),
                            transcript.index("PROBE STAGE PROMPT"))
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_non_capture_hook_output_stays_out_of_the_conversation(self):
        ws = self._workspace(BASE_CLASS + PROBE_CLASS)
        try:
            engine, _result, _order = self._run(ws, "HookProbe")
            transcript = self._transcript(engine, "HookProbe")
            self.assertNotIn("[HOOK Preamble/pretool[1]]", transcript)
            self.assertNotIn("[HOOK Preamble/preexit[1]]", transcript)
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_hook_free_class_is_completely_inert(self):
        ws = self._workspace(BASE_CLASS + PLAIN_CLASS)
        try:
            engine, result, order = self._run(ws, "PlainProbe")
            self.assertFalse(result.startswith("Error"), result)
            self.assertEqual(order, ["llm-1", "llm-2"])
            transcript = self._transcript(engine, "PlainProbe")
            self.assertNotIn("[HOOK ", transcript)
        finally:
            shutil.rmtree(ws, ignore_errors=True)


class RequiredGatingTests(_WiringCase, unittest.TestCase):
    def test_required_pretool_aborts_the_call_before_it_runs(self):
        ws = self._workspace(BASE_CLASS + GATE_CLASS)
        try:
            engine, result, order = self._run(ws, "GateProbe")
            self.assertIsInstance(result, str)
            self.assertFalse(result.startswith("Error"), result)
            # list_files was attempted and blocked; success_tool was allowed
            self.assertIn("posttool-success_tool", order)
            self.assertNotIn("posttool-list_files", order)
            transcript = self._transcript(engine, "GateProbe")
            self.assertIn("was NOT executed", transcript)
        finally:
            shutil.rmtree(ws, ignore_errors=True)


POSTTOOL_EMIT_OUTCOME = {
    "kind": "python",
    "code": "emit('ok=%s err=%s' % (yaha.tool.ok, yaha.tool.error))",
    "required": False,
    "capture": True,
    "index": 1,
    "label": "posttool[1]",
}


class ToolOutcomeObservationTests(_WiringCase, unittest.TestCase):
    """F5: `yaha.tool.ok` follows the documented tool-result convention.

    The heuristic used to be a case-sensitive `startswith("Error")`, so a tool
    reporting `ERROR: ...` was described to a posttool hook as successful. The
    observation is asserted through the real `_execute_tool_with_hooks` path and
    a real python hook, because that is where the value is produced and consumed.
    """

    def _agent_and_stage(self, ws, tool_result):
        with patch("sys.stdin", _NonTTY()):
            engine = YAHAEngine(workspace_dir=str(ws))
            config = engine._resolve_agent_class("HookProbe")
            agent = engine._make_agent(config, None, 0)
        agent.tool_map["list_files"] = lambda path=".": tool_result
        stage = {"name": "Coding", "hooks": {"posttool": [POSTTOOL_EMIT_OUTCOME]},
                 "allowed_tools": [], "required_tools": [], "exit_tools": []}
        return agent, stage

    def _observed(self, ws, tool_result):
        agent, stage = self._agent_and_stage(ws, tool_result)
        try:
            result, failure, ran = agent._execute_tool_with_hooks(
                stage, "list_files", {"path": "."}, 1, 1, "aftermath")
            self.assertIsNone(failure)
            self.assertTrue(ran)
            return result, "\n".join(
                str(m.get("content", "")) for m in agent.history)
        finally:
            agent._shutdown_hooks()

    def test_posttool_ok_is_false_for_an_uppercase_error_result(self):
        ws = self._workspace(BASE_CLASS + PROBE_CLASS)
        try:
            result, observed = self._observed(ws, "ERROR: nope")
            self.assertEqual(result, "ERROR: nope")
            self.assertIn("ok=False", observed)
            self.assertIn("ERROR: nope", observed)
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_posttool_ok_is_true_for_a_normal_result(self):
        ws = self._workspace(BASE_CLASS + PROBE_CLASS)
        try:
            _result, observed = self._observed(ws, "fine")
            self.assertIn("ok=True", observed)
            self.assertIn("err=None", observed)
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_posttool_ok_ignores_surrounding_whitespace(self):
        ws = self._workspace(BASE_CLASS + PROBE_CLASS)
        try:
            _result, observed = self._observed(ws, "  Error: nope")
            self.assertIn("ok=False", observed)
        finally:
            shutil.rmtree(ws, ignore_errors=True)


def _routing_class(code, fail_target=None, recovery=True):
    """A 3-stage probe whose Preamble `preenter` always fails."""
    lines = [
        "\n## %s\n" % code,
        "- **Inherited:** BaseAgent\n",
        "- **Code:** %s\n" % code[:3].title(),
        "- **Description:** preenter routing probe\n",
        "- **Model:** DefaultModel\n",
        "- **Preamble:**\n",
        "    - **Inherited:** None\n",
    ]
    if fail_target:
        lines.append("    - **Fail Target:** %s\n" % fail_target)
    lines += [
        "    - **preenter:**\n",
        "        - bash (required): exit 1\n",
        "    > PREENTER ROUTE PROMPT\n",
    ]
    if recovery:
        lines += [
            "- **Recovery:**\n",
            "    - **Inherited:** None\n",
            "    > RECOVERY PROMPT\n",
        ]
    lines += ["- **Aftermath:**\n", "    - **Inherited:** None\n"]
    return "".join(lines)


class PreenterFailureRoutingTests(_WiringCase, unittest.TestCase):
    """F8: a failing required `preenter` routes through the SHARED lookup.

    The preenter path used to inline its own `fail_target` default and chain
    lookup while the tool path used another one; these tests pin that both
    resolve the same target (and that the documented bare-string `iter_run`
    contract for the unrecoverable case is unchanged).
    """

    def _drive(self, ws, agent_class, stages=2):
        with patch("sys.stdin", _NonTTY()):
            engine = YAHAEngine(workspace_dir=str(ws))
            agent = engine._make_agent(
                engine._resolve_agent_class(agent_class), None, 0)
        agent.runtime = None
        gen = agent.iter_run("probe task")
        tokens = []
        try:
            for _ in range(stages):
                tokens.append(next(gen))
            ended = None
        except StopIteration as stop:
            ended = stop.value
        finally:
            gen.close()
            agent._shutdown_hooks()
        return agent, tokens, ended

    def test_preenter_failure_routes_to_the_explicit_fail_target(self):
        ws = self._workspace(BASE_CLASS + _routing_class(
            "RouteProbe", fail_target="Recovery"))
        try:
            _agent, tokens, _ended = self._drive(ws, "RouteProbe")
            self.assertEqual([t[1] for t in tokens], ["Preamble", "Recovery"])
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_preenter_failure_without_a_fail_target_routes_to_aftermath(self):
        ws = self._workspace(BASE_CLASS + _routing_class("DefaultProbe"))
        try:
            _agent, tokens, _ended = self._drive(ws, "DefaultProbe")
            self.assertEqual([t[1] for t in tokens], ["Preamble", "Aftermath"])
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_preenter_failure_message_uses_the_shared_wording(self):
        ws = self._workspace(BASE_CLASS + _routing_class(
            "WordingProbe", fail_target="Recovery"))
        try:
            agent, _tokens, _ended = self._drive(ws, "WordingProbe")
            injected = [str(m.get("content", "")) for m in agent.history]
            matching = [m for m in injected
                        if m.startswith("Error: required hook")]
            self.assertTrue(matching, injected)
            self.assertIn("Preamble/preenter[1]", matching[0])
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_unrecoverable_fail_target_ends_the_chain_with_a_bare_string(self):
        ws = self._workspace(BASE_CLASS + _routing_class(
            "AbortProbe", fail_target="Preamble", recovery=False))
        try:
            _agent, tokens, ended = self._drive(ws, "AbortProbe", stages=3)
            self.assertEqual([t[1] for t in tokens], ["Preamble"])
            self.assertIsInstance(ended, str)
            self.assertIn("nothing to route to", ended)
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_routing_helpers_resolve_the_same_target(self):
        chain = [{"name": "Preamble"}, {"name": "Recovery"},
                 {"name": "Aftermath"}]
        from yaha.core.agent import Agent
        self.assertEqual(Agent._chain_target_index(chain, "recovery", 2), 1)
        self.assertEqual(Agent._chain_target_index(chain, "missing", 2), 2)
        self.assertEqual(Agent._stage_failure_target({}), "aftermath")
        self.assertEqual(
            Agent._stage_failure_target({"fail_target": "Recovery"}),
            "Recovery")


class _RecordingRuntime:
    """Minimal runtime stand-in that appends the event signal to order.txt.

    Writing the signal to the SAME file the hooks append to turns the whole hook
    sequence and the event signal into one timeline, which is exactly what the
    ordering guarantee is about: `pretool -> tool -> posttool -> preexit ->
    exit action`, with the event signalled only at the exit action.
    """

    def __init__(self, workspace):
        self.workspace = workspace
        self.blocked = []

    def _append(self, line):
        with Path(self.workspace, "order.txt").open("a") as handle:
            handle.write(line + "\n")

    def set_event(self, name, modifier=None):
        # `modifier` is the optional prompt prefix the producer hands to every
        # consumer of the pulse. This double only records WHICH event was
        # signalled, so the prefix is accepted and ignored.
        self._append("EVENT:%s" % name)

    def record_blocked_signal(self, agent_name, hook_label, detail, event=None):
        self.blocked.append({"agent": agent_name, "hook": hook_label,
                             "detail": detail, "event": event})
        self._append("BLOCKED:%s" % (event,))


def _bash(code, required=False, label="hook[1]"):
    return {"kind": "bash", "code": code, "required": required,
            "capture": False, "index": 1, "label": label}


PRETOOL_ECHO = _bash("echo pretool >> order.txt", label="pretool[1]")
POSTTOOL_ECHO = _bash("echo posttool >> order.txt", label="posttool[1]")
PREEXIT_ECHO = _bash("echo preexit >> order.txt", label="preexit[1]")
POSTTOOL_FAILS = _bash("echo posttool >> order.txt; exit 1", required=True,
                       label="posttool[1]")
PREEXIT_FAILS = _bash("echo preexit >> order.txt; exit 1", required=True,
                      label="preexit[1]")


class EventSignalOrderingTests(_WiringCase, unittest.TestCase):
    """An event terminator signals its event at exactly ONE point.

    Regression suite for the review MINOR: the signal used to be deferred only
    when the stage happened to declare a `preexit` hook, so a `posttool` hook
    observed the event as already set in hook-free-preexit stages and unset in
    others. The signal is now always deferred to the exit action.
    """

    def _agent_and_stage(self, ws, hooks):
        with patch("sys.stdin", _NonTTY()):
            engine = YAHAEngine(workspace_dir=str(ws))
            config = engine._resolve_agent_class("HookProbe")
            agent = engine._make_agent(config, None, 0)
        agent.tool_map["list_files"] = lambda path=".": "[]"
        runtime = _RecordingRuntime(str(ws))
        agent.runtime = runtime
        stage = {"name": "Coding", "hooks": hooks, "allowed_tools": [],
                 "required_tools": [], "exit_tools": []}
        return agent, stage, runtime

    @staticmethod
    def _order(ws):
        path = Path(ws, "order.txt")
        return path.read_text().split() if path.exists() else []

    def test_event_is_signalled_after_preexit_when_a_preexit_hook_exists(self):
        ws = self._workspace(BASE_CLASS + PROBE_CLASS)
        try:
            agent, stage, _rt = self._agent_and_stage(ws, {
                "pretool": [PRETOOL_ECHO],
                "posttool": [POSTTOOL_ECHO],
                "preexit": [PREEXIT_ECHO],
            })
            agent._execute_tool_with_hooks(
                stage, "set_event_success", {"name": "go"}, 1, 1, "aftermath",
                exits_stage=True)
            self.assertEqual(self._order(ws),
                             ["pretool", "posttool", "preexit", "EVENT:go"])
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_event_is_signalled_after_posttool_without_a_preexit_hook(self):
        # THE case the reviewer flagged: no preexit hook must not change when a
        # posttool hook can observe the event.
        ws = self._workspace(BASE_CLASS + PROBE_CLASS)
        try:
            agent, stage, _rt = self._agent_and_stage(ws, {
                "pretool": [PRETOOL_ECHO],
                "posttool": [POSTTOOL_ECHO],
            })
            agent._execute_tool_with_hooks(
                stage, "set_event_success", {"name": "go"}, 1, 1, "aftermath",
                exits_stage=True)
            self.assertEqual(self._order(ws), ["pretool", "posttool", "EVENT:go"])
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_set_event_loop_is_deferred_the_same_way(self):
        ws = self._workspace(BASE_CLASS + PROBE_CLASS)
        try:
            agent, stage, _rt = self._agent_and_stage(ws, {
                "posttool": [POSTTOOL_ECHO],
            })
            result, failure, _ran = agent._execute_tool_with_hooks(
                stage, "set_event_loop", {"name": "go"}, 1, 1, "aftermath",
                exits_stage=True)
            self.assertIsNone(failure)
            self.assertEqual(result, "LOOP_SIGNALED")
            self.assertEqual(self._order(ws), ["posttool", "EVENT:go"])
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_a_failing_required_posttool_abandons_the_pending_signal(self):
        ws = self._workspace(BASE_CLASS + PROBE_CLASS)
        try:
            agent, stage, runtime = self._agent_and_stage(ws, {
                "posttool": [POSTTOOL_FAILS],
            })
            _result, failure, _ran = agent._execute_tool_with_hooks(
                stage, "set_event_success", {"name": "go"}, 1, 1, "aftermath",
                exits_stage=True)
            self.assertIsNotNone(failure, "a required posttool failure must fail")
            self.assertEqual(runtime.blocked[0]["event"], "go")
            self.assertEqual(runtime.blocked[0]["hook"], "Coding/posttool[1]")
            self.assertNotIn("EVENT:go", self._order(ws))
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_a_failing_required_preexit_still_abandons_the_signal(self):
        ws = self._workspace(BASE_CLASS + PROBE_CLASS)
        try:
            agent, stage, runtime = self._agent_and_stage(ws, {
                "preexit": [PREEXIT_FAILS],
            })
            _result, failure, _ran = agent._execute_tool_with_hooks(
                stage, "set_event_success", {"name": "go"}, 1, 1, "aftermath",
                exits_stage=True)
            self.assertIsNotNone(failure)
            self.assertEqual(runtime.blocked[0]["event"], "go")
            self.assertEqual(runtime.blocked[0]["hook"], "Coding/preexit[1]")
            self.assertNotIn("EVENT:go", self._order(ws))
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_a_refused_terminator_never_signals(self):
        # An empty event name is refused, so there is nothing to defer and
        # nothing to signal - and the stage must not complete.
        ws = self._workspace(BASE_CLASS + PROBE_CLASS)
        try:
            agent, stage, _rt = self._agent_and_stage(ws, {
                "posttool": [POSTTOOL_ECHO],
                "preexit": [PREEXIT_ECHO],
            })
            result, failure, _ran = agent._execute_tool_with_hooks(
                stage, "set_event_success", {"name": ""}, 1, 1, "aftermath",
                exits_stage=True)
            self.assertIsNone(failure)
            self.assertTrue(str(result).startswith("Error"), result)
            self.assertNotIn("EVENT:", " ".join(self._order(ws)))
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_a_hook_free_class_signals_without_any_hook_traffic(self):
        # The `engine is None` fast path is untouched: no hooks declared means
        # the real terminator runs and nothing is deferred.
        ws = self._workspace(BASE_CLASS + PLAIN_CLASS)
        try:
            with patch("sys.stdin", _NonTTY()):
                engine = YAHAEngine(workspace_dir=str(ws))
                config = engine._resolve_agent_class("PlainProbe")
                agent = engine._make_agent(config, None, 0)
            runtime = _RecordingRuntime(str(ws))
            agent.runtime = runtime
            stage = agent.config.prompt_chain[0]
            agent._execute_tool_with_hooks(
                stage, "set_event_success", {"name": "go"}, 1, 1, "aftermath",
                exits_stage=True)
            self.assertEqual(self._order(ws), ["EVENT:go"])
        finally:
            shutil.rmtree(ws, ignore_errors=True)


class AskUserExclusionTests(_WiringCase, unittest.TestCase):
    """`ask_user` must not fire pretool/posttool, but keeps its preexit gate."""

    def _agent_and_stage(self, ws):
        with patch("sys.stdin", _NonTTY()):
            engine = YAHAEngine(workspace_dir=str(ws))
            config = engine._resolve_agent_class("HookProbe")
            agent = engine._make_agent(config, None, 0)
        agent.ask_user = lambda question: "ok"
        agent.tool_map["ask_user"] = agent.ask_user
        agent.tool_map["list_files"] = lambda path=".": "[]"
        stage = agent.config.prompt_chain[0]
        return agent, stage

    def test_ask_user_runs_no_hooks(self):
        ws = self._workspace(BASE_CLASS + PROBE_CLASS)
        try:
            agent, stage = self._agent_and_stage(ws)
            result, failure, ran = agent._execute_tool_with_hooks(
                stage, "ask_user", {"question": "q"}, 1, 1, "aftermath",
                exits_stage=False)
            self.assertEqual(result, "ok")
            self.assertIsNone(failure)
            self.assertTrue(ran)
            order_file = ws / "order.txt"
            self.assertFalse(order_file.exists(),
                             "ask_user must not fire pretool/posttool hooks")
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_a_normal_tool_does_run_hooks(self):
        ws = self._workspace(BASE_CLASS + PROBE_CLASS)
        try:
            agent, stage = self._agent_and_stage(ws)
            agent._execute_tool_with_hooks(
                stage, "list_files", {"path": "."}, 1, 1, "aftermath",
                exits_stage=False)
            order = (ws / "order.txt").read_text().split()
            self.assertEqual(order, ["pretool-list_files", "posttool-list_files"])
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_ask_user_listed_as_an_exit_tool_still_runs_preexit(self):
        # The docs used to call `ask_user` "the ONE exclusion". Only
        # pretool/posttool are skipped; a class that lists `ask_user` as an Exit
        # Tool must still pass the stage-level preexit gate, otherwise choosing
        # an exit tool would be a way to bypass it.
        ws = self._workspace(BASE_CLASS + PROBE_CLASS)
        try:
            agent, stage = self._agent_and_stage(ws)
            agent._execute_tool_with_hooks(
                stage, "ask_user", {"question": "q"}, 1, 1, "aftermath",
                exits_stage=True)
            order = (ws / "order.txt").read_text().split()
            self.assertEqual(order, ["preexit"],
                             "ask_user must run preexit only: %r" % (order,))
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_preexit_runs_on_the_exit_action(self):
        ws = self._workspace(BASE_CLASS + PROBE_CLASS)
        try:
            agent, stage = self._agent_and_stage(ws)
            agent._execute_tool_with_hooks(
                stage, "success_tool", {"summary": "done"}, 1, 1, "aftermath",
                exits_stage=True)
            order = (ws / "order.txt").read_text().split()
            self.assertEqual(order, ["pretool-success_tool", "posttool-success_tool",
                                     "preexit"])
        finally:
            shutil.rmtree(ws, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
