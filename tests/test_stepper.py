import unittest

from hardware.stepper import MAX_HZ, pulse_timing, steps_done


class PulseTimingTest(unittest.TestCase):
    def test_even_period_splits_in_half(self):
        self.assertEqual(pulse_timing(1000), (500, 500))

    def test_odd_period_keeps_full_length(self):
        on, off = pulse_timing(3000)          # 333 us period
        self.assertEqual(on + off, 333)
        self.assertLessEqual(abs(on - off), 1)

    def test_rejects_zero_negative_and_too_fast(self):
        for hz in (0, -100, MAX_HZ + 1):
            with self.assertRaises(ValueError):
                pulse_timing(hz)

    def test_max_rate_still_well_above_driver_minimum_pulse(self):
        on, off = pulse_timing(MAX_HZ)
        self.assertGreaterEqual(min(on, off), 3)   # TB6600 / HBS57H want >= 2.5 us


class StepsDoneTest(unittest.TestCase):
    def test_counts_by_time(self):
        self.assertEqual(steps_done(0.5, 400, 1000), 200)

    def test_never_exceeds_request(self):
        self.assertEqual(steps_done(10.0, 400, 1000), 1000)

    def test_never_negative(self):
        self.assertEqual(steps_done(-0.1, 400, 1000), 0)


if __name__ == "__main__":
    unittest.main()
