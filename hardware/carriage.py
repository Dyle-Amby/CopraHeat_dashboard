"""Bin-carriage positioning: which switch is which bin, and when a move is done.

The carriage has no encoder; its position is only ever known at a limit
switch. A move therefore runs the stepper and watches the switches: it
arrives when the target switch is pressed, passes over any switch that lies
between start and target, and faults on anything else (a switch from the
wrong side means the motor direction or the switch order is wrong).

Pure logic, no GPIO: the bench tool and, later, the supervisor feed it
switch readings. The defaults below are placeholders until confirmed on the
fabricated machine.
"""

from hardware.limits import newly_triggered

# limit switches from the bottom of the carriage's travel to the top
ORDER = ("LS1", "LS2", "LS3")
# grade whose bin sits at each of those stops, same bottom-to-top order
BINS = ("great", "good", "bad")

MOVING, ARRIVED, FAULT = "moving", "arrived", "fault"


def stops(order=ORDER, bins=BINS) -> dict[str, str]:
    """grade -> limit switch name."""
    if len(order) != len(bins) or len(set(order)) != len(order) or len(set(bins)) != len(bins):
        raise ValueError("need one distinct switch per distinct bin")
    return dict(zip(bins, order))


def located_at(state: dict[str, bool]) -> str | None:
    """The switch the carriage is resting on, or None if it is between stops.
    More than one pressed at once is a wiring fault, not a position."""
    pressed = [name for name, hit in state.items() if hit]
    if len(pressed) > 1:
        raise RuntimeError(f"{', '.join(pressed)} all read pressed: check the switch wiring with `bench limits`")
    return pressed[0] if pressed else None


def moving_up(start: str, target: str, order=ORDER) -> bool:
    return order.index(target) > order.index(start)


class MoveWatch:
    """Decides, reading by reading, whether a carriage move is done.

    `initial` is the switch state taken before the motor starts. With no
    `target` the move is a homing run: the first switch reached is the answer.
    """

    def __init__(self, initial: dict[str, bool], target: str | None, order=ORDER, release_within: int = 400):
        self.order = order
        self.target = target
        self.start = located_at(initial)
        self.release_within = release_within
        self._prev = initial
        self._released = self.start is None
        if target is not None:
            if self.start is not None:
                lo, hi = sorted((order.index(self.start), order.index(target)))
                self.passable = set(order[lo + 1:hi])
            else:
                # If started between stops, all switches on the way to target are passable
                self.passable = set(order[:order.index(target)])

    def update(self, state: dict[str, bool], steps_sent: int) -> tuple[str, str | None]:
        """(MOVING, None), (ARRIVED, switch) or (FAULT, message)."""
        new = newly_triggered(self._prev, state)
        self._prev = state
        for name in new:
            if self.target is None or name == self.target:
                return ARRIVED, name
            if name not in self.passable:
                return FAULT, (f"hit {name} on the way to {self.target}: the motor direction "
                               "(--up-reverse) or the switch order (--order) is wrong")
        if not self._released:
            if not state[self.start]:
                self._released = True
            elif steps_sent >= self.release_within:
                return FAULT, (f"{self.start} never released after ~{steps_sent} steps: driving into the "
                               "end stop, so the motor direction is probably inverted (--up-reverse)")
        return MOVING, None
