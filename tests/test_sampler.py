"""Unit tests for batch-level ensemble vision grading and the SortingSampler.

Covers:
- Grade decision & tie-breaking rules
- Visual contamination guard (food safety)
- Softmax consensus weighting
- In-place retry logic and non-advancing on blurred/low-confidence captures
- Distinct-position counting (jog mode vs static mode)
- Static mode dev flag in snapshot
- Startup config validation & allowable travel bounds
- Needs_review triggers (timeout, excessive rejections, zero valid)
- State machine protection: no COMPLETE on grade_ready alone, no discharge before grade_ready
- Operator override flow for needs_review
- Schema migration from v1 to v2 preserving existing rows
- Legacy rendering checks
"""

import json
import sqlite3
import unittest
from pathlib import Path

from hardware import db, grading
from hardware.batch import BatchController, BatchState, Commands, Inputs, apply_command
from hardware.states import ConveyorCommand


class TestGradingLogic(unittest.TestCase):
    def test_startup_validation_valid(self):
        cfg = dict(grading.DEFAULT_GRADING_CONFIG)
        grading.validate_grading_config(cfg)

    def test_startup_validation_exceeds_travel(self):
        cfg = dict(grading.DEFAULT_GRADING_CONFIG)
        # 100 * 100 = 10000 steps, but camera_to_discharge is 5000 - 500 = 4500
        cfg["n_samples"] = 100
        cfg["steps_per_sample"] = 100
        with self.assertRaises(ValueError):
            grading.validate_grading_config(cfg)

    def test_startup_validation_unreachable_min_valid(self):
        cfg = dict(grading.DEFAULT_GRADING_CONFIG)
        cfg["min_valid"] = 25
        cfg["max_attempts"] = 20
        with self.assertRaises(ValueError):
            grading.validate_grading_config(cfg)

    def test_decide_zero_valid_vectors_guards_cleanly(self):
        grade, guard = grading.decide_grade([], tau_bad=0.20)
        self.assertIsNone(grade)
        self.assertFalse(guard)

    def test_soft_voting_weights_high_confidence(self):
        # 1 strong great vs 2 marginal good
        # great: [0.01, 0.04, 0.95]
        # good 1: [0.24, 0.51, 0.25]
        # good 2: [0.24, 0.51, 0.25]
        # sum: bad=0.49, good=1.06, great=1.45 -> great wins!
        vectors = [
            [0.01, 0.04, 0.95],
            [0.24, 0.51, 0.25],
            [0.24, 0.51, 0.25],
        ]
        grade, guard = grading.decide_grade(vectors, tau_bad=0.20)
        self.assertEqual(grade, "great")
        self.assertFalse(guard)

    def test_visual_contamination_guard_triggers_above_tau(self):
        # tau_bad = 0.20; 3 out of 10 are bad (30%)
        # remaining 7 are 99% great
        vectors = [[0.90, 0.05, 0.05]] * 3 + [[0.01, 0.01, 0.98]] * 7
        grade, guard = grading.decide_grade(vectors, tau_bad=0.20)
        self.assertEqual(grade, "bad")
        self.assertTrue(guard)

    def test_visual_contamination_guard_does_not_trigger_below_tau(self):
        # 1 out of 10 is bad (10% < 20%)
        vectors = [[0.90, 0.05, 0.05]] * 1 + [[0.01, 0.01, 0.98]] * 9
        grade, guard = grading.decide_grade(vectors, tau_bad=0.20)
        self.assertEqual(grade, "great")
        self.assertFalse(guard)

    def test_softmax_tie_breaking_prioritizes_safety(self):
        # bad vs good tied -> bad wins
        scores_bad_good = {"bad": 0.45, "good": 0.45, "great": 0.10}
        self.assertEqual(grading.break_tie(scores_bad_good), "bad")

        # good vs great tied -> good wins
        scores_good_great = {"bad": 0.10, "good": 0.45, "great": 0.45}
        self.assertEqual(grading.break_tie(scores_good_great), "good")


