#!/usr/bin/env python3
"""Stage hooks across the multi-agency runtime.

The property under test is the strict one agreed with the user: a failing
`preexit (required)` hook must BLOCK the event-set half of `set_event_success`, so
the peer really stays parked and the run ends through the deadlock detector -
which must then name the agent, the hook label and the failure instead of only
naming the parked agent.
"""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from yaha.core.engine import YAHAEngine
from yaha.core.runtime import WAITING, AgentRuntime, RuntimeDeadlockError

HOME = Path(__file__).resolve().parent.parent
TMP = HOME / "tmp"

LT = chr(60)
GT = chr(62)
TOOL_OPEN = LT + "tool_code" + GT
TOOL_CLOSE = LT + "/tool_code" + GT
FENCE = chr(96) * 3

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


def _producer(preexit_cmd):
    return (
        "\n## Producer\n"
        "- **Inherited:** BaseAgent\n"
        "- **Code:** Prd\n"
        "- **Description:** signals an event after a preexit gate\n"
        "- **Model:** DefaultModel\n"
        "- **Preamble:**\n"
        "    - **Inherited:** None\n"
        "    - **Exit Tools:**\n"
        "        - set_event_success\n"
        "    - **preexit:**\n"
        "        - bash (required): %s\n" % preexit_cmd +
        "    > PRODUCER STAGE PROMPT\n"
        "\n"
        "- **Aftermath:**\n"
        "    - **Inherited:** None\n"
        "\n### Toolset (Available Tools)\n"
        "    - set_event_success\n"
    )


def _producer_posttool(posttool_cmd):
    return (
        "\n## ProducerPosttool\n"
        "- **Inherited:** BaseAgent\n"
        "- **Code:** Ppt\n"
        "- **Description:** signals an event after a posttool gate\n"
        "- **Model:** DefaultModel\n"
        "- **Preamble:**\n"
        "    - **Inherited:** None\n"
        "    - **Exit Tools:**\n"
        "        - set_event_success\n"
        "    - **posttool:**\n"
        "        - bash (required): %s\n" % posttool_cmd +
        "    > PRODUCER STAGE PROMPT\n"
        "\n"
        "- **Aftermath:**\n"
        "    - **Inherited:** None\n"
        "\n### Toolset (Available Tools)\n"
        "    - set_event_success\n"
    )


CONSUMER = (
    "\n## Consumer\n"
    "- **Inherited:** BaseAgent\n"
    "- **Code:** Cns\n"
    "- **Description:** waits for the producer's event\n"
    "- **Model:** DefaultModel\n"
    "- **Preamble:**\n"
    "    - **Inherited:** None\n"
    "    - **Wait Event:** go\n"
    "    - **Exit Tools:**\n"
    "        - success_tool\n"
    "    > CONSUMER STAGE PROMPT\n"
    "\n"
    "- **Aftermath:**\n"
    "    - **Inherited:** None\n"
    "\n### Toolset (Available Tools)\n"
    "    - success_tool\n"
)


class _NonTTY:
    def isatty(self):
        return False

    def fileno(self):
        raise OSError("no fileno")


def _tool(name, args):
    return TOOL_OPEN + json.dumps({"tool": name, "args": args}) + TOOL_CLOSE


class _PairProvider:
    """Producer signals the event; the consumer acknowledges once woken."""

    usage = {"prompt_tokens": 1, "completion_tokens": 1}

    def __init__(self, cfg=None):
        self.calls = 0

    def generate(self, messages, **kwargs):
        text = str(messages[-1].get("content", ""))
        if "PRODUCER STAGE PROMPT" in text:
            return (_tool("set_event_success", {"name": "go", "summary": "signalled"}),
                    dict(self.usage), None)
        return _tool("success_tool", {"summary": "woken"}), dict(self.usage), None


