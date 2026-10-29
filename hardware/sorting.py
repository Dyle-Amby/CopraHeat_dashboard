"""Operator-led per-piece sorting, carriage dispatch, and discharge verification.

Pure logic, GPIO-free, fully unit-testable.
Stages:
    POSITION    -> move the batch to camera point once, then stop
    STAGE_PIECE -> wait for operator to isolate one piece; allow bounded jogs
    SAMPLE      -> capture one image and wait for operator review
    REVIEW      -> accept suggested bin, choose another, or retake in place
    CARRIAGE  -> goto target bin, wait at_target, prompt on timeout or fault
    DISCHARGE -> move camera_to_discharge_steps for this piece
    VERIFY    -> wait 10 s, then verify stable bin weight rise
    MORE_PIECES -> ask operator whether to stage another piece or finish
    DONE      -> operator finished sorting
"""

from dataclasses import dataclass, field
from enum import Enum
import math
from typing import Any, Sequence

from hardware.states import ConveyorCommand
from hardware.grading import DEFAULT_GRADING_CONFIG, SortingSampler


class SortingStage(str, Enum):
    POSITION = "position"
    STAGE_PIECE = "stage_piece"
    STAGE_JOG = "stage_jog"
    SAMPLE = "sample"
    REVIEW = "review"
    CARRIAGE = "carriage"
    DISCHARGE = "discharge"
    VERIFY = "verify"
    MORE_PIECES = "more_pieces"
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
    chamber_to_camera_steps: int = 0
    camera_to_discharge_steps: int = 0
    camera_timeout_s: float = 10.0
    sample_hz: float = 400.0
    conveyor_hz: float = 400.0
    settle_s: float = 0.3
    bin_settle_s: float = 10.0
    bin_verify_timeout_s: float = 30.0
    min_weight_rise_kg: float = 0.05
    carriage_move_timeout_s: float = 30.0
    discharge_timeout_s: float = 60.0
    position_timeout_s: float = 45.0

    def __post_init__(self):
        # Override config attributes if explicitly supplied
        cfg = dict(DEFAULT_GRADING_CONFIG)
        cfg.update(self.config)
        cfg["sample_hz"] = self.sample_hz
        cfg["settle_s"] = self.settle_s
        cfg["carriage_move_timeout_s"] = self.carriage_move_timeout_s
        cfg["discharge_timeout_s"] = self.discharge_timeout_s
        self.sampler = SortingSampler(config=cfg)

        self.stage: SortingStage = SortingStage.POSITION
        self.stage_entered_at: float = 0.0
        self.active_prompt: Prompt | None = None
        self.alarms: set[str] = set()
        self.piece_number = 1
        self.piece_attempt = 0
        self.piece_grade: str | None = None
        self.piece_prediction: dict[str, Any] | None = None
        self.piece_events: list[tuple[str, dict[str, Any]]] = []
        self._stage_jog_started = False
        self._stage_jog_started_at: float | None = None
        self._stage_jog_forward = True

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
        self._verify_readings: list[float] = []
        self._capture_requested_at: float | None = None
        self._position_move_started_at: float | None = None
        self._sample_move_started_at: float | None = None
        self._discharge_move_started_at: float | None = None

        # Conveyor command output
        self.conveyor_command: ConveyorCommand = ConveyorCommand.STOP
        self.conveyor_move_steps: int = 0
        self.conveyor_move_hz: float = 400.0
        self.conveyor_move_forward: bool = True
        self.is_complete: bool = False

    def start(self, now: float) -> None:
        """Start or restart the sorting flow."""
        # Build fresh sampler with existing config
        self.sampler = SortingSampler(config=self.sampler.config)
        self.stage = SortingStage.POSITION
        self.stage_entered_at = now
        self.active_prompt = None
        self.alarms.clear()
        self.piece_number = 1
        self.piece_attempt = 0
        self.piece_grade = None
        self.piece_prediction = None
        self.piece_events.clear()
        self._stage_jog_started = False
        self._stage_jog_started_at = None
        self._stage_jog_forward = True
        self._position_move_started = False
        self._position_move_done = False
        self._position_move_started_at = None
        self._sample_move_started = False
        self._sample_move_started_at = None
        self._sample_move_completed_at = None
        self._waiting_capture = False
        self.capture_requested = False
        self._capture_requested_at = None
        self._carriage_move_started_at = None
        self.carriage_target = None
        self._discharge_move_started = False
        self._discharge_move_started_at = None
        self._discharge_started_at = None
        self._discharge_done = False
        self.baseline_bin_kg = None
        self.weight_out_kg = None
        self._verify_started_at = None
        self._verify_readings = []
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
        """Handle operator response to a blocking prompt. Only clears active_prompt if action is valid."""
        if not self.active_prompt:
            return False

        prompt_id = self.active_prompt.id

        if prompt_id.startswith("stage_piece_"):
            if action == "piece_staged":
                self.active_prompt = None
                self.stage = SortingStage.SAMPLE
                self.stage_entered_at = now
                self._waiting_capture = False
                self.capture_requested = False
                self._capture_requested_at = None
                self._sample_move_completed_at = now
                return True
            if action in ("jog_forward", "jog_reverse"):
                self.active_prompt = None
                self.stage = SortingStage.STAGE_JOG
                self.stage_entered_at = now
                self._stage_jog_started = False
                self._stage_jog_started_at = None
                self._stage_jog_forward = action == "jog_forward"
                return True
            if action == "abort":
                self.active_prompt = None
                self.alarms.add("aborted")
                return True

        if prompt_id.startswith("piece_review_"):
            if action == "retake":
                self.active_prompt = None
                self.stage = SortingStage.SAMPLE
                self.stage_entered_at = now
                self._waiting_capture = False
                self.capture_requested = False
                self._capture_requested_at = None
                self.piece_prediction = None
                self._sample_move_completed_at = now
                return True
            if action == "accept_suggested":
                if not self.piece_prediction or self.piece_prediction.get("grade") not in ("bad", "good", "great"):
                    return False
                selected_grade = self.piece_prediction["grade"]
            elif action in ("bad", "good", "great"):
                selected_grade = action
            elif action == "abort":
                self.active_prompt = None
                self.alarms.add("aborted")
                return True
            else:
                return False

            self.piece_grade = selected_grade
            self.piece_events.append(("piece_reviewed", {
                "piece": self.piece_number,
                "attempt": self.piece_attempt,
                "predicted_grade": self.piece_prediction.get("grade") if self.piece_prediction else None,
                "selected_grade": selected_grade,
                "confidence": self.piece_prediction.get("confidence") if self.piece_prediction else None,
                "scores": self.piece_prediction.get("scores") if self.piece_prediction else None,
                "image_url": self.piece_prediction.get("image_url") if self.piece_prediction else None,
            }))
            self.active_prompt = None
            self.stage = SortingStage.CARRIAGE
            self.stage_entered_at = now
            self._carriage_move_started_at = None
            return True

        if prompt_id.startswith("more_pieces_"):
            if action in ("yes_more", "more"):
                self.piece_number += 1
                self.piece_attempt = 0
                self.piece_grade = None
                self.piece_prediction = None
                self.active_prompt = None
                self.stage = SortingStage.STAGE_PIECE
                self.stage_entered_at = now
                self.baseline_bin_kg = None
                self._verify_started_at = None
                self._verify_readings = []
                self._carriage_move_started_at = None
                self.carriage_target = None
                self._discharge_move_started = False
                self._discharge_move_started_at = None
                self._discharge_started_at = None
                self._stage_piece_prompt(now)
                return True
            if action in ("no_more", "done"):
                self.active_prompt = None
                self.stage = SortingStage.DONE
                self.is_complete = True
                self.piece_events.append(("piece_sorting_finished", {"pieces": self.piece_number}))
                return True
            if action == "abort":
                self.active_prompt = None
                self.alarms.add("aborted")
                return True

        if prompt_id.startswith("stage_jog_failed_"):
            if action == "retry":
                self.active_prompt = None
                self.stage = SortingStage.STAGE_JOG
                self._stage_jog_started = False
                self._stage_jog_started_at = None
                return True
            if action == "abort":
                self.active_prompt = None
                self.alarms.add("aborted")
                return True

        if prompt_id == "position_uncalibrated":
            if action == "override":
                self.active_prompt = None
                self.stage = SortingStage.STAGE_PIECE
                self.stage_entered_at = now
                self._stage_piece_prompt(now)
                return True
            if action == "abort":
                self.active_prompt = None
                self.alarms.add("aborted")
                return True
        elif prompt_id == "discharge_uncalibrated":
            if action == "override":
                self.active_prompt = None
                self.piece_events.append(("piece_drop_unverified", {"piece": self.piece_number}))
                self._more_pieces_prompt(now)
                return True
            if action == "abort":
                self.active_prompt = None
                self.alarms.add("aborted")
                return True
        elif prompt_id == "positioning_stall":
            if action == "retry":
                self.active_prompt = None
                self._position_move_started = False
                self._position_move_started_at = None
                self.stage_entered_at = now
                return True
            if action == "abort":
                self.active_prompt = None
                self.alarms.add("aborted")
                return True
        elif prompt_id == "empty_belt_detected":
            if action in ("proceed_anyway", "proceed"):
                self.active_prompt = None
                self.stage = SortingStage.STAGE_PIECE
                self.stage_entered_at = now
                self._stage_piece_prompt(now)
                return True
            if action == "retry_position":
                self.active_prompt = None
                self._position_move_started = False
                self._position_move_started_at = None
                self.stage_entered_at = now
                return True
            if action == "abort":
                self.active_prompt = None
                self.alarms.add("aborted")
                return True
        elif prompt_id == "carriage_fault":
            if action == "retry":
                self.active_prompt = None
                self._carriage_move_started_at = None
                return True
            if action == "abort":
                self.active_prompt = None
                self.alarms.add("aborted")
                return True
        elif prompt_id == "discharge_timeout":
            if action == "retry":
                self.active_prompt = None
                self._discharge_move_started = False
                self._discharge_move_started_at = None
                self.stage_entered_at = now
                return True
            if action == "proceed_to_verify":
                self.active_prompt = None
                self.stage = SortingStage.VERIFY
                self.stage_entered_at = now
                self._verify_started_at = now
                return True
            if action == "abort":
                self.active_prompt = None
                self.alarms.add("aborted")
                return True
        elif prompt_id == "bin_did_not_gain_weight":
            if action == "override":
                self.active_prompt = None
                self.piece_events.append(("piece_drop_unverified", {"piece": self.piece_number}))
                self._more_pieces_prompt(now)
                return True
            if action == "retry_discharge":
                self.active_prompt = None
                self.stage = SortingStage.DISCHARGE
                self.stage_entered_at = now
                self._discharge_move_started = False
                self._discharge_move_started_at = None
                self._discharge_started_at = None
                self._verify_readings = []
                return True
            if action == "abort":
                self.active_prompt = None
                self.alarms.add("aborted")
                return True

        return False

    def _stage_piece_prompt(self, now: float) -> None:
        self.active_prompt = Prompt(
            id=f"stage_piece_{self.piece_number}",
            message=f"Stage piece {self.piece_number}: clear the camera view so only one piece is at the camera point, then confirm.",
            options=["jog_reverse", "jog_forward", "piece_staged", "abort"],
            created_at=now,
            context={"piece": self.piece_number, "jog_step_limit": 250},
        )

    def _more_pieces_prompt(self, now: float) -> None:
        self.stage = SortingStage.MORE_PIECES
        self.stage_entered_at = now
        self.active_prompt = Prompt(
            id=f"more_pieces_{self.piece_number}",
            message="The piece has been dropped and the bin scale has settled. Is there more copra to sort?",
            options=["yes_more", "no_more"],
            created_at=now,
            context={"piece": self.piece_number},
        )

    def drain_piece_events(self) -> list[tuple[str, dict[str, Any]]]:
        events, self.piece_events = self.piece_events, []
        return events

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
        capture_result: dict[str, Any] | None = None,
        capture_done: bool = False,
    ) -> None:
        """Advance sorting state machine for one supervisor tick."""
        if self.is_complete:
            self.conveyor_command = ConveyorCommand.STOP
            self.conveyor_move_steps = 0
            self.capture_requested = False
            return

        effective_grade = operator_grade or getattr(self.sampler, "operator_grade", None) or self.sampler.batch_grade

        # Item 7: Control Panel override_grade releases sorting_needs_review
        if self.active_prompt and self.active_prompt.id == "sorting_needs_review":
            if operator_grade in ("bad", "good", "great"):
                self.sampler.operator_grade = operator_grade
                self.alarms.discard("sorting_needs_review")
                self.stage = SortingStage.CARRIAGE
                self.stage_entered_at = now
                self.active_prompt = None

        # If a blocking prompt is waiting for operator, hold machine safely
        if self.active_prompt is not None:
            self.conveyor_command = ConveyorCommand.STOP
            self.conveyor_move_steps = 0
            self.capture_requested = False
            return

        if self.stage is SortingStage.STAGE_PIECE:
            if self.active_prompt is None:
                self._stage_piece_prompt(now)
            self.conveyor_command = ConveyorCommand.STOP
            self.conveyor_move_steps = 0
            self.capture_requested = False
            return

        if self.stage is SortingStage.STAGE_JOG:
            if not self._stage_jog_started:
                self._stage_jog_started = True
                self._stage_jog_started_at = now
                self.conveyor_command = ConveyorCommand.SORT_CREEP
                self.conveyor_move_steps = 250
                self.conveyor_move_hz = self.conveyor_hz
                self.conveyor_move_forward = self._stage_jog_forward
                return
            jog_age = now - (self._stage_jog_started_at or now)
            if conveyor_busy or jog_age < 0.15:
                self.conveyor_command = ConveyorCommand.SORT_CREEP
                self.conveyor_move_steps = 0
                return
            if conveyor_last_result != "done":
                self.active_prompt = Prompt(
                    id=f"stage_jog_failed_{self.piece_number}",
                    message=f"Staging jog failed ({conveyor_last_result}). Retry or stop staging.",
                    options=["retry", "abort"],
                    created_at=now,
                    context={"piece": self.piece_number},
                )
                self.conveyor_command = ConveyorCommand.STOP
                self.conveyor_move_steps = 0
                return
            self.stage = SortingStage.STAGE_PIECE
            self.stage_entered_at = now
            self._stage_jog_started = False
            self._stage_jog_started_at = None
            self.conveyor_move_forward = True
            self._stage_piece_prompt(now)
            self.conveyor_command = ConveyorCommand.STOP
            self.conveyor_move_steps = 0
            return

        if self.stage is SortingStage.SAMPLE:
            self.conveyor_command = ConveyorCommand.STOP
            self.conveyor_move_steps = 0
            self.conveyor_move_forward = True
            if self._sample_move_completed_at is not None and now - self._sample_move_completed_at < self.settle_s:
                self.capture_requested = False
                return
            if not self._waiting_capture:
                self._waiting_capture = True
                self._capture_requested_at = now
                self.capture_requested = True
                return
            if capture_done or capture_result is not None or frame_vector is not None:
                prediction = dict(capture_result or {})
                if frame_vector is not None and not prediction.get("scores"):
                    scores = dict(zip(("bad", "good", "great"), frame_vector))
                    grade = max(scores, key=scores.get)
                    prediction.update(grade=grade, confidence=scores[grade], scores=scores)
                self.piece_prediction = prediction
                self.piece_attempt += 1
                options = ["accept_suggested", "bad", "good", "great", "retake", "abort"]
                if not prediction.get("grade"):
                    options.remove("accept_suggested")
                image_url = prediction.get("image_url")
                self.active_prompt = Prompt(
                    id=f"piece_review_{self.piece_number}_{self.piece_attempt}",
                    message=f"Review piece {self.piece_number}: accept the suggested bin, choose another, or retake the photo.",
                    options=options,
                    created_at=now,
                    context={"piece": self.piece_number, **prediction, "image_url": image_url},
                )
                self.stage = SortingStage.REVIEW
                self.stage_entered_at = now
                self.capture_requested = False
                self._waiting_capture = False
                self._capture_requested_at = None
                self.piece_events.append(("piece_captured", {
                    "piece": self.piece_number,
                    "attempt": self.piece_attempt,
                    "predicted_grade": prediction.get("grade"),
                    "confidence": prediction.get("confidence"),
                    "scores": prediction.get("scores"),
                    "image_url": image_url,
                }))
                return
            if self._capture_requested_at is not None and now - self._capture_requested_at > self.camera_timeout_s:
                self.active_prompt = Prompt(
                    id=f"piece_camera_timeout_{self.piece_number}",
                    message="Camera capture failed. Check the camera and retry this piece.",
                    options=["retry_capture", "abort"],
                    created_at=now,
                    context={"piece": self.piece_number},
                )
                self.alarms.add("camera_timeout")
                self.capture_requested = False
            return

        if self.stage is SortingStage.REVIEW:
            self.conveyor_command = ConveyorCommand.STOP
            self.conveyor_move_steps = 0
            self.capture_requested = False
            return

        if self.stage is SortingStage.MORE_PIECES:
            if self.active_prompt is None:
                self.active_prompt = Prompt(
                    id=f"more_pieces_{self.piece_number}",
                    message="The piece has been dropped and the bin scale has settled. Is there more copra to sort?",
                    options=["yes_more", "no_more"],
                    created_at=now,
                    context={"piece": self.piece_number},
                )
            self.conveyor_command = ConveyorCommand.STOP
            self.conveyor_move_steps = 0
            self.capture_requested = False
            return

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
                self._position_move_started_at = now
                self.conveyor_command = ConveyorCommand.SORT_CREEP
                self.conveyor_move_steps = self.chamber_to_camera_steps
                self.conveyor_move_hz = self.conveyor_hz
                return

            # Wait short start grace (0.15s) so tx_busy lag doesn't treat move as immediately done
            move_age = now - (self._position_move_started_at or now)
            if conveyor_busy or move_age < 0.15:
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

            # Move has finished - require conveyor_last_result == "done"
            if conveyor_last_result != "done":
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

            # Stop at the camera point and let the operator isolate one piece.
            self.stage = SortingStage.STAGE_PIECE
            self.stage_entered_at = now
            self._stage_piece_prompt(now)
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
                        self._sample_move_started_at = now
                        self.conveyor_command = ConveyorCommand.SORT_CREEP
                        self.conveyor_move_steps = steps
                        self.conveyor_move_hz = self.sample_hz
                        self.capture_requested = False
                        return
                else:
                    # Initial sample point (position 0) or retry in-place
                    self._sample_move_started = True
                    self._sample_move_started_at = None
                    self.conveyor_command = ConveyorCommand.STOP
                    self.conveyor_move_steps = 0
                    self._sample_move_completed_at = now
                    self._waiting_capture = False
                    self.capture_requested = False

            # Sub-step B: Jog move in progress -> wait for completion with start grace
            jog_age = now - (self._sample_move_started_at or now)
            if self._sample_move_started_at is not None and (conveyor_busy or jog_age < 0.15):
                self.conveyor_command = ConveyorCommand.SORT_CREEP
                self.conveyor_move_steps = 0
                self.capture_requested = False
                return

            if self._sample_move_started_at is not None and conveyor_last_result != "done":
                self.active_prompt = Prompt(
                    id="positioning_stall",
                    message=f"Conveyor jog failed ({conveyor_last_result}).",
                    options=["retry", "abort"],
                    created_at=now,
                )
                self.alarms.add("conveyor_stall")
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
                self._capture_requested_at = now
                self.capture_requested = True
                self.conveyor_command = ConveyorCommand.STOP
                self.conveyor_move_steps = 0
                return

            # Camera timeout check
            if self._waiting_capture and self._capture_requested_at is not None:
                if (now - self._capture_requested_at) > self.camera_timeout_s:
                    self.active_prompt = Prompt(
                        id="camera_timeout",
                        message=f"Camera capture timed out after {self.camera_timeout_s:.0f}s. Check USB webcam or retry.",
                        options=["retry_capture", "skip_sample", "abort"],
                        created_at=now,
                    )
                    self.alarms.add("camera_timeout")
                    return

            # Sub-step F: Capture received (Item 4: frame_vector may be None if rejected, keyed by capture_done)
            if capture_done or frame_vector is not None:
                # Record frame in sampler (even None for rejected frame)
                self.sampler.record_capture(frame_vector)
                self.capture_requested = False
                self._waiting_capture = False
                self._capture_requested_at = None
                self._sample_move_started = False
                self._sample_move_started_at = None
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

            target = self.piece_grade or effective_grade or "good"
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
            if self.camera_to_discharge_steps <= 0:
                self.active_prompt = Prompt(
                    id="discharge_uncalibrated",
                    message="Camera-to-drop distance is 0 (uncalibrated). Calibrate in /components or override.",
                    options=["override", "abort"],
                    created_at=now,
                )
                self.conveyor_command = ConveyorCommand.STOP
                return

            if not self._discharge_move_started:
                self._discharge_move_started = True
                self._discharge_started_at = now
                self._discharge_move_started_at = now
                self.conveyor_command = ConveyorCommand.SORT_CREEP
                self.conveyor_move_steps = self.camera_to_discharge_steps
                self.conveyor_move_hz = self.conveyor_hz
                return

            discharge_age = now - (self._discharge_move_started_at or now)
            if conveyor_busy or discharge_age < 0.15:
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

            # Move finished - require conveyor_last_result == "done"
            if conveyor_last_result != "done":
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
                self._verify_readings = []

            elapsed = now - self._verify_started_at
            # Allow bin load cell to settle
            if elapsed < self.bin_settle_s:
                return

            # Item 8: A None baseline must not count as zero; dead cell raises bin_scale_fault
            if self.baseline_bin_kg is None:
                if bin_kg is not None:
                    self.baseline_bin_kg = bin_kg
                else:
                    self.alarms.add("bin_scale_fault")
                    if elapsed >= self.bin_verify_timeout_s:
                        self.active_prompt = Prompt(
                            id="bin_did_not_gain_weight",
                            message="Bin scale not responding (no readings). Verify scale or choose action:",
                            options=["retry_discharge", "override", "abort"],
                            created_at=now,
                        )
                    return

            if bin_kg is None:
                self.alarms.add("bin_scale_fault")
                if elapsed >= self.bin_verify_timeout_s:
                    self.active_prompt = Prompt(
                        id="bin_did_not_gain_weight",
                        message="Bin scale not responding during verification. Choose action:",
                        options=["retry_discharge", "override", "abort"],
                        created_at=now,
                    )
                return

            self._verify_readings.append(bin_kg)
            if len(self._verify_readings) > 10:
                self._verify_readings.pop(0)

            # Check stable rise: 5 readings within 0.03 kg
            if len(self._verify_readings) >= 5:
                window = self._verify_readings[-5:]
                spread = max(window) - min(window)
                if spread <= 0.03:
                    stable_val = sum(window) / len(window)
                    weight_rise = max(0.0, stable_val - self.baseline_bin_kg)
                    if weight_rise >= self.min_weight_rise_kg:
                        self.weight_out_kg = (self.weight_out_kg or 0.0) + weight_rise
                        self.piece_events.append(("piece_drop_verified", {
                            "piece": self.piece_number,
                            "grade": self.piece_grade,
                            "weight_kg": weight_rise,
                        }))
                        self._more_pieces_prompt(now)
                        return

            # If elapsed past verification timeout without stable weight rise
            if elapsed >= self.bin_verify_timeout_s:
                current = bin_kg
                weight_rise = max(0.0, current - self.baseline_bin_kg)
                self.active_prompt = Prompt(
                    id="bin_did_not_gain_weight",
                    message=(
                        f"Target bin did not gain weight after discharge "
                        f"(baseline: {self.baseline_bin_kg:.2f} kg, current: {current:.2f} kg, rise: {weight_rise:.2f} kg). "
                        "Verify bin copra drop or choose action:"
                    ),
                    options=["retry_discharge", "override", "abort"],
                    created_at=now,
                    context={"baseline_kg": self.baseline_bin_kg, "current_kg": current, "rise_kg": weight_rise},
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
