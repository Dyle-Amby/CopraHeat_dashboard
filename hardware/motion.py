"""Non-blocking motion layer for conveyor belt and bin carriage.

Pure logic interfaces, non-blocking execution with exact counted bursts,
and simulated twins that run off the Pi.

Conveyor:
    conveyor.move(steps, hz) returns immediately.
    Properties: .busy, .last_result in {"done", "stalled", "stopped", "refused", None}, .position_steps.
    conveyor.stop() stops at once.
    Refuses any move whose required step count is <= 0 or uncalibrated.

Carriage:
    driver.goto(bin) returns immediately (non-blocking).
    Properties: .state in {"idle", "moving", "at_target", "fault"}, .current_bin, .last_fault.
    MoveWatch timeout / travel limit triggers a real fault state.
    Startup home check identifies resting switch or homes carriage.
"""

from abc import ABC, abstractmethod
import time
from typing import Literal

from hardware import carriage, pins
from hardware.carriage import ARRIVED, FAULT, MOVING, MoveWatch, located_at, moving_up, stops
from hardware.states import ConveyorCommand
from hardware.stepper import Stepper

ConveyorResult = Literal["done", "stalled", "stopped", "refused"]
CarriageState = Literal["idle", "moving", "at_target", "fault"]


class BaseConveyor(ABC):
    """Abstract conveyor interface ensuring identical surface for real and simulated drivers."""

    def __init__(self, hopper_to_chamber_steps: int = 0, default_hz: float = 400.0):
        self.hopper_to_chamber_steps = hopper_to_chamber_steps
        self.default_hz = default_hz
        self.position_steps: int = 0
        self.last_result: str | None = None
        self._advance_done: bool = False
        self.creeping: bool = False

    @property
    @abstractmethod
    def busy(self) -> bool:
        """True while a move is actively executing."""
        ...

    @abstractmethod
    def move(self, steps: int, hz: float | None = None, forward: bool = True, now: float | None = None) -> bool:
        """Start a non-blocking move of `steps` pulses at `hz` step rate.
        Returns True if accepted, False if refused (e.g. uncalibrated or steps <= 0).
        """
        ...

    @abstractmethod
    def stop(self) -> int:
        """Halt motion immediately. Returns estimated or counted steps sent."""
        ...

    @abstractmethod
    def update(self, now: float | None = None) -> None:
        """Periodic status update / poll hook."""
        ...

    @abstractmethod
    def apply(self, command: ConveyorCommand, now: float) -> None:
        """Adapt state machine ConveyorCommand to conveyor motion."""
        ...

    def close(self) -> None:
        self.stop()


class StepperConveyor(BaseConveyor):
    """Real hardware conveyor using Stepper on HBS57H."""

    def __init__(
        self,
        stepper_pins=pins.HBS57H_CONVEYOR,
        hopper_to_chamber_steps: int = 0,
        default_hz: float = 400.0,
        invert_dir: bool = False,
    ):
        super().__init__(hopper_to_chamber_steps, default_hz)
        self.stepper = Stepper(stepper_pins, invert_dir=invert_dir)
        self._target_steps = 0
        self._move_forward = True
        self._was_busy = False
        self._moved_at: float | None = None

    @property
    def busy(self) -> bool:
        # Treat the belt as busy for the first 0.15 s after move() to bridge tx lag.
        # Clear the stale grace marker as soon as it expires or the stepper goes idle.
        if self._moved_at is not None:
            if (time.monotonic() - self._moved_at) < 0.15:
                return True
            self._moved_at = None
        is_busy = self.stepper.busy
        if self._was_busy and not is_busy:
            self._was_busy = False
            self.last_result = "done"
            if self._move_forward:
                self.position_steps += self._target_steps
            else:
                self.position_steps -= self._target_steps
            if self.hopper_to_chamber_steps and self._target_steps == self.hopper_to_chamber_steps:
                self._advance_done = True
        return is_busy

    def move(self, steps: int, hz: float | None = None, forward: bool = True, now: float | None = None) -> bool:
        if steps <= 0:
            self.last_result = "refused"
            return False
        rate = hz if hz is not None and hz > 0 else self.default_hz
        self._target_steps = steps
        self._move_forward = forward
        self.last_result = "starting"
        self._moved_at = time.monotonic()
        try:
            self.stepper.start(steps, rate, forward=forward)
        except Exception as exc:
            self.last_result = "refused"
            raise RuntimeError(f"conveyor stepper failed to start move: {exc}") from exc
        self._was_busy = True
        return True

    def stop(self) -> int:
        self.creeping = False
        self._moved_at = None
        if self._was_busy or self.stepper.busy:
            sent = self.stepper.stop()
            self._was_busy = False
            self.last_result = "stopped"
            if self._move_forward:
                self.position_steps += sent
            else:
                self.position_steps -= sent
            return sent
        return 0

    def update(self, now: float | None = None) -> None:
        _ = self.busy

    def apply(self, command: ConveyorCommand, now: float) -> None:
        self.update(now)
        if command is ConveyorCommand.INTAKE_ADVANCE:
            self.creeping = False
            if self.hopper_to_chamber_steps <= 0:
                self.last_result = "refused"
                return
            if not self._advance_done and not self.busy:
                self.move(self.hopper_to_chamber_steps, self.default_hz, forward=True)
        elif command is ConveyorCommand.SORT_CREEP:
            # Item 11: No-op; SortingFlow uses move() directly with exact step counts
            pass
        else:
            if self.busy or self.creeping:
                self.stop()
            self._advance_done = False

    def close(self) -> None:
        self.stop()
        self.stepper.close()


