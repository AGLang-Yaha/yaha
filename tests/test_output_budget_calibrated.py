"""M1 guard: the tool-output budget must convert tokens to bytes at the CALIBRATED
density, while the historical default (4 chars/token) stays byte-for-byte what the
existing assertions pin.
"""
import unittest

from yaha.tools.output_guard import (CHARS_PER_TOKEN, MIN_OUTPUT_BUDGET_BYTES,
                                     compute_output_budget)


class TestCalibratedBudget(unittest.TestCase):
    def test_default_parameter_keeps_the_pinned_history(self):
        # The pre-existing assertions in test_output_guard.py must survive untouched.
        self.assertEqual(compute_output_budget(128000, 28000), 200000)
        self.assertEqual(compute_output_budget(128000, 0), 256000)
        self.assertEqual(compute_output_budget(128000, 28000, chars_per_token=CHARS_PER_TOKEN),
                         200000)

    def test_calibrated_density_halves_the_FREE_WINDOW_IN_TOKENS(self):
        # 128000 - 28000 = 100000 free tokens. At the measured ~3.22 chars/token the
        # budget must be about half of that in TOKENS, i.e. 50000 * 3.22 bytes.
        density = 3.22
        budget_bytes = compute_output_budget(128000, 28000, chars_per_token=density)
        self.assertAlmostEqual(budget_bytes / density, 50000, delta=50)
        # and it must be SMALLER than the nominal-4 budget, i.e. less generous
        self.assertLess(budget_bytes, compute_output_budget(128000, 28000))

    def test_degenerate_density_falls_back_to_the_default(self):
        for bad in (0, -1.0):
            self.assertEqual(compute_output_budget(128000, 28000, chars_per_token=bad),
                             compute_output_budget(128000, 28000),
                             'a non-positive density must not produce a nonsense budget')

    def test_floor_still_applies(self):
        self.assertEqual(compute_output_budget(1000, 999, chars_per_token=3.22),
                         compute_output_budget(1000, 999))
        self.assertGreaterEqual(compute_output_budget(0, 0, chars_per_token=3.22),
                                MIN_OUTPUT_BUDGET_BYTES)


if __name__ == '__main__':
    unittest.main()
