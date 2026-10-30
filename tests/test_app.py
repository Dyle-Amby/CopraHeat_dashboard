import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import create_app, fmt_duration, supervisor_status
from hardware import db
from hardware.batch import BatchRecord


class AppCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        path = Path(self._tmp.name) / "cords.db"
        self.app = create_app(path)
        self.client = self.app.test_client()
        self.conn = db.connect(path)

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def supervisor_up(self, state="idle", **fields):
        db.update_live(self.conn, state=state, supervisor_pid=4242, **fields)

    def post_command(self, name, payload=None):
        return self.client.post("/api/commands", json={"name": name, "payload": payload})


class TestPages(AppCase):
    def test_every_page_renders_on_an_empty_database(self):
        for url in ("/", "/sensor-logs", "/batch-history", "/control-panel", "/components"):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_history_shows_real_rows(self):
        batch_id = db.start_batch(self.conn, target_kg=2.0)
        db.finish_batch(
            self.conn, batch_id,
            BatchRecord(7200.0, 6000.0, 2.0, 1.1, 45.0, False, None, frozenset()),
            "complete",
        )
        html = self.client.get("/batch-history").get_data(as_text=True)
        self.assertIn("45.0%", html)
        self.assertIn("2h 00m", html)
        self.assertNotIn("No batches recorded yet", html)

    def test_stale_mockup_labels_are_gone(self):
        # no ambient sensor, no diverter flap, no live weight loss
        pages = "".join(
            self.client.get(url).get_data(as_text=True)
            for url in ("/", "/sensor-logs", "/control-panel")
        )
        for stale in ("Ambient", "Diverter flap", "trapdoor", "Emergency stop"):
            self.assertNotIn(stale, pages)

    def test_components_page_renders(self):
        resp = self.client.get("/components")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Components &amp; Calibration", resp.data)
        self.assertIn(b"tab-hopper", resp.data)
        self.assertIn(b"tab-conveyor", resp.data)
        self.assertIn(b"tab-carriage", resp.data)
        self.assertIn(b"tab-chamber", resp.data)
        self.assertIn(b"tab-camera", resp.data)
        self.assertIn(b'id="carriage-state"', resp.data)
        self.assertIn(b'id="carriage-fault"', resp.data)
        html = resp.get_data(as_text=True)
        self.assertEqual(html.count('id="btnCalibrationLock"'), 1)
        self.assertLess(html.index('id="btnCalibrationLock"'), html.index('id="componentTabs"'))

    def test_piece_image_route_serves_saved_capture(self):
        image_path = Path(self._tmp.name) / "piece_captures" / "batch_7" / "piece_2_attempt_1.jpg"
        image_path.parent.mkdir(parents=True)
        image_path.write_bytes(b"jpeg test data")

        with patch("app.Path", return_value=Path(self._tmp.name)):
            response = self.client.get("/api/batches/7/pieces/2/image")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "image/jpeg")
        self.assertEqual(response.data, b"jpeg test data")
        response.close()


class TestLive(AppCase):
    def test_before_the_supervisor_has_ever_run(self):
        body = self.client.get("/api/live").get_json()
        self.assertEqual(body["supervisor"], "never_run")
        self.assertIsNone(body["batch"])

    def test_running_supervisor_with_a_batch(self):
        batch_id = db.start_batch(self.conn, target_kg=2.0)
        self.supervisor_up(
            "drying", batch_id=batch_id, chamber_c=64.5,
            drying_remaining_s=420.0, alarms=["overtemp"],
        )
        body = self.client.get("/api/live").get_json()
        self.assertEqual(body["supervisor"], "running")
        self.assertEqual(body["chamber_c"], 64.5)
        self.assertEqual(body["alarms"], ["overtemp"])
        self.assertEqual(body["batch"]["id"], batch_id)
        self.assertEqual(body["drying_remaining_s"], 420.0)

    def test_clean_stop_and_dead_process_are_told_apart(self):
        self.supervisor_up()
        db.mark_supervisor_stopped(self.conn)
        self.assertEqual(self.client.get("/api/live").get_json()["supervisor"], "stopped")

        self.supervisor_up()
        self.conn.execute("UPDATE machine_state SET updated_at = '2000-01-01T00:00:00'")
        self.assertEqual(self.client.get("/api/live").get_json()["supervisor"], "not_responding")

    def test_supervisor_status_words(self):
        self.assertEqual(supervisor_status(None), "never_run")
        self.assertEqual(supervisor_status({"supervisor_pid": None, "stale": False}), "stopped")
        self.assertEqual(supervisor_status({"supervisor_pid": 1, "stale": True}), "not_responding")
        self.assertEqual(supervisor_status({"supervisor_pid": 1, "stale": False}), "running")


