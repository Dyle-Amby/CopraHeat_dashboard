"""A whole batch through the real state machine against the fake machine.

The unit tests drive each transition in isolation; this proves the pieces
actually hand off to each other -- including the conveyor handshake and a
drying endpoint reached from a physically-shaped humidity curve rather than a
stub flag.
"""

import unittest

from hardware.batch import BatchController, Inputs
from hardware.conveyor import SimulatedConveyor
from hardware.simulate import SimulatedMachine
from hardware.states import BatchState

STEP_S = 2.0
CEILING_S = 12 * 3600


def run_batch(controller=None, abort_when=None):
    """Tick until the batch finishes. Returns (controller, commands log).

    `abort_when(cmd, sim)` fires an operator abort the first time it is true.
    """
    c = controller or BatchController()
    sim = SimulatedMachine(target_kg=c.target_kg)
    belt = SimulatedConveyor(advance_s=4.0)
    c.start()

    cmd = None
    log = []
    now = 0.0
    while now < CEILING_S:
        sim.advance(now, cmd)
        cmd = c.update(
            Inputs(
                now=now,
                chamber_temp=sim.chamber_temp,
                exhaust_temp=sim.exhaust_temp,
                exhaust_rh=sim.exhaust_rh,
                hopper_kg=sim.hopper_kg,
                conveyor_busy=belt.busy,
                sorting_complete=sim.sorting_complete,
                sorted_kg=sim.sorted_kg,
            )
        )
        belt.apply(cmd.conveyor, now)
        log.append(cmd)
        if abort_when is not None and abort_when(cmd, sim):
            c.abort("test abort")
            abort_when = None
        if cmd.state in (BatchState.COMPLETE, BatchState.ABORTED):
            return c, log
        now += STEP_S
    raise AssertionError(f"batch never finished; stuck in {cmd.state.value}")


class TestFullBatch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.controller, cls.log = run_batch()

    def test_reaches_complete(self):
        self.assertIs(self.controller.state, BatchState.COMPLETE)

    def test_visits_every_phase_in_order(self):
        seen = []
        for cmd in self.log:
            if not seen or cmd.state is not seen[-1]:
                seen.append(cmd.state)
        self.assertEqual(
            seen,
            [
                BatchState.INTAKE,
                BatchState.DRYING,
                BatchState.COOLDOWN,
                BatchState.SORTING,
                BatchState.COMPLETE,
            ],
        )

    def test_finishes_without_alarms(self):
        self.assertEqual(self.log[-1].alarms, frozenset())

    def test_heaters_only_ever_fire_while_drying(self):
        hot = {cmd.state for cmd in self.log if cmd.heater_on}
        self.assertEqual(hot, {BatchState.DRYING})

    def test_gate_only_ever_opens_during_intake(self):
        open_in = {cmd.state for cmd in self.log if cmd.gate_open}
        self.assertEqual(open_in, {BatchState.INTAKE})

    def test_the_endpoint_fired_on_the_humidity_curve_not_the_ceiling(self):
        self.assertFalse(self.controller.endpoint.timeout)
        self.assertTrue(self.controller.endpoint.done)

    def test_record_shows_a_plausible_weight_loss(self):
        record = self.controller.record()
        self.assertFalse(record.aborted)
        self.assertGreater(record.weight_in_kg, record.weight_out_kg)
        self.assertAlmostEqual(record.weight_loss_pct, 45.0, places=3)
        self.assertLess(record.drying_s, record.duration_s)


class TestAbortedBatch(unittest.TestCase):
    def test_aborting_a_hot_chamber_purges_then_stops(self):
        controller, log = run_batch(
            abort_when=lambda cmd, sim: cmd.state is BatchState.DRYING and sim.chamber_temp > 65.0
        )
        self.assertIs(controller.state, BatchState.ABORTED)
        self.assertTrue(controller.record().aborted)
        # the chamber was hot, so cooldown ran with the fans on before it stopped
        cooled = [cmd for cmd in log if cmd.state is BatchState.COOLDOWN]
        self.assertTrue(cooled)
        self.assertTrue(any(cmd.fans_on for cmd in cooled))
        self.assertFalse(any(cmd.heater_on for cmd in cooled))
        self.assertFalse(log[-1].fans_on)

    def test_aborting_a_cold_chamber_stops_straight_away(self):
        controller, log = run_batch(abort_when=lambda cmd, sim: cmd.state is BatchState.INTAKE)
        self.assertIs(controller.state, BatchState.ABORTED)
        self.assertNotIn(BatchState.COOLDOWN, {cmd.state for cmd in log})


if __name__ == "__main__":
    unittest.main()
