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
            camera_to_discharge_steps=1500,
            sample_hz=400.0,
            conveyor_hz=1600.0,
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

        # Answer prompt with override -> enters operator staging
        answered = flow.answer_prompt("override", now=101.0)
        self.assertTrue(answered)
        self.assertEqual(flow.stage, SortingStage.STAGE_PIECE)

    def test_position_stops_for_operator_staging(self):
        # Tick 1: Initiates position move
        self.flow.update(now=100.0, conveyor_busy=False)
        self.assertEqual(self.flow.conveyor_move_steps, 2000)
        self.assertEqual(self.flow.conveyor_move_hz, 1600.0)
        self.assertEqual(self.flow.conveyor_command, ConveyorCommand.SORT_CREEP)

        # Tick 2: Conveyor busy
        self.flow.update(now=101.0, conveyor_busy=True)
        self.assertEqual(self.flow.stage, SortingStage.POSITION)

        # Tick 3: Conveyor done; wait for operator to isolate one piece.
        self.flow.update(now=105.0, conveyor_busy=False, conveyor_last_result="done", copra_present=True)
        self.assertEqual(self.flow.stage, SortingStage.STAGE_PIECE)
        self.assertEqual(self.flow.active_prompt.id, "stage_piece_1")

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
        self.assertEqual(self.flow.stage, SortingStage.STAGE_PIECE)

    def test_stage_settle_and_capture_cycle(self):
        self.flow.stage = SortingStage.SAMPLE
        self.flow.stage_entered_at = 100.0
        self.flow._sample_move_completed_at = 100.0

        # Wait for the short settle interval after operator staging.
        self.flow.update(now=100.0, conveyor_busy=False)
        self.assertFalse(self.flow.capture_requested)
        self.flow.update(now=100.35, conveyor_busy=False)
        self.assertTrue(self.flow.capture_requested)

        fake_vec = [0.05, 0.15, 0.80]  # clearly great
        self.flow.update(now=100.4, conveyor_busy=False, frame_vector=fake_vec)
        self.assertFalse(self.flow.capture_requested)
        self.assertEqual(self.flow.stage, SortingStage.REVIEW)
        self.assertEqual(self.flow.active_prompt.id, "piece_review_1_1")
        self.assertEqual(self.flow.piece_prediction["grade"], "great")

    def test_single_piece_capture_waits_for_operator_review(self):
        flow = SortingFlow(
            config={"n_samples": 1, "min_valid": 1, "max_attempts": 1},
            chamber_to_camera_steps=2000,
            camera_to_discharge_steps=1500,
            conveyor_hz=1600.0,
            sample_hz=400.0,
        )
        flow.start(now=100.0)
        flow.stage = SortingStage.SAMPLE

        flow.update(now=100.0, conveyor_busy=False)
        flow.update(now=100.35, conveyor_busy=False)
        self.assertTrue(flow.capture_requested)

        flow.update(
            now=100.4,
            conveyor_busy=False,
            frame_vector=[0.05, 0.15, 0.80],
            capture_done=True,
        )

        self.assertEqual(flow.stage, SortingStage.REVIEW)
        self.assertEqual(flow.active_prompt.id, "piece_review_1_1")
        self.assertEqual(flow.conveyor_command, ConveyorCommand.STOP)

    def test_retake_keeps_piece_at_camera_for_another_review(self):
        flow = SortingFlow(
            chamber_to_camera_steps=2000,
            camera_to_discharge_steps=1500,
        )
        flow.start(now=100.0)
        flow.stage = SortingStage.REVIEW
        flow.piece_prediction = {"grade": "good", "confidence": 0.8}
        flow.active_prompt = Prompt(
            id="piece_review_1", message="review", options=["retake"], created_at=100.0
        )
        self.assertTrue(flow.answer_prompt("retake", now=101.0))
        self.assertEqual(flow.stage, SortingStage.SAMPLE)
        self.assertEqual(flow.conveyor_move_steps, 0)

    def test_operator_bin_choice_becomes_carriage_target(self):
        flow = SortingFlow(chamber_to_camera_steps=2000, camera_to_discharge_steps=1500)
        flow.start(now=100.0)
        flow.stage = SortingStage.REVIEW
        flow.piece_prediction = {"grade": "good", "confidence": 0.8, "scores": {"good": 0.8}}
        flow.active_prompt = Prompt(
            id="piece_review_1_1", message="review", options=["bad"], created_at=100.0
        )

        self.assertTrue(flow.answer_prompt("bad", now=101.0))
        flow.update(now=102.0, conveyor_busy=False, carriage_state="moving")

        self.assertEqual(flow.piece_grade, "bad")
        self.assertEqual(flow.carriage_target, "bad")

    def test_more_pieces_loops_back_to_next_operator_staging_prompt(self):
        flow = SortingFlow(chamber_to_camera_steps=2000, camera_to_discharge_steps=1500)
        flow.start(now=100.0)
        flow._more_pieces_prompt(100.0)

        self.assertTrue(flow.answer_prompt("yes_more", now=101.0))

        self.assertEqual(flow.piece_number, 2)
        self.assertEqual(flow.stage, SortingStage.STAGE_PIECE)
        self.assertEqual(flow.active_prompt.id, "stage_piece_2")

    def test_bin_settle_uses_ten_seconds_by_default(self):
        self.assertEqual(SortingFlow().bin_settle_s, 10.0)

    def test_missing_camera_to_drop_calibration_prompts_without_moving(self):
        flow = SortingFlow(camera_to_discharge_steps=0)
        flow.stage = SortingStage.DISCHARGE

        flow.update(now=100.0, conveyor_busy=False)

        self.assertEqual(flow.active_prompt.id, "discharge_uncalibrated")
        self.assertEqual(flow.conveyor_move_steps, 0)

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
        self.assertEqual(self.flow.conveyor_move_steps, 1500)

        # Tick 2: Move done -> enters VERIFY
        self.flow.update(now=108.0, conveyor_busy=False, conveyor_last_result="done", bin_kg=10.0)
        self.assertEqual(self.flow.stage, SortingStage.VERIFY)

        # Tick 3: Scale settles and records 5 stable readings within 0.03 kg (Item 8)
        for t in (109.1, 109.2, 109.3, 109.4, 109.5):
            self.flow.update(now=t, conveyor_busy=False, bin_kg=10.50)
        self.assertEqual(self.flow.stage, SortingStage.MORE_PIECES)
        self.assertFalse(self.flow.is_complete)
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

        # Operator records the drop as unverified, then may continue the batch.
        self.flow.answer_prompt("override", now=107.0)
        self.assertEqual(self.flow.stage, SortingStage.MORE_PIECES)
        self.assertFalse(self.flow.is_complete)
        self.assertIsNone(self.flow.weight_out_kg)


if __name__ == "__main__":
    unittest.main()