class TestCommands(AppCase):
    def test_refused_when_no_supervisor_is_listening(self):
        resp = self.post_command("start")
        self.assertEqual(resp.status_code, 409)
        self.assertIn("never run", resp.get_json()["error"])
        self.assertEqual(db.pending_commands(self.conn), [])

    def test_refused_when_the_supervisor_has_died(self):
        self.supervisor_up()
        self.conn.execute("UPDATE machine_state SET updated_at = '2000-01-01T00:00:00'")
        self.assertEqual(self.post_command("start").status_code, 409)
        self.assertEqual(db.pending_commands(self.conn), [])

    def test_queued_when_running(self):
        self.supervisor_up()
        resp = self.post_command("start")
        self.assertEqual(resp.status_code, 202)
        body = resp.get_json()
        self.assertEqual(body["status"], db.PENDING)
        self.assertEqual([c["id"] for c in db.pending_commands(self.conn)], [body["id"]])

    def test_unknown_or_missing_names_are_rejected(self):
        self.supervisor_up()
        self.assertEqual(self.post_command("launch_missiles").status_code, 400)
        self.assertEqual(self.client.post("/api/commands", data="junk").status_code, 400)
        self.assertEqual(db.pending_commands(self.conn), [])

    def test_bad_target_weights_never_reach_the_queue(self):
        self.supervisor_up()
        for payload in (None, "heavy", "0", "-1", "nan", "inf"):
            with self.subTest(payload=payload):
                self.assertEqual(self.post_command("set_target_kg", payload).status_code, 400)
        self.assertEqual(db.pending_commands(self.conn), [])

    def test_target_weight_is_normalised(self):
        self.supervisor_up()
        self.post_command("set_target_kg", " 3.5 ")
        self.assertEqual(db.pending_commands(self.conn)[0]["payload"], "3.5")

    def test_abort_reason_is_defaulted_and_trimmed(self):
        self.supervisor_up("drying")
        self.post_command("abort", "   ")
        self.post_command("abort", "  smoke in the chamber  ")
        self.post_command("abort", "x" * 1000)
        payloads = [c["payload"] for c in db.pending_commands(self.conn)]
        self.assertEqual(payloads[0], "operator abort")
        self.assertEqual(payloads[1], "smoke in the chamber")
        self.assertEqual(len(payloads[2]), 200)

    def test_other_commands_drop_any_payload(self):
        self.supervisor_up()
        self.post_command("start", "please")
        self.assertIsNone(db.pending_commands(self.conn)[0]["payload"])

    def test_the_machines_answer_comes_back(self):
        self.supervisor_up()
        command_id = self.post_command("end_drying").get_json()["id"]
        db.resolve_command(self.conn, command_id, db.REJECTED, "not drying (state is idle)")
        body = self.client.get(f"/api/commands/{command_id}").get_json()
        self.assertEqual(body["status"], db.REJECTED)
        self.assertEqual(body["result"], "not drying (state is idle)")

    def test_unknown_command_id_is_404(self):
        self.assertEqual(self.client.get("/api/commands/999").status_code, 404)


class TestBatchesAPI(AppCase):
    def test_samples_for_a_batch_and_only_newer_ones(self):
        batch_id = db.start_batch(self.conn, target_kg=2.0)
        for temp in (60.0, 65.0):
            db.log_sample(self.conn, batch_id=batch_id, state="drying", chamber_c=temp)
        rows = self.client.get(f"/api/batches/{batch_id}/samples").get_json()
        self.assertEqual([r["chamber_c"] for r in rows], [60.0, 65.0])
        newer = self.client.get(f"/api/batches/{batch_id}/samples?after={rows[0]['id']}").get_json()
        self.assertEqual([r["chamber_c"] for r in newer], [65.0])

    def test_unknown_batch_is_404_as_json(self):
        for url in ("/api/batches/999", "/api/batches/999/samples"):
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 404)
            self.assertIn("error", resp.get_json())

    def test_batches_newest_first(self):
        first = db.start_batch(self.conn, target_kg=2.0)
        second = db.start_batch(self.conn, target_kg=2.0)
        ids = [b["id"] for b in self.client.get("/api/batches").get_json()]
        self.assertEqual(ids, [second, first])


class TestFormatting(unittest.TestCase):
    def test_duration(self):
        self.assertEqual(fmt_duration(None), "—")
        self.assertEqual(fmt_duration(59 * 60), "59m")
        self.assertEqual(fmt_duration(5 * 3600 + 42 * 60 + 10), "5h 42m")



if __name__ == "__main__":
    unittest.main()
