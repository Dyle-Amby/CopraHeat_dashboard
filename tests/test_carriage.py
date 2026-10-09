import unittest

from hardware.carriage import ARRIVED, FAULT, MOVING, MoveWatch, located_at, moving_up, stops

CLEAR = {"LS1": False, "LS2": False, "LS3": False}


def at(*names):
    return dict(CLEAR, **{n: True for n in names})


class StopsTest(unittest.TestCase):
    def test_default_map(self):
        self.assertEqual(stops(), {"great": "LS1", "good": "LS2", "bad": "LS3"})

    def test_custom_order(self):
        self.assertEqual(stops(("LS3", "LS2", "LS1"), ("bad", "good", "great"))["great"], "LS1")

    def test_rejects_duplicates_and_mismatch(self):
        for order, bins in [(("LS1", "LS1", "LS2"), ("a", "b", "c")),
                            (("LS1", "LS2", "LS3"), ("a", "a", "c")),
                            (("LS1", "LS2"), ("a", "b", "c"))]:
            with self.assertRaises(ValueError):
                stops(order, bins)


class LocatedAtTest(unittest.TestCase):
    def test_between_stops(self):
        self.assertIsNone(located_at(CLEAR))

    def test_on_a_stop(self):
        self.assertEqual(located_at(at("LS2")), "LS2")

    def test_two_pressed_is_wiring_fault(self):
        with self.assertRaises(RuntimeError):
            located_at(at("LS1", "LS3"))


class MovingUpTest(unittest.TestCase):
    def test_directions(self):
        self.assertTrue(moving_up("LS1", "LS3"))
        self.assertFalse(moving_up("LS3", "LS2"))


class MoveWatchTest(unittest.TestCase):
    def test_bottom_to_top_passes_the_middle(self):
        w = MoveWatch(at("LS1"), "LS3")
        self.assertEqual(w.update(CLEAR, 50), (MOVING, None))       # left LS1
        self.assertEqual(w.update(at("LS2"), 500), (MOVING, None))  # passing over LS2
        self.assertEqual(w.update(CLEAR, 600), (MOVING, None))
        self.assertEqual(w.update(at("LS3"), 1000), (ARRIVED, "LS3"))

    def test_adjacent_move(self):
        w = MoveWatch(at("LS2"), "LS1")
        w.update(CLEAR, 50)
        self.assertEqual(w.update(at("LS1"), 400), (ARRIVED, "LS1"))

    def test_wrong_side_switch_faults(self):
        # heading LS2 -> LS3 but the motor actually goes down
        w = MoveWatch(at("LS2"), "LS3")
        w.update(CLEAR, 50)
        status, msg = w.update(at("LS1"), 400)
        self.assertEqual(status, FAULT)
        self.assertIn("LS1", msg)

    def test_driving_into_end_stop_faults(self):
        w = MoveWatch(at("LS1"), "LS2", release_within=400)
        self.assertEqual(w.update(at("LS1"), 399)[0], MOVING)
        self.assertEqual(w.update(at("LS1"), 400)[0], FAULT)

    def test_release_guard_ends_once_released(self):
        w = MoveWatch(at("LS1"), "LS3", release_within=400)
        w.update(CLEAR, 50)
        self.assertEqual(w.update(CLEAR, 5000), (MOVING, None))

    def test_homing_takes_first_switch_either_way(self):
        self.assertEqual(MoveWatch(CLEAR, None).update(at("LS2"), 300), (ARRIVED, "LS2"))
        self.assertEqual(MoveWatch(CLEAR, None).update(at("LS3"), 300), (ARRIVED, "LS3"))

    def test_target_already_pressed_at_start_is_not_an_arrival(self):
        # a switch held since before the move is not news
        w = MoveWatch(at("LS2"), "LS1")
        self.assertEqual(w.update(at("LS2"), 10), (MOVING, None))


if __name__ == "__main__":
    unittest.main()
