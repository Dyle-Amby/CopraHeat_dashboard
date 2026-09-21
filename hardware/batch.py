"""Batch orchestration for the whole Intake -> Drying -> Sorting cycle.

No GPIO here, same as the controllers it drives: feed sensor readings in, get
actuator commands out. The runner applies `Commands` to pins and reports back
through `Inputs`; every decision about *what* the machine should be doing lives
here, so it is all unit-testable off the Pi.

Operator actions (start, end drying early, abort, reset) are requests: they
validate immediately so the dashboard gets a straight answer, then take effect
on the next `update()`, where there are fresh readings to act on.
"""

from dataclasses import dataclass

from hardware.climate import FanController, HeaterController
from hardware.drying import DryingEndpoint
from hardware.hopper import HopperGate
from hardware.states import BatchState, ConveyorCommand

_LIVE = (BatchState.INTAKE, BatchState.DRYING, BatchState.COOLDOWN, BatchState.SORTING)
_FINISHED = (BatchState.COMPLETE, BatchState.ABORTED)

# The operator vocabulary, kept next to the methods it maps to. The data layer
# validates against it and the supervisor dispatches with apply_command().
COMMAND_NAMES = frozenset(
    {"start", "end_drying", "finish_sorting", "abort", "reset", "set_target_kg"}
)


@dataclass(frozen=True)
class Inputs:
    """One tick of the world, as the runner sees it."""

    now: float
    chamber_temp: float | None = None       # DS18B20
    exhaust_temp: float | None = None       # DHT22, near the exhaust fan
    exhaust_rh: float | None = None
    hopper_kg: float | None = None          # HX711 on the hopper gate
    conveyor_busy: bool = False             # runner is mid-move
    sorting_complete: bool = False          # the sorter has graded the last piece
    sorted_kg: float | None = None          # sum of the three bin load cells


@dataclass(frozen=True)
class Commands:
    """What the runner should do with its pins this tick."""

    state: BatchState
    heater_on: bool
    fans_on: bool
    gate_open: bool
    conveyor: ConveyorCommand
    alarms: frozenset[str]


@dataclass(frozen=True)
class BatchRecord:
    """End-of-batch figures.

    Durations are deltas of whatever clock the runner feeds in (monotonic);
    wall-clock timestamps are the runner's to stamp when it writes the row.
    """

    duration_s: float
    drying_s: float | None
    weight_in_kg: float | None
    weight_out_kg: float | None
    weight_loss_pct: float | None
    aborted: bool
    abort_reason: str | None
    alarms: frozenset[str]


def apply_command(controller: "BatchController", name: str, payload: str | None = None) -> str:
    """Dispatch an operator command by name and return what to tell the operator.

    Raises ValueError for a name or payload that makes no sense, and RuntimeError
    (from the controller) for a request the machine refuses in its current state.
    Callers turn either into the message shown next to the button, so a
    double-tapped Start reads "cannot start from drying" rather than doing
    nothing quietly.
    """
    if name == "start":
        controller.start()
        return "batch will start on the next tick"
    if name == "end_drying":
        controller.end_drying()
        return "drying will end on the next tick"
    if name == "finish_sorting":
        controller.finish_sorting()
        return "sorting will end on the next tick"
    if name == "abort":
        reason = payload or "operator abort"
        controller.abort(reason)
        return f"aborting: {reason}"
    if name == "reset":
        controller.reset()
        return "returning to idle"
    if name == "set_target_kg":
        if payload is None:
            raise ValueError("set_target_kg needs a target weight")
        controller.set_target_kg(float(payload))   # float() rejects junk payloads
        return f"target weight set to {controller.target_kg} kg"
    raise ValueError(f"unknown command {name!r}")


