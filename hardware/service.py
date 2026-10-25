"""Supervisor service lifecycle manager.

Provides swappable systemd user service control and subprocess fallback
for development and non-systemd environments.
"""

from abc import ABC, abstractmethod
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

from hardware import db

ACTIVE_BATCH_STATES = frozenset({"intake", "drying", "cooldown", "sorting"})
CONFIRMATION_PHRASE = "INTERRUPT BATCH"


class BaseSupervisorService(ABC):
    """Abstract interface for controlling the supervisor process/service."""

    @abstractmethod
    def is_active(self) -> tuple[bool, str, int | None]:
        """Check service status. Returns (is_active, substate, pid)."""
        ...

    @abstractmethod
    def start_service(self) -> tuple[bool, str]:
        """Start the supervisor service."""
        ...

    @abstractmethod
    def stop_service(self) -> tuple[bool, str]:
        """Stop the supervisor service."""
        ...

    @abstractmethod
    def restart_service(self) -> tuple[bool, str]:
        """Restart the supervisor service."""
        ...

    def get_status(self, conn=None) -> dict:
        """Comprehensive status combining service state and SQLite heartbeat."""
        active, substate, pid = self.is_active()
        heartbeat_ok = False
        last_heartbeat = None
        batch_active = False
        batch_state = None
        batch_id = None
        supervisor_pid = pid

        if conn is not None:
            try:
                live = db.live_state(conn, stale_after_s=5.0)
                if live is not None:
                    last_heartbeat = live.get("updated_at")
                    heartbeat_ok = not live.get("stale", True)
                    batch_id = live.get("batch_id")
                    b_state = live.get("state")
                    if b_state:
                        batch_state = str(b_state).lower()
                        batch_active = batch_state in ACTIVE_BATCH_STATES
                    if live.get("supervisor_pid"):
                        supervisor_pid = live.get("supervisor_pid")
            except Exception:
                pass

        return {
            "active": active,
            "substate": substate,
            "pid": supervisor_pid,
            "heartbeat_ok": heartbeat_ok,
            "last_heartbeat": last_heartbeat,
            "batch_active": batch_active,
            "batch_state": batch_state,
            "batch_id": batch_id,
            "backend": getattr(self, "backend_name", "unknown"),
        }


class SystemdSupervisorService(BaseSupervisorService):
    """Controls the supervisor via `systemctl --user`."""

    backend_name = "systemd"

    def __init__(self, unit_name: str = "cords-supervisor"):
        self.unit_name = unit_name

    def _env(self) -> dict:
        env = os.environ.copy()
        if "XDG_RUNTIME_DIR" not in env and hasattr(os, "getuid"):
            try:
                env["XDG_RUNTIME_DIR"] = f"/run/user/{os.getuid()}"
            except Exception:
                pass
        return env

    def is_active(self) -> tuple[bool, str, int | None]:
        try:
            res = subprocess.run(
                ["systemctl", "--user", "is-active", self.unit_name],
                capture_output=True,
                text=True,
                env=self._env(),
                timeout=5.0,
            )
            substate = res.stdout.strip()
            active = (res.returncode == 0 and substate == "active")

            pid = None
            show_res = subprocess.run(
                ["systemctl", "--user", "show", self.unit_name, "--property=MainPID,SubState"],
                capture_output=True,
                text=True,
                env=self._env(),
                timeout=5.0,
            )
            for line in show_res.stdout.splitlines():
                if line.startswith("MainPID="):
                    try:
                        p = int(line.split("=", 1)[1])
                        if p > 0:
                            pid = p
                    except ValueError:
                        pass
                elif line.startswith("SubState="):
                    substate = line.split("=", 1)[1].strip()

            return active, substate or ("active" if active else "inactive"), pid
        except Exception as exc:
            return False, f"error: {exc}", None

    def start_service(self) -> tuple[bool, str]:
        try:
            res = subprocess.run(
                ["systemctl", "--user", "start", self.unit_name],
                capture_output=True,
                text=True,
                env=self._env(),
                timeout=15.0,
            )
            if res.returncode == 0:
                return True, "supervisor started via systemctl"
            return False, res.stderr.strip() or f"systemctl exited with code {res.returncode}"
        except Exception as exc:
            return False, f"failed to start service: {exc}"

    def stop_service(self) -> tuple[bool, str]:
        try:
            res = subprocess.run(
                ["systemctl", "--user", "stop", self.unit_name],
                capture_output=True,
                text=True,
                env=self._env(),
                timeout=25.0,
            )
            if res.returncode == 0:
                return True, "supervisor stopped via systemctl"
            return False, res.stderr.strip() or f"systemctl exited with code {res.returncode}"
        except Exception as exc:
            return False, f"failed to stop service: {exc}"

    def restart_service(self) -> tuple[bool, str]:
        try:
            res = subprocess.run(
                ["systemctl", "--user", "restart", self.unit_name],
                capture_output=True,
                text=True,
                env=self._env(),
                timeout=30.0,
            )
            if res.returncode == 0:
                return True, "supervisor restarted via systemctl"
            return False, res.stderr.strip() or f"systemctl exited with code {res.returncode}"
        except Exception as exc:
            return False, f"failed to restart service: {exc}"


