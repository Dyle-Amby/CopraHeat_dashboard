import unittest

from hardware.batch import COMMAND_NAMES, BatchController, Inputs, apply_command
from hardware.climate import FanController
from hardware.drying import DryingEndpoint
from hardware.states import BatchState, ConveyorCommand

IDLE = BatchState.IDLE
INTAKE = BatchState.INTAKE
DRYING = BatchState.DRYING
COOLDOWN = BatchState.COOLDOWN
SORTING = BatchState.SORTING
COMPLETE = BatchState.COMPLETE
ABORTED = BatchState.ABORTED


class StubEndpoint:
    """Stands in for DryingEndpoint so tests can call the endpoint directly."""

    def __init__(self):
        self.done = False
        self.timeout = False
        self.fault = False
        self.started_at = None

    def start(self, now):
        self.started_at = now

    def update(self, exhaust_temp, exhaust_rh, now):
        return self.done


def make(**kwargs):
    # dwell_s=0 so fan assertions are about the state rule, not relay debounce
    kwargs.setdefault("fans", FanController(dwell_s=0.0))
    kwargs.setdefault("endpoint", StubEndpoint())
    return BatchController(**kwargs)


def to_intake(c, t=0.0, chamber=25.0):
    c.start()
    c.update(Inputs(now=t, chamber_temp=chamber))
    return t + 1


def dispense(c, t, chamber=25.0, kg=2.0):
    """Fill the hopper to target, then empty it. Returns the time after."""
    for _ in range(5):   # StableWeight needs 5 agreeing samples before the gate trusts one
        c.update(Inputs(now=t, chamber_temp=chamber, hopper_kg=kg))
        t += 1
    for _ in range(5):
        c.update(Inputs(now=t, chamber_temp=chamber, hopper_kg=0.0))
        t += 1
    return t


def advance(c, t, chamber=25.0):
    """Acknowledge the conveyor move and let it finish."""
    c.update(Inputs(now=t, chamber_temp=chamber, conveyor_busy=True))
    t += 1
    c.update(Inputs(now=t, chamber_temp=chamber, conveyor_busy=False))
    return t + 1


def to_drying(c, t=0.0, chamber=25.0):
    return advance(c, dispense(c, to_intake(c, t, chamber), chamber), chamber)


class TestIdle(unittest.TestCase):
    def test_starts_idle_with_everything_off(self):
        c = make()
        out = c.update(Inputs(now=0.0, chamber_temp=25.0))
        self.assertIs(out.state, IDLE)
        self.assertFalse(out.heater_on)
        self.assertFalse(out.fans_on)
        self.assertFalse(out.gate_open)
        self.assertIs(out.conveyor, ConveyorCommand.STOP)
        self.assertEqual(out.alarms, frozenset())

    def test_idle_does_not_heat_a_cold_chamber(self):
        c = make()
        self.assertFalse(c.update(Inputs(now=0.0, chamber_temp=20.0)).heater_on)

    def test_start_enters_intake_on_next_update(self):
        c = make()
        c.start()
        self.assertIs(c.state, IDLE)   # the request has not been acted on yet
        self.assertIs(c.update(Inputs(now=0.0)).state, INTAKE)

    def test_start_is_refused_when_a_batch_is_running(self):
        c = make()
        to_intake(c)
        with self.assertRaises(RuntimeError):
            c.start()

    def test_abort_is_refused_with_no_batch(self):
        with self.assertRaises(RuntimeError):
            make().abort()

    def test_elapsed_is_none_before_a_batch_starts(self):
        self.assertIsNone(make().elapsed_s(10.0))