class TestSortingSampler(unittest.TestCase):
    def setUp(self):
        self.cfg = dict(grading.DEFAULT_GRADING_CONFIG)
        self.cfg["n_samples"] = 3
        self.cfg["min_valid"] = 2
        self.cfg["max_attempts"] = 10
        self.cfg["retry_max"] = 2

    def test_distinct_position_counting_jog_mode(self):
        sampler = grading.SortingSampler(self.cfg)
        # Pos 0: record valid
        sampler.record_capture([0.05, 0.15, 0.80])
        self.assertEqual(sampler.sample_valid_n, 1)

        # Pos 0: another capture at same position does NOT increment valid count
        sampler.record_capture([0.05, 0.15, 0.80])
        self.assertEqual(sampler.sample_valid_n, 1)

        # Step forward to Pos 1
        steps = sampler.step_forward()
        self.assertGreater(steps, 0)
        sampler.record_capture([0.05, 0.15, 0.80])
        self.assertEqual(sampler.sample_valid_n, 2)

    def test_static_mode_repeats_do_not_count_and_dev_flagged(self):
        cfg = dict(self.cfg)
        cfg["sample_mode"] = "static"
        sampler = grading.SortingSampler(cfg)

        self.assertFalse(sampler.can_jog_forward())
        sampler.record_capture([0.05, 0.15, 0.80])
        self.assertEqual(sampler.sample_valid_n, 1)

        # Subsequent captures in static mode do not count toward distinct positions
        sampler.record_capture([0.05, 0.15, 0.80])
        sampler.record_capture([0.05, 0.15, 0.80])
        self.assertEqual(sampler.sample_valid_n, 1)

        snapshot = json.loads(sampler.snapshot_config())
        self.assertTrue(snapshot["is_static_dev_mode"])
        self.assertEqual(snapshot["sample_mode"], "static")

    def test_retry_mechanism_retries_at_same_position(self):
        sampler = grading.SortingSampler(self.cfg)
        # Attempt 1 failed (blur)
        sampler.record_capture(None)
        self.assertEqual(sampler.total_attempts, 1)
        self.assertEqual(sampler.position_attempts, 1)
        self.assertEqual(sampler.position_index, 0)

        # Attempt 2 success
        sampler.record_capture([0.1, 0.8, 0.1])
        self.assertEqual(sampler.sample_valid_n, 1)
        self.assertEqual(sampler.position_index, 0)

    def test_needs_review_on_excessive_rejections(self):
        sampler = grading.SortingSampler(self.cfg)
        # Feed 5 consecutive None captures -> 100% rejection > max_rejected_ratio (0.40)
        for _ in range(5):
            sampler.record_capture(None)
        self.assertTrue(sampler.grade_ready)
        self.assertTrue(sampler.needs_review)

    def test_software_jog_cap_never_exceeded(self):
        cfg = dict(self.cfg)
        cfg["camera_to_discharge_steps"] = 600
        cfg["safety_margin_steps"] = 100  # cap = 500
        cfg["steps_per_sample"] = 200
        cfg["n_samples"] = 2
        cfg["min_valid"] = 1
        sampler = grading.SortingSampler(cfg)

        # 0 -> 200
        self.assertEqual(sampler.step_forward(), 200)
        # 200 -> 400
        self.assertEqual(sampler.step_forward(), 200)
        # 400 + 200 = 600 > 500 cap -> refused!
        self.assertFalse(sampler.can_jog_forward())
        self.assertEqual(sampler.step_forward(), 0)


