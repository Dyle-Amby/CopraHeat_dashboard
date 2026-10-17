"""Batch-level copra grading and ensemble sampling logic.

Contains the SortingSampler, ensemble mathematics (soft-voting, visual contamination guard,
and tie-breaking), configuration validation, frame filtering, and belt jog accounting.
Pure logic, no direct GPIO or hardware dependencies.
"""

from dataclasses import dataclass, field
import json
import math
from typing import Any, Sequence

# Class order matches labels.txt lines 1-3
LABEL_ORDER: tuple[str, str, str] = ("bad", "good", "great")

# Unmeasured placeholder configuration defaults (tune on fabricated physical machine)
DEFAULT_GRADING_CONFIG: dict[str, Any] = {
    "sample_mode": "jog",              # "jog" | "static" (static is dev-only)
    "n_samples": 20,                   # Target valid distinct-position frames
    "min_valid": 10,                   # Minimum valid frames to permit auto-decision
    "max_attempts": 40,                # Total camera capture attempts before review
    "retry_max": 3,                    # Retries at the exact same position before advancing
    "steps_per_sample": 200,           # Conveyor steps between sample points
    "sample_hz": 200.0,                # Step rate for inter-sample jog
    "settle_s": 0.3,                   # Settle delay after jog before image capture
    "tau_bad": 0.20,                   # Visual contamination guard threshold (e.g. mould/char)
    "min_confidence": 0.55,            # Per-frame minimum top confidence
    "blur_threshold": 100.0,           # Minimum Laplacian variance (OpenCV)
    "max_rejected_ratio": 0.40,        # Max ratio of rejected frames before needs_review
    "camera_to_discharge_steps": 5000, # Steps from camera FOV center to discharge edge
    "discharge_total_steps": 3000,     # Steps to discharge batch past roller
    "safety_margin_steps": 500,        # Safety buffer before discharge edge
    "discharge_timeout_s": 60.0,       # Timeout for automated discharge step
    "carriage_move_timeout_s": 30.0,   # Timeout for carriage repositioning
}


def validate_grading_config(cfg: dict[str, Any]) -> None:
    """Startup validation for grading parameters.

    Fails if N_SAMPLES * STEPS_PER_SAMPLE exceeds allowable travel before discharge,
    or if MIN_VALID is unreachable within MAX_ATTEMPTS.
    """
    mode = cfg.get("sample_mode", "jog")
    if mode not in ("jog", "static"):
        raise ValueError(f"sample_mode must be 'jog' or 'static', got {mode!r}")

    n_samples = int(cfg["n_samples"])
    min_valid = int(cfg["min_valid"])
    max_attempts = int(cfg["max_attempts"])
    steps_per_sample = int(cfg["steps_per_sample"])
    camera_to_discharge = int(cfg["camera_to_discharge_steps"])
    safety_margin = int(cfg["safety_margin_steps"])

    if n_samples <= 0 or min_valid <= 0 or max_attempts <= 0:
        raise ValueError("n_samples, min_valid, and max_attempts must be positive integers")

    if min_valid > n_samples:
        raise ValueError(f"min_valid ({min_valid}) cannot exceed n_samples ({n_samples})")

    if min_valid > max_attempts:
        raise ValueError(
            f"min_valid ({min_valid}) is unreachable with max_attempts ({max_attempts})"
        )

    if mode == "jog":
        max_allowable_jog = camera_to_discharge - safety_margin
        if max_allowable_jog <= 0:
            raise ValueError(
                f"camera_to_discharge_steps ({camera_to_discharge}) must exceed "
                f"safety_margin_steps ({safety_margin})"
            )
        # To sample N distinct positions, we jog N-1 times (or up to N times)
        needed_jog = n_samples * steps_per_sample
        if needed_jog > max_allowable_jog:
            raise ValueError(
                f"Sampling jog travel ({needed_jog} steps = {n_samples} * {steps_per_sample}) "
                f"exceeds allowable travel to discharge edge ({max_allowable_jog} steps = "
                f"{camera_to_discharge} - {safety_margin}). Adjust N_SAMPLES or STEPS_PER_SAMPLE."
            )


def break_tie(mean_scores: dict[str, float]) -> str:
    """Deterministic tie-breaker prioritizing safety: bad > good > great."""
    max_score = max(mean_scores.values())
    tied = [label for label in ("bad", "good", "great") if math.isclose(mean_scores[label], max_score, rel_tol=1e-7, abs_tol=1e-7)]
    return tied[0]


def decide_grade(vectors: Sequence[Sequence[float]], tau_bad: float) -> tuple[str | None, bool]:
    """Run ensemble decision math over valid softmax vectors.

    Returns:
        (batch_grade, is_visual_contamination)
        Note: If vectors is empty, returns (None, False).
    """
    if not vectors:
        return None, False

    m = len(vectors)
    # Check visual contamination guard (proportion classified as 'bad' (idx 0))
    bad_count = sum(1 for v in vectors if int(max(range(len(v)), key=lambda idx: v[idx])) == 0)
    bad_fraction = bad_count / m
    if bad_fraction >= tau_bad:
        return "bad", True

    # Soft-voting consensus
    sums = {label: 0.0 for label in LABEL_ORDER}
    for v in vectors:
        for idx, label in enumerate(LABEL_ORDER):
            sums[label] += float(v[idx])

    means = {label: sums[label] / m for label in LABEL_ORDER}
    grade = break_tie(means)
    return grade, False


