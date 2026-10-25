import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path

from app import create_app
from hardware import db
from hardware.service import (
    BaseSupervisorService,
    CONFIRMATION_PHRASE,
    SubprocessSupervisorService,
    SystemdSupervisorService,
    get_supervisor_service,
    set_supervisor_service,
)


class MockSupervisorService(BaseSupervisorService):
    backend_name = "mock"

    def __init__(self, active: bool = False, substate: str = "inactive", pid: int | None = None):
        self._active = active
        self._substate = substate
        self._pid = pid
        self.started_calls = 0
        self.stopped_calls = 0
        self.restarted_calls = 0

    def is_active(self) -> tuple[bool, str, int | None]:
        return self._active, self._substate, self._pid

    def start_service(self) -> tuple[bool, str]:
        if self._active:
            return False, "already running"
        self._active = True
        self._substate = "running"
        self._pid = 9999
        self.started_calls += 1
        return True, "mock started"

    def stop_service(self) -> tuple[bool, str]:
        if not self._active:
            return True, "already stopped"
        self._active = False
        self._substate = "inactive"
        self._pid = None
        self.stopped_calls += 1
        return True, "mock stopped"

    def restart_service(self) -> tuple[bool, str]:
        self.restarted_calls += 1
        self._active = True
        self._substate = "running"
        self._pid = 10001
        return True, "mock restarted"


class TestServiceBackends(unittest.TestCase):
    def test_subprocess_service_lifecycle(self):
        # Spawns a lightweight python command that terminates
        svc = SubprocessSupervisorService(run_args=["python", "-c", "import time; time.sleep(0.5)"])
        self.assertEqual(svc.backend_name, "subprocess")
        active, sub, pid = svc.is_active()
        self.assertFalse(active)

        ok, msg = svc.start_service()
        self.assertTrue(ok)
        active, sub, pid = svc.is_active()
        self.assertTrue(active)
        self.assertIsNotNone(pid)

        # Double start refused
        ok2, msg2 = svc.start_service()
        self.assertFalse(ok2)

        # Stop
        ok3, msg3 = svc.stop_service()
        self.assertTrue(ok3)
        active, sub, pid = svc.is_active()
        self.assertFalse(active)

    @patch("subprocess.run")
    def test_systemd_service_status_parsing(self, mock_run):
        svc = SystemdSupervisorService("cords-supervisor")
        self.assertEqual(svc.backend_name, "systemd")

        # Mock is-active and show
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="active\n"),
            MagicMock(returncode=0, stdout="MainPID=1234\nSubState=running\n"),
        ]
        active, substate, pid = svc.is_active()
        self.assertTrue(active)
        self.assertEqual(substate, "running")
        self.assertEqual(pid, 1234)

    @patch("subprocess.run")
    def test_systemd_service_commands(self, mock_run):
        svc = SystemdSupervisorService("cords-supervisor")

        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        ok, msg = svc.start_service()
        self.assertTrue(ok)
        mock_run.assert_called_with(
            ["systemctl", "--user", "start", "cords-supervisor"],
            capture_output=True,
            text=True,
            env=svc._env(),
            timeout=15.0,
        )

        ok, msg = svc.stop_service()
        self.assertTrue(ok)
        mock_run.assert_called_with(
            ["systemctl", "--user", "stop", "cords-supervisor"],
            capture_output=True,
            text=True,
            env=svc._env(),
            timeout=25.0,
        )

        ok, msg = svc.restart_service()
        self.assertTrue(ok)


