import math
import unittest

from hardware.drying import DryingEndpoint, absolute_humidity

MIN = 60
HOUR = 3600


def synthetic_curve(t: float, start=24.0, peak=55.0, rise_s=30 * MIN, tau_s=HOUR, floor=25.0) -> float:
    """Ambient -> linear rise to peak -> exponential decay toward the inlet level."""
    if t <= rise_s:
        return start + (peak - start) * t / rise_s
    return floor + (peak - floor) * math.exp(-(t - rise_s) / tau_s)


class AbsoluteHumidityTest(unittest.TestCase):
    def test_known_values(self):
        self.assertAlmostEqual(absolute_humidity(25.0, 100.0), 23.0, delta=0.3)
        self.assertAlmostEqual(absolute_humidity(0.0, 100.0), 4.8, delta=0.2)
        self.assertAlmostEqual(absolute_humidity(30.0, 80.0), 24.2, delta=0.3)

    def test_temperature_independence_of_the_point(self):
        # same water content reported at two chamber temps gives the same AH
        ah = absolute_humidity(65.0, 20.0)
        hot_rh = 20.0 * absolute_humidity(65.0, 100.0) / absolute_humidity(70.0, 100.0)
        self.assertAlmostEqual(absolute_humidity(70.0, hot_rh), ah, places=6)

    def test_dry_air_is_zero(self):
        self.assertEqual(absolute_humidity(65.0, 0.0), 0.0)


class DryingEndpointTest(unittest.TestCase):
    def setUp(self):
        self.ep = DryingEndpoint(min_time_s=30 * MIN, max_time_s=8 * HOUR, window_s=10 * MIN,
                                 flat_slope=0.05, recovery_fraction=0.8, min_rise=2.0, fault_after_s=120)
        self.ep.start(0.0)

    def run_curve(self, until_s, step_s=30, curve=synthetic_curve):
        """Feed the curve as (temp, rh) pairs that reproduce its AH at 65 C; return time of done or None."""
        t = 0.0
        while t <= until_s:
            ah = curve(t)
            rh = 100.0 * ah / absolute_humidity(65.0, 100.0)
            if self.ep.update(65.0, rh, t):
                return t
            t += step_s
        return None

    def test_requires_start(self):
        with self.assertRaises(RuntimeError):
            DryingEndpoint().update(65.0, 50.0, 0.0)

    def test_not_done_during_rise_or_at_peak(self):
        self.assertIsNone(self.run_curve(40 * MIN))
        self.assertFalse(self.ep.done)

    def test_done_once_decay_flattens(self):
        done_at = self.run_curve(8 * HOUR)
        self.assertIsNotNone(done_at)
        self.assertGreater(done_at, 2 * HOUR)
        self.assertLess(done_at, 5 * HOUR)
        self.assertLessEqual(self.ep.ah, self.ep.peak_ah - 0.8 * (self.ep.peak_ah - self.ep.start_ah))
        self.assertGreaterEqual(self.ep.slope, -0.05)

    def test_done_latches(self):
        done_at = self.run_curve(8 * HOUR)
        self.assertTrue(self.ep.update(65.0, 90.0, done_at + 10 * MIN))  # humidity spike after done is ignored

    def test_flat_plateau_at_peak_does_not_trigger(self):
        def plateau(t):
            return 55.0 if 30 * MIN <= t <= 3 * HOUR else synthetic_curve(t)
        self.assertIsNone(self.run_curve(3 * HOUR, curve=plateau))

    def test_min_time_floor(self):
        # a curve that is already flat and recovered from the start still waits for min_time
        self.ep.start_ah = 24.0
        self.ep.peak_ah = 55.0
        for t in range(0, 29 * MIN, 30):
            self.assertFalse(self.ep.update(65.0, 100.0 * 26.0 / absolute_humidity(65.0, 100.0), t))

    def test_no_rise_never_completes(self):
        self.assertIsNone(self.run_curve(6 * HOUR, curve=lambda t: 24.0))
        self.assertFalse(self.ep.done)

    def test_timeout_flag(self):
        self.run_curve(8 * HOUR + MIN, curve=lambda t: 24.0)
        self.assertTrue(self.ep.timeout)
        self.assertFalse(self.ep.done)

    def test_sensor_fault_after_gap(self):
        self.ep.update(65.0, 50.0, 0.0)
        self.assertFalse(self.ep.update(None, None, 60.0))
        self.assertFalse(self.ep.fault)
        self.assertFalse(self.ep.update(None, None, 120.0))
        self.assertTrue(self.ep.fault)
        self.ep.update(65.0, 50.0, 130.0)
        self.assertFalse(self.ep.fault)

    def test_occasional_none_does_not_derail(self):
        t = 0.0
        while t <= 8 * HOUR:
            if int(t) % 300 == 0:
                done = self.ep.update(None, None, t)
            else:
                rh = 100.0 * synthetic_curve(t) / absolute_humidity(65.0, 100.0)
                done = self.ep.update(65.0, rh, t)
            if done:
                break
            t += 30
        self.assertTrue(self.ep.done)
        self.assertFalse(self.ep.fault)

    def test_restart_clears_state(self):
        self.run_curve(8 * HOUR)
        self.assertTrue(self.ep.done)
        self.ep.start(1e6)
        self.assertFalse(self.ep.done)
        self.assertIsNone(self.ep.peak_ah)
        self.assertEqual(self.ep.started_at, 1e6)


if __name__ == "__main__":
    unittest.main()
