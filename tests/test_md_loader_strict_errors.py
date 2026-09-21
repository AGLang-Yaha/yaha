# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

import shutil
import tempfile
import unittest
from pathlib import Path

from yaha.core.md_loader import (
    MarkdownLoadError,
    load_md_with_includes,
    load_md_with_macros,
)


class _TempFixtureMixin:
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="yaha_md_loader_strict_"))
        self._n = 0

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def write(self, content, suffix=".md"):
        self._n += 1
        p = self._tmp / f"strict_{self._n}{suffix}"
        p.write_text(content, encoding="utf-8")
        return p


class TestStrictMarkdownErrors(_TempFixtureMixin, unittest.TestCase):
    def test_unknown_macro_raises(self):
        p = self.write('<!--ref name="nope", params= ["a"] -->')
        with self.assertRaisesRegex(MarkdownLoadError, "unknown macro.*nope"):
            load_md_with_macros(str(p))

    def test_missing_include_raises_in_includes_loader(self):
        p = self.write('<!-- include path="no-such-file.md" -->\ntext\n')
        with self.assertRaisesRegex(MarkdownLoadError, "not found"):
            load_md_with_includes(str(p))

    def test_missing_include_raises_in_macros_loader(self):
        p = self.write('<!-- include path="no-such-file.md" -->\ntext\n')
        with self.assertRaisesRegex(MarkdownLoadError, "not found"):
            load_md_with_macros(str(p))

    def test_circular_include_raises(self):
        a = self.write("", suffix="-a.md")
        b = self.write("", suffix="-b.md")
        a.write_text(f'<!-- include path="{b.name}" -->', encoding="utf-8")
        b.write_text(f'<!-- include path="{a.name}" -->', encoding="utf-8")
        with self.assertRaisesRegex(MarkdownLoadError, "circular"):
            load_md_with_includes(str(a))

    def test_duplicate_macro_definition_raises(self):
        p = self.write(
            '<!-- define name="x" -->one<!-- enddefine -->\n'
            '<!-- define name="x" -->two<!-- enddefine -->\n'
            '<!--ref name="x" -->\n'
        )
        with self.assertRaisesRegex(MarkdownLoadError, "duplicate macro"):
            load_md_with_macros(str(p))

    def test_recursive_depth_guard_raises(self):
        p = self.write(
            '<!-- define name="loop" -->a<!--ref name="loop" -->b<!-- enddefine -->\n'
            '<!--ref name="loop" -->\n'
        )
        with self.assertRaisesRegex(MarkdownLoadError, "depth"):
            load_md_with_macros(str(p))


if __name__ == "__main__":
    unittest.main()