class _RuntimePairCase(unittest.TestCase):
    def _workspace_for(self, producer_md):
        TMP.mkdir(parents=True, exist_ok=True)
        ws = Path(tempfile.mkdtemp(prefix="yaha_hooks_runtime_", dir=str(TMP)))
        yh = ws / ".yaha"
        yh.mkdir(parents=True)
        (yh / "memory").mkdir()
        (yh / "ext").mkdir()
        (yh / "logs").mkdir()
        (yh / "tool-schema.md").write_text("[]")
        (yh / "AgentClass.md").write_text(
            BASE_CLASS + producer_md + CONSUMER, encoding="utf-8")
        (yh / "Model.md").write_text("")
        return ws

    def _workspace(self, preexit_cmd):
        return self._workspace_for(_producer(preexit_cmd))

    def _run_pair_for(self, producer_md, producer_class):
        ws = self._workspace_for(producer_md)
        try:
            with patch("yaha.llm.ollama.OllamaProvider", _PairProvider):
                with patch("sys.stdin", _NonTTY()):
                    engine = YAHAEngine(workspace_dir=str(ws))
                    producer = engine._make_agent(
                        engine._resolve_agent_class(producer_class), None, 0)
                    consumer = engine._make_agent(
                        engine._resolve_agent_class("Consumer"), None, 0)
                    runtime = AgentRuntime(ui=producer.ui)
                    runtime.spawn(producer, "produce", name="prd", tag="prd")
                    runtime.spawn(consumer, "consume", name="cns", tag="cns")
                    try:
                        runtime.run()
                        return runtime, None
                    except RuntimeDeadlockError as exc:
                        return runtime, exc
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def _run_pair(self, preexit_cmd):
        return self._run_pair_for(_producer(preexit_cmd), "Producer")


class PreexitBlocksTheSignal(_RuntimePairCase, unittest.TestCase):
    def test_failing_required_preexit_blocks_the_event(self):
        runtime, error = self._run_pair("exit 1")
        self.assertIsNotNone(error, "the run must deadlock, not signal the peer")
        self.assertFalse(runtime.event_is_set("go"),
                         "the event must NOT have been signalled")

    def test_deadlock_message_names_the_hook_and_the_event(self):
        _runtime, error = self._run_pair("exit 1")
        message = str(error)
        self.assertIn("deadlock", message.lower())
        self.assertIn("cns", message)                   # the parked agent
        self.assertIn("go", message)                    # the event it waits on
        self.assertIn("Preamble/preexit[1]", message)   # the real culprit
        self.assertIn("code 1", message)                # and why it failed

    def test_blocked_signal_is_recorded_for_diagnostics(self):
        runtime, _error = self._run_pair("exit 1")
        self.assertEqual(len(runtime.blocked_signals), 1)
        record = runtime.blocked_signals[0]
        self.assertEqual(record["event"], "go")
        self.assertIn("preexit", record["hook"])
        self.assertIn("blocked_signals", runtime.snapshot())

    def test_passing_preexit_signals_the_event_and_the_run_completes(self):
        runtime, error = self._run_pair("exit 0")
        self.assertIsNone(error, "a passing gate must let the handshake complete")
        self.assertEqual(runtime.blocked_signals, [])


class PosttoolBlocksTheSignal(_RuntimePairCase, unittest.TestCase):
    """A failing required `posttool` must abandon a pending event signal.

    Regression suite for the review MINOR: a stage that declared no `preexit`
    hook had its event signalled BEFORE the `posttool` hooks ran, so a failing
    posttool gate released the peer anyway - the gate was decorative. With the
    signal deferred to the exit action, the peer stays parked and the run ends
    through the deadlock detector instead.
    """

    def _run_posttool_pair(self, posttool_cmd):
        return self._run_pair_for(_producer_posttool(posttool_cmd),
                                  "ProducerPosttool")

    def test_failing_required_posttool_blocks_the_event(self):
        runtime, error = self._run_posttool_pair("exit 1")
        self.assertIsNotNone(error, "the run must deadlock, not signal the peer")
        self.assertFalse(runtime.event_is_set("go"),
                         "the event must NOT have been signalled")

    def test_deadlock_message_names_the_posttool_hook_and_the_event(self):
        _runtime, error = self._run_posttool_pair("exit 1")
        message = str(error)
        self.assertIn("deadlock", message.lower())
        self.assertIn("cns", message)                   # the parked agent
        self.assertIn("go", message)                    # the event it waits on
        self.assertIn("posttool[1]", message)           # the real culprit
        self.assertIn("code 1", message)                # and why it failed

    def test_blocked_signal_is_recorded_for_the_posttool_path(self):
        runtime, _error = self._run_posttool_pair("exit 1")
        self.assertEqual(len(runtime.blocked_signals), 1)
        record = runtime.blocked_signals[0]
        self.assertEqual(record["event"], "go")
        self.assertIn("posttool", record["hook"])

    def test_passing_posttool_signals_the_event_and_the_run_completes(self):
        runtime, error = self._run_posttool_pair("exit 0")
        self.assertIsNone(error, "a passing gate must let the handshake complete")
        self.assertEqual(runtime.blocked_signals, [])


