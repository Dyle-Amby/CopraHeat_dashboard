"""Drying-chamber climate control logic. No GPIO here: feed readings in, get commands out."""

import math
from dataclasses import dataclass

from hardware.states import BatchState


@dataclass(frozen=True)
class HysteresisBand:
    low: float   # switch ON at or below this
    high: float  # switch OFF at or above this

    def __post_init__(self):
        if self.low >= self.high:
            raise ValueError(f"band low ({self.low}) must be below high ({self.high})")


class HeaterController:
    """Two-point hysteresis for the IR heaters, active only while DRYING.

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

    def update(self, state: BatchState, temp_c: float | None) -> bool:
        self.fault = temp_c is None or temp_c >= self.max_temp
        if self.fault or state is not BatchState.DRYING:
            self.heater_on = False
        elif temp_c <= self.band.low:
            self.heater_on = True
        elif temp_c >= self.band.high:
            self.heater_on = False
        # inside the band: hold the previous state
        return self.heater_on


class FanController:
    """Continuous airflow while drying, gated by a heat-up interlock.

    Both fans share one mechanical relay, so a minimum dwell between state
    changes protects the contacts from chatter around the interlock
    boundary. Only the over-temp override may bypass the dwell.
    """

    def __init__(self, interlock_temp: float = 60.0, overtemp: float = 75.0, dwell_s: float = 30.0):
        if interlock_temp >= overtemp:
            raise ValueError(f"interlock ({interlock_temp}) must be below overtemp ({overtemp})")
        self.interlock_temp = interlock_temp
        self.overtemp = overtemp
        self.dwell_s = dwell_s
        self.fans_on = False
        self.overtemp_active = False
        self._last_change = -math.inf

    def update(self, state: BatchState, temp_c: float | None, now: float) -> bool:
        self.overtemp_active = temp_c is not None and temp_c > self.overtemp
        if self.overtemp_active:
            self._switch(True, now)
            return self.fans_on

        if state is BatchState.DRYING:
            desired = temp_c is None or temp_c >= self.interlock_temp
        elif state is BatchState.COOLDOWN:
            desired = True
        else:
            desired = False

        if now - self._last_change >= self.dwell_s:
            self._switch(desired, now)
        return self.fans_on

    def _switch(self, on: bool, now: float) -> None:
        if on != self.fans_on:
            self.fans_on = on
            self._last_change = now
