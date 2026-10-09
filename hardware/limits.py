"""The three bin-carriage limit switches.

Wired NC to GND with the Pi's internal pull-ups. At rest the contact is closed
and holds the line low; pressing the switch opens it and the pull-up takes the
line high. A cut wire or an unplugged connector also reads high, so every
wiring fault looks like "stop", never like "all clear".
"""

from hardware import pins
from hardware.gpiochip import open_rp1_chip

SWITCHES = {
    "LS1": pins.LIMIT_SWITCH_1,
    "LS2": pins.LIMIT_SWITCH_2,
    "LS3": pins.LIMIT_SWITCH_3,
}


def triggered(level: int) -> bool:
    """High = switch pressed OR wire broken; the two are indistinguishable by design."""
    return level == 1


def newly_triggered(before: dict[str, bool], after: dict[str, bool]) -> list[str]:
    return [name for name, hit in after.items() if hit and not before.get(name, False)]


class LimitSwitches:
    def __init__(self):
        import lgpio

        self._lgpio = lgpio
        self._h = open_rp1_chip()
        for gpio in SWITCHES.values():
            lgpio.gpio_claim_input(self._h, gpio, lgpio.SET_PULL_UP)

    def read(self) -> dict[str, bool]:
        return {name: triggered(self._lgpio.gpio_read(self._h, gpio)) for name, gpio in SWITCHES.items()}

    def close(self) -> None:
        self._lgpio.gpiochip_close(self._h)