def _parked_consumer_with_kernel():
    """A consumer that starts a python hook kernel, THEN parks on the event.

    The kernel must belong to the agent that is still parked when the deadlock
    aborts: an agent that dies through the normal path releases its kernel via
    `_step` -> `_kill` anyway, so it proves nothing about the abort path.
    """
    return (
        "\n## ParkedConsumer\n"
        "- **Inherited:** BaseAgent\n"
        "- **Code:** Pkc\n"
        "- **Description:** starts a kernel, then waits for the event\n"
        "- **Model:** DefaultModel\n"
        "- **Preamble:**\n"
        "    - **Inherited:** None\n"
        "    - **Exit Tools:**\n"
        "        - success_tool\n"
        "    - **preenter:**\n"
        "        - python (capture):\n"
        "            " + FENCE + "\n"
        "            import os\n"
        "            path = os.path.join(yaha.workspace.root,\n"
        "                                 'consumer-kernel.pid')\n"
        "            open(path, 'w').write(str(os.getpid()))\n"
        "            " + FENCE + "\n"
        "    > CONSUMER-KERNEL STAGE PROMPT\n"
        "\n"
        "- **WaitStage:**\n"
        "    - **Inherited:** None\n"
        "    - **Wait Event:** go\n"
        "    - **Exit Tools:**\n"
        "        - success_tool\n"
        "    > WAIT STAGE PROMPT\n"
        "\n"
        "- **Aftermath:**\n"
        "    - **Inherited:** None\n"
        "\n### Toolset (Available Tools)\n"
        "    - success_tool\n"
    )


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class DeadlockAbortCleanup(_RuntimePairCase, unittest.TestCase):
    """F2: a deadlock abort must release the pooled agents' hook kernels.

    The comment claimed an abandoned agent releases its kernel, but only the
    normal death path (`_step` -> `_kill`) called `_shutdown_hooks`, so a
    long-lived process such as the ChatAgent loop kept one python kernel
    subprocess per PARKED agent. The parked agent here owns a kernel (a python
    hook ran on an earlier stage of its chain) and writes its own pid, so
    "released" is asserted against the OS rather than against a flag.
    """

    def _pair(self, preexit_cmd):
        TMP.mkdir(parents=True, exist_ok=True)
        ws = Path(tempfile.mkdtemp(prefix="yaha_hooks_abort_", dir=str(TMP)))
        yh = ws / ".yaha"
        yh.mkdir(parents=True)
        (yh / "memory").mkdir()
        (yh / "ext").mkdir()
        (yh / "logs").mkdir()
        (yh / "tool-schema.md").write_text("[]")
        (yh / "AgentClass.md").write_text(
            BASE_CLASS + _producer(preexit_cmd) + _parked_consumer_with_kernel(),
            encoding="utf-8")
        (yh / "Model.md").write_text("")
        with patch("yaha.llm.ollama.OllamaProvider", _PairProvider):
            with patch("sys.stdin", _NonTTY()):
                engine = YAHAEngine(workspace_dir=str(ws))
                producer = engine._make_agent(
                    engine._resolve_agent_class("Producer"), None, 0)
                consumer = engine._make_agent(
                    engine._resolve_agent_class("ParkedConsumer"), None, 0)
                runtime = AgentRuntime(ui=producer.ui)
                runtime.spawn(producer, "produce", name="prd", tag="prd")
                runtime.spawn(consumer, "consume", name="cns", tag="cns")
                try:
                    runtime.run()
                    error = None
                except RuntimeDeadlockError as exc:
                    error = exc
        return ws, runtime, error, producer, consumer

    def test_deadlock_abort_reaps_the_pool(self):
        ws, runtime, error, _producer, _consumer = self._pair("exit 1")
        try:
            self.assertIsNotNone(error, "the run must deadlock")
            self.assertEqual(runtime.pool, {})
            self.assertEqual(runtime.snapshot()["live"], [])
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_deadlock_abort_shuts_down_the_hook_kernels(self):
        ws, runtime, error, producer, consumer = self._pair("exit 1")
        try:
            self.assertIsNotNone(error, "the run must deadlock")
            pid_file = Path(ws, "consumer-kernel.pid")
            self.assertTrue(pid_file.exists(), "the python hook never ran")
            # The kernel belongs to the agent that is STILL parked at the abort
            # (the producer died through the normal path and released its own).
            self.assertIsNone(consumer._hook_engine_obj)
            self.assertIsNone(producer._hook_engine_obj)
            pid = int(pid_file.read_text())
            self.assertFalse(_pid_alive(pid),
                             "kernel subprocess %d survived the abort" % pid)
            self.assertEqual(runtime.pool, {})
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_the_diagnostic_is_built_before_the_pool_is_reaped(self):
        ws, _runtime, error, _producer, _consumer = self._pair("exit 1")
        try:
            message = str(error)
            self.assertIn("deadlock", message.lower())
            self.assertIn("cns", message)
            self.assertIn("go", message)
            self.assertIn("Preamble/preexit[1]", message)
            self.assertIn("code 1", message)
        finally:
            shutil.rmtree(ws, ignore_errors=True)


