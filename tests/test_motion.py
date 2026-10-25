import unittest
from unittest.mock import MagicMock

from hardware.motion import CarriageDriver, SimulatedCarriage, SimulatedConveyor, StepperConveyor
from hardware.states import ConveyorCommand


class TestSimulatedConveyorMotion(unittest.TestCase):
    def setUp(self):
        self.conveyor = SimulatedConveyor(
            advance_s=4.0,
            hopper_to_chamber_steps=1000,
            default_hz=500.0,
        )

    def test_refuse_on_uncalibrated_or_zero_steps(self):
        # Explicit zero steps
        accepted = self.conveyor.move(0, hz=400.0)
        self.assertFalse(accepted)
        self.assertEqual(self.conveyor.last_result, "refused")
        self.assertFalse(self.conveyor.busy)

        # Negative steps
        accepted = self.conveyor.move(-100, hz=400.0)
        self.assertFalse(accepted)
        self.assertEqual(self.conveyor.last_result, "refused")

        # Uncalibrated hopper_to_chamber_steps
        uncal = SimulatedConveyor(hopper_to_chamber_steps=0)
        uncal.apply(ConveyorCommand.INTAKE_ADVANCE, now=0.0)
        self.assertEqual(uncal.last_result, "refused")
        self.assertFalse(uncal.busy)

    def test_move_handshake_busy_and_done(self):
        # 1000 steps at 500 Hz = 2.0 seconds duration
        accepted = self.conveyor.move(1000, hz=500.0, now=10.0)
        self.assertTrue(accepted)
        self.assertTrue(self.conveyor.busy)
        self.assertIsNone(self.conveyor.last_result)

        # Mid-move: still busy
        self.conveyor.update(now=11.0)
        self.assertTrue(self.conveyor.busy)

        # Completed move: done and position_steps updated
        self.conveyor.update(now=12.0)
        self.assertFalse(self.conveyor.busy)
        self.assertEqual(self.conveyor.last_result, "done")
        self.assertEqual(self.conveyor.position_steps, 1000)

    def test_stop_on_abort_or_close(self):
        # Start 1000 steps at 500 Hz
        self.conveyor.move(1000, hz=500.0, now=0.0)
        self.assertTrue(self.conveyor.busy)

        # Stop after 1.0 s (500 steps sent)
        sent = self.conveyor.stop(now=1.0)
        self.assertFalse(self.conveyor.busy)
        self.assertEqual(self.conveyor.last_result, "stopped")
        self.assertEqual(sent, 500)
        self.assertEqual(self.conveyor.position_steps, 500)

        # Close is safe and idempotent
        self.conveyor.close()
        self.assertFalse(self.conveyor.busy)


class TestStepperConveyor(unittest.TestCase):
    def test_refuse_on_zero_steps(self):
        # Create StepperConveyor with mocked stepper
        stepper_mock = MagicMock()
        stepper_mock.busy = False
        conv = StepperConveyor.__new__(StepperConveyor)
        conv.hopper_to_chamber_steps = 0
        conv.default_hz = 400.0
        conv.position_steps = 0
        conv.last_result = None
        conv._advance_done = False
        conv.stepper = stepper_mock
        conv._target_steps = 0
        conv._move_forward = True
        conv._was_busy = False

        accepted = conv.move(0)
        self.assertFalse(accepted)
        self.assertEqual(conv.last_result, "refused")
        stepper_mock.start.assert_not_called()

    def test_exact_counted_burst_and_completion(self):
        stepper_mock = MagicMock()
        stepper_mock.busy = True
        conv = StepperConveyor.__new__(StepperConveyor)
        conv.hopper_to_chamber_steps = 1500
        conv.default_hz = 500.0
        conv.position_steps = 0
        conv.last_result = None
        conv._advance_done = False
        conv.stepper = stepper_mock
        conv._target_steps = 0
        conv._move_forward = True
        conv._was_busy = False

        accepted = conv.move(1500, hz=500.0, forward=True)
        self.assertTrue(accepted)
        stepper_mock.start.assert_called_once_with(1500, 500.0, forward=True)
        self.assertTrue(conv.busy)

        # Stepper finishes
        stepper_mock.busy = False
        self.assertFalse(conv.busy)
        self.assertEqual(conv.last_result, "done")
        self.assertEqual(conv.position_steps, 1500)


class TestCarriageDrivers(unittest.TestCase):
    def test_simulated_carriage_flow(self):
        c = SimulatedCarriage(move_s_per_hop=0.2)
        self.assertEqual(c.state, "at_target")
        self.assertIsNotNone(c.current_bin)

        # Goto another bin
        accepted = c.goto("great", now=0.0)
        self.assertTrue(accepted)
        self.assertEqual(c.state, "moving")

        # Update mid-move
        c.update(now=0.1)
        self.assertEqual(c.state, "moving")

        # Update upon arrival
        c.update(now=0.5)
        self.assertEqual(c.state, "at_target")
        self.assertEqual(c.current_bin, "great")

    def test_carriage_driver_startup_home_check(self):
        driver = CarriageDriver.__new__(CarriageDriver)
        driver.order = ("LS3", "LS2", "LS1")
        driver.bins = ("bad", "good", "great")
        driver.stops = {"bad": "LS3", "good": "LS2", "great": "LS1"}
        driver.bins_by_stop = {"LS3": "bad", "LS2": "good", "LS1": "great"}
        driver.state = "idle"
        driver.current_bin = None
        driver.last_fault = None

        limits_mock = MagicMock()
        limits_mock.read.return_value = {"LS1": True, "LS2": False, "LS3": False}
        driver._limits = limits_mock

        bin_found = driver.check_home()
        self.assertEqual(bin_found, "great")
        self.assertEqual(driver.state, "at_target")
        self.assertEqual(driver.current_bin, "great")


if __name__ == "__main__":
    unittest.main()
