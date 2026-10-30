import contextlib
import io
import unittest
from unittest import mock

from hardware import bench, pins


class BenchCliTest(unittest.TestCase):
    def parse_fails(self, argv):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            bench.main(argv)

    def test_motor_pins_match_pin_map(self):
        self.assertEqual(bench.MOTORS["conveyor"], pins.HBS57H_CONVEYOR)
        self.assertEqual(bench.MOTORS["carriage"], pins.TB6600_CARRIAGE)

    def test_relay_pins_match_pin_map(self):
        self.assertEqual(bench.RELAYS, {"fan": pins.FAN_RELAY, "heater": pins.HEATER_SSR})

    def test_unknown_relay_rejected(self):
        self.parse_fails(["relay", "servo"])

    def test_all_four_cells_present(self):
        self.assertEqual(set(bench.CELLS), {"hopper", "great", "good", "bad"})

    def test_unknown_motor_rejected(self):
        self.parse_fails(["jog", "servo"])

    def test_jog_accepts_legacy_single_dash_hz(self):
        with mock.patch.object(bench, "jog") as jog, mock.patch.object(bench.signal, "signal"):
            bench.main(["jog", "conveyor", "--steps", "10000", "-hz", "2000", "--reverse"])

        args = jog.call_args.args[0]
        self.assertEqual(args.motor, "conveyor")
        self.assertEqual(args.steps, 10000)
        self.assertEqual(args.hz, 2000)
        self.assertTrue(args.reverse)

    def test_hopper_calibration_points_elsewhere(self):
        # the hopper needs two tare offsets; run_hopper --calibrate owns that
        self.parse_fails(["scale-cal", "hopper"])

    def test_sort_rejects_unknown_switch_in_order(self):
        self.parse_fails(["sort", "--order", "LS1", "LS2", "LS9"])

    def test_subcommand_required(self):
        self.parse_fails([])


if __name__ == "__main__":
    unittest.main()
