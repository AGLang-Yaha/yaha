#!/usr/bin/env python3
"""Tests for the <!--comment--> ... <!--endcomment--> block directive in md_loader.

A comment block is author-facing only. It is stripped while the file is read -
before define registration, include inlining and macro expansion - so a directive
parked inside a comment is INERT and comment text never reaches loaded content.
Malformed markers fail fast with MarkdownLoadError.
"""
import shutil
import tempfile
import unittest
from pathlib import Path

import yaha
from yaha.config.agents import parse_agent_class_md
from yaha.core.md_loader import (
    MarkdownLoadError,
    load_md_with_includes,
    load_md_with_macros,
)

TEMPLATE = Path(yaha.__file__).resolve().parent / "templates" / "AgentClass.md"

NL = chr(10)
OPEN = "<!--" + "comment" + "-->"
CLOSE = "<!--" + "endcomment" + "-->"


class _TempFixtureMixin:
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="yaha_md_comments_"))
        self._n = 0

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def write(self, content, suffix=".md"):
        self._n += 1
        p = self._tmp / f"cmt_{self._n}{suffix}"
        p.write_text(content, encoding="utf-8")
        return p


class TestCommentStripping(_TempFixtureMixin, unittest.TestCase):
    def test_comment_block_is_stripped(self):
        p = self.write(NL.join(["before", OPEN, "hidden text", CLOSE, "after", ""]))
        out = load_md_with_macros(str(p))
        self.assertIn("before", out)
        self.assertIn("after", out)
        self.assertNotIn("hidden text", out)
        self.assertNotIn(OPEN, out)
        self.assertNotIn(CLOSE, out)

    def test_comment_preserves_line_numbering(self):
        src = NL.join(["line1", OPEN, "removed1", "removed2", CLOSE, "line6", ""])
        p = self.write(src)
        lines = load_md_with_macros(str(p)).split(NL)
        self.assertEqual(lines[0], "line1")
        # A stripped block keeps the line count, so line6 stays on line 6.
        self.assertEqual(lines[5], "line6")
        self.assertEqual(len(lines), len(src.split(NL)))

    def test_inline_single_line_comment_does_not_glue_neighbours(self):
        p = self.write("a" + OPEN + "x" + CLOSE + "b" + NL)
        out = load_md_with_macros(str(p))
        self.assertTrue(out.startswith("ab"), repr(out))

    def test_comment_preamble_creates_no_class(self):
        p = self.write(NL.join([
            OPEN,
            "## NotAClass",
            "- **Code:** Nah",
            CLOSE,
            "",
            "## Real",
            "- **Code:** Real",
            "- **Description:** the real one",
            "",
            "- **Step:**",
            "    > do the thing",
            "",
        ]))
        classes = parse_agent_class_md(str(p))
        self.assertIn("Real", classes)
        # A class heading commented out must not become a real class.
        self.assertNotIn("NotAClass", classes)

    def test_shipped_template_loads_after_comment_wrap(self):
        classes = parse_agent_class_md(str(TEMPLATE))
        for name in ("CodeAgent", "CodeReviewer", "Crewlda", "CrewCda", "CrewRva"):
            self.assertIn(name, classes)
        self.assertNotIn("Multi-Agent Stages (patch 1)", classes)


class TestCommentedOutDirectivesAreInert(_TempFixtureMixin, unittest.TestCase):
    def test_commented_out_ref_is_inert(self):
        p = self.write(NL.join([OPEN, '<!--ref name="nope" -->', CLOSE, "kept", ""]))
        out = load_md_with_macros(str(p))
        self.assertIn("kept", out)
        self.assertNotIn("nope", out)

    def test_commented_out_define_is_not_registered(self):
        # If the commented define were registered it would collide with the live
        # define of the same name and raise a duplicate-macro error.
        p = self.write(NL.join([
            OPEN,
            '<!-- define name="x" -->one<!-- enddefine -->',
            CLOSE,
            '<!-- define name="x" -->two<!-- enddefine -->',
            '<!--ref name="x" -->',
            "",
        ]))
        out = load_md_with_macros(str(p))
        self.assertIn("two", out)
        self.assertNotIn("one", out)

    def test_commented_out_include_is_inert(self):
        p = self.write(NL.join([
            OPEN,
            '<!-- include path="no-such-file-xyz.md" -->',
            CLOSE,
            "kept",
            "",
        ]))
        out = load_md_with_macros(str(p))
        self.assertIn("kept", out)

    def test_comments_stripped_by_includes_loader(self):
        p = self.write(NL.join([
            OPEN,
            "hidden",
            CLOSE,
            '<!-- define name="x" -->live<!-- enddefine -->',
            "kept",
            "",
        ]))
        out = load_md_with_includes(str(p))
        self.assertNotIn("hidden", out)
        self.assertIn("kept", out)
        # This loader stays macro-inert.
        self.assertIn("<!-- define", out)


class TestMalformedCommentsFailFast(_TempFixtureMixin, unittest.TestCase):
    def test_unterminated_comment_raises(self):
        p = self.write(NL.join(["head", OPEN, "body never closed", ""]))
        with self.assertRaises(MarkdownLoadError) as ctx:
            load_md_with_macros(str(p))
        msg = str(ctx.exception)
        self.assertIn("unterminated", msg)
        self.assertIn(str(p), msg)
        self.assertTrue(msg.startswith("<!--"), msg)

    def test_stray_endcomment_raises(self):
        p = self.write(NL.join(["head", CLOSE, "tail", ""]))
        with self.assertRaises(MarkdownLoadError) as ctx:
            load_md_with_macros(str(p))
        msg = str(ctx.exception)
        self.assertIn("endcomment", msg)
        self.assertIn(str(p), msg)


if __name__ == "__main__":
    unittest.main()