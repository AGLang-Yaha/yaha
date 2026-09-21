#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

'''TUI multi-agent labelling: per-agent tags and the runtime status suffix.

Section headers are unchanged for single-agent runs (so existing output stays
identical) and gain a [tag] prefix plus an 'agents N | active: ...' suffix only
when more than one agent is live.
'''
import contextlib
import io
import unittest

from yaha.ui.terminal import (
    YAHAUI,
    set_multi_agent_state,
    multi_agent_suffix,
    reset_multi_agent_state,
)


def _reset():
    set_multi_agent_state(live=1, active=None, stage=None)


class SuffixTests(unittest.TestCase):
    def tearDown(self):
        _reset()

    def test_no_suffix_for_single_agent(self):
        set_multi_agent_state(live=1, active='main', stage='Preamble')
        self.assertEqual(multi_agent_suffix(), '')

    def test_suffix_lists_live_count_active_and_stage(self):
        set_multi_agent_state(live=3, active='w2', stage='Coding')
        self.assertEqual(multi_agent_suffix(), 'agents 3 | active: w2 (Coding)')

    def test_suffix_without_stage(self):
        set_multi_agent_state(live=2, active='w1', stage='')
        self.assertEqual(multi_agent_suffix(), 'agents 2 | active: w1')


class TagTests(unittest.TestCase):
    def tearDown(self):
        _reset()

    def test_single_agent_header_has_no_tag(self):
        ui = YAHAUI()
        ui.set_agent_tag('main')
        ui.set_stage_name('Preamble')
        set_multi_agent_state(live=1, active='main', stage='Preamble')
        self.assertEqual(ui._subject_label('BaseAgent', 'abcd', 'Question'),
                         'BaseAgent (abcd) : Preamble : Question')

    def test_multi_agent_header_prefixes_tag(self):
        ui = YAHAUI()
        ui.set_agent_tag('w2')
        ui.set_stage_name('Coding')
        set_multi_agent_state(live=3, active='w2', stage='Coding')
        self.assertEqual(ui._subject_label('BaseAgent', 'abcd', 'Question'),
                         '[w2] BaseAgent (abcd) : Coding : Question')

    def test_set_active_agent_publishes_runtime_state(self):
        ui = YAHAUI()
        ui.set_active_agent('w5', 'w5', 'Plan', 4)
        suffix = multi_agent_suffix()
        self.assertIn('agents 4', suffix)
        self.assertIn('w5', suffix)
        self.assertIn('Plan', suffix)

    def test_section_rule_includes_runtime_suffix(self):
        ui = YAHAUI()
        set_multi_agent_state(live=3, active='w2', stage='Coding')
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ui._section_rule('Header', 'bold')
        out = buf.getvalue()
        self.assertIn('Header', out)
        self.assertIn('agents 3', out)



def _t_reset_clears_suffix(self):
    set_multi_agent_state(live=3, active='w2', stage='Coding')
    self.assertNotEqual(multi_agent_suffix(), '')
    reset_multi_agent_state()
    self.assertEqual(multi_agent_suffix(), '')
    set_multi_agent_state(live=2, active='x')
    self.assertEqual(multi_agent_suffix(), 'agents 2 | active: x')


def _t_reset_makes_single_agent_headers_untagged(self):
    ui = YAHAUI()
    ui.set_agent_tag('w2')
    ui.set_stage_name('Coding')
    set_multi_agent_state(live=3, active='w2', stage='Coding')
    self.assertTrue(ui._subject_label('A', 'b', 'Q').startswith('[w2]'))
    reset_multi_agent_state()
    self.assertEqual(ui._subject_label('A', 'b', 'Q'), 'A (b) : Coding : Q')


SuffixTests.test_reset_clears_suffix = _t_reset_clears_suffix
TagTests.test_reset_makes_single_agent_headers_untagged = _t_reset_makes_single_agent_headers_untagged


if __name__ == '__main__':
    unittest.main()
