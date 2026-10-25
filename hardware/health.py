"""Component health registry for CORDS hardware.

Tracks last-OK timestamps, status ('ok', 'warn', 'fault', 'unknown'),
and fault messages for all hardware subsystems. Evaluates timeouts and returns
active fault alarms.
"""

from dataclasses import dataclass, field
from datetime import datetime
import json
from typing import Any


@dataclass
class ComponentStatus:
    name: str
    status: str = "unknown"  # "ok", "warn", "fault", "unknown"
    last_ok_at: float | None = None
    last_checked_at: float | None = None
    message: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "last_ok_at": self.last_ok_at,
            "last_checked_at": self.last_checked_at,
            "message": self.message,
            "details": self.details,
        }


class ComponentHealthRegistry:
    """Registry maintaining component health and timeouts."""

    STANDARD_COMPONENTS = (
        "chamber_sensor",
        "exhaust_sensor",
        "hopper_scale",
        "bin_scale",
        "conveyor",
        "carriage",
        "camera",
        "heater",
        "fans",
        "hopper_gate",
    )

    def __init__(
        self,
        *,
        hopper_timeout_s: float = 5.0,
        bin_timeout_s: float = 5.0,
        chamber_timeout_s: float = 10.0,
        exhaust_timeout_s: float = 10.0,
        conveyor_ack_timeout_s: float = 5.0,
    ):
        self.hopper_timeout_s = hopper_timeout_s
        self.bin_timeout_s = bin_timeout_s
        self.chamber_timeout_s = chamber_timeout_s
        self.exhaust_timeout_s = exhaust_timeout_s
        self.conveyor_ack_timeout_s = conveyor_ack_timeout_s

        self._components: dict[str, ComponentStatus] = {
            name: ComponentStatus(name=name) for name in self.STANDARD_COMPONENTS
        }

    def record_ok(self, name: str, now: float, details: dict[str, Any] | None = None) -> None:
        if name not in self._components:
            self._components[name] = ComponentStatus(name=name)
        comp = self._components[name]
        comp.status = "ok"
        comp.last_ok_at = now
        comp.last_checked_at = now
        comp.message = None
        if details:
            comp.details.update(details)

    def record_fault(self, name: str, reason: str, now: float, details: dict[str, Any] | None = None) -> None:
        if name not in self._components:
            self._components[name] = ComponentStatus(name=name)
        comp = self._components[name]
        comp.status = "fault"
        comp.last_checked_at = now
        comp.message = reason
        if details:
            comp.details.update(details)

    def record_warn(self, name: str, reason: str, now: float, details: dict[str, Any] | None = None) -> None:
        if name not in self._components:
            self._components[name] = ComponentStatus(name=name)
        comp = self._components[name]
        comp.status = "warn"
        comp.last_checked_at = now
        comp.message = reason
        if details:
            comp.details.update(details)

    def evaluate_timeouts(self, now: float, *, batch_active: bool = True) -> set[str]:
        """Check timeouts for sensors/scales and return active alarm strings."""
        alarms: set[str] = set()

        # Hopper scale timeout
        hopper = self._components.get("hopper_scale")
        if hopper and hopper.last_ok_at is not None:
            if now - hopper.last_ok_at > self.hopper_timeout_s:
                hopper.status = "fault"
                hopper.message = f"no valid reading for {now - hopper.last_ok_at:.1f}s"
                alarms.add("hopper_scale_fault")
        elif hopper and batch_active and hopper.last_ok_at is None:
            # Never received an OK during active batch
            if hopper.last_checked_at is not None and now - hopper.last_checked_at > self.hopper_timeout_s:
                hopper.status = "fault"
                hopper.message = "no readings received"
                alarms.add("hopper_scale_fault")

        # Bin scale timeout (primarily active during sorting verification or monitoring)
        bin_sc = self._components.get("bin_scale")
        if bin_sc and bin_sc.last_ok_at is not None:
            if now - bin_sc.last_ok_at > self.bin_timeout_s and bin_sc.status != "unknown":
                bin_sc.status = "fault"
                bin_sc.message = f"no valid reading for {now - bin_sc.last_ok_at:.1f}s"
                alarms.add("bin_scale_fault")

        # Chamber sensor timeout
        chamber = self._components.get("chamber_sensor")
        if chamber and chamber.last_ok_at is not None:
            if now - chamber.last_ok_at > self.chamber_timeout_s:
                chamber.status = "fault"
                chamber.message = f"no temperature reading for {now - chamber.last_ok_at:.1f}s"
                alarms.add("chamber_sensor_fault")

        # Exhaust sensor timeout
        exhaust = self._components.get("exhaust_sensor")
        if exhaust and exhaust.last_ok_at is not None:
            if now - exhaust.last_ok_at > self.exhaust_timeout_s:
                exhaust.status = "fault"
                exhaust.message = f"no RH/temp reading for {now - exhaust.last_ok_at:.1f}s"
                alarms.add("exhaust_sensor_fault")

        # Conveyor check
        conveyor = self._components.get("conveyor")
        if conveyor and conveyor.status == "fault":
            if "ack" in (conveyor.message or "").lower():
                alarms.add("conveyor_ack_timeout")
            elif "stall" in (conveyor.message or "").lower():
                alarms.add("conveyor_stall")

        # Carriage check
        carriage = self._components.get("carriage")
        if carriage and carriage.status == "fault":
            alarms.add("carriage_fault")

        return alarms

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """Return dict of all components suitable for machine_state.components JSON."""
        return {name: comp.to_dict() for name, comp in self._components.items()}

    def to_json(self) -> str:
        return json.dumps(self.snapshot())
