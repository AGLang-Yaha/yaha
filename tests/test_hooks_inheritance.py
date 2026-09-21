#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

"""Stage-hook and list inheritance.

The rule is uniform - `Inherited: All` keeps and appends, anything else
replaces:

* the **stage-level** `- **Inherited:** All` inherits a field the child did not
  author at all (scalars AND lists AND hooks);
* a **per-block** `- **Inherited:** All` inside a tool list or a hook block
  appends the child's items AFTER the parent's, and works on its own;
* otherwise the authored block replaces the parent's list.

Rows 1-3 reproduce the pre-existing behaviour for Allowed/Required/Exit Tools,
so the shipped template must be unaffected.
"""
import shutil
import tempfile
import unittest
from pathlib import Path

import yaha
from yaha.config.agents import parse_agent_class_md

TEMPLATE = Path(yaha.__file__).resolve().parent / "templates" / "AgentClass.md"
FENCE = chr(96) * 3


def _hook(kind="bash", mods="", code="echo hi", fence=False):
    if kind == "python":
        body = ("        - python%s:\n"
                "            " + FENCE + "\n"
                "            %s\n"
                "            " + FENCE + "\n") % (mods, code)
        return body
    return "        - bash%s: %s\n" % (mods, code)


def _class(name, parent, stage_body, stage_name="Coding", extra=""):
    return (
        "## %s\n"
        "- **Inherited:** %s\n"
        "- **Code:** %s\n"
        "- **Model:** DefaultModel\n"
        "%s"
        "- **%s:**\n"
        "%s"
        "    > %s PROMPT\n"
        "- **Aftermath:**\n"
        "    - **Inherited:** None\n"
        % (name, parent, name[:3], extra, stage_name, stage_body, name)
    )


class _FixtureMixin:
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="yaha_hooks_inherit_"))
        self._n = 0

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def classes(self, text):
        self._n += 1
        path = self._tmp / ("cls_%d.md" % self._n)
        path.write_text(text, encoding="utf-8")
        return parse_agent_class_md(str(path))

    def stage(self, classes, cls, name="Coding"):
        for stage in classes[cls].prompt_chain:
            if stage["name"] == name:
                return stage
        raise AssertionError("%s has no stage %s" % (cls, name))


class ToolListInheritance(_FixtureMixin, unittest.TestCase):
    BASE = ("    - **Allowed Tools:**\n"
            "        - read_file\n"
            "        - bash_tool\n")

    def test_stage_flag_inherits_a_list_the_child_never_authored(self):
        text = _class("Base", "None", self.BASE) + _class(
            "Kid", "Base", "    - **Inherited:** All\n")
        classes = self.classes(text)
        self.assertEqual(sorted(self.stage(classes, "Kid")["allowed_tools"]),
                         ["bash_tool", "read_file"])

    def test_authored_block_replaces_even_under_the_stage_flag(self):
        text = _class("Base", "None", self.BASE) + _class(
            "Kid", "Base",
            "    - **Inherited:** All\n"
            "    - **Allowed Tools:**\n"
            "        - create_file\n")
        classes = self.classes(text)
        self.assertEqual(self.stage(classes, "Kid")["allowed_tools"],
                         ["create_file"])

    def test_per_block_marker_appends_parent_then_child(self):
        text = _class("Base", "None", self.BASE) + _class(
            "Kid", "Base",
            "    - **Inherited:** All\n"
            "    - **Allowed Tools:**\n"
            "        - **Inherited:** All\n"
            "        - create_file\n")
        classes = self.classes(text)
        self.assertEqual(self.stage(classes, "Kid")["allowed_tools"],
                         ["read_file", "bash_tool", "create_file"])

    def test_per_block_marker_works_without_the_stage_flag(self):
        text = _class("Base", "None", self.BASE) + _class(
            "Kid", "Base",
            "    - **Allowed Tools:**\n"
            "        - **Inherited:** All\n"
            "        - create_file\n")
        classes = self.classes(text)
        stage = self.stage(classes, "Kid")
        self.assertEqual(stage["allowed_tools"],
                         ["read_file", "bash_tool", "create_file"])
        # ... and nothing else was inherited: no stage flag, so the prompt is
        # the child's own.
        self.assertEqual(stage["content"], "Kid PROMPT")

    def test_no_marker_no_flag_means_replace(self):
        text = _class("Base", "None", self.BASE) + _class(
            "Kid", "Base",
            "    - **Allowed Tools:**\n"
            "        - create_file\n")
        classes = self.classes(text)
        self.assertEqual(self.stage(classes, "Kid")["allowed_tools"],
                         ["create_file"])

    def test_unrelated_field_is_not_inherited_without_the_stage_flag(self):
        text = _class("Base", "None", self.BASE) + _class(
            "Kid", "Base",
            "    - **Allowed Tools:**\n"
            "        - **Inherited:** All\n"
            "        - create_file\n")
        classes = self.classes(text)
        self.assertEqual(self.stage(classes, "Kid")["required_tools"], [])
        self.assertEqual(self.stage(classes, "Kid")["exit_tools"], [])


