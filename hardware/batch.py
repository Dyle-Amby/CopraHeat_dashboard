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
from typing import Any

from hardware.climate import FanController, HeaterController
from hardware.drying import DryingEndpoint
from hardware.hopper import HopperGate
from hardware.sorting import Prompt
from hardware.states import BatchState, ConveyorCommand

_LIVE = (BatchState.INTAKE, BatchState.DRYING, BatchState.COOLDOWN, BatchState.SORTING)
_FINISHED = (BatchState.COMPLETE, BatchState.ABORTED)

# The operator vocabulary, kept next to the methods it maps to. The data layer
# validates against it and the supervisor dispatches with apply_command().
COMMAND_NAMES = frozenset(
    {
        "start",
        "skip_intake",
        "end_drying",
        "finish_sorting",
        "abort",
        "reset",
        "set_target_kg",
        "override_grade",
        "set_drying_timer",
        "answer_prompt",
        "clear_prompt",
    }
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
    grade_ready: bool = False               # ensemble sampling finished or review needed
    batch_grade: str | None = None          # machine decision: "great" | "good" | "bad"
    needs_review: bool = False              # review flag
    sorting_complete: bool = False          # discharge finished and load cell stable
    sorted_kg: float | None = None          # sum of the three bin load cells
    sample_vectors: list[list[float]] | None = None
    sample_valid_n: int = 0
    sample_total_n: int = 0
    sorting_config_snapshot: str | None = None


@dataclass(frozen=True)
class Commands:
    """What the runner should do with its pins this tick."""

    state: BatchState
    heater_on: bool
    fans_on: bool
    gate_open: bool
    conveyor: ConveyorCommand
    alarms: frozenset[str]
    fan_reason: str = "off"
    sorting_stage: str | None = None        # "sample" | "await_review" | "discharge" | None
    carriage_target: str | None = None      # "great" | "good" | "bad" | None
    active_prompt: Prompt | None = None
    drying_timer_duration_s: float | None = None
    drying_timer_deadline: float | None = None


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
    batch_grade: str | None = None          # machine determination
    operator_grade: str | None = None       # operator selection if overridden
    override_used: bool = False             # True if operator selected the bin
    needs_review: bool = False              # review required flag
    sample_vectors: list[list[float]] | None = None
    sample_valid_n: int = 0
    sample_total_n: int = 0
    sorting_config_snapshot: str | None = None
    drying_timed_out: bool = False
    drying_timer_duration_s: float | None = None
    drying_timer_deadline: float | None = None


def apply_command(controller: "BatchController", name: str, payload: Any = None) -> str:
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
    if name == "skip_intake":
        controller.skip_intake()
        return "skipping intake: moving to drying on the next tick"
    if name == "end_drying":
        controller.end_drying()
        return "drying will end on the next tick"
    if name == "finish_sorting":
        controller.finish_sorting()
        return "sorting will end on the next tick"
    if name == "override_grade":
        if payload not in ("great", "good", "bad"):
            raise ValueError(f"invalid grade {payload!r}; expected 'great', 'good', or 'bad'")
        controller.override_grade(payload)
        return f"grade overridden to {payload}"
    if name == "abort":
        reason = payload if isinstance(payload, str) else (payload.get("reason", "operator abort") if isinstance(payload, dict) else "operator abort")
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
    if name == "set_drying_timer":
        if payload is None:
            raise ValueError("set_drying_timer requires a duration payload")
        import json
        duration_s = None
        if isinstance(payload, (int, float)):
            duration_s = float(payload)
        elif isinstance(payload, dict):
            if "duration_min" in payload:
                duration_s = float(payload["duration_min"]) * 60.0
            elif "duration_s" in payload:
                duration_s = float(payload["duration_s"])
        elif isinstance(payload, str):
            try:
                data = json.loads(payload)
                if isinstance(data, dict):
                    if "duration_min" in data:
                        duration_s = float(data["duration_min"]) * 60.0
                    elif "duration_s" in data:
                        duration_s = float(data["duration_s"])
                else:
                    duration_s = float(data)
            except Exception:
                duration_s = float(payload)
        if duration_s is None or duration_s <= 0:
            raise ValueError("drying timer duration must be positive")
        controller.set_drying_timer(duration_s)
        return f"drying timer set to {duration_s / 60:.1f} minutes"
    if name == "answer_prompt":
        if payload is None:
            raise ValueError("answer_prompt requires an action payload")
        import json
        action = None
        prompt_id = None
        if isinstance(payload, dict):
            action = payload.get("action") or payload.get("option")
            prompt_id = payload.get("id")
        elif isinstance(payload, str):
            try:
                data = json.loads(payload)
                if isinstance(data, dict):
                    action = data.get("action") or data.get("option")
                    prompt_id = data.get("id")
                else:
                    action = str(data)
            except Exception:
                action = payload
        if not action:
            raise ValueError("answer_prompt requires an action")
        ok = controller.answer_prompt(action, prompt_id=prompt_id)
        return f"prompt answered with {action}" if ok else "no matching prompt to answer"
    if name == "clear_prompt":
        controller.clear_prompt()
        return "active prompt cleared"
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
        intake_advance_timeout_s: float = 30.0,
        max_sorting_s: float = 60 * 60,
        heater: HeaterController | None = None,
        fans: FanController | None = None,
        gate: HopperGate | None = None,
        endpoint: DryingEndpoint | None = None,
    ):
        if intake_advance_timeout_s <= conveyor_ack_s:
            raise ValueError(
                f"intake_advance_timeout_s ({intake_advance_timeout_s:.1f} s) must be "
                f"greater than conveyor_ack_s ({conveyor_ack_s:.1f} s)"
            )
        self.cooldown_target_c = cooldown_target_c
        self.max_cooldown_s = max_cooldown_s
        self.intake_stall_s = intake_stall_s
        self.conveyor_ack_s = conveyor_ack_s
        self.intake_advance_timeout_s = intake_advance_timeout_s
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

    def skip_intake(self) -> None:
        """Manual intake override to skip dispensing and conveyor advance, moving directly to drying."""
        if self.state is not BatchState.INTAKE:
            raise RuntimeError(f"not in intake (state is {self.state.value})")
        self._skip_intake_requested = True

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

    def override_grade(self, grade: str) -> None:
        """Operator manual bin assignment (used especially on needs_review)."""
        if self.state is not BatchState.SORTING:
            raise RuntimeError(f"cannot override grade from {self.state.value}")
        if grade not in ("great", "good", "bad"):
            raise ValueError(f"invalid grade {grade!r}")
        self.operator_grade = grade
        self.override_used = True
        self._alarms.discard("sorting_needs_review")

    def abort(self, reason: str = "operator abort") -> None:
        if self.state not in _LIVE:
            raise RuntimeError(f"no batch to abort (state is {self.state.value})")
        self._abort_requested = reason

    def reset(self) -> None:
        if self.state not in _FINISHED:
            raise RuntimeError(f"cannot reset from {self.state.value}")
        self._reset_requested = True

    def set_drying_timer(self, duration_s: float, now: float | None = None) -> None:
        """Set operator drying timer. Clamped so deadline never exceeds 8 h from drying start."""
        if self.state not in (BatchState.DRYING, BatchState.INTAKE, BatchState.IDLE):
            raise RuntimeError(f"cannot set drying timer in {self.state.value}")
        self.drying_timer_duration_s = duration_s
        ref_time = now if now is not None else (self._drying_started or self._entered_at or 0.0)
        max_duration = getattr(self.endpoint, "max_time_s", 28800.0)
        if self._drying_started is not None:
            remaining_to_8h = max(0.0, (self._drying_started + max_duration) - ref_time)
            effective_dur = min(duration_s, remaining_to_8h)
            self.drying_timer_deadline = ref_time + effective_dur
        else:
            self.drying_timer_deadline = ref_time + min(duration_s, max_duration)
        self._alarms.discard("drying_timer_expired")
        if self.active_prompt and self.active_prompt.id == "drying_timer_expired":
            self.active_prompt = None

    def answer_prompt(self, action: str, now: float | None = None, prompt_id: str | None = None) -> bool:
        """Answer an active blocking prompt."""
        if not self.active_prompt:
            return False
        if prompt_id and self.active_prompt.id != prompt_id:
            return False

        p_id = self.active_prompt.id
        max_duration = getattr(self.endpoint, "max_time_s", 28800.0)
        now_val = now if now is not None else getattr(self, "_last_now", 0.0)
        if p_id == "drying_timer_expired":
            if action.startswith("extend_"):
                try:
                    mins = float(action.replace("extend_", ""))
                    add_s = mins * 60.0
                except Exception:
                    add_s = 1800.0
                # Extend from max(deadline, now), capped at 8 h after drying start
                base_time = max(self.drying_timer_deadline or 0.0, now_val)
                start_time = self._drying_started if self._drying_started is not None else 0.0
                max_deadline = start_time + max_duration
                new_deadline = min(base_time + add_s, max_deadline)
                self.drying_timer_deadline = new_deadline
                self._alarms.discard("drying_timer_expired")
                self.active_prompt = None
                return True
            elif action == "end_drying":
                self._alarms.discard("drying_timer_expired")
                self.active_prompt = None
                self.end_drying()
                return True
        elif p_id in ("carriage_fault", "positioning_stall", "discharge_timeout", "bin_did_not_gain_weight"):
            if action == "abort":
                self.abort(f"operator aborted on {p_id}")
                self.active_prompt = None
                return True
            elif action in ("retry", "proceed_to_verify", "override", "retry_discharge"):
                self.active_prompt = None
                return True
        return False

    def clear_prompt(self) -> None:
        self.active_prompt = None

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
            batch_grade=self.batch_grade,
            operator_grade=self.operator_grade,
            override_used=self.override_used,
            needs_review=self.needs_review,
            sample_vectors=self.sample_vectors,
            sample_valid_n=self.sample_valid_n,
            sample_total_n=self.sample_total_n,
            sorting_config_snapshot=self.sorting_config_snapshot,
            drying_timed_out=self.drying_timed_out,
            drying_timer_duration_s=self.drying_timer_duration_s,
            drying_timer_deadline=self.drying_timer_deadline,
        )

    # --- the loop ------------------------------------------------------------

    def update(self, i: Inputs) -> Commands:
        now = i.now
        self._last_now = now

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
        sorting_stage = None
        carriage_target = None

        if self.state is BatchState.INTAKE:
            gate_open, conveyor = self._intake(i)
        elif self.state is BatchState.DRYING:
            self._drying(i)
        elif self.state is BatchState.COOLDOWN:
            self._cooldown(i)
        elif self.state is BatchState.SORTING:
            conveyor, sorting_stage, carriage_target = self._sorting(i)

        # run the climate controllers on the state we just landed in, so a
        # transition takes effect on this tick rather than the next one
        heater_on = self.heater.update(self.state, i.chamber_temp)
        fans_on = self.fans.update(self.state, i.chamber_temp, now, i.exhaust_rh)
        self._set_alarm("overtemp", self.fans.overtemp_active)

        return Commands(
            state=self.state,
            heater_on=heater_on,
            fans_on=fans_on,
            gate_open=gate_open,
            conveyor=conveyor,
            alarms=frozenset(self._alarms),
            fan_reason=self.fans.reason,
            sorting_stage=sorting_stage,
            carriage_target=carriage_target,
            active_prompt=self.active_prompt,
            drying_timer_duration_s=self.drying_timer_duration_s,
            drying_timer_deadline=self.drying_timer_deadline,
        )

    # --- per-state handling --------------------------------------------------

    def _intake(self, i: Inputs) -> tuple[bool, ConveyorCommand]:
        # --- Skip-intake: consume the request, arm the advance clock -----------
        # Only consume once; if the advance is already running, ignore duplicate.
        if self._skip_intake_requested and not self._dispensed:
            self._skip_intake_requested = False
            self._dispensed = True
            self._intake_skipped = True          # telemetry flag for logs / dashboard
            if self.weight_in_kg is None:
                self.weight_in_kg = self.gate.stable_kg or i.hopper_kg or self.target_kg
            self._alarms.discard("intake_stall")
            self._alarms.discard("hopper_jam")
            self._advance_started = i.now        # reset clock; advance starts this tick
        elif self._skip_intake_requested:
            # advance already in progress: silently drop the duplicate request
            self._skip_intake_requested = False

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

        # --- Advance phase: carry the batch into the chamber ------------------
        # Order matters: check completion before the timeout so a belt that
        # finishes on the exact timeout tick still transitions cleanly.

        # 1. Ack seen and belt just went idle → transition to DRYING.
        #    Both alarms are cleared here because the move succeeded.
        if self._conveyor_ack and not i.conveyor_busy:
            self._alarms.discard("conveyor_stall")
            self._alarms.discard("intake_advance_timeout")
            self._to(BatchState.DRYING, i.now)
            return False, ConveyorCommand.STOP

        # 2. Hard timeout: covers both "belt never moved" and "belt went busy
        #    but never finished". Stop and hold; operator must abort.
        if i.now - self._advance_started >= self.intake_advance_timeout_s:
            self._alarms.add("intake_advance_timeout")
            return False, ConveyorCommand.STOP

        # 3. Belt is busy right now → record that it acknowledged the command.
        if i.conveyor_busy:
            self._conveyor_ack = True
        # 4. Belt never moved within the early-warning window.
        elif i.now - self._advance_started >= self.conveyor_ack_s:
            self._alarms.add("conveyor_stall")

        return False, ConveyorCommand.INTAKE_ADVANCE

    def _drying(self, i: Inputs) -> None:
        done = self.endpoint.update(i.exhaust_temp, i.exhaust_rh, i.now)
        self._set_alarm("exhaust_sensor_fault", self.endpoint.fault)
        self._set_alarm("chamber_sensor_fault", i.chamber_temp is None)

        if self.endpoint.timeout:
            self._alarms.add("drying_timeout")
            self._alarms.discard("drying_timer_expired")
            self.drying_timed_out = True
            self.drying_timer_deadline = None
            self.active_prompt = None
            self._to(BatchState.COOLDOWN, i.now)
            return

        if done or self._end_drying_requested:
            self._end_drying_requested = False
            self.drying_timer_deadline = None
            self.active_prompt = None
            self._alarms.discard("drying_timer_expired")
            self._to(BatchState.COOLDOWN, i.now)
            return

        # Operator drying timer check
        if self.drying_timer_deadline is not None:
            if i.now >= self.drying_timer_deadline:
                self._alarms.add("drying_timer_expired")
                start = self._drying_started if self._drying_started is not None else self._entered_at
                max_duration = getattr(self.endpoint, "max_time_s", 28800.0)
                remaining_to_8h = max(0.0, (start + max_duration) - i.now)
                rem_min = int(remaining_to_8h // 60)
                if self.active_prompt is None or self.active_prompt.id != "drying_timer_expired":
                    self.active_prompt = Prompt(
                        id="drying_timer_expired",
                        message=(
                            f"Drying timer expired but copra is not yet dry. "
                            f"{rem_min} min remaining before 8 h limit. Choose action:"
                        ),
                        options=["extend_15", "extend_30", "extend_60", "end_drying"],
                        created_at=i.now,
                        context={"remaining_to_8h_s": remaining_to_8h, "rem_min": rem_min},
                    )

    def _cooldown(self, i: Inputs) -> None:
        self._set_alarm("chamber_sensor_fault", i.chamber_temp is None)
        cool = i.chamber_temp is not None and i.chamber_temp <= self.cooldown_target_c
        # a dead chamber probe must not strand the batch in cooldown forever
        if cool or i.now - self._entered_at >= self.max_cooldown_s:
            self._to(BatchState.ABORTED if self.aborted else BatchState.SORTING, i.now)

    def _sorting(self, i: Inputs) -> tuple[ConveyorCommand, str, str | None]:
        if i.sorted_kg is not None:
            self.weight_out_kg = i.sorted_kg

        # Update metadata if provided in Inputs
        if i.batch_grade is not None and self.batch_grade is None:
            self.batch_grade = i.batch_grade
        if i.needs_review:
            self.needs_review = True
            if not self.override_used:
                self._alarms.add("sorting_needs_review")
        if i.sample_vectors is not None:
            self.sample_vectors = i.sample_vectors
        if i.sample_valid_n:
            self.sample_valid_n = i.sample_valid_n
        if i.sample_total_n:
            self.sample_total_n = i.sample_total_n
        if i.sorting_config_snapshot is not None:
            self.sorting_config_snapshot = i.sorting_config_snapshot

        effective_grade = self.operator_grade or self.batch_grade

        # Operator manual finish override OR discharge completion
        if i.sorting_complete or self._finish_sorting_requested:
            self._finish_sorting_requested = False
            self._to(BatchState.COMPLETE, i.now)
            return ConveyorCommand.STOP, "complete", effective_grade

        if i.now - self._entered_at >= self.max_sorting_s:
            self._alarms.add("sorting_timeout")

        # Before grade is ready: sampling stage, NO discharge
        if not i.grade_ready and effective_grade is None:
            return ConveyorCommand.STOP, "sample", None

        # Grade is ready, but review needed and no operator override yet
        if self.needs_review and not self.override_used and self.operator_grade is None:
            return ConveyorCommand.STOP, "await_review", None

        # Target confirmed (either via auto-grade or operator override): discharge stage
        return ConveyorCommand.SORT_CREEP, "discharge", effective_grade

    # --- transitions ---------------------------------------------------------

    def _to(self, state: BatchState, now: float) -> None:
        if state is BatchState.DRYING:
            self.endpoint.start(now)
            self._drying_started = now
            if self.drying_timer_duration_s is not None and self.drying_timer_deadline is None:
                max_dur = getattr(self.endpoint, "max_time_s", 28800.0)
                self.drying_timer_deadline = now + min(self.drying_timer_duration_s, max_dur)
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
        self.batch_grade: str | None = None
        self.operator_grade: str | None = None
        self.override_used: bool = False
        self.needs_review: bool = False
        self.sample_vectors: list[list[float]] | None = None
        self.sample_valid_n: int = 0
        self.sample_total_n: int = 0
        self.sorting_config_snapshot: str | None = None
        self.drying_timed_out: bool = False
        # Preserve drying_timer_duration_s if set in IDLE prior to start
        preserved_timer = getattr(self, "drying_timer_duration_s", None)
        self.drying_timer_duration_s: float | None = preserved_timer
        self.drying_timer_deadline: float | None = None
        self.active_prompt: Prompt | None = None
        self._intake_skipped = False
        self._dispensed = False
        self._conveyor_ack = False
        self._advance_started = now
        self._drying_started: float | None = None
        self._start_requested = False
        self._skip_intake_requested = False
        self._end_drying_requested = False
        self._finish_sorting_requested = False
        self._abort_requested: str | None = None
        self._reset_requested = False
