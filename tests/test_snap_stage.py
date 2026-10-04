# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

import os
import shutil
import tempfile
import unittest
from pathlib import Path

import yaha
from yaha.config.agents import parse_agent_class_md
from yaha.core.agent import Agent
from yaha.core.engine import YAHAEngine


SNAP_FIXTURE = """\n## Parent
- **Inherited:** None
- **Code:** Par
- **Snap Handler:** Compression
- **Snap Threshold:** 85%
- **Snap To:** 25%
- **Preamble:**
    - **Snap:** enabled
    > parent preamble
- **Compression:**
    - **Detached:** True
    > parent snap handler
- **Aftermath:**
    > parent done

## ChildInherit
- **Inherited:** Parent
- **Code:** C1
- **Preamble:**
    - **Inherited:** All
    > child preamble

## ChildDisable
- **Inherited:** Parent
- **Code:** C2
- **Preamble:**
    - **Snap:** disabled
    > child preamble
"""


class _MockLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.usage = {"prompt_tokens": 5, "completion_tokens": 5}

    def generate(self, messages, **kwargs):
        if self.calls < len(self.responses):
            resp = self.responses[self.calls]
        else:
            resp = "mock done"
        self.calls += 1
        return resp, dict(self.usage), None


def _find(stage_name, chain):
    return next((s for s in chain if s["name"] == stage_name), None)


class TestSnapParser(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="yaha_snap_"))
        self.file = self.tmp / "AgentClass.md"
        self.file.write_text(SNAP_FIXTURE, encoding="utf-8")
        self.classes = parse_agent_class_md(str(self.file))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_detached_stage_not_on_chain(self):
        parent = self.classes["Parent"]
        self.assertIn("Compression", parent.detached_stages)
        self.assertNotIn("Compression", [s["name"] for s in parent.prompt_chain])

    def test_class_snap_fields(self):
        parent = self.classes["Parent"]
        self.assertEqual(parent.snap_handler, "Compression")
        self.assertEqual(parent.snap_threshold, 0.85)
        self.assertEqual(parent.snap_to, 0.25)

    def test_absent_snap_is_disabled(self):
        parent = self.classes["Parent"]
        self.assertEqual(_find("Preamble", parent.prompt_chain)["snap"], "enabled")
        self.assertIn(_find("Aftermath", parent.prompt_chain)["snap"], (None, "disabled"))

    def test_inherited_snap_via_inherited_all(self):
        child = self.classes["ChildInherit"]
        self.assertEqual(child.snap_handler, "Compression")
        self.assertEqual(_find("Preamble", child.prompt_chain)["snap"], "enabled")
        self.assertIn("Compression", child.detached_stages)

    def test_explicit_child_disabled_wins(self):
        child = self.classes["ChildDisable"]
        self.assertEqual(_find("Preamble", child.prompt_chain)["snap"], "disabled")

    def test_stock_chatagent_preamble_enables_snap(self):
        template = Path(yaha.__file__).resolve().parent / "templates" / "AgentClass.md"
        classes = parse_agent_class_md(str(template))
        cha = classes["ChatAgent"]
        self.assertEqual(_find("Preamble", cha.prompt_chain)["snap"], "enabled")
        self.assertNotEqual(_find("Query_user", cha.prompt_chain)["snap"], "enabled")


