#!/usr/bin/env python3
"""Stage-hook grammar: parsing, modifiers, strict errors and leak safety.

A hook block is a stage property:

    - **preenter:**
        - **Inherited:** All
        - bash (required, capture): git status --porcelain
        - python (capture):
            ```
            print(yaha.stage.name)
            ```

Malformed hook blocks fail fast with HookSpecError at load time: a silently
ignored hook would leave an operator believing a gate is armed when it is not.
"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaha
from _template_fixture import agentclass_doc_text
from yaha.config.agents import parse_agent_class_md
from yaha.hooks.grammar import (
    HOOK_NAMES,
    HookSpecError,
    parse_stage_hooks,
    relabel_hooks,
)

TEMPLATE = Path(yaha.__file__).resolve().parent / "templates" / "AgentClass.md"
FENCE = chr(96) * 3


def _template(preenter="", pretool="", posttool="", preexit="", stage_flag=""):
    return (
        "## TestAgent\n"
        "- **Inherited:** None\n"
        "- **Code:** Tst\n"
        "- **Model:** DefaultModel\n"
        "- **Preamble:**\n"
        "    - **Inherited:** %s\n"
        "%s%s%s%s"
        "    > PREAMBLE PROMPT\n"
        "- **Aftermath:**\n"
        "    - **Inherited:** None\n"
        % (stage_flag, preenter, pretool, posttool, preexit)
    )


class _FixtureMixin:
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="yaha_hooks_grammar_"))
        self._n = 0

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def classes(self, text):
        self._n += 1
        path = self._tmp / ("cls_%d.md" % self._n)
        path.write_text(text, encoding="utf-8")
        return parse_agent_class_md(str(path))

    def preamble(self, text):
        return self.classes(text)["TestAgent"].prompt_chain[0]


class ParseHookBlocks(_FixtureMixin, unittest.TestCase):
    def test_bash_and_python_items_parse_in_order(self):
        text = _template(preenter=(
            "    - **preenter:**\n"
            "        - bash (required, capture): echo hi\n"
            "        - python (capture):\n"
            "            " + FENCE + "\n"
            "            x = 1\n"
            "            " + FENCE + "\n"))
        stage = self.preamble(text)
        specs = stage["hooks"]["preenter"]
        self.assertEqual([s["kind"] for s in specs], ["bash", "python"])
        self.assertTrue(specs[0]["required"] and specs[0]["capture"])
        self.assertEqual(specs[0]["code"], "echo hi")
        self.assertFalse(specs[1]["required"])
        self.assertTrue(specs[1]["capture"])
        self.assertEqual(specs[1]["code"], "x = 1")
        self.assertEqual([s["label"] for s in specs], ["preenter[1]", "preenter[2]"])
        for name in ("pretool", "posttool", "preexit"):
            self.assertEqual(stage["hooks"][name], [])

    def test_all_four_kinds_and_case_insensitive_banner(self):
        text = _template(
            preenter="    - **PreEnter:**\n        - bash: echo 1\n",
            pretool="    - **pretool:**\n        - bash: echo 2\n",
            posttool="    - **posttool:**\n        - bash: echo 3\n",
            preexit="    - **preexit:**\n        - bash: echo 4\n")
        stage = self.preamble(text)
        for name in HOOK_NAMES:
            self.assertEqual(len(stage["hooks"][name]), 1, name)

    def test_modifiers_are_order_free_and_case_insensitive(self):
        text = _template(
            preenter="    - **preenter:**\n        - bash (CAPTURE, Required): echo hi\n")
        spec = self.preamble(text)["hooks"]["preenter"][0]
        self.assertTrue(spec["required"] and spec["capture"])

    def test_per_block_inherit_marker_is_recorded_and_not_an_item(self):
        text = _template(preenter=(
            "    - **preenter:**\n"
            "        - **Inherited:** All\n"
            "        - bash: echo hi\n"))
        stage = self.preamble(text)
        self.assertTrue(stage["field_inherit"]["preenter"])
        self.assertEqual(len(stage["hooks"]["preenter"]), 1)
        self.assertEqual(stage["hooks"]["preenter"][0]["code"], "echo hi")

    def test_multiline_bash_continuation_is_kept(self):
        """A `|` block scalar is layout: the indicator never reaches bash.

        The regression this pins: the indicator used to be kept literally, so
        the parsed code was `'|\\n    echo one'` and `/bin/bash -c` rejected it
        with a syntax error. Asserting only "contains echo one" hid that, so the
        effective command is asserted exactly AND executed.
        """
        text = _template(preenter=(
            "    - **preenter:**\n"
            "        - bash: |\n"
            "            echo one\n"
            "            echo two\n"))
        spec = self.preamble(text)["hooks"]["preenter"][0]
        self.assertEqual(spec["code"], "echo one\necho two")
        self.assertNotIn("|", spec["code"])
        proc = subprocess.run(["/bin/bash", "-c", spec["code"]],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "one\ntwo")

    def test_block_scalar_body_is_dedented_relative_to_the_item(self):
        text = _template(preenter=(
            "    - **preenter:**\n"
            "        - bash: |\n"
            "            if true; then\n"
            "                echo deep\n"
            "            fi\n"))
        spec = self.preamble(text)["hooks"]["preenter"][0]
        self.assertEqual(spec["code"],
                         "if true; then\n    echo deep\nfi")
        proc = subprocess.run(["/bin/bash", "-c", spec["code"]],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "deep")

    def test_plain_continuation_lines_still_work_without_an_indicator(self):
        text = _template(preenter=(
            "    - **preenter:**\n"
            "        - bash: echo one\n"
            "            echo two\n"))
        spec = self.preamble(text)["hooks"]["preenter"][0]
        self.assertTrue(spec["code"].startswith("echo one"))
        self.assertIn("echo two", spec["code"])

    def test_shell_text_is_not_mistaken_for_a_block_scalar(self):
        """Only a whole-line `|`/`>` header is layout, never shell text."""
        for command in ("> out.txt", "|| true", "|tee log.txt"):
            text = _template(preenter=(
                "    - **preenter:**\n"
                "        - bash: %s\n" % command))
            spec = self.preamble(text)["hooks"]["preenter"][0]
            self.assertEqual(spec["code"], command)

    def test_python_fence_with_language_info_is_accepted(self):
        text = _template(preenter=(
            "    - **preenter:**\n"
            "        - python:\n"
            "            " + FENCE + "python\n"
            "            y = 2\n"
            "            " + FENCE + "\n"))
        spec = self.preamble(text)["hooks"]["preenter"][0]
        self.assertEqual(spec["code"], "y = 2")

    def test_declared_fields_records_authored_hook_kinds(self):
        text = _template(preenter="    - **preenter:**\n        - bash: echo 1\n")
        stage = self.preamble(text)
        self.assertIn("preenter", stage["declared_fields"])
        self.assertNotIn("pretool", stage["declared_fields"])


class HookLeakSafety(_FixtureMixin, unittest.TestCase):
    def test_hook_code_never_leaks_into_the_stage_prompt(self):
        text = _template(preenter=(
            "    - **preenter:**\n"
            "        - python:\n"
            "            " + FENCE + "\n"
            "            a = 1\n"
            "            > this looks like a prompt line\n"
            "            b = 2\n"
            "            " + FENCE + "\n"))
        stage = self.preamble(text)
        self.assertNotIn("this looks like a prompt line", stage["content"])
        self.assertIn("PREAMBLE PROMPT", stage["content"])

    def test_hook_marker_does_not_flip_the_stage_level_flag(self):
        # Only the hook block declares Inherited: All; the stage itself says
        # None, so nothing else may inherit.
        text = _template(stage_flag="None", preenter=(
            "    - **preenter:**\n"
            "        - **Inherited:** All\n"
            "        - bash: echo hi\n"))
        stage = self.preamble(text)
        self.assertEqual(stage["content"], "PREAMBLE PROMPT")
        self.assertTrue(stage["field_inherit"]["preenter"])

    def test_shipped_template_loads_and_declares_no_hooks(self):
        classes = parse_agent_class_md(str(TEMPLATE))
        for required in ("BaseAgent", "CodeAgent", "Crewlda", "CrewCda", "CrewRva"):
            self.assertIn(required, classes)
        for cfg in classes.values():
            for stage in cfg.prompt_chain:
                for name in HOOK_NAMES:
                    self.assertEqual(stage["hooks"][name], [],
                                     "%s/%s" % (cfg.name, stage["name"]))


class HookStrictErrors(_FixtureMixin, unittest.TestCase):
    def _assert_error(self, text, fragment=None):
        with self.assertRaises(HookSpecError) as ctx:
            self.classes(text)
        if fragment:
            self.assertIn(fragment, str(ctx.exception))

    def test_python_item_with_inline_code_is_an_error(self):
        self._assert_error(
            _template(preenter="    - **preenter:**\n        - python: print(1)\n"),
            "fenced")

    def test_python_item_without_any_block_is_an_error(self):
        self._assert_error(
            _template(preenter="    - **preenter:**\n        - python:\n"))

    def test_unterminated_python_fence_is_an_error(self):
        self._assert_error(_template(preenter=(
            "    - **preenter:**\n"
            "        - python:\n"
            "            " + FENCE + "\n"
            "            z = 3\n")), "not terminated")

    def test_empty_bash_command_is_an_error(self):
        self._assert_error(
            _template(preenter="    - **preenter:**\n        - bash:\n"),
            "empty command")

    def test_unknown_modifier_is_an_error(self):
        self._assert_error(
            _template(preenter="    - **preenter:**\n        - bash (mandatory): echo hi\n"),
            "unknown hook modifier")

    def test_duplicate_modifier_is_an_error(self):
        self._assert_error(
            _template(preenter="    - **preenter:**\n"
                               "        - bash (required, required): echo hi\n"),
            "duplicate")

    def test_unknown_item_kind_is_an_error(self):
        self._assert_error(
            _template(preenter="    - **preenter:**\n        - shell: echo hi\n"),
            "must be '- bash")

    def test_empty_hook_block_is_an_error(self):
        self._assert_error(
            _template(preenter="    - **preenter:**\n"), "declares no hooks")

    def test_bad_inherited_value_in_a_hook_block_is_an_error(self):
        self._assert_error(_template(preenter=(
            "    - **preenter:**\n"
            "        - **Inherited:** Sometimes\n"
            "        - bash: echo hi\n")))

    def test_folded_block_scalar_indicator_is_rejected(self):
        """`>` folds newlines into spaces, which this grammar does not do.

        Keeping the indicator silently would hand bash a command it cannot
        parse, which is exactly what the literal form used to do.
        """
        for marker in (">", ">-", ">+"):
            self._assert_error(_template(preenter=(
                "    - **preenter:**\n"
                "        - bash: %s\n"
                "            echo one\n"
                "            echo two\n" % marker)), "folded")

    def test_numeric_block_scalar_indent_indicator_is_rejected(self):
        self._assert_error(_template(preenter=(
            "    - **preenter:**\n"
            "        - bash: |2\n"
            "            echo one\n")), "indentation indicators")

    def test_empty_block_scalar_is_an_error(self):
        self._assert_error(_template(preenter=(
            "    - **preenter:**\n"
            "        - bash: |\n")), "empty command")

    def test_python_block_scalar_indicator_is_still_rejected(self):
        self._assert_error(_template(preenter=(
            "    - **preenter:**\n"
            "        - python: |\n"
            "            x = 1\n")), "fenced")


class StagePropertyParsing(_FixtureMixin, unittest.TestCase):
    """Hook regions are blanked BEFORE stage fields are parsed.

    That is what keeps hook code out of the prompt, but it also removes bullet
    lines a field parser may be relying on as an anchor. `Fail Target` used to be
    read only when another `- **...**` bullet followed it, so writing it as the
    stage's last bullet before the prompt made it silently parse as absent - and
    blanking a hook block turned exactly that layout into the normal one. A
    failing required hook then routed to the default instead of the declared
    target.
    """

    HEADER = (
        "## TestAgent\n"
        "- **Inherited:** None\n"
        "- **Code:** Tst\n"
        "- **Model:** DefaultModel\n"
        "- **Preamble:**\n"
        "    - **Inherited:** None\n"
    )
    FOOTER = (
        "- **Aftermath:**\n"
        "    - **Inherited:** None\n"
    )

    def _stage(self, body):
        return self.preamble(self.HEADER + body + self.FOOTER)

    def test_fail_target_before_a_prompt_line_parses(self):
        stage = self._stage(
            "    - **Fail Target:** Recovery\n"
            "    > PROMPT\n")
        self.assertEqual(stage["fail_target"], "Recovery")
        self.assertIn("fail_target", stage["declared_fields"])

    def test_fail_target_after_a_hook_block_still_parses(self):
        stage = self._stage(
            "    - **Fail Target:** Recovery\n"
            "    - **preenter:**\n"
            "        - bash (required): exit 1\n"
            "    > PROMPT\n")
        self.assertEqual(stage["fail_target"], "Recovery")
        self.assertEqual(len(stage["hooks"]["preenter"]), 1)
        self.assertNotIn("preenter", stage["content"])

    def test_fail_target_on_the_next_line_still_parses(self):
        stage = self._stage(
            "    - **Fail Target:**\n"
            "        Recovery\n"
            "    > PROMPT\n")
        self.assertEqual(stage["fail_target"], "Recovery")

    def test_fail_target_before_another_bullet_still_parses(self):
        stage = self._stage(
            "    - **Fail Target:** Recovery\n"
            "    - **Max Turns:** 5\n"
            "    > PROMPT\n")
        self.assertEqual(stage["fail_target"], "Recovery")


class DocumentationContract(unittest.TestCase):
    """The reference docs must keep stating the parts a user cannot infer.

    Cheap guards, deliberately shallow: they exist so a future edit cannot drop
    the trust model or the timeout precedence silently.
    """

    def setUp(self):
        self.docs = (Path(yaha.__file__).resolve().parent
                     / "docs" / "hooks.md").read_text(encoding="utf-8")
        # The authoring/events documentation moved into the shipped
        # `docs/agentclass.md` when the template became include-based.
        self.template = agentclass_doc_text()

    def test_documentation_states_the_trust_model(self):
        self.assertIn("Trust model", self.docs)
        self.assertIn("bash_tool", self.docs)
        self.assertIn("untrusted shell", self.docs)
        self.assertIn("OUTSIDE the", self.template)

    def test_documentation_states_the_timeout_precedence(self):
        for text in (self.docs, self.template):
            self.assertIn("YAHA_HOOK_TIMEOUT", text)
            self.assertIn("precedence", text)


class DirectApiTests(unittest.TestCase):
    def test_fast_path_returns_the_identical_text(self):
        block = "- **Exit Tools:**\n    - success_tool\n> prompt\n"
        hooks, inherit, declared, cleaned = parse_stage_hooks(block, "s")
        self.assertEqual(cleaned, block)
        self.assertEqual(declared, set())
        self.assertEqual(hooks["preenter"], [])
        self.assertEqual(inherit, {name: False for name in HOOK_NAMES})

    def test_relabel_hooks_renumbers_diagnostics(self):
        specs = {"preexit": [
            {"kind": "bash", "code": "a", "index": 7, "label": "preexit[7]"},
            {"kind": "bash", "code": "b", "index": 9, "label": "preexit[9]"}]}
        out = relabel_hooks(specs)
        self.assertEqual([s["label"] for s in out["preexit"]],
                         ["preexit[1]", "preexit[2]"])
        self.assertEqual([s["index"] for s in out["preexit"]], [1, 2])


if __name__ == "__main__":
    unittest.main(verbosity=2)
