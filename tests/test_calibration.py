import unittest
from hardware import db
from tests.test_app import AppCase

class TestCalibrationCommands(AppCase):
    def test_maintenance_gate_blocks_without_lock(self):
        # Without lock, maintenance command is rejected with 409
        resp = self.client.post("/api/commands", json={"name": "tare_hopper", "payload": {"gate": "closed"}})
        self.assertEqual(resp.status_code, 409)
        self.assertIn("calibration lock required", resp.get_json()["error"])

    def test_hopper_tare_command_with_lock(self):
        # 1. Acquire lock
        resp = self.client.post("/api/commands", json={"name": "acquire_calibration_lock", "payload": {"duration_s": 300}})
        self.assertEqual(resp.status_code, 202)

        # 2. Queue tare command
        resp = self.client.post("/api/commands", json={"name": "tare_hopper", "payload": {"gate": "closed"}})
        self.assertEqual(resp.status_code, 202)

        # 3. Verify queued in DB
        pending = db.pending_commands(self.conn)
        self.assertTrue(any(r["name"] == "tare_hopper" for r in pending))

    def test_carriage_goto_command_with_lock(self):
        self.client.post("/api/commands", json={"name": "acquire_calibration_lock"})
        resp = self.client.post("/api/commands", json={"name": "goto_carriage_bin", "payload": {"bin": "good"}})
        self.assertEqual(resp.status_code, 202)
        pending = db.pending_commands(self.conn)
        self.assertTrue(any(r["name"] == "goto_carriage_bin" for r in pending))