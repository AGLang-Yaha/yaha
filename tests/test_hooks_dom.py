#!/usr/bin/env python3
"""The read-only `yaha` DOM: live snapshot content + read-only enforcement.

`build_snapshot` reads the live agent in the parent process and materialises a
plain, JSON-able dict; the kernel binds it as `yaha`. These tests pin what a
hook can actually see and prove that mutation is impossible.
"""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from yaha.hooks.dom import YahaReadOnlyError, YahaView, build_snapshot, class_view


class FakeClassConfig:
    def __init__(self, name, code, inherited=None):
        self.name = name
        self.code = code
        self.inherited = inherited
        self.model = "m"
        self.prompt_chain = [{
            "name": "Coding",
            "wait_event": "code_ready",
            "allowed_tools": ["read_file"],
            "required_tools": [],
            "exit_tools": ["success_tool"],
            "hooks": {"preexit": [{"label": "preexit[1]"}]},
        }]


class FakeSession:
    def __init__(self, workspace):
        self.workspace_dir = workspace
        self.folder_name = "sess-1"
        self.log_dir = os.path.join(workspace, ".yaha", "logs", "sess-1")

    def get_error_log_path(self):
        return os.path.join(self.log_dir, "error.log")


class FakeLogger:
    log_path = "/tmp/agent.log"


class FakeRuntime:
    blocked_signals = [{"agent": "prd", "hook": "Preamble/preexit[1]",
                        "detail": "boom", "event": "go"}]

    def snapshot(self):
        return {"live": ["prd", "cns"], "ready": ["prd"],
                "waiting": {"go": ["cns"]}, "events": ["go"], "max_agents": 2,
                "blocked_signals": list(self.blocked_signals)}

    def active_name(self):
        return "prd"


class FakeAgent:
    def __init__(self, workspace):
        self.config = FakeClassConfig("CrewCda", "Cwd", "CodeAgent")
        self.agent_classes = {
            "BaseAgent": FakeClassConfig("BaseAgent", "Baa", None),
            "CodeAgent": FakeClassConfig("CodeAgent", "Cda", "BaseReflector"),
            "BaseReflector": FakeClassConfig("BaseReflector", "Bar", "BaseAgent"),
            "CrewCda": self.config,
        }
        self.agent_id = "abcd1234"
        self.session = FakeSession(workspace)
        self.logger = FakeLogger()
        self.model_cfg = SimpleNamespace(model="m", provider="p")
        self.history = [{"role": "system", "content": "s"}] * 3
        self.context_size = 1000
        self._last_prompt_tokens = 12
        # The compression instrument a REAL agent exposes. The snapshot publishes this
        # value (hooks/dom.py), not the raw provider anchor, so the stub must carry it or
        # the published figure silently falls back to 0.
        self._chars_at_last_usage = None
        self._usage_is_predicted = False
        self._current_turn = 5
        self._last_stage_turn = 2
        self._chain_index = 3
        self._chain_len = 6
        self.runtime = FakeRuntime()
        self._hook_engine_obj = None

    def _context_usage_ratio(self):
        # Same shape as the real instrument for a stub with no calibration history.
        return (self._last_prompt_tokens or 0) / (self.context_size or 1)


