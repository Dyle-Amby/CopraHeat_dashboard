import unittest

from hardware import pins
from hardware.limits import SWITCHES, newly_triggered, triggered


class TriggeredTest(unittest.TestCase):
    def test_low_is_released(self):
        # NC contact closed to GND holds the line low
        self.assertFalse(triggered(0))

    def test_high_is_pressed_or_broken(self):
        self.assertTrue(triggered(1))


class NewlyTriggeredTest(unittest.TestCase):
    clear = {"LS1": False, "LS2": False, "LS3": False}

    def test_nothing_changed(self):
        self.assertEqual(newly_triggered(self.clear, self.clear), [])

    def test_rising_edge_reported(self):
        after = dict(self.clear, LS2=True)
        self.assertEqual(newly_triggered(self.clear, after), ["LS2"])

    def test_switch_held_since_start_is_not_new(self):
        held = dict(self.clear, LS1=True)
        self.assertEqual(newly_triggered(held, held), [])

    def test_leaving_a_switch_is_not_a_trigger(self):
        held = dict(self.clear, LS1=True)
        self.assertEqual(newly_triggered(held, self.clear), [])


class PinMapTest(unittest.TestCase):
    def test_switches_match_pin_map(self):
        self.assertEqual(
            SWITCHES,
            {"LS1": pins.LIMIT_SWITCH_1, "LS2": pins.LIMIT_SWITCH_2, "LS3": pins.LIMIT_SWITCH_3},
        )


if __name__ == "__main__":
    unittest.main()