class TestIntake(unittest.TestCase):
    def test_gate_opens_at_target_and_captures_the_hopper_weight(self):
        c = make()
        t = to_intake(c)
        for _ in range(4):
            out = c.update(Inputs(now=t, chamber_temp=25.0, hopper_kg=2.0))
            t += 1
            self.assertFalse(out.gate_open)   # not yet stable
        out = c.update(Inputs(now=t, chamber_temp=25.0, hopper_kg=2.0))
        self.assertTrue(out.gate_open)
        self.assertAlmostEqual(c.weight_in_kg, 2.0)

    def test_gate_closes_when_empty_then_the_conveyor_is_commanded(self):
        c = make()
        t = dispense(c, to_intake(c))
        out = c.update(Inputs(now=t, chamber_temp=25.0))
        self.assertIs(c.state, INTAKE)
        self.assertFalse(out.gate_open)
        self.assertIs(out.conveyor, ConveyorCommand.INTAKE_ADVANCE)

    def test_conveyor_move_completing_enters_drying(self):
        c = make()
        endpoint = c.endpoint
        t = to_drying(c)
        self.assertIs(c.state, DRYING)
        self.assertIsNotNone(endpoint.started_at)
        self.assertIs(c.update(Inputs(now=t)).conveyor, ConveyorCommand.STOP)

    def test_conveyor_that_never_moves_raises_a_stall_alarm(self):
        c = make(conveyor_ack_s=5.0)
        t = dispense(c, to_intake(c))
        out = c.update(Inputs(now=t + 10.0, chamber_temp=25.0))
        self.assertIn("conveyor_stall", out.alarms)
        self.assertIs(c.state, INTAKE)
        self.assertIs(out.conveyor, ConveyorCommand.INTAKE_ADVANCE)

    def test_idle_conveyor_is_not_mistaken_for_a_finished_move(self):
        c = make()
        t = dispense(c, to_intake(c))
        for _ in range(3):   # conveyor never reports busy, so the move never "ends"
            c.update(Inputs(now=t, chamber_temp=25.0))
            t += 1
        self.assertIs(c.state, INTAKE)

    def test_hopper_that_never_fills_raises_a_stall_alarm(self):
        c = make(intake_stall_s=60.0)
        to_intake(c)
        out = c.update(Inputs(now=100.0, chamber_temp=25.0, hopper_kg=0.2))
        self.assertIn("intake_stall", out.alarms)
        self.assertIs(c.state, INTAKE)

    def test_hopper_jam_surfaces_as_an_alarm_without_closing_the_gate(self):
        c = make()
        t = to_intake(c)
        for _ in range(5):
            c.update(Inputs(now=t, chamber_temp=25.0, hopper_kg=2.0))
            t += 1
        out = c.update(Inputs(now=t + 60.0, chamber_temp=25.0, hopper_kg=1.5))
        self.assertIn("hopper_jam", out.alarms)
        self.assertTrue(out.gate_open)

    def test_no_heat_during_intake(self):
        c = make()
        to_intake(c, chamber=50.0)
        out = c.update(Inputs(now=1.0, chamber_temp=50.0))
        self.assertFalse(out.heater_on)
        self.assertFalse(out.fans_on)

    def test_target_weight_is_settable_until_the_batch_is_dispensed(self):
        c = make()
        t = to_intake(c)
        c.set_target_kg(3.0)
        self.assertEqual(c.target_kg, 3.0)
        dispense(c, t, kg=3.0)
        with self.assertRaises(RuntimeError):
            c.set_target_kg(4.0)

    def test_target_weight_below_empty_is_refused(self):
        with self.assertRaises(ValueError):
            make().set_target_kg(0.01)


