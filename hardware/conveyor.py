"""Conveyor motion, behind the interface the batch state machine commands.

The real driver (57HS82 via the HBS57H on GPIO22/23, through the 74HCT245
buffer) is not written yet: the pulse rate and the calibrated intake step count
can only be settled on the fabricated machine, and a stepper pulse train on the
Pi 5 needs lgpio waves rather than a Python loop. `SimulatedConveyor` stands in
until then and runs anywhere.

The supervisor talks to this interface either way, so dropping the real driver
in later changes nothing above it.
"""

from hardware.states import ConveyorCommand


class SimulatedConveyor:
    """Time-based stand-in. `advance_s` is however long the intake move takes."""

    def __init__(self, advance_s: float = 4.0):
        self.advance_s = advance_s
        self.busy = False
        self.creeping = False
        self._moving_until: float | None = None
        self._advance_done = False

    def apply(self, command: ConveyorCommand, now: float) -> None:
        if command is ConveyorCommand.INTAKE_ADVANCE:
            self.creeping = False
            # the state machine keeps asking until it sees the move end, so a
            # finished move must not restart itself
            if self._moving_until is None and not self._advance_done:
                self._moving_until = now + self.advance_s
            elif self._moving_until is not None and now >= self._moving_until:
                self._moving_until = None
                self._advance_done = True
            self.busy = self._moving_until is not None
        elif command is ConveyorCommand.SORT_CREEP:
            self.creeping = True
            self._moving_until = None
            self.busy = True
        else:
            self.stop()

    def stop(self) -> None:
        self.creeping = False
        self.busy = False
        self._moving_until = None
        self._advance_done = False   # armed again for the next batch

    def close(self) -> None:
        self.stop()
