#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

"""Guards the AgentClass authoring rule: no bare free-style text.

The class template is INCLUDE-BASED (`templates/AgentClass.md` is a small root
that pulls the classes in with `<!--include path="..." -->`), so the rule is
checked on EVERY file of the include closure, not just the root:

* every non-blank line of every closure file must be a heading, a
  `- **Field:**` bullet, a `- item` bullet, a `>`-quoted prompt line, an
  indented continuation of one of those, or a formal directive
  (`include`, `define`, `enddefine`, `ref`); the SPDX header sits inside a
  `<!--comment--> ... <!--endcomment-->` block, so it is stripped before the
  check;
* comment regions must be well formed in every closure file (balanced, opener
  before closer) - `_strip_comments` itself rejects an unterminated block;
* the resolved template must still parse into the shipped classes, crew
  included.

The include closure and the resolved text come from `_template_fixture`, the
single place that knows the layout, so a future template split cannot silently
stale this guard out again.
"""
import re
import unittest

from _template_fixture import (TEMPLATE, include_closure, strip_comments,
                               template_text)
from yaha.config.agents import parse_agent_class_md

NL = chr(10)
OPEN_RE = re.compile(r"<!-- *comment *-->", re.IGNORECASE)
CLOSE_RE = re.compile(r"<!-- *endcomment *-->", re.IGNORECASE)
DIRECTIVE_RE = re.compile(r"^<!-- *\s*(include|define|enddefine|ref)\b",
                          re.IGNORECASE)
FIELD_BULLET_RE = re.compile(r"^- \*\*[^*]+:\*\*")
ITEM_BULLET_RE = re.compile(r"^- \S")


def _closure_files():
    files = include_closure()
    assert files, "template include closure is empty"
    return files


def _bad_lines(path):
    raw = path.read_text(encoding="utf-8")
    lines = strip_comments(raw, str(path)).split(NL)
    bad = []
    for i, line in enumerate(lines, 1):
        if not line.strip():
            continue
        if line.startswith("#"):
            continue
        if line.startswith(">"):
            continue
        if FIELD_BULLET_RE.match(line) or ITEM_BULLET_RE.match(line):
            continue
        if line.startswith(chr(9)):
            continue
        if DIRECTIVE_RE.match(line) or DIRECTIVE_RE.match(line.lstrip()):
            continue
        if line.startswith("    ") and line.lstrip().startswith(("-", ">")):
            continue
        bad.append((i, line))
    return bad


class TestTemplateHygiene(unittest.TestCase):
    def test_no_bare_freestyle_text_in_any_included_file(self):
        for path in _closure_files():
            bad = _bad_lines(path)
            self.assertFalse(
                bad, "bare free-style text in %s (outside comment blocks):%s%s"
                % (path.name, NL,
                   NL.join("  %d: %s" % (n, t) for n, t in bad)))

    def test_comment_regions_are_well_formed_in_every_included_file(self):
        for path in _closure_files():
            raw = path.read_text(encoding="utf-8")
            openers = [i for i, l in enumerate(raw.split(NL)) if OPEN_RE.search(l)]
            closers = [i for i, l in enumerate(raw.split(NL)) if CLOSE_RE.search(l)]
            self.assertEqual(len(openers), len(closers),
                             "unbalanced comment block in " + path.name)
            for opener, closer in zip(openers, closers):
                self.assertLess(opener, closer,
                                "comment closes before it opens in " + path.name)
            # The loader's own rule is the authority: an unterminated block raises.
            strip_comments(raw, str(path))

    def test_root_file_is_a_pure_include_index(self):
        raw = TEMPLATE.read_text(encoding="utf-8")
        for i, line in enumerate(strip_comments(raw, str(TEMPLATE)).split(NL), 1):
            if not line.strip():
                continue
            # `- item` bullets are allowed (an index may carry SPDX-ish notes in
            # a comment, which are stripped); prose is not.
            self.assertTrue(
                DIRECTIVE_RE.match(line.lstrip()) or ITEM_BULLET_RE.match(line),
                "root template line %d is not a directive: %r" % (i, line))

    def test_crew_classes_still_contract_clean(self):
        classes = parse_agent_class_md(str(TEMPLATE))
        for name in ("Crewlda", "CrewCda", "CrewRva"):
            self.assertIn(name, classes)
            self.assertTrue(classes[name].prompt_chain,
                            name + " has an empty prompt chain")
        lead = classes["Crewlda"]
        self.assertIn("CrewCda", lead.agentset)
        self.assertIn("CrewRva", lead.agentset)

    def test_resolved_template_exposes_every_shipped_class(self):
        text = template_text()
        for name in ("BaseAgent", "CodeAgent", "CodeReviewer", "ChatAgent",
                     "Crewlda", "CrewCda", "CrewRva"):
            self.assertIn("## " + name, text)


if __name__ == "__main__":
    unittest.main()