class TestDrying(unittest.TestCase):
    def test_heaters_follow_the_hysteresis_band(self):
        c = make()
        t = to_drying(c)
        self.assertTrue(c.update(Inputs(now=t, chamber_temp=50.0)).heater_on)
        self.assertFalse(c.update(Inputs(now=t + 1, chamber_temp=72.0)).heater_on)

    def test_endpoint_moves_to_cooldown_and_records_the_drying_time(self):
        c = make()
        t = to_drying(c)
        c.update(Inputs(now=t, chamber_temp=65.0))
        c.endpoint.done = True
        out = c.update(Inputs(now=t + 600.0, chamber_temp=65.0))
        self.assertIs(out.state, COOLDOWN)
        # drying_s spans the DRYING entry (when the endpoint was started) to now
        self.assertAlmostEqual(c.drying_s, (t + 600.0) - c.endpoint.started_at)

    def test_manual_override_ends_drying(self):
        c = make()
        t = to_drying(c)
        c.end_drying()
        self.assertIs(c.update(Inputs(now=t, chamber_temp=65.0)).state, COOLDOWN)

    def test_end_drying_is_refused_outside_drying(self):
        with self.assertRaises(RuntimeError):
            make().end_drying()

    def test_timeout_aborts_the_batch(self):
        c = make()
        t = to_drying(c)
        c.endpoint.timeout = True
        out = c.update(Inputs(now=t, chamber_temp=65.0))
        self.assertIn("drying_timeout", out.alarms)
        self.assertTrue(c.aborted)
        self.assertIs(out.state, COOLDOWN)   # hot chamber: purge before stopping

    def test_sensor_faults_set_and_clear(self):
        c = make()
        t = to_drying(c)
        c.endpoint.fault = True
        out = c.update(Inputs(now=t, chamber_temp=None))
        self.assertIn("exhaust_sensor_fault", out.alarms)
        self.assertIn("chamber_sensor_fault", out.alarms)
        c.endpoint.fault = False
        out = c.update(Inputs(now=t + 1, chamber_temp=65.0))
        self.assertNotIn("exhaust_sensor_fault", out.alarms)
        self.assertNotIn("chamber_sensor_fault", out.alarms)

    def test_overtemp_raises_an_alarm(self):
        c = make()
        t = to_drying(c)
        out = c.update(Inputs(now=t, chamber_temp=82.0))
        self.assertIn("overtemp", out.alarms)
        self.assertFalse(out.heater_on)
        self.assertTrue(out.fans_on)

    def test_gate_stays_shut_outside_intake(self):
        c = make()
        t = to_drying(c)
        self.assertFalse(c.update(Inputs(now=t, chamber_temp=65.0, hopper_kg=5.0)).gate_open)

    def test_real_endpoint_is_driven_by_exhaust_readings(self):
        c = make(endpoint=DryingEndpoint())
        t = to_drying(c)
        c.update(Inputs(now=t, chamber_temp=65.0, exhaust_temp=60.0, exhaust_rh=40.0))
        self.assertIsNotNone(c.endpoint.ah)
        self.assertIs(c.state, DRYING)


class TestCooldown(unittest.TestCase):
    def setUp(self):
        self.c = make(cooldown_target_c=40.0, max_cooldown_s=300.0)
        self.t = to_drying(self.c)
        self.c.end_drying()
        self.c.update(Inputs(now=self.t, chamber_temp=65.0))

    def test_fans_purge_with_the_heaters_off(self):
        out = self.c.update(Inputs(now=self.t + 1, chamber_temp=65.0))
        self.assertTrue(out.fans_on)
        self.assertFalse(out.heater_on)

    def test_reaching_the_target_moves_to_sorting(self):
        out = self.c.update(Inputs(now=self.t + 10, chamber_temp=39.0))
        self.assertIs(out.state, SORTING)
        self.assertFalse(out.fans_on)

    def test_a_dead_probe_falls_back_to_the_time_ceiling(self):
        self.assertIs(self.c.update(Inputs(now=self.t + 100, chamber_temp=None)).state, COOLDOWN)
        out = self.c.update(Inputs(now=self.t + 301, chamber_temp=None))
        self.assertIs(out.state, SORTING)
        self.assertIn("chamber_sensor_fault", out.alarms)

    def test_an_aborted_batch_stops_instead_of_sorting(self):
        self.c.abort("operator abort")
        self.c.update(Inputs(now=self.t + 1, chamber_temp=65.0))
        self.assertIs(self.c.state, COOLDOWN)   # keeps purging
        out = self.c.update(Inputs(now=self.t + 10, chamber_temp=39.0))
        self.assertIs(out.state, ABORTED)


