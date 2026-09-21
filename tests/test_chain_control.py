#!/usr/bin/env python3
'''Pure unit tests for the prompt-chain control layer (yaha.core.chain).

No Agent, no UI, no LLM: `chain.py` is stdlib-only, so the walk/budget rules can
be pinned directly. Every constant and transition here is one the reviewer
verified by hand against the pre-image, so this file is the regression net for
the walk/budget extraction.
'''
import unittest

from yaha.core.chain import ChainBudget, ChainWalk, StageExit

CHAIN = [{'name': 'A'}, {'name': 'B'}, {'name': 'C'}]
DETACHED = {'D': {'name': 'D'}}


class StageExitTests(unittest.TestCase):
    def test_bare_statuses_are_not_mistaken_for_moves(self):
        for status in (StageExit.DONE, StageExit.LOOP, StageExit.RESTART,
                       StageExit.ERROR, StageExit.INTERRUPT_REWIND,
                       StageExit.FAIL):
            self.assertFalse(StageExit.is_exit_to(status), status)
            self.assertFalse(StageExit.is_fail(status), status)
            self.assertFalse(StageExit.is_interrupt_jump(status), status)

    def test_moves_carry_their_payload(self):
        self.assertTrue(StageExit.is_exit_to('EXIT_TO:Alpha'))
        self.assertEqual(StageExit.exit_to_target('EXIT_TO:Alpha'), 'Alpha')
        self.assertTrue(StageExit.is_fail('FAIL:aftermath'))
        self.assertEqual(StageExit.fail_target('FAIL:aftermath'), 'aftermath')
        self.assertTrue(StageExit.is_interrupt_jump('INTERRUPT_JUMP:B'))
        self.assertEqual(StageExit.interrupt_jump_target('INTERRUPT_JUMP:B'), 'B')

    def test_a_jump_prefix_is_not_a_fail(self):
        self.assertFalse(StageExit.is_fail('EXIT_TO:aftermath'))
        self.assertFalse(StageExit.is_exit_to('FAIL:aftermath'))


class ChainWalkTests(unittest.TestCase):
    def test_plain_advance(self):
        walk = ChainWalk(CHAIN)
        self.assertEqual(walk.stage['name'], 'A')
        self.assertEqual(walk.advance(), 1)
        self.assertFalse(walk.at_end())

    def test_advance_unwinds_a_call_to_the_recorded_return_index(self):
        walk = ChainWalk(CHAIN)
        walk.idx = 1
        self.assertIsNone(walk.push_call('D', DETACHED))
        self.assertTrue(walk.in_call)
        self.assertEqual(walk.stage['name'], 'D')
        self.assertEqual(walk.advance(), 2)
        self.assertFalse(walk.in_call)

    def test_nested_calls_unwind_to_the_one_recorded_return_index(self):
        walk = ChainWalk(CHAIN)
        walk.idx = 0
        self.assertIsNone(walk.push_call('D', DETACHED))
        self.assertIsNone(walk.push_call('D', DETACHED))
        self.assertEqual(walk.return_idx, 1)
        self.assertEqual(walk.depth, 2)
        self.assertEqual(walk.advance(), 1)
        self.assertEqual(walk.depth, 0)
        self.assertIsNone(walk.return_idx)

    def test_call_without_a_recorded_return_falls_back_to_the_current_index(self):
        walk = ChainWalk(CHAIN)
        walk.idx = 2
        walk.call_stage = {'name': 'D'}
        self.assertEqual(walk.advance(), 2)

    def test_call_depth_is_capped_without_mutating_the_frame(self):
        walk = ChainWalk(CHAIN)
        for _ in range(ChainWalk.CALL_DEPTH_CAP):
            self.assertIsNone(walk.push_call('D', DETACHED))
        self.assertEqual(walk.depth, ChainWalk.CALL_DEPTH_CAP)
        before = walk.call_stage
        self.assertEqual(
            walk.push_call('D', DETACHED),
            'Error: exit_to call depth exceeded (16); aborting the chain.')
        self.assertIs(walk.call_stage, before)

    def test_target_and_jump_lookups_are_case_insensitive(self):
        walk = ChainWalk(CHAIN)
        self.assertEqual(walk.target_index('b', 99), 1)
        self.assertEqual(walk.target_index('nope', 99), 99)
        self.assertIsNone(walk.target_index('nope'))
        self.assertEqual(walk.jump_index(' C '), 2)
        self.assertIsNone(walk.jump_index('nope'))
        self.assertIsNone(walk.jump_index(''))

    def test_runaway_guard_is_strictly_more_than_1000(self):
        walk = ChainWalk(CHAIN)
        for _ in range(ChainWalk.RUNAWAY_ENTRIES):
            self.assertIsNone(walk.enter())
        self.assertEqual(
            walk.enter(),
            'Error: the chain walk exceeded 1000 stage entries in one '
            'pass (runaway exit_to jump/call cycle); aborting.')

    def test_at_end_and_stage_on_an_empty_chain(self):
        walk = ChainWalk([])
        self.assertTrue(walk.at_end())
        self.assertIsNone(walk.stage)


class ChainBudgetTests(unittest.TestCase):
    def test_restart_cap_boundary_is_inclusive(self):
        budget = ChainBudget()
        for _ in range(ChainBudget.RESTART_CAP):
            budget.note_restart()
        self.assertEqual(budget.restart_count, ChainBudget.RESTART_CAP)
        self.assertTrue(budget.can_start_pass())
        budget.note_restart()
        self.assertFalse(budget.can_start_pass())

    def test_uncapped_loops_never_exhaust(self):
        budget = ChainBudget(-1)
        for _ in range(50):
            self.assertTrue(budget.charge_operation())
        self.assertFalse(budget.loops_exhausted())
        self.assertEqual(budget.loop_cap_label(), 'unlimited')

    def test_loop_cap_is_charged_once_per_operation(self):
        budget = ChainBudget(2)
        self.assertTrue(budget.charge_operation())     # 1
        self.assertTrue(budget.charge_operation())     # 2
        self.assertFalse(budget.charge_operation())    # 3 -> spent
        self.assertTrue(budget.loops_exhausted())
        self.assertEqual(budget.loop_cap_label(), '2')

    def test_terminal_answers(self):
        budget = ChainBudget(2)
        budget.charge_operation()
        budget.charge_operation()
        budget.note_restart()
        self.assertEqual(budget.exhausted_answer(),
                         'Error: Maximum chain restarts reached.')
        budget.charge_operation()
        self.assertEqual(budget.exhausted_answer(),
                         'Error: Maximum chain loops reached (2).')
        self.assertEqual(budget.loops_answer(),
                         'Error: Maximum chain loops reached (2).')


if __name__ == '__main__':
    unittest.main()