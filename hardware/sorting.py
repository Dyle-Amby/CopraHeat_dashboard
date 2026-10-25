"""State machine for copra sorting, visual sampling, carriage dispatch, and discharge verification.

Pure logic, GPIO-free, fully unit-testable.
Stages:
    POSITION  -> move chamber mid to camera point (chamber_to_camera_steps), wait done, verify copra visible
    SAMPLE    -> jog steps_per_sample, wait done, wait settle timer (no sleep), capture; repeat until grade_ready
    REVIEW    -> wait for operator review only if sampler.needs_review is True
    CARRIAGE  -> goto target bin, wait at_target, prompt on timeout or fault
    DISCHARGE -> move discharge_total_steps, wait done, prompt on timeout/stall
    VERIFY    -> wait for bin scale stable weight rise over baseline; prompt if no weight gain
    DONE      -> sorting complete
"""

from dataclasses import dataclass, field
from enum import Enum
import math
from typing import Any, Sequence

from hardware.states import ConveyorCommand
from hardware.grading import DEFAULT_GRADING_CONFIG, SortingSampler, decide_grade


class SortingStage(str, Enum):
    POSITION = "position"
    SAMPLE = "sample"
    REVIEW = "review"
    CARRIAGE = "carriage"
    DISCHARGE = "discharge"
    VERIFY = "verify"
    DONE = "done"


@dataclass
class Prompt:
    id: str
    message: str
    options: list[str]
    created_at: float
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "message": self.message,
            "options": list(self.options),
            "created_at": self.created_at,
            "context": self.context,
        }


