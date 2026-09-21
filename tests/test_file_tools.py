# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

import os
import shutil
import tempfile
import unittest

from yaha.tools.file_tools import read_file, write_file, edit_file


class TestFileToolsLineNumberCoercion(unittest.TestCase):
    """Tool-call args may arrive as strings (DSML parameters are always
    strings); line-number arguments must be coerced instead of raising
    'unsupported operand type(s)'."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="yaha_filetools_")
        self.path = os.path.join(self.dir, "f.txt")
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("one\ntwo\nthree\nfour\n")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_read_file_accepts_string_line_numbers(self):
        result = read_file(self.path, "2", "3")
        self.assertIn("LINES READ: 2", result)
        self.assertIn("two", result)
        self.assertIn("three", result)
        self.assertNotIn("one", result)

    def test_read_file_rejects_non_integer_line_numbers(self):
        result = read_file(self.path, "abc", 3)
        self.assertIn("start_line must be an integer", result)

    def test_write_file_accepts_string_start_line(self):
        result = write_file(self.path, "inserted\n", "2")
        with open(self.path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
        self.assertEqual(lines, ["one", "inserted", "two", "three", "four"])
        self.assertIn("+inserted", result)

    def test_edit_file_accepts_string_line_range(self):
        result = edit_file(self.path, start_line="2", end_line="3", replace_text="replaced\n")
        with open(self.path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
        self.assertEqual(lines, ["one", "replaced", "four"])
        self.assertIn("-two", result)


if __name__ == "__main__":
    unittest.main()
