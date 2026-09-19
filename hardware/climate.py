"""Drying-chamber climate control logic. No GPIO here: feed readings in, get commands out."""

from dataclasses import dataclass


@dataclass(frozen=True)
class HysteresisBand:
    low: float   # switch ON at or below this
    high: float  # switch OFF at or above this

    def __post_init__(self):
        if self.low >= self.high:
            raise ValueError(f"band low ({self.low}) must be below high ({self.high})")


class HeaterController:
    """Two-point hysteresis for the IR heaters.

    A reading of None (sensor fault) or anything at/above max_temp forces the
    heaters OFF regardless of band state. Mains heaters fail safe, not warm.
    """

    def __init__(self, band: HysteresisBand = HysteresisBand(60.0, 70.0), max_temp: float = 80.0):
        if band.high >= max_temp:
            raise ValueError(f"band high ({band.high}) must be below max_temp ({max_temp})")
        self.band = band
        self.max_temp = max_temp
        self.heater_on = False
        self.fault = False

    def update(self, temp_c: float | None) -> bool:
        if temp_c is None or temp_c >= self.max_temp:
            self.fault = True
            self.heater_on = False
        else:
            self.fault = False
            if temp_c <= self.band.low:
                self.heater_on = True
            elif temp_c >= self.band.high:
                self.heater_on = False
            # inside the band: hold the previous state
        return self.heater_on
