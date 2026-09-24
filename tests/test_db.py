import tempfile
import unittest
from pathlib import Path

from hardware import db
from hardware.batch import BatchRecord

RECORD = BatchRecord(
    duration_s=6420.0,
    drying_s=5940.0,
    weight_in_kg=2.0,
    weight_out_kg=1.1,
    weight_loss_pct=45.0,
    aborted=False,
    abort_reason=None,
    alarms=frozenset({"overtemp"}),
)


class DBCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "cords.db"
        self.conn = db.connect(self.path)
        db.init(self.conn)

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()


class TestSchema(DBCase):
    def test_tables_exist(self):
        names = {
            row["name"]
            for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        self.assertLessEqual(
            {"batches", "sensor_log", "commands", "machine_state", "settings"}, names
        )

    def test_schema_version_is_stamped(self):
        self.assertEqual(self.conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)

    def test_wal_is_on(self):
        self.assertEqual(self.conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")

    def test_init_is_idempotent_and_keeps_edited_settings(self):
        db.set_setting(self.conn, "target_kg", 3.5)
        db.init(self.conn)
        self.assertEqual(db.get_settings(self.conn)["target_kg"], "3.5")

    def test_a_future_schema_refuses_to_open(self):
        self.conn.execute(f"PRAGMA user_version={db.SCHEMA_VERSION + 1}")
        with self.assertRaises(RuntimeError):
            db.init(self.conn)

    def test_no_ambient_or_live_moisture_columns(self):
        # there is no ambient sensor and moisture is not measured; weight loss is
        # a post-sort figure on `batches`, never a live sample
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(sensor_log)")}
        self.assertNotIn("ambient_c", columns)
        self.assertNotIn("moisture_pct", columns)
        self.assertNotIn("weight_loss_pct", columns)
        self.assertLessEqual({"chamber_c", "exhaust_c", "exhaust_rh", "exhaust_ah"}, columns)


class TestBatches(DBCase):
    def test_a_batch_row_exists_from_the_start(self):
        batch_id = db.start_batch(self.conn, target_kg=2.0)
        row = db.get_batch(self.conn, batch_id)
        self.assertEqual(row["state"], "intake")
        self.assertIsNotNone(row["started_at"])
        self.assertIsNone(row["finished_at"])

    def test_finishing_writes_the_record(self):
        batch_id = db.start_batch(self.conn, target_kg=2.0)
        db.finish_batch(self.conn, batch_id, RECORD, "complete")
        row = db.get_batch(self.conn, batch_id)
        self.assertEqual(row["state"], "complete")
        self.assertAlmostEqual(row["weight_loss_pct"], 45.0)
        self.assertAlmostEqual(row["drying_s"], 5940.0)
        self.assertEqual(row["aborted"], 0)
        self.assertEqual(row["alarms"], ["overtemp"])
        self.assertIsNotNone(row["finished_at"])

    def test_vision_columns_stay_null(self):
        batch_id = db.start_batch(self.conn, target_kg=2.0)
        db.finish_batch(self.conn, batch_id, RECORD, "complete")
        row = db.get_batch(self.conn, batch_id)
        for column in ("great", "good", "bad", "final_moisture_pct"):
            self.assertIsNone(row[column])

    def test_recent_batches_are_newest_first(self):
        ids = [db.start_batch(self.conn, target_kg=2.0) for _ in range(3)]
        rows = db.recent_batches(self.conn, limit=2)
        self.assertEqual([row["id"] for row in rows], [ids[2], ids[1]])

    def test_an_unfinished_batch_is_marked_interrupted(self):
        batch_id = db.start_batch(self.conn, target_kg=2.0)
        self.assertEqual(db.mark_interrupted_batches(self.conn), 1)
        row = db.get_batch(self.conn, batch_id)
        self.assertEqual(row["state"], "interrupted")
        self.assertEqual(row["aborted"], 1)
        self.assertIsNotNone(row["finished_at"])

    def test_finished_batches_are_left_alone(self):
        batch_id = db.start_batch(self.conn, target_kg=2.0)
        db.finish_batch(self.conn, batch_id, RECORD, "complete")
        self.assertEqual(db.mark_interrupted_batches(self.conn), 0)
        self.assertEqual(db.get_batch(self.conn, batch_id)["state"], "complete")


class TestBatchState(DBCase):
    def test_a_running_batch_follows_its_phase(self):
        batch_id = db.start_batch(self.conn, target_kg=2.0)
        db.set_batch_state(self.conn, batch_id, "drying")
        self.assertEqual(db.get_batch(self.conn, batch_id)["state"], "drying")

    def test_a_finished_batch_keeps_its_final_state(self):
        batch_id = db.start_batch(self.conn, target_kg=2.0)
        db.finish_batch(self.conn, batch_id, RECORD, "complete")
        db.set_batch_state(self.conn, batch_id, "drying")
        self.assertEqual(db.get_batch(self.conn, batch_id)["state"], "complete")


class TestSensorLog(DBCase):
    def test_samples_come_back_in_order_for_their_batch(self):
        one = db.start_batch(self.conn, target_kg=2.0)
        two = db.start_batch(self.conn, target_kg=2.0)
        for temp in (60.0, 65.0, 70.0):
            db.log_sample(self.conn, batch_id=one, state="drying", chamber_c=temp)
        db.log_sample(self.conn, batch_id=two, state="drying", chamber_c=99.0)

        samples = db.batch_samples(self.conn, one)
        self.assertEqual([s["chamber_c"] for s in samples], [60.0, 65.0, 70.0])

    def test_after_id_returns_only_newer_rows(self):
        batch_id = db.start_batch(self.conn, target_kg=2.0)
        for temp in (60.0, 65.0, 70.0):
            db.log_sample(self.conn, batch_id=batch_id, state="drying", chamber_c=temp)
        first = db.batch_samples(self.conn, batch_id)[0]["id"]
        newer = db.batch_samples(self.conn, batch_id, after_id=first)
        self.assertEqual([s["chamber_c"] for s in newer], [65.0, 70.0])

    def test_idle_samples_have_no_batch(self):
        db.log_sample(self.conn, batch_id=None, state="idle", chamber_c=30.0)
        row = self.conn.execute("SELECT * FROM sensor_log").fetchone()
        self.assertIsNone(row["batch_id"])
        self.assertEqual(row["state"], "idle")

    def test_missing_readings_are_stored_as_null(self):
        batch_id = db.start_batch(self.conn, target_kg=2.0)
        db.log_sample(self.conn, batch_id=batch_id, state="drying")
        sample = db.batch_samples(self.conn, batch_id)[0]
        self.assertIsNone(sample["chamber_c"])
        self.assertEqual(sample["heater_on"], 0)


class TestLiveState(DBCase):
    def test_none_before_the_supervisor_ever_ran(self):
        self.assertIsNone(db.live_state(self.conn))

    def test_the_snapshot_stays_a_single_row(self):
        db.update_live(self.conn, state="idle", supervisor_pid=1)
        db.update_live(self.conn, state="drying", supervisor_pid=1, chamber_c=65.0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM machine_state").fetchone()[0], 1)
        self.assertEqual(db.live_state(self.conn)["state"], "drying")

    def test_flags_come_back_as_booleans_and_alarms_as_a_list(self):
        db.update_live(
            self.conn, state="drying", supervisor_pid=1,
            heater_on=True, fans_on=False, gate_open=False,
            alarms=frozenset({"overtemp", "hopper_jam"}),
        )
        live = db.live_state(self.conn)
        self.assertIs(live["heater_on"], True)
        self.assertIs(live["fans_on"], False)
        self.assertEqual(live["alarms"], ["hopper_jam", "overtemp"])

    def test_a_fresh_heartbeat_reads_as_running(self):
        db.update_live(self.conn, state="drying", supervisor_pid=4242)
        live = db.live_state(self.conn)
        self.assertFalse(live["stale"])
        self.assertTrue(live["running"])

    def test_a_stale_heartbeat_is_not_running(self):
        db.update_live(self.conn, state="drying", supervisor_pid=4242)
        self.conn.execute("UPDATE machine_state SET updated_at = '2020-01-01T00:00:00' WHERE id = 1")
        live = db.live_state(self.conn)
        self.assertTrue(live["stale"])
        self.assertFalse(live["running"])

    def test_a_clean_stop_clears_the_pid(self):
        db.update_live(self.conn, state="idle", supervisor_pid=4242)
        db.mark_supervisor_stopped(self.conn)
        live = db.live_state(self.conn)
        self.assertIsNone(live["supervisor_pid"])
        self.assertFalse(live["running"])
        self.assertFalse(live["stale"])   # stopped on purpose, not silently dead


class TestCommands(DBCase):
    def test_a_queued_command_starts_pending(self):
        command_id = db.queue_command(self.conn, "start")
        self.assertEqual(db.command_status(self.conn, command_id)["status"], db.PENDING)

    def test_unknown_commands_are_refused_at_the_door(self):
        with self.assertRaises(ValueError):
            db.queue_command(self.conn, "launch_missiles")

    def test_pending_commands_come_back_oldest_first(self):
        first = db.queue_command(self.conn, "start")
        second = db.queue_command(self.conn, "abort", "operator abort")
        self.assertEqual([row["id"] for row in db.pending_commands(self.conn)], [first, second])

    def test_resolved_commands_drop_out_of_the_queue(self):
        command_id = db.queue_command(self.conn, "start")
        db.resolve_command(self.conn, command_id, db.DONE, "batch will start on the next tick")
        self.assertEqual(db.pending_commands(self.conn), [])
        row = db.command_status(self.conn, command_id)
        self.assertEqual(row["status"], db.DONE)
        self.assertEqual(row["result"], "batch will start on the next tick")
        self.assertIsNotNone(row["resolved_at"])

    def test_a_refusal_keeps_the_machines_own_words(self):
        command_id = db.queue_command(self.conn, "end_drying")
        db.resolve_command(self.conn, command_id, db.REJECTED, "not drying (state is idle)")
        row = db.command_status(self.conn, command_id)
        self.assertEqual(row["status"], db.REJECTED)
        self.assertIn("not drying", row["result"])

    def test_only_done_or_rejected_are_valid_outcomes(self):
        command_id = db.queue_command(self.conn, "start")
        with self.assertRaises(ValueError):
            db.resolve_command(self.conn, command_id, "maybe")

    def test_commands_left_pending_expire_rather_than_run_late(self):
        stale = db.queue_command(self.conn, "start")
        answered = db.queue_command(self.conn, "abort")
        db.resolve_command(self.conn, answered, db.REJECTED, "no batch to abort")
        self.assertEqual(db.expire_pending_commands(self.conn), 1)
        self.assertEqual(db.pending_commands(self.conn), [])
        row = db.command_status(self.conn, stale)
        self.assertEqual(row["status"], db.REJECTED)
        self.assertIn("expired", row["result"])
        # an already-answered command keeps its own answer
        self.assertEqual(db.command_status(self.conn, answered)["result"], "no batch to abort")

    def test_payloads_survive_the_round_trip(self):
        command_id = db.queue_command(self.conn, "set_target_kg", "3.5")
        self.assertEqual(db.pending_commands(self.conn)[0]["payload"], "3.5")
        self.assertEqual(db.command_status(self.conn, command_id)["name"], "set_target_kg")


class TestSettings(DBCase):
    def test_defaults_are_seeded(self):
        self.assertEqual(db.get_settings(self.conn), db.DEFAULT_SETTINGS)

    def test_setting_a_value_overwrites_rather_than_duplicating(self):
        db.set_setting(self.conn, "target_kg", 3.0)
        db.set_setting(self.conn, "target_kg", 4.0)
        self.assertEqual(db.get_settings(self.conn)["target_kg"], "4.0")
        count = self.conn.execute("SELECT COUNT(*) FROM settings WHERE key = 'target_kg'").fetchone()[0]
        self.assertEqual(count, 1)

    def test_new_keys_can_be_added(self):
        db.set_setting(self.conn, "drying_flat_slope", 0.04)
        self.assertEqual(db.get_settings(self.conn)["drying_flat_slope"], "0.04")


if __name__ == "__main__":
    unittest.main()
