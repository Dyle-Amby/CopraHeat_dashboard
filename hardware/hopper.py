"""Hopper gate control logic. No GPIO here: feed weights in, get the gate command out."""

from collections import deque
from enum import Enum


class HopperState(Enum):
    CLOSED_WAITING = "closed_waiting"
    OPEN_DISPENSING = "open_dispensing"


class StableWeight:
    """Reports a weight only once the last N consecutive readings agree within a tolerance."""

    def __init__(self, samples: int = 5, tolerance_kg: float = 0.03):
        self.samples = samples
        self.tolerance_kg = tolerance_kg
        self._window: deque[float] = deque(maxlen=samples)

    def update(self, weight_kg: float | None) -> float | None:
        if weight_kg is None:
            self._window.clear()
            return None
        self._window.append(weight_kg)
        if len(self._window) < self.samples:
            return None
        if max(self._window) - min(self._window) > self.tolerance_kg:
            return None
        return sum(self._window) / self.samples


class HopperGate:
    """CLOSED_WAITING -> opens when the stable weight reaches target.
    OPEN_DISPENSING -> closes when the stable weight drops to empty.

    A gate open longer than jam_after_s without emptying raises `jammed` but
    stays open: closing on a jam could trap material in the gate. The stability
    requirement also means the gate never closes on a transient dip mid-flow.
    """

    def __init__(
        self,
        target_kg: float,
        empty_kg: float = 0.05,
        jam_after_s: float = 30.0,
        stable_samples: int = 5,
        stable_tolerance_kg: float = 0.03,
    ):
        self.empty_kg = empty_kg
        self.jam_after_s = jam_after_s
        self.target_kg = target_kg
        self.state = HopperState.CLOSED_WAITING
        self.stable_kg: float | None = None
        self.jammed = False
        self.opened_at: float | None = None
        self._stable = StableWeight(stable_samples, stable_tolerance_kg)

    @property
    def target_kg(self) -> float:
        return self._target_kg

    @target_kg.setter
    def target_kg(self, value: float) -> None:
        # dashboard-settable, so validate here: a target at/below empty would oscillate the gate
        if value <= self.empty_kg:
            raise ValueError(f"target ({value} kg) must be above empty threshold ({self.empty_kg} kg)")
        self._target_kg = value

    @property
    def gate_open(self) -> bool:
        return self.state is HopperState.OPEN_DISPENSING

    def update(self, weight_kg: float | None, now: float) -> bool:
        self.stable_kg = self._stable.update(weight_kg)

        if self.state is HopperState.CLOSED_WAITING:
            if self.stable_kg is not None and self.stable_kg >= self.target_kg:
                self.state = HopperState.OPEN_DISPENSING
                self.opened_at = now
        elif self.stable_kg is not None and self.stable_kg <= self.empty_kg:
            self.state = HopperState.CLOSED_WAITING
            self.opened_at = None
            self.jammed = False
        elif now - self.opened_at >= self.jam_after_s:
            self.jammed = True

        return self.gate_open