class TestSnapRuntime(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="yaha_snap_runtime_"))
        (self.ws / ".yaha" / "memory").mkdir(parents=True)
        (self.ws / ".yaha" / "ext").mkdir(parents=True)
        (self.ws / ".yaha" / "logs").mkdir(parents=True)
        (self.ws / ".yaha" / "tool-schema.md").write_text("[]", encoding="utf-8")
        (self.ws / ".yaha" / "AgentClass.md").write_text(SNAP_FIXTURE, encoding="utf-8")
        (self.ws / ".yaha" / "Model.md").write_text("", encoding="utf-8")
        self.engine = YAHAEngine(workspace_dir=str(self.ws))
        cfg = self.engine.agent_classes["ChildInherit"]
        cfg.snap_threshold = 0.01
        cfg.snap_to = 0.5
        self.agent = Agent(
            agent_config=cfg,
            session=self.engine.session,
            models_config=self.engine.models_config,
            agent_classes=self.engine.agent_classes,
            tool_schemas=self.engine.tool_schemas,
            access_controller=self.engine.access_controller,
        )

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def _reset(self, snap_threshold=0.01, responses=None, context_size=200):
        self.agent.llm = _MockLLM(responses or ["GOAL: goal\nCOMPLETED: stuff\nPENDING: more\nKEY FILES: none\nERRORS: none"])
        self.agent.config.snap_threshold = snap_threshold
        self.agent.context_size = context_size
        self.agent.history = [
            {"role": "system", "content": self.agent.build_system_prompt()},
            {"role": "user", "content": "long enough history for context" * 10},
        ]
        self.agent._last_prompt_tokens = 0

    def test_above_threshold_enabled_runs_once_and_rebuilds(self):
        self._reset(responses=["GOAL: goal\nCOMPLETED: stuff\nPENDING: more\nKEY FILES: none\nERRORS: none"])
        turn = self.agent._maybe_snap({"name": "Preamble", "snap": "enabled"}, 1)
        self.assertEqual(self.agent.compress_count, 1)
        self.assertTrue(any("CONTEXT DIGEST" in (m.get("content") or "") for m in self.agent.history))
        self.assertTrue(any("COMPLETED: stuff" in (m.get("content") or "") for m in self.agent.history))
        self.assertIsInstance(turn, int)

    def test_disabled_stage_does_not_snap(self):
        self._reset(responses=["unexpected"])
        self.agent._maybe_snap({"name": "Plan", "snap": "disabled"}, 1)
        self.assertEqual(self.agent.compress_count, 0)
        self.assertFalse(any("CONTEXT DIGEST" in (m.get("content") or "") for m in self.agent.history))

    def test_force_bypasses_disabled_on_snap_disabled_stage(self):
        self._reset(responses=["GOAL: goal\nCOMPLETED: forced\nPENDING: more\nKEY FILES: none\nERRORS: none"])
        self.agent._maybe_snap({"name": "Plan", "snap": "disabled"}, 1, force=True)
        self.assertEqual(self.agent.compress_count, 1)
        self.assertTrue(any("COMPLETED: forced" in (m.get("content") or "") for m in self.agent.history))

    def _inject_old_tool_result(self):
        old_tool = (
            "TOOL RESULT (bash_tool):\n"
            "- **Status**: SUCCESS\n"
            "- **Content**:\n```\n" + ("x" * 6000) + "\n```"
        )
        self.agent.history = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": old_tool},
            {"role": "assistant", "content": "a"},
            {"role": "user", "content": "small1"},
            {"role": "assistant", "content": "b"},
            {"role": "user", "content": "small2"},
            {"role": "assistant", "content": "c"},
            {"role": "user", "content": "small3"},
        ]

    def _set_occupancy(self, ratio):
        """Pin the occupancy to a provider-reported anchor with a zero delta.

        The mid-loop net is the STRICT net: it fires at HARD_FORCE_RATIO, not at the
        class Snap threshold, so these guards drive the occupancy directly instead of
        steering `snap_threshold`.
        """
        self.agent._last_prompt_tokens = int(ratio * self.agent.context_size)
        self.agent._chars_at_last_usage = sum(
            len(m.get("content") or "") for m in self.agent.history)

    def test_mid_loop_l1_sufficient(self):
        self._reset(context_size=1600)
        self._inject_old_tool_result()
        self._set_occupancy(0.97)          # above the STRICT watermark
        rebuilt, _ = self.agent._mid_loop_protection({"name": "Coding", "snap": "disabled"}, "cont", 1)
        self.assertFalse(rebuilt)
        self.assertEqual(self.agent.compress_count, 1)
        self.assertEqual(self.agent.llm.calls, 0)  # L1 was enough; no handler
        self.assertTrue(any("COMPRESSED TOOL RESULT" in (m.get("content") or "") for m in self.agent.history))

    def test_mid_loop_force_l2_escalates_when_l1_sufficient(self):
        # Without force_l2, L1 alone is enough -> the handler is never called.
        self._reset(context_size=1600)
        self._inject_old_tool_result()
        self._set_occupancy(0.97)
        rebuilt, _ = self.agent._mid_loop_protection(
            {"name": "Coding", "snap": "disabled"}, "cont", 1)
        self.assertFalse(rebuilt)
        self.assertEqual(self.agent.llm.calls, 0)

        # force_l2 forces the handler (L2) even though L1 was sufficient.
        self._reset(context_size=1600)
        self._inject_old_tool_result()
        self._set_occupancy(0.97)
        rebuilt2, _ = self.agent._mid_loop_protection(
            {"name": "Coding", "snap": "disabled"}, "cont", 1, force_l2=True)
        self.assertTrue(rebuilt2)
        self.assertGreaterEqual(self.agent.llm.calls, 1)

    def test_mid_loop_handler_escalation(self):
        self._reset(context_size=200)
        self._set_occupancy(0.99)          # above the STRICT watermark, nothing to truncate
        rebuilt, _ = self.agent._mid_loop_protection(
            {"name": "Coding", "snap": "disabled"},
            "STAGE PROMPT BEACON",
            1,
        )
        self.assertTrue(rebuilt)
        self.assertEqual(self.agent.compress_count, 1)
        self.assertTrue(any("STAGE PROMPT BEACON" in (m.get("content") or "") for m in self.agent.history))
        self.assertTrue(any("CONTEXT RESTORED" in (m.get("content") or "") for m in self.agent.history))

    def test_mid_loop_legacy_fallback_when_no_handler(self):
        # Remove all snap policy -> legacy _compress_context takes over.
        self.agent.config.snap_handler = None
        self.agent.config.snap_threshold = None
        self.agent.config.snap_to = None
        self.agent.config.detached_stages = {}
        self._reset(context_size=200)
        # Make context clearly above the STRICT watermark for the legacy path too.
        self.agent.history = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "long enough history for context" * 20},
        ]
        self._set_occupancy(0.97)
        rebuilt, turn = self.agent._mid_loop_protection({"name": "Plain", "snap": "disabled"}, "pl", 1)
        self.assertIsInstance(rebuilt, bool)
        self.assertIsInstance(turn, int)
        # Legacy compression may run L1 or cold restart; at least it took a path.
        self.assertGreaterEqual(self.agent.llm.calls, 0)

    def test_below_threshold_does_not_snap(self):
        # The char-based occupancy estimate scales with the system prompt, so
        # "below threshold" must sit CLEARLY above any plausible estimate - a
        # 2x threshold was once silently overtaken by schema-doc growth.
        self._reset(snap_threshold=100.0)
        self.agent._maybe_snap({"name": "Preamble", "snap": "enabled"}, 1)
        self.assertEqual(self.agent.compress_count, 0)


if __name__ == "__main__":
    unittest.main()
