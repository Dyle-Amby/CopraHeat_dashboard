"""Unit tests for the SortingFlow state machine (hardware/sorting.py).

Verifies all stages (POSITION, SAMPLE, REVIEW, CARRIAGE, DISCHARGE, VERIFY, DONE),
non-blocking timers, prompt generation and resolution, and scale-based discharge verification.
"""

import unittest
from hardware.batch import ConveyorCommand
from hardware.sorting import Prompt, SortingFlow, SortingStage


class TestSortingFlow(unittest.TestCase):

    def setUp(self):
        self.flow = SortingFlow(
            chamber_to_camera_steps=2000,
            discharge_total_steps=3000,
            sample_hz=400.0,
            settle_s=0.3,
            bin_settle_s=1.0,
            bin_verify_timeout_s=5.0,
            min_weight_rise_kg=0.05,
            carriage_move_timeout_s=10.0,
            discharge_timeout_s=20.0,
            position_timeout_s=15.0,
        )
        self.flow.start(now=100.0)

    def test_initial_state_is_position(self):
        self.assertEqual(self.flow.stage, SortingStage.POSITION)
        self.assertFalse(self.flow.is_complete)
        self.assertIsNone(self.flow.active_prompt)

    def test_position_uncalibrated_raises_prompt(self):
        flow = SortingFlow(chamber_to_camera_steps=0)
        flow.start(now=100.0)
        flow.update(now=100.0, conveyor_busy=False)
        self.assertIsNotNone(flow.active_prompt)
        self.assertEqual(flow.active_prompt.id, "position_uncalibrated")

        # Answer prompt with override -> enters SAMPLE
        answered = flow.answer_prompt("override", now=101.0)
        self.assertTrue(answered)
        self.assertEqual(flow.stage, SortingStage.SAMPLE)

    def test_position_normal_transition_to_sample(self):
        # Tick 1: Initiates position move
        self.flow.update(now=100.0, conveyor_busy=False)
        self.assertEqual(self.flow.conveyor_move_steps, 2000)
        self.assertEqual(self.flow.conveyor_command, ConveyorCommand.SORT_CREEP)

        # Tick 2: Conveyor busy
        self.flow.update(now=101.0, conveyor_busy=True)
        self.assertEqual(self.flow.stage, SortingStage.POSITION)

        # Tick 3: Conveyor done, copra verified present
        self.flow.update(now=105.0, conveyor_busy=False, conveyor_last_result="done", copra_present=True)
        self.assertEqual(self.flow.stage, SortingStage.SAMPLE)
        self.assertIsNone(self.flow.active_prompt)

    def test_position_empty_belt_raises_prompt(self):
        # Tick 1: Start move
        self.flow.update(now=100.0, conveyor_busy=False)
        # Tick 2: Move done, but empty belt detected
        self.flow.update(now=105.0, conveyor_busy=False, conveyor_last_result="done", copra_present=False)
        self.assertEqual(self.flow.stage, SortingStage.POSITION)
        self.assertIsNotNone(self.flow.active_prompt)
        self.assertEqual(self.flow.active_prompt.id, "empty_belt_detected")

        # Operator chooses proceed anyway
        self.flow.answer_prompt("proceed_anyway", now=106.0)
        self.assertEqual(self.flow.stage, SortingStage.SAMPLE)

    def test_sample_stage_jog_settle_capture_cycle(self):
        # Move directly to sample stage
        self.flow.stage = SortingStage.SAMPLE
        self.flow.stage_entered_at = 100.0

        # Tick 1: Initial sample point (position 0) -> move_started, completed_at set
        self.flow.update(now=100.0, conveyor_busy=False)
        self.assertFalse(self.flow.capture_requested)

        # Tick 2: During settle time (< 0.3s)
        self.flow.update(now=100.1, conveyor_busy=False)
        self.assertFalse(self.flow.capture_requested)

        # Tick 3: Settle delay elapsed (0.3s) -> capture requested!
        self.flow.update(now=100.35, conveyor_busy=False)
        self.assertTrue(self.flow.capture_requested)

        # Tick 4: Feed valid capture frame vector [bad, good, great]
        fake_vec = [0.05, 0.15, 0.80]  # clearly great
        self.flow.update(now=100.4, conveyor_busy=False, frame_vector=fake_vec)
        self.assertFalse(self.flow.capture_requested)
        self.assertEqual(self.flow.sampler.sample_valid_n, 1)

    def test_sample_completion_moves_to_carriage(self):
        flow = SortingFlow(
            config={"n_samples": 4, "min_valid": 2, "max_attempts": 10, "steps_per_sample": 100},
            settle_s=0.1,
        )
        flow.start(now=100.0)
        flow.stage = SortingStage.SAMPLE
        flow.stage_entered_at = 100.0

        now = 100.0
        while flow.stage is SortingStage.SAMPLE and now < 200.0:
            flow.update(now=now, conveyor_busy=False)
            if flow.capture_requested:
                flow.update(now=now, conveyor_busy=False, frame_vector=[0.05, 0.15, 0.80])
            now += 0.2

        self.assertEqual(flow.stage, SortingStage.CARRIAGE)
        self.assertEqual(flow.batch_grade, "great")
        self.assertIsNone(flow.active_prompt)

    def test_carriage_timeout_raises_prompt(self):
        self.flow.stage = SortingStage.CARRIAGE
        self.flow.stage_entered_at = 100.0
        self.flow.sampler.batch_grade = "good"

        # Start carriage goto
        self.flow.update(now=100.0, conveyor_busy=False, carriage_state="moving")
        self.assertEqual(self.flow.carriage_target, "good")

        # Exceed timeout (10.0s)
        self.flow.update(now=111.0, conveyor_busy=False, carriage_state="moving")
        self.assertIsNotNone(self.flow.active_prompt)
        self.assertEqual(self.flow.active_prompt.id, "carriage_fault")
        self.assertIn("carriage_fault", self.flow.alarms)

        # Retry carriage
        self.flow.answer_prompt("retry", now=112.0)
        self.assertIsNone(self.flow.active_prompt)

    def test_carriage_arrival_advances_to_discharge(self):
        self.flow.stage = SortingStage.CARRIAGE
        self.flow.stage_entered_at = 100.0
        self.flow.sampler.batch_grade = "good"

        # Carriage arrives at target
        self.flow.update(
            now=102.0,
            conveyor_busy=False,
            carriage_state="at_target",
            carriage_current_bin="good",
            bin_kg=12.5,
        )
        self.assertEqual(self.flow.stage, SortingStage.DISCHARGE)
        self.assertEqual(self.flow.baseline_bin_kg, 12.5)

    def test_discharge_and_verify_weight_gain(self):
        self.flow.stage = SortingStage.DISCHARGE
        self.flow.stage_entered_at = 100.0
        self.flow.baseline_bin_kg = 10.0

        # Tick 1: Starts discharge conveyor move
        self.flow.update(now=100.0, conveyor_busy=False)
        self.assertEqual(self.flow.conveyor_move_steps, 3000)

        # Tick 2: Move done -> enters VERIFY
        self.flow.update(now=108.0, conveyor_busy=False, conveyor_last_result="done", bin_kg=10.0)
        self.assertEqual(self.flow.stage, SortingStage.VERIFY)

        # Tick 3: Scale settles (1.0s) and rises by 0.50 kg (10.50 kg)
        self.flow.update(now=109.5, conveyor_busy=False, bin_kg=10.50)
        self.assertEqual(self.flow.stage, SortingStage.DONE)
        self.assertTrue(self.flow.is_complete)
        self.assertAlmostEqual(self.flow.weight_out_kg, 0.50)

    def test_verify_no_weight_rise_raises_prompt(self):
        self.flow.stage = SortingStage.VERIFY
        self.flow.stage_entered_at = 100.0
        self.flow._verify_started_at = 100.0
        self.flow.baseline_bin_kg = 10.0

        # After verify timeout (5.0s), bin weight didn't change
        self.flow.update(now=106.0, conveyor_busy=False, bin_kg=10.0)
        self.assertIsNotNone(self.flow.active_prompt)
        self.assertEqual(self.flow.active_prompt.id, "bin_did_not_gain_weight")

        # Operator overrides weight check
        self.flow.answer_prompt("override", now=107.0)
        self.assertEqual(self.flow.stage, SortingStage.DONE)
        self.assertTrue(self.flow.is_complete)


if __name__ == "__main__":
    unittest.main()
