# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

import os
import tempfile
import unittest
from pathlib import Path

from yaha.config.agents import parse_agent_class_md
from yaha.core.md_loader import (
    load_md_with_includes,
    load_md_with_macros,
)


class _TempFixtureMixin:
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="yaha_md_loader_macros_"))
        self._n = 0

    def tearDown(self):
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    def write(self, content, suffix=".md"):
        self._n += 1
        p = self._tmp / f"mac_{self._n}{suffix}"
        p.write_text(content, encoding="utf-8")
        return p


class TestMacroLoadBasics(_TempFixtureMixin, unittest.TestCase):
    def test_basic_macro_expansion_and_define_removed(self):
        p = self.write(
            '<!-- define name="greet" -->Hello world<!-- enddefine -->\n\n'
            'before\n<!--ref name="greet" -->\nafter\n'
        )
        out = load_md_with_macros(str(p))
        self.assertIn("Hello world", out)
        self.assertIn("before", out)
        self.assertIn("after", out)
        self.assertNotIn("<!-- define", out)
        self.assertNotIn("<!-- enddefine", out)
        self.assertNotIn("<!--ref", out)

    def test_macro_param_substitution(self):
        p = self.write(
            '<!-- define name="banner", params= ["who", "org"] -->\n'
            "## {{1}} @ {{2}}\n"
            "<!-- enddefine -->\n"
            '<!--ref name="banner", params= ["Alice", "Acme"] -->\n'
        )
        out = load_md_with_macros(str(p))
        self.assertIn("## Alice @ Acme", out)

    def test_macro_defined_in_included_file_visible_to_root(self):
        inc = self.write(
            '<!-- define name="url", params= ["site"] -->\n'
            "https://{{1}}.example.com\n"
            "<!-- enddefine -->\n",
            suffix="-inc.md",
        )
        root = self.write(f'<!-- include path="{inc.name}" -->\n'
                         '<!--ref name="url", params= ["api"] -->\n')
        out = load_md_with_macros(str(root))
        self.assertIn("https://api.example.com", out)
        self.assertNotIn("<!-- define", out)

    def test_multiline_macro_inside_blockquote_preserves_lines(self):
        # Regression: body inside a `>` blockquote must keep each line instead
        # of collapsing into one long line / swallowing newlines.
        content = (
            '<!-- define name="proto" -->\n'
            "line one\n"
            "line two\n"
            "line three\n"
            "<!-- enddefine -->\n"
            "\n"
            "## Class\n"
            "- **System Prompt:**\n"
            '    > <!--ref name="proto" -->\n'
        )
        out = load_md_with_macros(str(self.write(content)))
        lines = [ln for ln in out.splitlines() if "line" in ln]
        self.assertEqual(lines, ["    > line one", "    > line two", "    > line three"])

    def test_nested_macro_expansion(self):
        p = self.write(
            '<!-- define name="inner" -->INNER<!-- enddefine -->\n'
            '<!-- define name="outer" -->[<!--ref name="inner" -->]<!-- enddefine -->\n'
            '<!--ref name="outer" -->\n'
        )
        out = load_md_with_macros(str(p))
        self.assertIn("[INNER]", out)

    def test_agent_class_parse_consumes_macros(self):
        inc = self.write(
            '<!-- define name="suffix" -->- use the right tone<!-- enddefine -->\n',
            suffix="-agents.md",
        )
        p = self.write(
            '<!-- define name="intro", params= ["n"] -->\n'
            "You are the {{1}} of this workspace.\n"
            "<!-- enddefine -->\n"
            f'<!-- include path="{inc.name}" -->\n'
            "\n"
            "## MyAgent\n"
            "- **Inherited:** None\n"
            "- **Code:** Mya\n"
            "- **System Prompt:**\n"
            "- **Preamble:**\n"
            '    > <!--ref name="intro", params= ["runner"] -->\n'
            "- **Aftermath:**\n"
            '    > <!--ref name="suffix" -->\n'
        )
        classes = parse_agent_class_md(str(p))
        self.assertIn("MyAgent", classes)
        chain = classes["MyAgent"].prompt_chain
        pre = next(s for s in chain if s["name"] == "Preamble")
        self.assertIn("You are the runner of this workspace.", pre["content"])
        aft = next(s for s in chain if s["name"] == "Aftermath")
        self.assertIn("- use the right tone", aft["content"])

    def test_load_md_with_includes_leaves_macros_raw(self):
        p = self.write('<!-- define name="x" -->body<!-- enddefine -->\n<!--ref name="x" -->\n')
        out = load_md_with_includes(str(p))
        self.assertIn("<!--ref", out)
        self.assertIn("<!-- define", out)


if __name__ == "__main__":
    unittest.main()