class SubprocessSupervisorService(BaseSupervisorService):
    """Subprocess fallback for environments without systemd user manager."""

    backend_name = "subprocess"
    _process: subprocess.Popen | None = None

    def __init__(self, run_args: list[str] | None = None):
        self.run_args = run_args or [sys.executable, "-m", "hardware.run_cords"]

    def is_active(self) -> tuple[bool, str, int | None]:
        if self._process is not None:
            poll = self._process.poll()
            if poll is None:
                return True, "running", self._process.pid
            else:
                code = self._process.returncode
                self._process = None
                return False, f"exited ({code})", None
        return False, "inactive", None

    def start_service(self) -> tuple[bool, str]:
        active, _, _ = self.is_active()
        if active:
            return False, "supervisor subprocess is already running"
        try:
            # Run in repository root
            repo_root = Path(__file__).resolve().parent.parent
            self._process = subprocess.Popen(
                self.run_args,
                cwd=str(repo_root),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True, f"supervisor started in background (PID {self._process.pid})"
        except Exception as exc:
            return False, f"failed to start supervisor subprocess: {exc}"

    def stop_service(self) -> tuple[bool, str]:
        active, _, _ = self.is_active()
        if not active or self._process is None:
            return True, "supervisor subprocess is not running"
        proc = self._process
        try:
            proc.terminate()
            try:
                proc.wait(timeout=20.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5.0)
            self._process = None
            return True, "supervisor subprocess terminated"
        except Exception as exc:
            self._process = None
            return False, f"error stopping supervisor: {exc}"

    def restart_service(self) -> tuple[bool, str]:
        self.stop_service()
        time.sleep(0.5)
        return self.start_service()


_INSTANCE: BaseSupervisorService | None = None


def get_supervisor_service() -> BaseSupervisorService:
    """Return the configured supervisor service instance."""
    global _INSTANCE
    if _INSTANCE is not None:
        return _INSTANCE

    backend = os.environ.get("CORDS_SUPERVISOR_BACKEND", "").lower().strip()
    if backend == "systemd":
        _INSTANCE = SystemdSupervisorService()
    elif backend == "subprocess":
        _INSTANCE = SubprocessSupervisorService()
    elif os.name != "nt" and shutil.which("systemctl") is not None:
        _INSTANCE = SystemdSupervisorService()
    else:
        _INSTANCE = SubprocessSupervisorService()

    return _INSTANCE


def set_supervisor_service(service: BaseSupervisorService | None) -> None:
    """Override singleton (used in unit testing)."""
    global _INSTANCE
    _INSTANCE = service