class TestSorting(unittest.TestCase):
    def setUp(self):
        self.c = make()
        self.t = to_drying(self.c)
        self.c.end_drying()
        self.c.update(Inputs(now=self.t, chamber_temp=65.0))
        self.c.update(Inputs(now=self.t + 1, chamber_temp=30.0))

    def test_conveyor_creeps_past_the_camera(self):
        out = self.c.update(Inputs(now=self.t + 2, chamber_temp=30.0))
        self.assertIs(out.state, SORTING)
        self.assertIs(out.conveyor, ConveyorCommand.SORT_CREEP)

    def test_completion_stops_the_conveyor(self):
        out = self.c.update(Inputs(now=self.t + 2, chamber_temp=30.0, sorting_complete=True))
        self.assertIs(out.state, COMPLETE)
        self.assertIs(out.conveyor, ConveyorCommand.STOP)

    def test_manual_finish(self):
        self.c.finish_sorting()
        self.assertIs(self.c.update(Inputs(now=self.t + 2, chamber_temp=30.0)).state, COMPLETE)

    def test_overrunning_raises_an_alarm_without_advancing(self):
        c = make(max_sorting_s=60.0)
        t = to_drying(c)
        c.end_drying()
        c.update(Inputs(now=t, chamber_temp=65.0))
        c.update(Inputs(now=t + 1, chamber_temp=30.0))
        out = c.update(Inputs(now=t + 100, chamber_temp=30.0))
        self.assertIn("sorting_timeout", out.alarms)
        self.assertIs(out.state, SORTING)


class TestRecord(unittest.TestCase):
    def test_no_record_until_the_batch_finishes(self):
        c = make()
        to_drying(c)
        self.assertIsNone(c.record())

    def test_weight_loss_comes_from_hopper_in_versus_bins_out(self):
        c = make()
        t = to_drying(c)
        c.end_drying()
        c.update(Inputs(now=t, chamber_temp=65.0))
        c.update(Inputs(now=t + 1, chamber_temp=30.0))
        c.update(Inputs(now=t + 2, chamber_temp=30.0, sorted_kg=1.1, sorting_complete=True))
        r = c.record()
        self.assertAlmostEqual(r.weight_in_kg, 2.0)
        self.assertAlmostEqual(r.weight_out_kg, 1.1)
        self.assertAlmostEqual(r.weight_loss_pct, 45.0)
        self.assertFalse(r.aborted)
        self.assertIsNotNone(r.drying_s)

    def test_weight_loss_is_none_when_the_bins_were_never_read(self):
        c = make()
        t = to_drying(c)
        c.end_drying()
        c.update(Inputs(now=t, chamber_temp=65.0))
        c.update(Inputs(now=t + 1, chamber_temp=30.0))
        c.update(Inputs(now=t + 2, chamber_temp=30.0, sorting_complete=True))
        self.assertIsNone(c.record().weight_loss_pct)

    def test_an_aborted_record_keeps_the_reason(self):
        c = make()
        t = to_drying(c)
        c.abort("operator abort")
        c.update(Inputs(now=t, chamber_temp=30.0))   # cold enough to stop straight away
        r = c.record()
        self.assertTrue(r.aborted)
        self.assertEqual(r.abort_reason, "operator abort")
        self.assertIn("aborted", r.alarms)


