import os
import tempfile
import unittest
from unittest.mock import patch

from app import create_app, validate_command
from hardware import db


class TestCalibrationLockDB(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.conn = db.connect(self.tmp.name)
        db.init(self.conn)

    def tearDown(self):
        self.conn.close()
        try:
            os.remove(self.tmp.name)
        except Exception:
            pass

    def test_acquire_and_release(self):
        self.assertFalse(db.is_calibration_locked(self.conn))
        self.assertIsNone(db.get_calibration_lock(self.conn))

        ok = db.acquire_calibration_lock(self.conn, duration_s=10, component="conveyor", session_id="s1")
        self.assertTrue(ok)
        self.assertTrue(db.is_calibration_locked(self.conn))

        lock = db.get_calibration_lock(self.conn)
        self.assertEqual(lock["component"], "conveyor")
        self.assertEqual(lock["session_id"], "s1")

        # Conflict from another session is refused
        ok2 = db.acquire_calibration_lock(self.conn, duration_s=10, component="hopper", session_id="s2")
        self.assertFalse(ok2)

        # Release
        released = db.release_calibration_lock(self.conn, session_id="s1")
        self.assertTrue(released)
        self.assertFalse(db.is_calibration_locked(self.conn))

    def test_lock_timeout(self):
        # Acquire with 0 second duration (instant timeout)
        db.acquire_calibration_lock(self.conn, duration_s=-1.0, component="conveyor")
        self.assertFalse(db.is_calibration_locked(self.conn))
        self.assertIsNone(db.get_calibration_lock(self.conn))

    def test_new_supervisor_clears_previous_instance_lock(self):
        db.acquire_calibration_lock(self.conn, duration_s=600, component="conveyor")
        self.assertTrue(db.is_calibration_locked(self.conn))

        self.assertTrue(db.clear_calibration_lock(self.conn))

        self.assertFalse(db.is_calibration_locked(self.conn))
        self.assertFalse(db.clear_calibration_lock(self.conn))


class TestValidateCommand(unittest.TestCase):
    def test_string_payloads(self):
        name, payload = validate_command("set_target_kg", "3.5")
        self.assertEqual(name, "set_target_kg")
        self.assertEqual(payload, "3.5")

        name, payload = validate_command("override_grade", "good")
        self.assertEqual(name, "override_grade")
        self.assertEqual(payload, "good")

        name, payload = validate_command("abort", "emergency test")
        self.assertEqual(name, "abort")
        self.assertEqual(payload, "emergency test")

    def test_json_object_payloads(self):
        # Dictionary payload for set_target_kg
        name, payload = validate_command("set_target_kg", {"target_kg": 4.2})
        self.assertEqual(name, "set_target_kg")
        self.assertEqual(payload, "4.2")

        # Dictionary payload for maintenance command
        name, payload = validate_command("jog_conveyor", {"steps": 500, "hz": 300.0, "direction": "forward"})
        self.assertEqual(name, "jog_conveyor")
        self.assertIn('"steps": 500', payload)


class TestMaintenanceGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name
        self.app = create_app(db_path=self.db_path)
        self.client = self.app.test_client()

    def tearDown(self):
        try:
            os.remove(self.db_path)
        except Exception:
            pass

    def _set_live(self, state="idle", supervisor_pid=1234, heater_on=False):
        conn = db.connect(self.db_path)
        db.update_live(
            conn,
            state=state,
            supervisor_pid=supervisor_pid,
            heater_on=heater_on,
        )
        conn.close()

    def test_supervisor_must_be_running(self):
        self._set_live(supervisor_pid=None)
        res = self.client.post("/api/commands", json={"name": "jog_conveyor", "payload": {}})
        self.assertEqual(res.status_code, 409)
        self.assertIn("supervisor stopped", res.get_json()["error"])

    def test_maintenance_gate_idle_only(self):
        self._set_live(state="drying", supervisor_pid=1234)
        res = self.client.post("/api/commands", json={"name": "jog_conveyor", "payload": {}})
        self.assertEqual(res.status_code, 409)
        self.assertIn("IDLE", res.get_json()["error"])

    def test_maintenance_gate_heaters_off(self):
        self._set_live(state="idle", supervisor_pid=1234, heater_on=True)
        res = self.client.post("/api/commands", json={"name": "jog_conveyor", "payload": {}})
        self.assertEqual(res.status_code, 409)
        self.assertIn("heaters to be off", res.get_json()["error"])

    def test_maintenance_gate_pin_protection(self):
        self._set_live(state="idle", supervisor_pid=1234, heater_on=False)
        with patch.dict(os.environ, {"CORDS_PIN": "1234"}):
            # Missing PIN
            res = self.client.post("/api/commands", json={"name": "acquire_calibration_lock", "payload": {}})
            self.assertEqual(res.status_code, 403)
            self.assertIn("PIN", res.get_json()["error"])

            # Correct PIN in header
            res = self.client.post(
                "/api/commands",
                json={"name": "acquire_calibration_lock", "payload": {}},
                headers={"X-CORDS-PIN": "1234"}
            )
            self.assertEqual(res.status_code, 202)

    def test_calibration_lock_required_for_maintenance(self):
        self._set_live(state="idle", supervisor_pid=1234, heater_on=False)
        # Without lock
        res = self.client.post("/api/commands", json={"name": "jog_conveyor", "payload": {"steps": 100}})
        self.assertEqual(res.status_code, 409)
        self.assertIn("calibration lock required", res.get_json()["error"])

        # Acquire lock
        res_lock = self.client.post("/api/commands", json={"name": "acquire_calibration_lock", "payload": {"duration_s": 60}})
        self.assertEqual(res_lock.status_code, 202)

        # Now jog is allowed
        res_jog = self.client.post("/api/commands", json={"name": "jog_conveyor", "payload": {"steps": 100}})
        self.assertEqual(res_jog.status_code, 202)

    def test_start_batch_refused_while_calibration_locked(self):
        self._set_live(state="idle", supervisor_pid=1234, heater_on=False)
        # Acquire lock
        self.client.post("/api/commands", json={"name": "acquire_calibration_lock", "payload": {"duration_s": 60}})

        # Start batch is refused
        res = self.client.post("/api/commands", json={"name": "start"})
        self.assertEqual(res.status_code, 409)
        self.assertIn("calibration in progress", res.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
