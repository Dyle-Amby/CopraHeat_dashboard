import unittest
from hardware import db
from tests.test_app import AppCase


class TestBatchMilestonesAndRecovery(AppCase):
    def setUp(self):
        super().setUp()
        self.supervisor_up("idle")

    def test_milestone_logging_and_api(self):
        batch_id = db.start_batch(self.conn, target_kg=2.0)
        db.log_batch_event(self.conn, batch_id, "batch_started", "Target: 2.0 kg")
        db.log_batch_event(self.conn, batch_id, "gate_opened")

        events = db.get_batch_events(self.conn, batch_id)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["event"], "batch_started")
        self.assertEqual(events[1]["event"], "gate_opened")

        # Test API endpoint
        resp = self.client.get(f"/api/batches/{batch_id}/events")
        self.assertEqual(resp.status_code, 200)
        json_events = resp.get_json()
        self.assertEqual(len(json_events), 2)
        self.assertEqual(json_events[0]["event"], "batch_started")

    def test_unclean_shutdown_prompt_flow(self):
        # 1. Open an active batch in DRYING
        batch_id = db.start_batch(self.conn, target_kg=2.0)
        db.set_batch_state(self.conn, batch_id, "drying")

        # 2. Simulate prompt posted
        prompt = {
            "id": "unclean_shutdown",
            "message": "Unclean shutdown detected during DRYING. Recover or abort?",
            "options": ["recover", "abort"]
        }
        self.supervisor_up("idle", active_prompt=prompt)

        # 3. Check /api/live contains active_prompt
        resp = self.client.get("/api/live")
        self.assertEqual(resp.status_code, 200)
        self.assertIsNotNone(resp.get_json()["active_prompt"])

        # 4. Operator answers prompt with 'recover'
        resp = self.client.post("/api/commands", json={
            "name": "answer_prompt",
            "payload": {"id": "unclean_shutdown", "action": "recover"}
        })
        self.assertEqual(resp.status_code, 202)