class TestApplyCommand(unittest.TestCase):
    def test_start_is_dispatched_by_name(self):
        c = make()
        apply_command(c, "start")
        self.assertIs(c.update(Inputs(now=0.0)).state, INTAKE)

    def test_abort_carries_its_reason_through_the_payload(self):
        c = make()
        to_intake(c)
        apply_command(c, "abort", "smoke in the chamber")
        c.update(Inputs(now=1.0, chamber_temp=25.0))
        self.assertEqual(c.abort_reason, "smoke in the chamber")

    def test_abort_without_a_payload_still_works(self):
        c = make()
        to_intake(c)
        apply_command(c, "abort")
        c.update(Inputs(now=1.0, chamber_temp=25.0))
        self.assertEqual(c.abort_reason, "operator abort")

    def test_set_target_kg_parses_its_payload(self):
        c = make()
        apply_command(c, "set_target_kg", "3.5")
        self.assertEqual(c.target_kg, 3.5)

    def test_set_target_kg_without_a_payload_is_refused(self):
        with self.assertRaises(ValueError):
            apply_command(make(), "set_target_kg")

    def test_a_junk_payload_is_refused(self):
        with self.assertRaises(ValueError):
            apply_command(make(), "set_target_kg", "heavy")

    def test_unknown_names_are_refused(self):
        with self.assertRaises(ValueError):
            apply_command(make(), "launch_missiles")

    def test_the_refusal_message_names_the_current_state(self):
        c = make()
        to_drying(c)
        with self.assertRaises(RuntimeError) as caught:
            apply_command(c, "start")
        self.assertIn("drying", str(caught.exception))

    def test_every_name_in_the_vocabulary_dispatches(self):
        # a name in COMMAND_NAMES that apply_command does not handle would raise
        # ValueError("unknown command"); a state refusal is RuntimeError
        for name in COMMAND_NAMES:
            with self.subTest(name=name):
                try:
                    apply_command(make(), name, "1.0")
                except ValueError as exc:
                    self.fail(f"{name} is not dispatched: {exc}")
                except RuntimeError:
                    pass   # refused by state, which means it was dispatched


class TestAbortAndReset(unittest.TestCase):
    def test_abort_with_a_cold_chamber_stops_immediately(self):
        c = make()
        to_intake(c)
        c.abort()
        out = c.update(Inputs(now=1.0, chamber_temp=25.0))
        self.assertIs(out.state, ABORTED)
        self.assertFalse(out.heater_on)
        self.assertIs(out.conveyor, ConveyorCommand.STOP)

    def test_abort_with_a_hot_chamber_purges_first(self):
        c = make()
        t = to_drying(c)
        c.abort()
        out = c.update(Inputs(now=t, chamber_temp=68.0))
        self.assertIs(out.state, COOLDOWN)
        self.assertTrue(out.fans_on)
        self.assertFalse(out.heater_on)

    def test_abort_with_an_unknown_chamber_temperature_purges_first(self):
        c = make()
        t = to_drying(c)
        c.abort()
        self.assertIs(c.update(Inputs(now=t, chamber_temp=None)).state, COOLDOWN)

    def test_abort_closes_an_open_gate(self):
        c = make()
        t = to_intake(c)
        for _ in range(5):
            out = c.update(Inputs(now=t, chamber_temp=25.0, hopper_kg=2.0))
            t += 1
        self.assertTrue(out.gate_open)
        c.abort()
        self.assertFalse(c.update(Inputs(now=t, chamber_temp=25.0, hopper_kg=2.0)).gate_open)

    def test_reset_is_refused_mid_batch(self):
        c = make()
        to_drying(c)
        with self.assertRaises(RuntimeError):
            c.reset()

    def test_reset_returns_to_idle_and_clears_the_batch(self):
        c = make()
        to_intake(c)
        c.abort()
        c.update(Inputs(now=1.0, chamber_temp=25.0))
        c.reset()
        out = c.update(Inputs(now=2.0, chamber_temp=25.0))
        self.assertIs(out.state, IDLE)
        self.assertEqual(out.alarms, frozenset())
        self.assertIsNone(c.record())
        self.assertFalse(c.aborted)

    def test_a_second_batch_can_run_after_a_reset(self):
        c = make()
        to_intake(c)
        c.abort()
        c.update(Inputs(now=1.0, chamber_temp=25.0))
        c.reset()
        c.update(Inputs(now=2.0, chamber_temp=25.0))
        t = to_drying(c, t=3.0)
        self.assertIs(c.state, DRYING)
        self.assertAlmostEqual(c.elapsed_s(t), t - 3.0)


if __name__ == "__main__":
    unittest.main()