class _ParkedHandle:
    """The bits of a runtime handle that `deadlock_message` reads."""

    def __init__(self, name, event):
        self.name = name
        self.tag = name
        self.status = WAITING
        self.wait_event = event


class BlockedSignalScoping(unittest.TestCase):
    """F6: blocked-signal records describe signals that NEVER happened.

    The list was append-only, so a handshake that failed one round and succeeded
    the next left an entry that an unrelated later deadlock would cite.
    """

    def _runtime(self):
        return AgentRuntime()

    def test_entry_is_dropped_once_the_event_is_signalled(self):
        rt = self._runtime()
        rt.record_blocked_signal("prd", "Preamble/preexit[1]", "boom", event="go")
        self.assertEqual(len(rt.blocked_signals), 1)
        rt.set_event("go")
        self.assertEqual(rt.blocked_signals, [])

    def test_entry_survives_when_the_event_is_never_signalled(self):
        rt = self._runtime()
        rt.record_blocked_signal("prd", "Preamble/preexit[1]", "boom", event="go")
        self.assertEqual(len(rt.blocked_signals), 1)
        self.assertIn("blocked_signals", rt.snapshot())

    def test_only_the_signalled_events_entries_are_dropped(self):
        rt = self._runtime()
        rt.record_blocked_signal("prd", "Preamble/preexit[1]", "boom", event="go")
        rt.record_blocked_signal("prd", "Preamble/preexit[1]", "later", event="stop")
        rt.set_event("go")
        self.assertEqual([e["event"] for e in rt.blocked_signals], ["stop"])

    def test_a_recovered_handshake_is_not_cited_by_a_later_deadlock(self):
        rt = self._runtime()
        rt.record_blocked_signal("prd", "Preamble/preexit[1]", "boom", event="go")
        rt.set_event("go")  # recovered: the peer was released in a later round
        rt.pool["cns"] = _ParkedHandle("cns", "stop")
        message = rt.deadlock_message()
        self.assertIn("cns waiting on 'stop'", message)
        self.assertNotIn("Preamble/preexit[1]", message)


if __name__ == "__main__":
    unittest.main(verbosity=2)
