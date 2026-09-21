# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

"""B5: the characters-to-tokens calibration must track observations and must model a
constant per-request framing term instead of folding it into density (review F4).
"""
import unittest

from yaha.core.context_calibration import (ContextCalibration, DEFAULT_TOKENS_PER_CHAR,
                                           MIN_TOKENS_PER_CHAR, MAX_TOKENS_PER_CHAR)


class TestContextCalibration(unittest.TestCase):
    def test_cold_start_is_four_chars_per_token(self):
        cal = ContextCalibration()
        self.assertAlmostEqual(cal.tokens_per_char(), DEFAULT_TOKENS_PER_CHAR, places=6)
        self.assertEqual(cal.predict(4000), 1000)

    def test_two_observations_are_reproduced(self):
        cal = ContextCalibration()
        cal.observe(1000, 250)
        cal.observe(4000, 1000)
        self.assertAlmostEqual(cal.predict(1000), 250, delta=5)
        self.assertAlmostEqual(cal.predict(4000), 1000, delta=20)

    def test_constant_framing_is_not_modelled_as_density(self):
        # slope 0.2 tokens/char with a constant 200-token framing offset
        cal = ContextCalibration()
        cal.observe(1000, 400)
        cal.observe(4000, 1000)
        self.assertAlmostEqual(cal.predict(2000), 600, delta=40)
        # a pure 0.25-tokens/char density model would answer 500
        self.assertGreater(cal.predict(2000), 560)

    def test_clamps_keep_the_rate_physical(self):
        high = ContextCalibration()
        for _ in range(3):
            high.observe(100, 100000)
        self.assertLessEqual(high.tokens_per_char(), MAX_TOKENS_PER_CHAR + 1e-9)

        low = ContextCalibration()
        for _ in range(3):
            low.observe(100000, 1)
        self.assertGreaterEqual(low.tokens_per_char(), MIN_TOKENS_PER_CHAR - 1e-9)

    def test_predict_never_negative(self):
        cal = ContextCalibration()
        self.assertGreaterEqual(cal.predict(0), 0)
        self.assertGreaterEqual(cal.predict(-5), 0)

    def test_observations_are_bounded(self):
        cal = ContextCalibration(window=3)
        for i in range(10):
            cal.observe(1000 + i, 250)
        self.assertLessEqual(len(cal.observations), 3)


if __name__ == '__main__':
    unittest.main()