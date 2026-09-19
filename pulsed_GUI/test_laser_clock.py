"""Offline checks of waveform constraints and the no-output default."""
import contextlib
import io
import unittest
from unittest.mock import patch

from laser_clock import clock_plan, main


class ClockTests(unittest.TestCase):
    def test_initial_and_target_rates(self):
        for rate, low, duty in [(1_000_000, 80, 0.2), (500_000, 180, 0.1)]:
            plan = clock_plan(rate, 200)
            self.assertEqual(plan["high_ticks"], 20)
            self.assertEqual(plan["low_ticks"], low)
            self.assertEqual(plan["realized_duty_cycle"], duty)
            self.assertEqual(plan["realized_nominal_frequency_hz"], rate)

    def test_quantization(self):
        plan = clock_plan(750_000, 204)
        self.assertEqual(plan["realized_high_ns"], 200)
        self.assertAlmostEqual(plan["realized_nominal_frequency_hz"], 100_000_000 / 133)

    def test_invalid_waveforms(self):
        for rate, high in [(0, 200), (float("nan"), 200), (float("inf"), 200),
                           (10_000_001, 200), (500_000, float("nan")),
                           (500_000, float("inf")), (500_000, -1),
                           (1_000_000, 310), (1_000_000, 10)]:
            with self.subTest(rate=rate, high=high), self.assertRaises(ValueError):
                clock_plan(rate, high)

    def test_duty_boundary(self):
        self.assertEqual(clock_plan(1_000_000, 300)["realized_duty_cycle"], 0.3)

    def test_default_does_not_run_hardware(self):
        with patch("laser_clock.run_clock") as run, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main([]), 0)
            run.assert_not_called()

    def test_invalid_duration_rejected_before_hardware(self):
        with patch("laser_clock.run_clock") as run, contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit):
            main(["--run", "--seconds", "nan"])
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