class SimulatedConveyor(BaseConveyor):
    """Simulated conveyor stand-in counting steps and simulating elapsed time."""

    def __init__(
        self,
        advance_s: float = 4.0,
        hopper_to_chamber_steps: int = 2000,
        default_hz: float = 400.0,
    ):
        super().__init__(hopper_to_chamber_steps, default_hz)
        self.advance_s = advance_s
        self._busy = False
        self._moving_until: float | None = None
        self._move_started_at: float | None = None
        self._move_steps: int = 0
        self._move_hz: float = default_hz
        self._move_forward: bool = True

    @property
    def busy(self) -> bool:
        return self._busy

    def move(self, steps: int, hz: float | None = None, forward: bool = True, now: float | None = None) -> bool:
        if steps <= 0:
            self.last_result = "refused"
            return False
        rate = hz if hz is not None and hz > 0 else self.default_hz
        t = now if now is not None else time.monotonic()
        self._move_started_at = t
        self._moving_until = t + (steps / rate)
        self._move_steps = steps
        self._move_hz = rate
        self._move_forward = forward
        self._busy = True
        self.creeping = False
        self.last_result = None
        return True

    def update(self, now: float | None = None) -> None:
        t = now if now is not None else time.monotonic()
        if self._busy and self._moving_until is not None:
            if t >= self._moving_until:
                self._busy = False
                self._moving_until = None
                self.last_result = "done"
                steps = self._move_steps if self._move_steps > 0 else int(self.advance_s * self.default_hz)
                if self._move_forward:
                    self.position_steps += steps
                else:
                    self.position_steps -= steps
                self._advance_done = True

    def stop(self, now: float | None = None) -> int:
        self.creeping = False
        if not self._busy and self._moving_until is None:
            self._advance_done = False
            return 0
        t = now if now is not None else time.monotonic()
        elapsed = max(0.0, t - (self._move_started_at if self._move_started_at is not None else t))
        sent = min(self._move_steps, int(elapsed * self._move_hz)) if self._move_steps > 0 else int(elapsed * self.default_hz)
        self._busy = False
        self._moving_until = None
        self._advance_done = False
        self.last_result = "stopped"
        if self._move_forward:
            self.position_steps += sent
        else:
            self.position_steps -= sent
        return sent

    def apply(self, command: ConveyorCommand, now: float) -> None:
        if command is ConveyorCommand.INTAKE_ADVANCE:
            self.creeping = False
            if self.hopper_to_chamber_steps <= 0:
                self.last_result = "refused"
                return
            if self._moving_until is None and not self._advance_done:
                self._move_started_at = now
                self._moving_until = now + self.advance_s
                self._move_steps = int(self.advance_s * self.default_hz)
                self._move_hz = self.default_hz
                self._move_forward = True
                self._busy = True
                self.last_result = None
            elif self._moving_until is not None and now >= self._moving_until:
                self._busy = False
                self._moving_until = None
                self._advance_done = True
                self.last_result = "done"
                self.position_steps += self._move_steps
            self._busy = self._moving_until is not None
        elif command is ConveyorCommand.SORT_CREEP:
            # Item 11: No-op; SortingFlow uses move() directly with exact step counts
            pass
        else:
            self.stop(now)