@dataclass
class SortingSampler:
    """Stateful sampler orchestrating multi-position image capture during SORTING.

    Enforces distinct-position counting, in-place retries, software jog caps,
    and fallback to needs_review.
    """

    config: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_GRADING_CONFIG))

    def __post_init__(self):
        validate_grading_config(self.config)
        self.sample_mode: str = str(self.config.get("sample_mode", "jog"))
        self.n_samples: int = int(self.config["n_samples"])
        self.min_valid: int = int(self.config["min_valid"])
        self.max_attempts: int = int(self.config["max_attempts"])
        self.retry_max: int = int(self.config["retry_max"])
        self.steps_per_sample: int = int(self.config["steps_per_sample"])
        self.sample_hz: float = float(self.config["sample_hz"])
        self.settle_s: float = float(self.config["settle_s"])
        self.tau_bad: float = float(self.config["tau_bad"])
        self.min_confidence: float = float(self.config["min_confidence"])
        self.blur_threshold: float = float(self.config["blur_threshold"])
        self.max_rejected_ratio: float = float(self.config["max_rejected_ratio"])
        self.camera_to_discharge_steps: int = int(self.config["camera_to_discharge_steps"])
        self.safety_margin_steps: int = int(self.config["safety_margin_steps"])

        self.max_jog_cap: int = self.camera_to_discharge_steps - self.safety_margin_steps
        self.current_position_steps: int = 0
        self.position_index: int = 0
        self.position_attempts: int = 0
        self.total_attempts: int = 0
        self.total_rejected: int = 0

        self.valid_vectors: list[list[float]] = []
        self._sampled_positions: set[int] = set()

        self.grade_ready: bool = False
        self.batch_grade: str | None = None
        self.needs_review: bool = False
        self.review_reason: str | None = None
        self.contamination_guard_triggered: bool = False

    @property
    def sample_valid_n(self) -> int:
        return len(self.valid_vectors)

    def can_jog_forward(self) -> bool:
        if self.sample_mode == "static":
            return False
        return (self.current_position_steps + self.steps_per_sample) <= self.max_jog_cap

    def step_forward(self) -> int:
        """Advance belt to the next distinct position if permitted by software cap."""
        if not self.can_jog_forward():
            return 0
        self.current_position_steps += self.steps_per_sample
        self.position_index += 1
        self.position_attempts = 0
        return self.steps_per_sample

    def record_capture(self, vector: Sequence[float] | None) -> None:
        """Process one capture attempt at the current belt position.

        If vector is None (blurred or low confidence):
          - Incurs an attempt.
          - If position_attempts < retry_max, stays at current position for retry.
        If vector is valid:
          - In jog mode: Counts toward sample_valid_n ONLY if position hasn't contributed yet.
          - In static mode: Only the first valid capture counts toward distinct sample_valid_n.
        """
        if self.grade_ready:
            return

        self.total_attempts += 1
        self.position_attempts += 1

        if vector is None:
            self.total_rejected += 1
        else:
            vec = [float(x) for x in vector]
            if self.sample_mode == "static":
                # Static mode: distinct-position rule strictly means position 0 can only contribute 1 valid sample
                if 0 not in self._sampled_positions:
                    self.valid_vectors.append(vec)
                    self._sampled_positions.add(0)
            else:
                # Jog mode: position_index represents distinct location
                if self.position_index not in self._sampled_positions:
                    self.valid_vectors.append(vec)
                    self._sampled_positions.add(self.position_index)

        self._check_termination()

    def _check_termination(self) -> None:
        # Check target met
        if len(self.valid_vectors) >= self.n_samples:
            self._finalize_decision()
            return

        # Check excessive rejection ratio
        if self.total_attempts >= 5:
            rejected_ratio = self.total_rejected / self.total_attempts
            if rejected_ratio > self.max_rejected_ratio:
                self.needs_review = True
                self.review_reason = f"Rejection ratio {rejected_ratio:.2f} exceeded max {self.max_rejected_ratio:.2f}"
                self._finalize_decision()
                return

        # Check total attempts limit
        if self.total_attempts >= self.max_attempts:
            self.needs_review = True
            self.review_reason = f"Max attempts {self.max_attempts} reached with {len(self.valid_vectors)} valid samples"
            self._finalize_decision()
            return

        # Check if reached travel limit and cannot jog further
        if not self.can_jog_forward() and self.position_attempts >= self.retry_max:
            if len(self.valid_vectors) < self.min_valid:
                self.needs_review = True
                self.review_reason = f"Discharge edge cap reached with only {len(self.valid_vectors)} valid samples"
            self._finalize_decision()

    def _finalize_decision(self) -> None:
        self.grade_ready = True
        if len(self.valid_vectors) < self.min_valid:
            self.needs_review = True
            if not self.review_reason:
                self.review_reason = f"Valid samples ({len(self.valid_vectors)}) below minimum ({self.min_valid})"

        if len(self.valid_vectors) == 0:
            self.batch_grade = None
            self.needs_review = True
            self.review_reason = "Zero valid samples captured"
            return

        grade, guard = decide_grade(self.valid_vectors, self.tau_bad)
        self.batch_grade = grade
        self.contamination_guard_triggered = guard

    def snapshot_config(self) -> str:
        """Return JSON string snapshot of configuration at evaluation time."""
        snap = dict(self.config)
        snap["is_static_dev_mode"] = (self.sample_mode == "static")
        return json.dumps(snap, sort_keys=True)
