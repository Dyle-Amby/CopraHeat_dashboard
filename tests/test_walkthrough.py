import unittest

from hardware import walkthrough


class WalkthroughCliTest(unittest.TestCase):
    def test_default_args(self):
        # Test default CLI arguments
        args = walkthrough.parse_args([])
        self.assertEqual(args.target_kg, 2.0)
        self.assertEqual(args.conveyor_steps, 400)
        self.assertEqual(args.conveyor_hz, 400.0)
        self.assertEqual(args.carriage_hz, 400.0)
        self.assertFalse(args.dry_run)
        self.assertFalse(args.fan_active_low)

    def test_custom_args(self):
        import sys
        old_argv = sys.argv
        try:
            sys.argv = [
                "walkthrough",
                "--dry-run",
                "--target-kg", "3.5",
                "--conveyor-steps", "600",
                "--fan-active-low",
            ]
            args = walkthrough.parse_args()
            self.assertTrue(args.dry_run)
            self.assertEqual(args.target_kg, 3.5)
            self.assertEqual(args.conveyor_steps, 600)
            self.assertTrue(args.fan_active_low)
        finally:
            sys.argv = old_argv

    def test_sim_sensors(self):
        sim = walkthrough.SensorSimulator(ambient_c=25.0)
        c, e, rh = sim.update(heater_on=True, fans_on=False, dt=1.0)
        self.assertGreater(c, 25.0)


if __name__ == "__main__":
    unittest.main()
