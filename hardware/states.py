"""Shared vocabulary between the batch state machine and the I/O layer.

Lives in its own module so the pure controllers (climate, hopper, drying) and
the orchestrator that drives them can both name these without importing each
other.
"""

from enum import Enum


class BatchState(Enum):
    IDLE = "idle"
    INTAKE = "intake"
    DRYING = "drying"
    COOLDOWN = "cooldown"
    SORTING = "sorting"
    COMPLETE = "complete"
    ABORTED = "aborted"


class ConveyorCommand(Enum):
    """The conveyor is dumb: it never knows where the batch is, it is told."""

    STOP = "stop"
    INTAKE_ADVANCE = "intake_advance"  # calibrated step count that carries the batch into the chamber
    SORT_CREEP = "sort_creep"          # slow continuous run past the camera
