import contextlib
import io
import unittest

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

    def test_hopper_calibration_points_elsewhere(self):
        # the hopper needs two tare offsets; run_hopper --calibrate owns that
        self.parse_fails(["scale-cal", "hopper"])

    def test_sort_rejects_unknown_switch_in_order(self):
        self.parse_fails(["sort", "--order", "LS1", "LS2", "LS9"])

    def test_subcommand_required(self):
        self.parse_fails([])


if __name__ == "__main__":
    unittest.main()
