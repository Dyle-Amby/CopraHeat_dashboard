import unittest

from hardware.conveyor import SimulatedConveyor
from hardware.states import ConveyorCommand

STOP = ConveyorCommand.STOP
ADVANCE = ConveyorCommand.INTAKE_ADVANCE
CREEP = ConveyorCommand.SORT_CREEP


class TestSimulatedConveyor(unittest.TestCase):
    def setUp(self):
        self.belt = SimulatedConveyor(advance_s=4.0)

    def test_starts_stopped(self):
        self.assertFalse(self.belt.busy)
        self.assertFalse(self.belt.creeping)

    def test_advance_reports_busy_until_the_move_ends(self):
        self.belt.apply(ADVANCE, 0.0)
        self.assertTrue(self.belt.busy)
        self.belt.apply(ADVANCE, 3.9)
        self.assertTrue(self.belt.busy)
        self.belt.apply(ADVANCE, 4.0)
        self.assertFalse(self.belt.busy)

    def test_a_finished_move_does_not_restart_itself(self):
        # the state machine keeps asking until it sees the move end
        self.belt.apply(ADVANCE, 0.0)
        self.belt.apply(ADVANCE, 4.0)
        for t in (4.2, 5.0, 9.0):
            self.belt.apply(ADVANCE, t)
            self.assertFalse(self.belt.busy)

    def test_stop_rearms_for_the_next_batch(self):
        self.belt.apply(ADVANCE, 0.0)
        self.belt.apply(ADVANCE, 4.0)
        self.belt.apply(STOP, 5.0)
        self.belt.apply(ADVANCE, 6.0)
        self.assertTrue(self.belt.busy)

    def test_creep_runs_continuously(self):
        self.belt.apply(CREEP, 0.0)
        self.assertTrue(self.belt.creeping)
        self.belt.apply(CREEP, 600.0)
        self.assertTrue(self.belt.busy)

    def test_stop_ends_a_creep(self):
        self.belt.apply(CREEP, 0.0)
        self.belt.apply(STOP, 1.0)
        self.assertFalse(self.belt.creeping)
        self.assertFalse(self.belt.busy)


if __name__ == "__main__":
    unittest.main()