class HookInheritance(_FixtureMixin, unittest.TestCase):
    def test_stage_flag_inherits_hooks_the_child_never_authored(self):
        base = _class("Base", "None",
                      "    - **preenter:**\n" + _hook(code="echo base"))
        kid = _class("Kid", "Base", "    - **Inherited:** All\n")
        classes = self.classes(base + kid)
        labels = [s["label"] for s in self.stage(classes, "Kid")["hooks"]["preenter"]]
        self.assertEqual(labels, ["preenter[1]"])
        self.assertEqual(self.stage(classes, "Kid")["hooks"]["preenter"][0]["code"],
                         "echo base")

    def test_authored_hook_block_replaces_without_a_marker(self):
        base = _class("Base", "None",
                      "    - **preenter:**\n" + _hook(code="echo base"))
        kid = _class("Kid", "Base",
                     "    - **Inherited:** All\n"
                     "    - **preenter:**\n" + _hook(code="echo kid"))
        classes = self.classes(base + kid)
        specs = self.stage(classes, "Kid")["hooks"]["preenter"]
        self.assertEqual([s["code"] for s in specs], ["echo kid"])

    def test_per_block_marker_appends_ancestor_first(self):
        base = _class("Base", "None",
                      "    - **preenter:**\n" + _hook(code="echo base"))
        kid = _class("Kid", "Base",
                     "    - **preenter:**\n"
                     "        - **Inherited:** All\n" + _hook(code="echo kid"))
        classes = self.classes(base + kid)
        specs = self.stage(classes, "Kid")["hooks"]["preenter"]
        self.assertEqual([s["code"] for s in specs], ["echo base", "echo kid"])
        self.assertEqual([s["label"] for s in specs], ["preenter[1]", "preenter[2]"])

    def test_per_block_marker_is_self_sufficient_for_hooks(self):
        base = _class("Base", "None",
                      "    - **preenter:**\n" + _hook(code="echo base"))
        kid = _class("Kid", "Base",
                     "    - **preenter:**\n"
                     "        - **Inherited:** All\n" + _hook(code="echo kid"))
        classes = self.classes(base + kid)
        stage = self.stage(classes, "Kid")
        self.assertEqual(len(stage["hooks"]["preenter"]), 2)
        # the stage itself did not inherit prompt text
        self.assertEqual(stage["content"], "Kid PROMPT")

    def test_three_generations_accumulate_transitively(self):
        gp = _class("Gran", "None",
                    "    - **preenter:**\n" + _hook(code="echo gran"))
        parent = _class("Par", "Gran",
                        "    - **Inherited:** All\n"
                        "    - **preenter:**\n"
                        "        - **Inherited:** All\n" + _hook(code="echo par"))
        kid = _class("Kid", "Par",
                     "    - **Inherited:** All\n"
                     "    - **preenter:**\n"
                     "        - **Inherited:** All\n" + _hook(code="echo kid"))
        classes = self.classes(gp + parent + kid)
        specs = self.stage(classes, "Kid")["hooks"]["preenter"]
        self.assertEqual([s["code"] for s in specs],
                         ["echo gran", "echo par", "echo kid"])
        self.assertEqual([s["label"] for s in specs],
                         ["preenter[1]", "preenter[2]", "preenter[3]"])

    def test_aftermath_hook_inherits_like_any_other_stage(self):
        text = (
            "## Gran\n"
            "- **Inherited:** None\n"
            "- **Code:** Gra\n"
            "- **Model:** DefaultModel\n"
            "- **Preamble:**\n"
            "    - **Inherited:** None\n"
            "    > P\n"
            "- **Aftermath:**\n"
            "    - **preenter:**\n" + _hook(code="echo gm") + "    > GM\n"
            "## Kid\n"
            "- **Inherited:** Gran\n"
            "- **Code:** Kid\n"
            "- **Model:** DefaultModel\n"
            "- **Preamble:**\n"
            "    - **Inherited:** All\n"
            "    > P\n"
            "- **Aftermath:**\n"
            "    - **Inherited:** All\n")
        classes = self.classes(text)
        specs = self.stage(classes, "Kid", "Aftermath")["hooks"]["preenter"]
        self.assertEqual([s["code"] for s in specs], ["echo gm"])
        self.assertEqual(specs[0]["label"], "preenter[1]")


class ShippedTemplateUnchanged(unittest.TestCase):
    """Rows 1-3 must reproduce today's behaviour for the real template."""

    @classmethod
    def setUpClass(cls):
        cls.classes = parse_agent_class_md(str(TEMPLATE))

    def _stage(self, cls, name):
        for stage in self.classes[cls].prompt_chain:
            if stage["name"] == name:
                return stage
        raise AssertionError("no stage %s" % name)

    def test_crew_coder_preamble_still_inherits_codeagent_tools(self):
        stage = self._stage("CrewCda", "Preamble")
        self.assertEqual(sorted(stage["allowed_tools"]),
                         ["ask_user", "fail_tool", "success_tool"])
        self.assertEqual(stage["required_tools"], ["ask_user"])
        self.assertEqual(sorted(stage["exit_tools"]), ["fail_tool", "success_tool"])
        self.assertEqual(len(stage["content"]), len(self._stage("CodeAgent", "Preamble")["content"]))

    def test_crew_coder_coding_still_stands_alone(self):
        # CrewCda/Coding deliberately does not say Inherited: All, so it must not
        # pick up CodeAgent/Coding's required ask_user gate.
        stage = self._stage("CrewCda", "Coding")
        self.assertEqual(stage["required_tools"], [])
        self.assertEqual(stage["allowed_tools"], [])
        self.assertEqual(stage["exit_tools"], ["set_event_success"])
        self.assertNotIn("ask_user", stage["content"])

    def test_no_shipped_stage_declares_hooks(self):
        for cfg in self.classes.values():
            for stage in cfg.prompt_chain:
                for name in ("preenter", "pretool", "posttool", "preexit"):
                    self.assertEqual(stage["hooks"][name], [])

    def test_hook_timeout_default_is_applied(self):
        for cfg in self.classes.values():
            self.assertEqual(cfg.hook_timeout, 300.0, cfg.name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
