import unittest
from hardware import db
from tests.test_app import AppCase

class TestCalibrationCommands(AppCase):
    def setUp(self):
        super().setUp()
        self.supervisor_up("idle")

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

    def test_conveyor_jog_with_lock(self):
        self.client.post("/api/commands", json={"name": "acquire_calibration_lock"})
        resp = self.client.post("/api/commands", json={"name": "jog_conveyor", "payload": {"steps": 500, "forward": True}})
        self.assertEqual(resp.status_code, 202)
        pending = db.pending_commands(self.conn)
        self.assertTrue(any(r["name"] == "jog_conveyor" for r in pending))

    def test_conveyor_stop_with_lock(self):
        self.client.post("/api/commands", json={"name": "acquire_calibration_lock"})
        resp = self.client.post("/api/commands", json={"name": "stop_conveyor"})
        self.assertEqual(resp.status_code, 202)
        pending = db.pending_commands(self.conn)
        self.assertTrue(any(r["name"] == "stop_conveyor" for r in pending))

    def test_conveyor_save_leg_with_lock(self):
        self.client.post("/api/commands", json={"name": "acquire_calibration_lock"})
        resp = self.client.post("/api/commands", json={
            "name": "save_conveyor_leg",
            "payload": {"leg": "hopper_to_chamber", "steps": 2500, "meta": {"spread_pct": 0.4}}
        })
        self.assertEqual(resp.status_code, 202)
        pending = db.pending_commands(self.conn)
        self.assertTrue(any(r["name"] == "save_conveyor_leg" for r in pending))

    def test_conveyor_set_p00_with_lock(self):
        self.client.post("/api/commands", json={"name": "acquire_calibration_lock"})
        resp = self.client.post("/api/commands", json={
            "name": "set_conveyor_p00",
            "payload": {"p00": 10000, "hz": 400.0}
        })
        self.assertEqual(resp.status_code, 202)
        pending = db.pending_commands(self.conn)
        self.assertTrue(any(r["name"] == "set_conveyor_p00" for r in pending))
