"""Unit tests for ComponentHealthRegistry (hardware/health.py)."""

import unittest
from hardware.health import ComponentHealthRegistry


class TestComponentHealthRegistry(unittest.TestCase):

    def setUp(self):
        self.registry = ComponentHealthRegistry(
            hopper_timeout_s=5.0,
            bin_timeout_s=5.0,
            chamber_timeout_s=10.0,
            exhaust_timeout_s=10.0,
        )

    def test_record_ok_and_snapshot(self):
        self.registry.record_ok("hopper_scale", now=100.0, details={"kg": 15.2})
        snap = self.registry.snapshot()
        self.assertIn("hopper_scale", snap)
        self.assertEqual(snap["hopper_scale"]["status"], "ok")
        self.assertEqual(snap["hopper_scale"]["last_ok_at"], 100.0)
        self.assertEqual(snap["hopper_scale"]["details"]["kg"], 15.2)

    def test_record_fault(self):
        self.registry.record_fault("chamber_sensor", "Sensor disconnected", now=100.0)
        snap = self.registry.snapshot()
        self.assertEqual(snap["chamber_sensor"]["status"], "fault")
        self.assertEqual(snap["chamber_sensor"]["message"], "Sensor disconnected")

    def test_hopper_scale_timeout(self):
        self.registry.record_ok("hopper_scale", now=100.0)
        # At t=102, no alarm
        alarms = self.registry.evaluate_timeouts(now=102.0)
        self.assertNotIn("hopper_scale_fault", alarms)

        # At t=106 (> 5s elapsed), hopper_scale_fault alarm raised
        alarms = self.registry.evaluate_timeouts(now=106.0)
        self.assertIn("hopper_scale_fault", alarms)
        snap = self.registry.snapshot()
        self.assertEqual(snap["hopper_scale"]["status"], "fault")

    def test_bin_scale_timeout(self):
        self.registry.record_ok("bin_scale", now=100.0)
        alarms = self.registry.evaluate_timeouts(now=106.0)
        self.assertIn("bin_scale_fault", alarms)

    def test_chamber_and_exhaust_timeouts(self):
        self.registry.record_ok("chamber_sensor", now=100.0)
        self.registry.record_ok("exhaust_sensor", now=100.0)

        # Before 10s
        alarms = self.registry.evaluate_timeouts(now=108.0)
        self.assertNotIn("chamber_sensor_fault", alarms)
        self.assertNotIn("exhaust_sensor_fault", alarms)

        # After 10s
        alarms = self.registry.evaluate_timeouts(now=111.0)
        self.assertIn("chamber_sensor_fault", alarms)
        self.assertIn("exhaust_sensor_fault", alarms)


if __name__ == "__main__":
    unittest.main()