class SnapshotContent(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.mkdtemp(prefix="yaha_dom_")
        self.agent = FakeAgent(self.workspace)
        self.stage = self.agent.config.prompt_chain[0]

    def tearDown(self):
        shutil.rmtree(self.workspace, ignore_errors=True)

    def test_agent_node(self):
        snap = build_snapshot(self.agent, stage=self.stage,
                              hook={"label": "preexit[1]", "kind": "bash"},
                              tool=None)
        view = YahaView.from_snapshot(snap)
        self.assertEqual(view.agent.name, "CrewCda")
        self.assertEqual(view.agent.agent_id, "abcd1234")
        self.assertEqual(view.agent.stage, "Coding")
        self.assertEqual(view.agent.turn, 5)
        self.assertEqual(view.agent.message_count, 3)
        self.assertEqual(view.agent.prompt_tokens, 12)
        self.assertEqual(view.agent.model, "m")

    def test_agent_class_lineage_and_inherits(self):
        snap = build_snapshot(self.agent, stage=self.stage)
        view = YahaView.from_snapshot(snap)
        klass = view.agent.agent_class
        self.assertEqual(klass.short_name, "Cwd")
        self.assertEqual(klass.lineage,
                         ["BaseAgent", "BaseReflector", "CodeAgent", "CrewCda"])
        self.assertTrue(klass.inherits("CodeAgent"))
        self.assertTrue(klass.inherits("codeagent"))     # case-insensitive
        self.assertFalse(klass.inherits("NoAgent"))
        self.assertIn("Coding", klass.stages)

    def test_stage_node(self):
        snap = build_snapshot(self.agent, stage=self.stage)
        view = YahaView.from_snapshot(snap)
        self.assertEqual(view.stage.name, "Coding")
        self.assertEqual(view.stage.index, 3)
        self.assertEqual(view.stage.chain_len, 6)
        self.assertEqual(view.stage.tools.allowed, ["read_file"])
        self.assertEqual(view.stage.tools.exit, ["success_tool"])
        self.assertEqual(view.stage.wait_event, "code_ready")
        self.assertEqual(view.stage.hooks["preexit"], ["preexit[1]"])

    def test_hook_node_is_present_only_when_bound(self):
        snap = build_snapshot(self.agent, stage=self.stage,
                              hook={"label": "preexit[1]", "kind": "bash",
                                    "required": True, "capture": False,
                                    "index": 1})
        view = YahaView.from_snapshot(snap)
        self.assertEqual(view.hook.label, "preexit[1]")
        self.assertTrue(view.hook.required)
        self.assertIsNone(YahaView.from_snapshot(
            build_snapshot(self.agent, stage=self.stage)).hook)

    def test_hook_view_exposes_a_closed_documented_key_set(self):
        # A hook must not be able to read arbitrary spec internals, and adding a
        # key to the view is a deliberate, documented act - so pin the set.
        snap = build_snapshot(self.agent, stage=self.stage,
                              hook={"label": "preexit[1]", "kind": "bash",
                                    "required": True, "capture": False,
                                    "index": 1, "code": "echo hi"})
        view = YahaView.from_snapshot(snap)
        self.assertEqual(set(view.hook.keys()),
                         {"name", "kind", "required", "capture", "index",
                          "label"})

    def test_hook_name_is_an_alias_of_label(self):
        snap = build_snapshot(self.agent, stage=self.stage,
                              hook={"label": "Coding/posttool[2]",
                                    "kind": "python", "index": 2})
        view = YahaView.from_snapshot(snap)
        self.assertEqual(view.hook.name, "Coding/posttool[2]")
        self.assertEqual(view.hook.name, view.hook.label)

    def test_hook_view_flags_are_real_booleans_even_when_absent(self):
        # A closed view always carries every documented key, so a hook can write
        # `if yaha.hook.required:` without a None/AttributeError surprise.
        view = YahaView.from_snapshot(build_snapshot(
            self.agent, stage=self.stage, hook={"label": "x", "kind": "bash"}))
        self.assertIs(view.hook.required, False)
        self.assertIs(view.hook.capture, False)

    def test_hook_source_code_is_not_exposed(self):
        # The hook's own source text was leaked by the raw-spec passthrough.
        snap = build_snapshot(self.agent, stage=self.stage,
                              hook={"label": "x", "kind": "bash",
                                    "code": "echo secret"})
        view = YahaView.from_snapshot(snap)
        with self.assertRaises(AttributeError):
            view.hook.code
        self.assertNotIn("code", view.hook.keys())

    def test_tool_node_only_inside_a_tool_hook(self):
        self.assertIsNone(YahaView.from_snapshot(
            build_snapshot(self.agent, stage=self.stage)).tool)
        snap = build_snapshot(self.agent, stage=self.stage, tool={
            "name": "bash_tool", "args": {"cmdline": "ls"}, "ok": False,
            "error": "Error: nope", "duration_ms": 7, "stage_attempt": 2})
        view = YahaView.from_snapshot(snap)
        self.assertEqual(view.tool.name, "bash_tool")
        self.assertEqual(view.tool.args.cmdline, "ls")
        self.assertFalse(view.tool.ok)
        self.assertEqual(view.tool.error, "Error: nope")
        self.assertEqual(view.tool.duration_ms, 7)

    def test_runtime_and_workspace_nodes(self):
        snap = build_snapshot(self.agent, stage=self.stage)
        view = YahaView.from_snapshot(snap)
        self.assertEqual(view.runtime.live, ["prd", "cns"])
        self.assertEqual(view.runtime.waiting.go, ["cns"])
        self.assertEqual(view.runtime.active, "prd")
        self.assertEqual(view.runtime.blocked_signals[0].hook,
                         "Preamble/preexit[1]")
        self.assertEqual(view.workspace.root, self.workspace)
        self.assertEqual(view.workspace.session_dir, "sess-1")
        self.assertTrue(view.log.error_log_path.endswith("error.log"))

    def test_snapshot_is_json_serialisable(self):
        import json
        snap = build_snapshot(self.agent, stage=self.stage,
                              hook={"label": "x"}, tool={"name": "t"})
        json.dumps(snap)         # must not raise: it crosses a process boundary

    def test_build_snapshot_survives_a_minimal_agent(self):
        class Bare:
            pass
        view = YahaView.from_snapshot(build_snapshot(Bare(), stage={"name": "S"}))
        self.assertEqual(view.stage.name, "S")
        self.assertIsNone(view.agent.agent_class)


class ReadOnlyEnforcement(unittest.TestCase):
    def setUp(self):
        self.view = YahaView.from_snapshot({
            "agent": {"name": "CrewCda"},
            "stage": {"name": "Coding", "tools": {"allowed": ["read_file"]}},
        })

    def test_assigning_an_existing_attribute_raises(self):
        with self.assertRaises(YahaReadOnlyError):
            self.view.agent.name = "hacked"

    def test_assigning_a_new_attribute_raises(self):
        with self.assertRaises(YahaReadOnlyError):
            self.view.agent.brand_new = 1

    def test_deleting_an_attribute_raises(self):
        with self.assertRaises(YahaReadOnlyError):
            del self.view.agent.name

    def test_unknown_attribute_is_an_attribute_error(self):
        with self.assertRaises(AttributeError):
            self.view.agent.nope

    def test_get_and_membership_are_read_only_conveniences(self):
        self.assertEqual(self.view.agent.get("name"), "CrewCda")
        self.assertEqual(self.view.agent.get("missing", "fallback"), "fallback")
        self.assertIn("name", self.view.agent)
        self.assertEqual(self.view.stage.tools.allowed[0], "read_file")


class ClassViewBuilder(unittest.TestCase):
    def test_lineage_walks_to_the_root(self):
        classes = {
            "BaseAgent": FakeClassConfig("BaseAgent", "Baa", None),
            "Kid": FakeClassConfig("Kid", "Kid", "BaseAgent"),
        }
        view = class_view(classes["Kid"], classes)
        self.assertEqual(view["lineage"], ["BaseAgent", "Kid"])
        self.assertEqual(view["short_name"], "Kid")

    def test_missing_parent_does_not_loop(self):
        view = class_view(FakeClassConfig("Orphan", "Orp", "Ghost"), {})
        self.assertEqual(view["lineage"], ["Orphan"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
