# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

"""AgentClass `Compression Keep Steps` parsing and inheritance."""
import os
import shutil
import tempfile
import unittest

from yaha.config.agents import parse_agent_class_md


FIXTURE = """
## Parent
- **Inherited:** None
- **Code:** Par
- **Compression Keep Steps:** 4
- **Preamble:**
    > parent preamble

## ChildOmit
- **Inherited:** Parent
- **Code:** Cho
- **Preamble:**
    - **Inherited:** All
    > child preamble

## ChildOverride
- **Inherited:** Parent
- **Code:** Chv
- **Compression Keep Steps:** 7
- **Preamble:**
    - **Inherited:** All
    > child preamble
"""


class TestCompressionKeepSteps(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='yaha_keep_steps_')
        self.path = os.path.join(self.tmp, 'AgentClass.md')
        with open(self.path, 'w', encoding='utf-8') as f:
            f.write(FIXTURE)
        self.classes = parse_agent_class_md(self.path)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_parsed_value(self):
        self.assertEqual(self.classes['Parent'].compression_keep_steps, 4)

    def test_inherits_from_parent(self):
        self.assertEqual(self.classes['ChildOmit'].compression_keep_steps, 4)

    def test_explicit_value_overrides_parent(self):
        self.assertEqual(self.classes['ChildOverride'].compression_keep_steps, 7)

    def test_absent_key_is_none(self):
        tmp = tempfile.mkdtemp(prefix='yaha_keep_steps_bare_')
        try:
            path = os.path.join(tmp, 'AgentClass.md')
            with open(path, 'w', encoding='utf-8') as f:
                f.write('\n## Bare\n- **Inherited:** None\n- **Code:** Bar\n- **Preamble:**\n    > hi\n')
            classes = parse_agent_class_md(path)
            self.assertIsNone(classes['Bare'].compression_keep_steps)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()