@dataclass
class SortingFlow:
    """State machine executing the complete sorting process."""

    config: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_GRADING_CONFIG))
    chamber_to_camera_steps: int = 2000
    discharge_total_steps: int = 3000
    sample_hz: float = 400.0
    settle_s: float = 0.3
    bin_settle_s: float = 2.0
    bin_verify_timeout_s: float = 15.0
    min_weight_rise_kg: float = 0.05
    carriage_move_timeout_s: float = 30.0
    discharge_timeout_s: float = 60.0
    position_timeout_s: float = 45.0

    def __post_init__(self):
        # Override config attributes if explicitly supplied
        cfg = dict(DEFAULT_GRADING_CONFIG)
        cfg.update(self.config)
        cfg["discharge_total_steps"] = self.discharge_total_steps
        cfg["sample_hz"] = self.sample_hz
        cfg["settle_s"] = self.settle_s
        cfg["carriage_move_timeout_s"] = self.carriage_move_timeout_s
        cfg["discharge_timeout_s"] = self.discharge_timeout_s
        self.sampler = SortingSampler(config=cfg)

        self.stage: SortingStage = SortingStage.POSITION
        self.stage_entered_at: float = 0.0
        self.active_prompt: Prompt | None = None
        self.alarms: set[str] = set()

        # POSITION stage internal tracking
        self._position_move_started: bool = False
        self._position_move_done: bool = False

        # SAMPLE stage internal tracking
        self._sample_move_started: bool = False
        self._sample_move_completed_at: float | None = None
        self._waiting_capture: bool = False
        self.capture_requested: bool = False

        # CARRIAGE stage internal tracking
        self._carriage_move_started_at: float | None = None
        self.carriage_target: str | None = None

        # DISCHARGE stage internal tracking
        self._discharge_move_started: bool = False
        self._discharge_started_at: float | None = None
        self._discharge_done: bool = False

        # VERIFY stage internal tracking
        self.baseline_bin_kg: float | None = None
        self.weight_out_kg: float | None = None
        self._verify_started_at: float | None = None

        # Conveyor command output
        self.conveyor_command: ConveyorCommand = ConveyorCommand.STOP
        self.conveyor_move_steps: int = 0
        self.conveyor_move_hz: float = 400.0
        self.is_complete: bool = False

    def start(self, now: float) -> None:
        """Start or restart the sorting flow."""
        self.stage = SortingStage.POSITION
        self.stage_entered_at = now
        self.active_prompt = None
        self.alarms.clear()
        self._position_move_started = False
        self._position_move_done = False
        self._sample_move_started = False
        self._sample_move_completed_at = None
        self._waiting_capture = False
        self.capture_requested = False
        self._carriage_move_started_at = None
        self.carriage_target = None
        self._discharge_move_started = False
        self._discharge_started_at = None
        self._discharge_done = False
        self.baseline_bin_kg = None
        self.weight_out_kg = None
        self._verify_started_at = None
        self.is_complete = False

    @property
    def batch_grade(self) -> str | None:
        return self.sampler.batch_grade

    @property
    def needs_review(self) -> bool:
        return self.sampler.needs_review

    @property
    def grade_ready(self) -> bool:
        return self.sampler.grade_ready

    def answer_prompt(self, action: str, now: float, operator_grade: str | None = None) -> bool:
        """Handle operator response to a blocking prompt."""
        if not self.active_prompt:
            return False

        prompt_id = self.active_prompt.id
        self.active_prompt = None

        if prompt_id == "position_uncalibrated":
            if action == "override":
                # Bypass position travel check and go straight to sample
                self.stage = SortingStage.SAMPLE
                self.stage_entered_at = now
                return True
            elif action == "abort":
                self.alarms.add("aborted")
                return True

        elif prompt_id == "positioning_stall":
            if action == "retry":
                self._position_move_started = False
                self.stage_entered_at = now
                return True
            elif action == "abort":
                self.alarms.add("aborted")
                return True

        elif prompt_id == "empty_belt_detected":
            if action in ("proceed_anyway", "proceed"):
                self.stage = SortingStage.SAMPLE
                self.stage_entered_at = now
                return True
            elif action == "retry_position":
                self._position_move_started = False
                self.stage_entered_at = now
                return True
            elif action == "abort":
                self.alarms.add("aborted")
                return True

        elif prompt_id == "sorting_needs_review":
            if action in ("bad", "good", "great"):
                self.sampler.operator_grade = action
                self.stage = SortingStage.CARRIAGE
                self.stage_entered_at = now
                return True
            elif action == "abort":
                self.alarms.add("aborted")
                return True

        elif prompt_id == "carriage_fault":
            if action == "retry":
                self._carriage_move_started_at = None
                return True
            elif action == "abort":
                self.alarms.add("aborted")
                return True

        elif prompt_id == "discharge_timeout":
            if action == "retry":
                self._discharge_move_started = False
                self.stage_entered_at = now
                return True
            elif action == "proceed_to_verify":
                self.stage = SortingStage.VERIFY
                self.stage_entered_at = now
                self._verify_started_at = now
                return True
            elif action == "abort":
                self.alarms.add("aborted")
                return True

        elif prompt_id == "bin_did_not_gain_weight":
            if action == "override":
                if self.weight_out_kg is None:
                    self.weight_out_kg = 0.0
                self.stage = SortingStage.DONE
                self.is_complete = True
                return True
            elif action == "retry_discharge":
                self.stage = SortingStage.DISCHARGE
                self.stage_entered_at = now
                self._discharge_move_started = False
                self._discharge_started_at = None
                return True
            elif action == "abort":
                self.alarms.add("aborted")
                return True

        return False

    def update(
        self,
        *,
        now: float,
        conveyor_busy: bool,
        conveyor_last_result: str = "done",
        conveyor_position_steps: int = 0,
        carriage_state: str = "idle",
        carriage_current_bin: str | None = None,
        bin_kg: float | None = None,
        operator_grade: str | None = None,
        copra_present: bool = True,
        frame_vector: Sequence[float] | None = None,
    ) -> None:
        """Advance sorting state machine for one supervisor tick."""
        if self.is_complete:
            self.conveyor_command = ConveyorCommand.STOP
            self.conveyor_move_steps = 0
            self.capture_requested = False
            return

        # If a blocking prompt is waiting for operator, hold machine safely
        if self.active_prompt is not None:
            self.conveyor_command = ConveyorCommand.STOP
            self.conveyor_move_steps = 0
            self.capture_requested = False
            return

        effective_grade = operator_grade or getattr(self.sampler, "operator_grade", None) or self.sampler.batch_grade

        # ---------------------------------------------------------------------
        # Stage 1: POSITION
        # ---------------------------------------------------------------------
        if self.stage is SortingStage.POSITION:
            self.capture_requested = False
            if self.chamber_to_camera_steps <= 0:
                self.active_prompt = Prompt(
                    id="position_uncalibrated",
                    message="Chamber to camera steps is 0 (uncalibrated). Calibrate in /components or override.",
                    options=["override", "abort"],
                    created_at=now,
                )
                self.conveyor_command = ConveyorCommand.STOP
                return

            if not self._position_move_started:
                # Trigger non-blocking move chamber -> camera
                self._position_move_started = True
                self.conveyor_command = ConveyorCommand.SORT_CREEP
                self.conveyor_move_steps = self.chamber_to_camera_steps
                self.conveyor_move_hz = self.sample_hz
                return

            # Waiting for position move to finish
            if conveyor_busy:
                self.conveyor_command = ConveyorCommand.SORT_CREEP
                self.conveyor_move_steps = 0
                if now - self.stage_entered_at > self.position_timeout_s:
                    self.active_prompt = Prompt(
                        id="positioning_stall",
                        message=f"Conveyor move to camera point timed out after {self.position_timeout_s:.0f}s.",
                        options=["retry", "abort"],
                        created_at=now,
                    )
                    self.alarms.add("conveyor_stall")
                return

            # Move has finished
            if conveyor_last_result in ("stalled", "stopped", "refused"):
                self.active_prompt = Prompt(
                    id="positioning_stall",
                    message=f"Conveyor position move failed ({conveyor_last_result}).",
                    options=["retry", "abort"],
                    created_at=now,
                )
                self.alarms.add("conveyor_stall")
                return

            # Conveyor arrived at camera point. Check copra presence
            if not copra_present:
                self.active_prompt = Prompt(
                    id="empty_belt_detected",
                    message="Camera detected an empty belt at positioning point. Check chamber discharge or continue.",
                    options=["proceed_anyway", "retry_position", "abort"],
                    created_at=now,
                )
                return

            # Copra verified at camera point -> advance to SAMPLE stage
            self.stage = SortingStage.SAMPLE
            self.stage_entered_at = now
            self._sample_move_started = False
            self._waiting_capture = False
            self.conveyor_command = ConveyorCommand.STOP
            self.conveyor_move_steps = 0
            return

        # ---------------------------------------------------------------------
        # Stage 2: SAMPLE
        # ---------------------------------------------------------------------
        elif self.stage is SortingStage.SAMPLE:
            # Check if grade is ready
            if self.sampler.grade_ready:
                self.capture_requested = False
                self.conveyor_command = ConveyorCommand.STOP
                self.conveyor_move_steps = 0
                if self.sampler.needs_review:
                    self.stage = SortingStage.REVIEW
                    self.stage_entered_at = now
                    self.alarms.add("sorting_needs_review")
                    self.active_prompt = Prompt(
                        id="sorting_needs_review",
                        message=f"Vision review required ({self.sampler.review_reason}). Select override grade:",
                        options=["bad", "good", "great", "abort"],
                        created_at=now,
                    )
                else:
                    self.stage = SortingStage.CARRIAGE
                    self.stage_entered_at = now
                return

            # Sub-step A: Not moved yet for this sample point -> trigger jog
            if not self._sample_move_started:
                if self.sampler.total_attempts > 0 and self.sampler.can_jog_forward():
                    steps = self.sampler.step_forward()
                    if steps > 0:
                        self._sample_move_started = True
                        self.conveyor_command = ConveyorCommand.SORT_CREEP
                        self.conveyor_move_steps = steps
                        self.conveyor_move_hz = self.sample_hz
                        self.capture_requested = False
                        return
                else:
                    # Initial sample point (position 0) or retry in-place
                    self._sample_move_started = True
                    self.conveyor_command = ConveyorCommand.STOP
                    self.conveyor_move_steps = 0
                    self._sample_move_completed_at = now
                    self._waiting_capture = False
                    self.capture_requested = False

            # Sub-step B: Jog move in progress -> wait for completion
            if conveyor_busy:
                self.conveyor_command = ConveyorCommand.SORT_CREEP
                self.conveyor_move_steps = 0
                self.capture_requested = False
                return

            # Sub-step C: Move just finished -> start settle timer
            if self._sample_move_completed_at is None:
                self._sample_move_completed_at = now
                self.conveyor_command = ConveyorCommand.STOP
                self.conveyor_move_steps = 0
                self.capture_requested = False
                return

            # Sub-step D: Settle timer countdown (NO time.sleep)
            if (now - self._sample_move_completed_at) < self.settle_s:
                self.conveyor_command = ConveyorCommand.STOP
                self.conveyor_move_steps = 0
                self.capture_requested = False
                return

            # Sub-step E: Settle complete -> request camera capture
            if not self._waiting_capture:
                self._waiting_capture = True
                self.capture_requested = True
                self.conveyor_command = ConveyorCommand.STOP
                self.conveyor_move_steps = 0
                return

            # Sub-step F: Capture received
            if frame_vector is not None:
                # Record frame in sampler
                self.sampler.record_capture(frame_vector)
                self.capture_requested = False
                self._waiting_capture = False
                self._sample_move_started = False
                self._sample_move_completed_at = None
                self.conveyor_command = ConveyorCommand.STOP
                self.conveyor_move_steps = 0

                # Re-check termination
                if self.sampler.grade_ready:
                    if self.sampler.needs_review:
                        self.stage = SortingStage.REVIEW
                        self.stage_entered_at = now
                        self.alarms.add("sorting_needs_review")
                        self.active_prompt = Prompt(
                            id="sorting_needs_review",
                            message=f"Vision review required ({self.sampler.review_reason}). Select override grade:",
                            options=["bad", "good", "great", "abort"],
                            created_at=now,
                        )
                    else:
                        self.stage = SortingStage.CARRIAGE
                        self.stage_entered_at = now
                return

        # ---------------------------------------------------------------------
        # Stage 3: REVIEW
        # ---------------------------------------------------------------------
        elif self.stage is SortingStage.REVIEW:
            self.capture_requested = False
            self.conveyor_command = ConveyorCommand.STOP
            self.conveyor_move_steps = 0
            if effective_grade in ("bad", "good", "great"):
                self.sampler.operator_grade = effective_grade
                self.alarms.discard("sorting_needs_review")
                self.stage = SortingStage.CARRIAGE
                self.stage_entered_at = now
                self.active_prompt = None
            return

        # ---------------------------------------------------------------------
        # Stage 4: CARRIAGE
        # ---------------------------------------------------------------------
        elif self.stage is SortingStage.CARRIAGE:
            self.capture_requested = False
            self.conveyor_command = ConveyorCommand.STOP
            self.conveyor_move_steps = 0

            target = effective_grade or "good"
            self.carriage_target = target

            if self._carriage_move_started_at is None:
                self._carriage_move_started_at = now

            if carriage_state == "at_target" and carriage_current_bin == target:
                # Arrived at destination bin -> prepare discharge
                self.baseline_bin_kg = bin_kg
                self.stage = SortingStage.DISCHARGE
                self.stage_entered_at = now
                self._discharge_move_started = False
                self._discharge_started_at = None
                return

            if carriage_state == "fault" or (now - self._carriage_move_started_at) > self.carriage_move_timeout_s:
                self.alarms.add("carriage_fault")
                self.active_prompt = Prompt(
                    id="carriage_fault",
                    message=f"Carriage reposition to {target} failed or timed out ({carriage_state}).",
                    options=["retry", "abort"],
                    created_at=now,
                )
                return

        # ---------------------------------------------------------------------
        # Stage 5: DISCHARGE
        # ---------------------------------------------------------------------
        elif self.stage is SortingStage.DISCHARGE:
            self.capture_requested = False
            if not self._discharge_move_started:
                self._discharge_move_started = True
                self._discharge_started_at = now
                self.conveyor_command = ConveyorCommand.SORT_CREEP
                self.conveyor_move_steps = self.discharge_total_steps
                self.conveyor_move_hz = self.sample_hz
                return

            if conveyor_busy:
                self.conveyor_command = ConveyorCommand.SORT_CREEP
                self.conveyor_move_steps = 0
                if self._discharge_started_at and (now - self._discharge_started_at) > self.discharge_timeout_s:
                    self.alarms.add("discharge_timeout")
                    self.active_prompt = Prompt(
                        id="discharge_timeout",
                        message=f"Discharge move exceeded timeout ({self.discharge_timeout_s:.0f}s).",
                        options=["retry", "proceed_to_verify", "abort"],
                        created_at=now,
                    )
                return

            # Move finished
            if conveyor_last_result in ("stalled", "stopped", "refused"):
                self.active_prompt = Prompt(
                    id="discharge_timeout",
                    message=f"Discharge conveyor move failed ({conveyor_last_result}).",
                    options=["retry", "proceed_to_verify", "abort"],
                    created_at=now,
                )
                return

            # Discharge move complete -> proceed to verification
            self.stage = SortingStage.VERIFY
            self.stage_entered_at = now
            self._verify_started_at = now
            self.conveyor_command = ConveyorCommand.STOP
            self.conveyor_move_steps = 0
            return

        # ---------------------------------------------------------------------
        # Stage 6: VERIFY
        # ---------------------------------------------------------------------
        elif self.stage is SortingStage.VERIFY:
            self.capture_requested = False
            self.conveyor_command = ConveyorCommand.STOP
            self.conveyor_move_steps = 0

            if self._verify_started_at is None:
                self._verify_started_at = now

            elapsed = now - self._verify_started_at
            # Allow bin load cell to settle
            if elapsed < self.bin_settle_s:
                return

            baseline = self.baseline_bin_kg or 0.0
            current = bin_kg or baseline
            weight_rise = max(0.0, current - baseline)

            if weight_rise >= self.min_weight_rise_kg:
                self.weight_out_kg = weight_rise
                self.stage = SortingStage.DONE
                self.is_complete = True
                return

            # If elapsed past verification timeout without weight rise
            if elapsed >= self.bin_verify_timeout_s:
                self.active_prompt = Prompt(
                    id="bin_did_not_gain_weight",
                    message=(
                        f"Target bin did not gain weight after discharge "
                        f"(baseline: {baseline:.2f} kg, current: {current:.2f} kg, rise: {weight_rise:.2f} kg). "
                        "Verify bin copra drop or choose action:"
                    ),
                    options=["retry_discharge", "override", "abort"],
                    created_at=now,
                    context={"baseline_kg": baseline, "current_kg": current, "rise_kg": weight_rise},
                )
                return

        # ---------------------------------------------------------------------
        # Stage 7: DONE
        # ---------------------------------------------------------------------
        elif self.stage is SortingStage.DONE:
            self.is_complete = True
            self.conveyor_command = ConveyorCommand.STOP
            self.conveyor_move_steps = 0
            self.capture_requested = False