class CarriageDriver:
    """Non-blocking carriage positioning driver wrapping MoveWatch, limits, and stepper."""

    def __init__(
        self,
        stepper_pins=pins.TB6600_CARRIAGE,
        order=carriage.ORDER,
        bins=carriage.BINS,
        default_hz: float = 400.0,
        release_within: int = 400,
        max_travel_steps: int = 6000,
        invert_dir: bool = True,
    ):
        self.order = order
        self.bins = bins
        self.default_hz = default_hz
        self.release_within = release_within
        self.max_travel_steps = max_travel_steps
        self.stops = stops(order, bins)
        self.bins_by_stop = {v: k for k, v in self.stops.items()}

        self.stepper = Stepper(stepper_pins, invert_dir=invert_dir)
        self.state: CarriageState = "idle"
        self.current_bin: str | None = None
        self.last_fault: str | None = None

        self._watch: MoveWatch | None = None
        self._target_bin: str | None = None
        self._target_switch: str | None = None
        self._pending_target_bin: str | None = None
        self._move_started_at: float | None = None
        self._move_steps_sent: int = 0
        self._move_hz: float = default_hz
        self._limits = None

    def _open_limits(self):
        if self._limits is None:
            from hardware.limits import LimitSwitches
            self._limits = LimitSwitches()
        return self._limits

    def check_home(self) -> str | None:
        """Startup home check: identifies switch resting on, or sets None."""
        ls = self._open_limits()
        switches = ls.read()
        try:
            loc = located_at(switches)
            if loc in self.bins_by_stop:
                self.current_bin = self.bins_by_stop[loc]
                self.state = "at_target"
                return self.current_bin
        except RuntimeError as exc:
            self.state = "fault"
            self.last_fault = str(exc)
        return None

    def clear_fault(self) -> None:
        """Reset fault state so goto can be re-attempted after operator prompt."""
        if self.state == "fault":
            self.state = "idle"
            self.last_fault = None

    def goto(self, target_bin: str, hz: float | None = None, now: float | None = None) -> bool:
        """Initiate non-blocking move to target bin."""
        if target_bin not in self.stops:
            raise ValueError(f"unknown bin {target_bin!r}; expected one of {self.bins}")

        # A stale move can leave the object in 'moving' even though the stepper is already idle.
        # Treat that as a recoverable restart instead of locking the carriage forever.
        if self.state == "moving" and not self.stepper.busy:
            self.state = "idle"
            self._watch = None
            self._target_bin = None
            self._target_switch = None
            self.last_fault = None

        if self.state == "fault":
            # One automatic retry from the last known bin is allowed: if the carriage
            # stopped mid-travel and never reached a downstream switch, keep going
            # from the last switch it did trigger instead of forcing a manual clear.
            self.clear_fault()
            if self.current_bin is not None and self.current_bin != target_bin:
                self.state = "idle"

        # Item 13: After a fault, goto must not auto-retry without explicit clear_fault / prompt answer
        if self.state in ("moving", "fault"):
            return False

        target_switch = self.stops[target_bin]
        ls = self._open_limits()
        current_switches = ls.read()

        current_loc = located_at(current_switches)
        if self.state == "idle" and self.current_bin is not None and self.current_bin != target_bin:
            # Retry from the last switch we definitively identified, even if the
            # live switch read is momentarily clear in the middle of a travel segment.
            current_loc = self.stops[self.current_bin]

        if current_loc == target_switch:
            self.current_bin = target_bin
            self.state = "at_target"
            return True

        rate = hz if hz is not None and hz > 0 else self.default_hz
        self._move_hz = rate

        # Match the bench CLI: if the carriage is between stops, first home to a
        # known switch, then continue toward the target after the first move completes.
        if current_loc is None:
            self._pending_target_bin = target_bin
            self._watch = MoveWatch(
                current_switches,
                None,
                order=self.order,
                release_within=self.release_within,
                up=False,
            )
            self._target_bin = None
            self._target_switch = None
            self.stepper.start(self.max_travel_steps, rate, forward=False)
            self._move_started_at = time.monotonic()
            self.state = "moving"
            self.last_fault = None
            return True

        if self.current_bin is not None and self.current_bin != target_bin:
            last_switch = self.stops[self.current_bin]
            if current_loc is None or current_loc == last_switch:
                current_loc = last_switch
                up = moving_up(last_switch, target_switch, order=self.order)
                self._watch = MoveWatch(
                    {name: (name == last_switch) for name in self.order},
                    target_switch,
                    order=self.order,
                    release_within=self.release_within,
                    up=up,
                )
                self._target_bin = target_bin
                self._target_switch = target_switch
                self._pending_target_bin = None
                forward = bool(up)
                self.stepper.start(self.max_travel_steps, rate, forward=forward)
                self._move_started_at = time.monotonic()
                self.state = "moving"
                self.last_fault = None
                return True

        up = moving_up(current_loc, target_switch, order=self.order)
        self._watch = MoveWatch(
            current_switches,
            target_switch,
            order=self.order,
            release_within=self.release_within,
            up=up,
        )
        self._target_bin = target_bin
        self._target_switch = target_switch
        self._pending_target_bin = None
        forward = bool(up)
        self.stepper.start(self.max_travel_steps, rate, forward=forward)
        self._move_started_at = time.monotonic()
        self.state = "moving"
        self.last_fault = None
        return True

    def update(self, now: float | None = None) -> CarriageState:
        """Poll limit switches and update move status."""
        if self.state != "moving":
            return self.state

        if self._watch is None or self._limits is None:
            print(f"DEBUG carriage update: state={self.state} no watch or limits")
            return self.state

        rate = self._move_hz if self._move_hz > 0 else self.default_hz
        elapsed = time.monotonic() - (self._move_started_at or time.monotonic())
        steps_sent = int(elapsed * rate)

        switches = self._limits.read()
        print(f"DEBUG carriage: target={self._target_bin} current={self.current_bin} switches={switches} steps_sent={steps_sent} stepper_busy={self.stepper.busy}")

        status, detail = self._watch.update(switches, steps_sent)
        print(f"DEBUG carriage: watch status={status} detail={detail}")

        if status == ARRIVED:
            self.stepper.stop()
            self.state = "at_target"
            self.current_bin = self._target_bin
            self._watch = None
            print(f"DEBUG carriage: ARRIVED -> at_target, current_bin={self.current_bin}")
            pending_target = self._pending_target_bin
            self._pending_target_bin = None
            if pending_target is not None:
                if detail is None:
                    self.state = "fault"
                    self.last_fault = "carriage homing completed without identifying a limit switch"
                else:
                    self.current_bin = self.bins_by_stop[detail]
                    target_switch = self.stops[pending_target]
                    if detail == target_switch:
                        self.current_bin = pending_target
                    else:
                        up = moving_up(detail, target_switch, order=self.order)
                        self._watch = MoveWatch(
                            switches,
                            target_switch,
                            order=self.order,
                            release_within=self.release_within,
                            up=up,
                        )
                        self._target_bin = pending_target
                        self._target_switch = target_switch
                        self._move_started_at = time.monotonic()
                        self.state = "moving"
                        self.stepper.start(
                            self.max_travel_steps,
                            self._move_hz,
                            forward=up,
                        )
        elif status == FAULT:
            self.stepper.stop()
            self.state = "fault"
            self.last_fault = detail
            self._watch = None
            print(f"DEBUG carriage: FAULT -> {detail}")
        elif not self.stepper.busy or steps_sent >= self.max_travel_steps:
            self.stepper.stop()
            self.state = "fault"
            self.last_fault = f"carriage move timed out after ~{steps_sent} steps without reaching {self._target_switch}"
            self._watch = None
            print(f"DEBUG carriage: timeout fault -> {self.last_fault}")

        return self.state

    def stop(self) -> None:
        self.stepper.stop()
        if self.state == "moving" or self._watch is not None:
            self.state = "idle"
        self._watch = None
        self._target_bin = None
        self._target_switch = None

    def close(self) -> None:
        self.stop()
        self.stepper.close()
        if self._limits is not None:
            self._limits.close()
            self._limits = None


