import unittest

from hardware.hopper import HopperGate, HopperState, StableWeight

CLOSED, OPEN = HopperState.CLOSED_WAITING, HopperState.OPEN_DISPENSING


class StableWeightTest(unittest.TestCase):
    def setUp(self):
        self.sw = StableWeight(samples=5, tolerance_kg=0.03)

    def test_needs_full_window(self):
        for _ in range(4):
            self.assertIsNone(self.sw.update(2.00))
        self.assertEqual(self.sw.update(2.00), 2.00)

    def test_reports_mean_when_within_tolerance(self):
        for w in (2.00, 2.01, 2.02, 2.00, 2.02):
            result = self.sw.update(w)
        self.assertAlmostEqual(result, 2.01)

    def test_spread_over_tolerance_is_unstable(self):
        for w in (2.00, 2.01, 2.02, 2.00):
            self.sw.update(w)
        self.assertIsNone(self.sw.update(2.04))  # spread 0.04 > 0.03

    def test_exactly_at_tolerance_is_stable(self):
        for w in (2.00, 2.00, 2.00, 2.00):
            self.sw.update(w)
        self.assertIsNotNone(self.sw.update(2.03))

    def test_none_reading_breaks_consecutive_run(self):
        for _ in range(4):
            self.sw.update(2.00)
        self.assertIsNone(self.sw.update(None))
        self.assertIsNone(self.sw.update(2.00))  # window restarted
        for _ in range(3):
            self.sw.update(2.00)
        self.assertEqual(self.sw.update(2.00), 2.00)

    def test_slides_past_a_transient(self):
        for w in (2.00, 2.00, 5.00, 2.00, 2.00):
            self.sw.update(w)
        self.assertIsNone(self.sw.update(2.00))  # spike still in window
        self.assertIsNone(self.sw.update(2.00))
        self.assertEqual(self.sw.update(2.00), 2.00)  # spike has fallen out


class HopperGateTest(unittest.TestCase):
    def setUp(self):
        self.gate = HopperGate(target_kg=2.0, empty_kg=0.05, jam_after_s=30.0)
        self.t = 0.0

    def feed(self, weight, n=1, dt=0.1):
        for _ in range(n):
            self.t += dt
            result = self.gate.update(weight, now=self.t)
        return result

    def test_starts_closed(self):
        self.assertIs(self.gate.state, CLOSED)
        self.assertFalse(self.gate.gate_open)

    def test_does_not_open_below_target(self):
        self.assertFalse(self.feed(1.99, n=10))

    def test_opens_at_target_once_stable(self):
        self.assertFalse(self.feed(2.0, n=4))
        self.assertTrue(self.feed(2.0))
        self.assertEqual(self.gate.opened_at, self.t)

    def test_single_spike_does_not_open(self):
        self.feed(1.0, n=5)
        self.assertFalse(self.feed(2.5))
        self.assertFalse(self.feed(1.0, n=5))

    def test_settling_readings_do_not_open(self):
        # weight climbing as copra is poured in: never 5-in-a-row within 0.03
        for w in (1.80, 1.90, 1.98, 2.05, 2.10, 2.16, 2.20):
            self.assertFalse(self.feed(w))
        self.assertTrue(self.feed(2.20, n=4))

    def test_stays_open_while_flowing(self):
        self.feed(2.0, n=5)
        for w in (1.8, 1.5, 1.1, 0.7, 0.3, 0.1):
            self.assertTrue(self.feed(w))

    def test_single_low_reading_does_not_close(self):
        self.feed(2.0, n=5)
        self.assertTrue(self.feed(0.0))
        self.assertTrue(self.feed(1.0))

    def test_closes_once_stable_at_empty(self):
        self.feed(2.0, n=5)
        self.assertTrue(self.feed(0.05, n=4))
        self.assertFalse(self.feed(0.05))
        self.assertIs(self.gate.state, CLOSED)
        self.assertIsNone(self.gate.opened_at)

    def test_jam_after_timeout_keeps_gate_open(self):
        self.feed(2.0, n=5)
        self.feed(1.5, n=299)  # 29.9 s open, stuck at 1.5 kg
        self.assertFalse(self.gate.jammed)
        self.assertTrue(self.feed(1.5))  # 30.0 s
        self.assertTrue(self.gate.jammed)
        self.assertTrue(self.gate.gate_open)

    def test_no_jam_when_it_empties_in_time(self):
        self.feed(2.0, n=5)
        self.feed(1.0, n=50)
        self.feed(0.02, n=5)
        self.assertFalse(self.gate.jammed)
        self.assertFalse(self.gate.gate_open)

    def test_jam_clears_when_finally_empty(self):
        self.feed(2.0, n=5)
        self.feed(1.5, n=400)
        self.assertTrue(self.gate.jammed)
        self.feed(0.01, n=5)
        self.assertFalse(self.gate.jammed)
        self.assertIs(self.gate.state, CLOSED)

    def test_sensor_fault_while_open_holds_gate_open(self):
        self.feed(2.0, n=5)
        self.assertTrue(self.feed(None, n=20))

    def test_sensor_fault_while_closed_holds_gate_closed(self):
        self.assertFalse(self.feed(None, n=20))

    def test_live_target_change_takes_effect(self):
        self.feed(1.5, n=5)
        self.assertFalse(self.gate.gate_open)
        self.gate.target_kg = 1.4
        self.assertTrue(self.feed(1.5))

    def test_two_full_cycles(self):
        for _ in range(2):
            self.assertTrue(self.feed(2.0, n=5))
            self.feed(1.0, n=30)
            self.assertFalse(self.feed(0.0, n=5))
        self.assertFalse(self.gate.jammed)

    def test_rejects_empty_at_or_above_target(self):
        with self.assertRaises(ValueError):
            HopperGate(target_kg=0.05, empty_kg=0.05)

    def test_rejects_live_target_at_or_below_empty(self):
        with self.assertRaises(ValueError):
            self.gate.target_kg = 0.05


if __name__ == "__main__":
    unittest.main()
