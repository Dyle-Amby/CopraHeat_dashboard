import os
import tempfile
import unittest
from pathlib import Path

from hardware import prototype_test as pt


class SwitchBinsTest(unittest.TestCase):
    def test_default_mapping_bottom_to_top(self):
        self.assertEqual(pt.switch_bins(), {"LS1": "great", "LS2": "good", "LS3": "bad"})

    def test_single_press_names_the_bin(self):
        state = {"LS1": False, "LS2": True, "LS3": False}
        self.assertIn("GOOD bin", pt.describe_switches(state, pt.switch_bins()))

    def test_none_pressed(self):
        state = {"LS1": False, "LS2": False, "LS3": False}
        self.assertIn("between stops", pt.describe_switches(state, pt.switch_bins()))

    def test_all_pressed_points_at_ground(self):
        state = {"LS1": True, "LS2": True, "LS3": True}
        self.assertIn("GND", pt.describe_switches(state, pt.switch_bins()))

    def test_two_pressed_is_wiring_fault(self):
        state = {"LS1": True, "LS2": False, "LS3": True}
        self.assertIn("wiring fault", pt.describe_switches(state, pt.switch_bins()))


@unittest.skipIf(os.name == "nt", "symlinks need privileges on Windows")
class FindPwmChipTest(unittest.TestCase):
    def test_picks_the_rp1_chip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for n, dev in ((0, "1f00098000.pwm"), (1, "107d517a80.pwm")):
                target = root / "devices" / dev / f"pwmchip{n}"
                target.mkdir(parents=True)
                (root / f"pwmchip{n}").symlink_to(target)
            self.assertEqual(pt.find_rp1_pwm_chip(root), 0)

    def test_missing_dir(self):
        self.assertIsNone(pt.find_rp1_pwm_chip(Path("/nonexistent/pwm")))


class PinsTest(unittest.TestCase):
    def test_fan_pin_has_a_physical_label(self):
        self.assertIn(pt.pins.FAN_RELAY, pt.PHYSICAL)


if __name__ == "__main__":
    unittest.main()