class TestBatchStateMachineSorting(unittest.TestCase):
    def setUp(self):
        self.bc = BatchController()
        # Fast-forward to SORTING
        self.bc.state = BatchState.SORTING
        self.bc.started_at = 100.0
        self.bc._entered_at = 100.0

    def test_no_complete_on_grade_ready_alone(self):
        # Grade is ready, but sorting_complete is False -> machine MUST stay in SORTING
        cmd = self.bc.update(Inputs(now=101.0, grade_ready=True, batch_grade="great", sorting_complete=False))
        self.assertEqual(cmd.state, BatchState.SORTING)
        self.assertEqual(cmd.sorting_stage, "discharge")
        self.assertEqual(cmd.carriage_target, "great")

    def test_no_discharge_before_grade_ready(self):
        # During sampling, conveyor MUST be STOP
        cmd = self.bc.update(Inputs(now=101.0, grade_ready=False, sorting_complete=False))
        self.assertEqual(cmd.conveyor, ConveyorCommand.STOP)
        self.assertEqual(cmd.sorting_stage, "sample")
        self.assertIsNone(cmd.carriage_target)

    def test_needs_review_halts_discharge_until_override(self):
        # Review flagged
        cmd = self.bc.update(Inputs(now=101.0, grade_ready=True, needs_review=True, batch_grade="bad"))
        self.assertEqual(cmd.conveyor, ConveyorCommand.STOP)
        self.assertEqual(cmd.sorting_stage, "await_review")
        self.assertIn("sorting_needs_review", self.bc.alarms)

        # Operator overrides grade
        msg = apply_command(self.bc, "override_grade", "good")
        self.assertIn("overridden to good", msg)
        self.assertNotIn("sorting_needs_review", self.bc.alarms)

        # Next tick can now discharge to the chosen bin
        cmd2 = self.bc.update(Inputs(now=102.0, grade_ready=True, needs_review=True))
        self.assertEqual(cmd2.sorting_stage, "discharge")
        self.assertEqual(cmd2.carriage_target, "good")
        self.assertEqual(cmd2.conveyor, ConveyorCommand.SORT_CREEP)

    def test_complete_reached_only_when_sorting_complete_received(self):
        self.bc.update(Inputs(now=101.0, grade_ready=True, batch_grade="great"))
        cmd = self.bc.update(Inputs(now=105.0, grade_ready=True, batch_grade="great", sorting_complete=True, sorted_kg=1.85))
        self.assertEqual(cmd.state, BatchState.COMPLETE)
        self.assertEqual(self.bc.state, BatchState.COMPLETE)

        rec = self.bc.record()
        self.assertIsNotNone(rec)
        self.assertEqual(rec.batch_grade, "great")
        self.assertEqual(rec.weight_out_kg, 1.85)


class TestDatabaseMigrationV2(unittest.TestCase):
    def test_v1_to_v2_migration_preserves_rows(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        # Create v1 schema
        conn.execute("PRAGMA user_version = 1")
        conn.execute("""
            CREATE TABLE batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                state TEXT NOT NULL,
                duration_s REAL,
                drying_s REAL,
                target_kg REAL,
                weight_in_kg REAL,
                weight_out_kg REAL,
                weight_loss_pct REAL,
                aborted INTEGER NOT NULL DEFAULT 0,
                abort_reason TEXT,
                alarms TEXT,
                great INTEGER,
                good INTEGER,
                bad INTEGER,
                final_moisture_pct REAL
            )
        """)
        # Insert legacy row
        conn.execute("INSERT INTO batches (started_at, state, great, good, bad) VALUES ('2026-10-01', 'complete', 10, 5, 1)")

        # Run init which triggers migration
        db.init(conn)

        # Verify version updated
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(ver, db.SCHEMA_VERSION)

        # Verify row preserved and new columns present with legacy nulls
        row = conn.execute("SELECT * FROM batches WHERE id = 1").fetchone()
        self.assertEqual(row["great"], 10)
        self.assertIsNone(row["batch_grade"])
        self.assertEqual(row["needs_review"], 0)
        conn.close()


if __name__ == "__main__":
    unittest.main()