class BatchController:
    """IDLE -> INTAKE -> DRYING -> COOLDOWN -> SORTING -> COMPLETE, plus ABORTED.

    An abort never leaves a hot chamber unventilated: while the chamber is above
    the cooldown target it routes through COOLDOWN with the fans purging, then
    lands in ABORTED instead of SORTING.
    """

    def __init__(
        self,
        target_kg: float = 2.0,
        cooldown_target_c: float = 40.0,
        max_cooldown_s: float = 30 * 60,
        intake_stall_s: float = 10 * 60,
        conveyor_ack_s: float = 5.0,
        max_sorting_s: float = 60 * 60,
        heater: HeaterController | None = None,
        fans: FanController | None = None,
        gate: HopperGate | None = None,
        endpoint: DryingEndpoint | None = None,
    ):
        self.cooldown_target_c = cooldown_target_c
        self.max_cooldown_s = max_cooldown_s
        self.intake_stall_s = intake_stall_s
        self.conveyor_ack_s = conveyor_ack_s
        self.max_sorting_s = max_sorting_s

        self.heater = heater or HeaterController()
        self.fans = fans or FanController()
        self.gate = gate or HopperGate(target_kg=target_kg)
        self.endpoint = endpoint or DryingEndpoint()

        self.state = BatchState.IDLE
        self._entered_at = 0.0
        self._clear_batch(0.0)

    # --- operator requests ---------------------------------------------------

    @property
    def target_kg(self) -> float:
        return self.gate.target_kg

    def set_target_kg(self, value: float) -> None:
        """Dashboard-settable. Refused once the hopper has already dispensed."""
        if self._dispensed:
            raise RuntimeError("batch already dispensed; target weight no longer applies")
        self.gate.target_kg = value   # the gate property validates the value

    def start(self) -> None:
        if self.state is not BatchState.IDLE:
            raise RuntimeError(f"cannot start from {self.state.value}")
        self._start_requested = True

    def end_drying(self) -> None:
        """Manual endpoint override. The absolute-humidity thresholds stay
        uncalibrated until real batches have been run against post-sort weight
        loss, so early runs need a way to call it by eye."""
        if self.state is not BatchState.DRYING:
            raise RuntimeError(f"not drying (state is {self.state.value})")
        self._end_drying_requested = True

    def finish_sorting(self) -> None:
        if self.state is not BatchState.SORTING:
            raise RuntimeError(f"not sorting (state is {self.state.value})")
        self._finish_sorting_requested = True

    def abort(self, reason: str = "operator abort") -> None:
        if self.state not in _LIVE:
            raise RuntimeError(f"no batch to abort (state is {self.state.value})")
        self._abort_requested = reason

    def reset(self) -> None:
        if self.state not in _FINISHED:
            raise RuntimeError(f"cannot reset from {self.state.value}")
        self._reset_requested = True

    # --- readouts ------------------------------------------------------------

    @property
    def alarms(self) -> frozenset[str]:
        return frozenset(self._alarms)

    def elapsed_s(self, now: float) -> float | None:
        if self.started_at is None:
            return None
        return (self.finished_at if self.finished_at is not None else now) - self.started_at

    def record(self) -> BatchRecord | None:
        """The end-of-batch row. None until the batch has finished."""
        if self.state not in _FINISHED or self.started_at is None or self.finished_at is None:
            return None
        loss = None
        if self.weight_in_kg and self.weight_out_kg is not None:
            loss = (self.weight_in_kg - self.weight_out_kg) / self.weight_in_kg * 100.0
        return BatchRecord(
            duration_s=self.finished_at - self.started_at,
            drying_s=self.drying_s,
            weight_in_kg=self.weight_in_kg,
            weight_out_kg=self.weight_out_kg,
            weight_loss_pct=loss,
            aborted=self.aborted,
            abort_reason=self.abort_reason,
            alarms=frozenset(self._alarms),
        )

    # --- the loop ------------------------------------------------------------

    def update(self, i: Inputs) -> Commands:
        now = i.now

        if self._reset_requested:
            self._reset_requested = False
            self._clear_batch(now)
            self._to(BatchState.IDLE, now)
        elif self._start_requested:
            self._start_requested = False
            self._clear_batch(now)
            self.started_at = now
            self._to(BatchState.INTAKE, now)
        elif self._abort_requested is not None:
            self._begin_abort(self._abort_requested, i)
            self._abort_requested = None

        gate_open = False
        conveyor = ConveyorCommand.STOP

        if self.state is BatchState.INTAKE:
            gate_open, conveyor = self._intake(i)
        elif self.state is BatchState.DRYING:
            self._drying(i)
        elif self.state is BatchState.COOLDOWN:
            self._cooldown(i)
        elif self.state is BatchState.SORTING:
            conveyor = self._sorting(i)

        # run the climate controllers on the state we just landed in, so a
        # transition takes effect on this tick rather than the next one
        heater_on = self.heater.update(self.state, i.chamber_temp)
        fans_on = self.fans.update(self.state, i.chamber_temp, now)
        self._set_alarm("overtemp", self.fans.overtemp_active)

        return Commands(
            state=self.state,
            heater_on=heater_on,
            fans_on=fans_on,
            gate_open=gate_open,
            conveyor=conveyor,
            alarms=frozenset(self._alarms),
        )

    # --- per-state handling --------------------------------------------------

    def _intake(self, i: Inputs) -> tuple[bool, ConveyorCommand]:
        if not self._dispensed:
            was_open = self.gate.gate_open
            gate_open = self.gate.update(i.hopper_kg, i.now)
            if gate_open and not was_open:
                # the weight the gate opened on is the hopper-in figure for the
                # end-of-batch weight loss; the cell reads nothing useful after
                self.weight_in_kg = self.gate.stable_kg
            self._set_alarm("hopper_jam", self.gate.jammed)
            if was_open and not gate_open:
                self._dispensed = True
                self._advance_started = i.now
            elif i.now - self._entered_at >= self.intake_stall_s:
                self._alarms.add("intake_stall")
            return gate_open, ConveyorCommand.STOP

        # Dispense done: carry the batch into the chamber. Dead reckoning -- the
        # runner owns the calibrated step count and reports the move through
        # conveyor_busy, so wait to see it go busy before trusting it went idle.
        # (Open mechanical question: whether the belt should instead creep during
        # dispensing to spread the copra. That changes this branch, nothing else.)
        if i.conveyor_busy:
            self._conveyor_ack = True
        elif self._conveyor_ack:
            self._to(BatchState.DRYING, i.now)
            return False, ConveyorCommand.STOP
        elif i.now - self._advance_started >= self.conveyor_ack_s:
            self._alarms.add("conveyor_stall")
        return False, ConveyorCommand.INTAKE_ADVANCE

    def _drying(self, i: Inputs) -> None:
        done = self.endpoint.update(i.exhaust_temp, i.exhaust_rh, i.now)
        self._set_alarm("exhaust_sensor_fault", self.endpoint.fault)
        self._set_alarm("chamber_sensor_fault", i.chamber_temp is None)

        if self.endpoint.timeout:
            self._alarms.add("drying_timeout")
            self._begin_abort("drying exceeded its time ceiling", i)
        elif done or self._end_drying_requested:
            self._end_drying_requested = False
            self._to(BatchState.COOLDOWN, i.now)

    def _cooldown(self, i: Inputs) -> None:
        self._set_alarm("chamber_sensor_fault", i.chamber_temp is None)
        cool = i.chamber_temp is not None and i.chamber_temp <= self.cooldown_target_c
        # a dead chamber probe must not strand the batch in cooldown forever
        if cool or i.now - self._entered_at >= self.max_cooldown_s:
            self._to(BatchState.ABORTED if self.aborted else BatchState.SORTING, i.now)

    def _sorting(self, i: Inputs) -> ConveyorCommand:
        if i.sorted_kg is not None:
            self.weight_out_kg = i.sorted_kg
        if i.sorting_complete or self._finish_sorting_requested:
            self._finish_sorting_requested = False
            self._to(BatchState.COMPLETE, i.now)
            return ConveyorCommand.STOP
        if i.now - self._entered_at >= self.max_sorting_s:
            self._alarms.add("sorting_timeout")
        return ConveyorCommand.SORT_CREEP

    # --- transitions ---------------------------------------------------------

    def _to(self, state: BatchState, now: float) -> None:
        if state is BatchState.DRYING:
            self.endpoint.start(now)
            self._drying_started = now
        elif self._drying_started is not None:
            self.drying_s = now - self._drying_started
            self._drying_started = None
        if state in _FINISHED:
            self.finished_at = now
        self.state = state
        self._entered_at = now

    def _begin_abort(self, reason: str, i: Inputs) -> None:
        self.aborted = True
        self.abort_reason = reason
        self._alarms.add("aborted")
        if self.state is BatchState.COOLDOWN:
            return   # already purging; _cooldown lands in ABORTED now the flag is set
        # an unknown chamber temperature is treated as hot: purge, then stop
        hot = i.chamber_temp is None or i.chamber_temp > self.cooldown_target_c
        self._to(BatchState.COOLDOWN if hot else BatchState.ABORTED, i.now)

    def _set_alarm(self, name: str, active: bool) -> None:
        self._alarms.add(name) if active else self._alarms.discard(name)

    def _clear_batch(self, now: float) -> None:
        self._alarms: set[str] = set()
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.drying_s: float | None = None
        self.weight_in_kg: float | None = None
        self.weight_out_kg: float | None = None
        self.aborted = False
        self.abort_reason: str | None = None
        self._dispensed = False
        self._conveyor_ack = False
        self._advance_started = now
        self._drying_started: float | None = None
        self._start_requested = False
        self._end_drying_requested = False
        self._finish_sorting_requested = False
        self._abort_requested: str | None = None
        self._reset_requested = False