class TestSupervisorAPI(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self._tmp.name) / "cords.db"
        self.conn = db.connect(self.db_path)
        db.init(self.conn)

        self.mock_svc = MockSupervisorService(active=False)
        set_supervisor_service(self.mock_svc)

        self.app = create_app(str(self.db_path))
        self.client = self.app.test_client()

    def tearDown(self):
        set_supervisor_service(None)
        self.conn.close()
        try:
            self._tmp.cleanup()
        except Exception:
            pass

    def test_get_supervisor_status(self):
        res = self.client.get("/api/supervisor")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertFalse(data["active"])
        self.assertEqual(data["backend"], "mock")
        self.assertFalse(data["batch_active"])

    def test_start_supervisor_success_and_conflict(self):
        res = self.client.post("/api/supervisor/start")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.mock_svc.started_calls, 1)

        # Starting while running returns 409
        res2 = self.client.post("/api/supervisor/start")
        self.assertEqual(res2.status_code, 409)
        self.assertIn("already running", res2.get_json()["error"])

    def test_stop_supervisor_when_stopped_returns_409(self):
        self.mock_svc._active = False
        res = self.client.post("/api/supervisor/stop")
        self.assertEqual(res.status_code, 409)
        self.assertIn("already stopped", res.get_json()["error"])

    def test_stop_supervisor_requires_confirmation_when_batch_active(self):
        self.mock_svc._active = True
        self.mock_svc._substate = "running"
        self.mock_svc._pid = 5555

        # Record active drying batch in DB
        b_id = db.start_batch(self.conn, target_kg=2.0)
        db.set_batch_state(self.conn, b_id, "drying")
        db.update_live(
            self.conn,
            state="drying",
            supervisor_pid=5555,
            batch_id=b_id,
            elapsed_s=100.0,
            target_kg=2.0,
            heater_on=True,
            fans_on=True,
            fan_reason="drying",
            gate_open=False,
            conveyor="stop",
            alarms=[],
        )

        # Stop without confirmation phrase fails
        res = self.client.post("/api/supervisor/stop", json={})
        self.assertEqual(res.status_code, 409)
        self.assertIn("Batch is currently running", res.get_json()["error"])
        self.assertIn(CONFIRMATION_PHRASE, res.get_json()["error"])
        self.assertEqual(self.mock_svc.stopped_calls, 0)

        # Stop with confirmation succeeds
        res_ok = self.client.post("/api/supervisor/stop", json={"confirm": CONFIRMATION_PHRASE})
        self.assertEqual(res_ok.status_code, 200)
        self.assertEqual(self.mock_svc.stopped_calls, 1)

    def test_restart_supervisor_requires_confirmation_when_batch_active(self):
        self.mock_svc._active = True
        b_id = db.start_batch(self.conn, target_kg=2.0)
        db.set_batch_state(self.conn, b_id, "sorting")
        db.update_live(
            self.conn,
            state="sorting",
            supervisor_pid=5555,
            batch_id=b_id,
            elapsed_s=200.0,
            target_kg=2.0,
            heater_on=False,
            fans_on=False,
            fan_reason="off",
            gate_open=False,
            conveyor="sort_creep",
            alarms=[],
        )

        # Without confirmation fails
        res = self.client.post("/api/supervisor/restart", json={"confirm": "wrong"})
        self.assertEqual(res.status_code, 409)

        # With confirmation succeeds
        res_ok = self.client.post("/api/supervisor/restart", json={"confirm": CONFIRMATION_PHRASE})
        self.assertEqual(res_ok.status_code, 200)
        self.assertEqual(self.mock_svc.restarted_calls, 1)

    def test_pin_protection_on_all_supervisor_actions(self):
        os.environ["CORDS_PIN"] = "8899"
        try:
            # Start without PIN fails
            res = self.client.post("/api/supervisor/start")
            self.assertEqual(res.status_code, 403)

            # Start with wrong PIN fails
            res = self.client.post("/api/supervisor/start", json={"pin": "1234"})
            self.assertEqual(res.status_code, 403)

            # Start with valid PIN header succeeds
            res_hdr = self.client.post("/api/supervisor/start", headers={"X-CORDS-PIN": "8899"})
            self.assertEqual(res_hdr.status_code, 200)

            # Stop without PIN fails
            res_stop = self.client.post("/api/supervisor/stop")
            self.assertEqual(res_stop.status_code, 403)

            # Stop with PIN payload succeeds
            res_stop_ok = self.client.post("/api/supervisor/stop", json={"pin": "8899"})
            self.assertEqual(res_stop_ok.status_code, 200)
        finally:
            os.environ.pop("CORDS_PIN", None)