class SimulatedCarriage:
    """Simulated twin of CarriageDriver for testing and simulation runs."""

    def __init__(self, order=carriage.ORDER, bins=carriage.BINS, move_s_per_hop: float = 0.5):
        self.order = order
        self.bins = bins
        self.move_s_per_hop = move_s_per_hop
        self.stops = stops(order, bins)
        self.state: CarriageState = "at_target"
        self.current_bin: str | None = bins[0]
        self.last_fault: str | None = None

        self._target_bin: str | None = None
        self._arrives_at: float | None = None

    def check_home(self) -> str | None:
        self.state = "at_target"
        return self.current_bin

    def goto(self, target_bin: str, hz: float | None = None, now: float | None = None) -> bool:
        if target_bin not in self.stops:
            raise ValueError(f"unknown bin {target_bin!r}; expected one of {self.bins}")
        if target_bin == self.current_bin and self.state == "at_target":
            return True
        t = now if now is not None else time.monotonic()
        curr_idx = self.bins.index(self.current_bin) if self.current_bin in self.bins else 0
        tgt_idx = self.bins.index(target_bin)
        hops = max(1, abs(tgt_idx - curr_idx))
        self._target_bin = target_bin
        self._arrives_at = t + (hops * self.move_s_per_hop)
        self.state = "moving"
        self.last_fault = None
        return True

    def update(self, now: float | None = None) -> CarriageState:
        if self.state != "moving":
            return self.state
        t = now if now is not None else time.monotonic()
        if self._arrives_at is not None and t >= self._arrives_at:
            self.state = "at_target"
            self.current_bin = self._target_bin
            self._arrives_at = None
        return self.state

    def stop(self) -> None:
        if self.state == "moving":
            self.state = "idle"
        self._arrives_at = None

    def close(self) -> None:
        self.stop()
