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
    """Airflow while drying: warm-up hold, then timed pulses, with temperature
    and humidity overrides. Both fans share one relay, so a minimum dwell
    protects the contacts. Only the over-temp override bypasses the dwell.
    """

    def __init__(
        self,
        interlock_temp: float = 60.0,
        overtemp: float = 75.0,
        dwell_s: float = 30.0,
        warmup_s: float = 1200.0,       # fans off for first 20 min of DRYING
        pulse_on_s: float = 60.0,       # then on for 1 min ...
        pulse_period_s: float = 300.0,  # ... every 5 min
        rh_on: float = 60.0,            # placeholder: tune from logs
        rh_off: float = 45.0,           # placeholder: tune from logs
    ):
        if interlock_temp >= overtemp:
            raise ValueError(f"interlock ({interlock_temp}) must be below overtemp ({overtemp})")
        if rh_off >= rh_on:
            raise ValueError(f"rh_off ({rh_off}) must be below rh_on ({rh_on})")
        if pulse_on_s < dwell_s or pulse_on_s >= pulse_period_s:
            raise ValueError("need dwell_s <= pulse_on_s < pulse_period_s")
        self.interlock_temp = interlock_temp
        self.overtemp = overtemp
        self.dwell_s = dwell_s
        self.warmup_s = warmup_s
        self.pulse_on_s = pulse_on_s
        self.pulse_period_s = pulse_period_s
        self.rh_on = rh_on
        self.rh_off = rh_off

        self.fans_on = False
        self.overtemp_active = False
        self.reason = "off"  # why the controller wants the fans in this state
        self._last_change = -math.inf
        self._drying_since: float | None = None
        self._humid = False

    def update(self, state: BatchState, temp_c: float | None, now: float,
               rh: float | None = None) -> bool:
        self.overtemp_active = temp_c is not None and temp_c > self.overtemp
        if self.overtemp_active:
            self.reason = "overtemp"
            self._switch(True, now)
            return self.fans_on

        if state is BatchState.DRYING:
            if self._drying_since is None:
                self._drying_since = now
            desired = self._drying_demand(temp_c, rh, now)
        else:
            self._drying_since = None
            self._humid = False
            desired = state is BatchState.COOLDOWN
            self.reason = "cooldown" if desired else "off"

        if now - self._last_change >= self.dwell_s:
            self._switch(desired, now)
        return self.fans_on

    def _drying_demand(self, temp_c, rh, now) -> bool:
        if temp_c is None:
            self.reason = "sensor-fault"
            return True
        if temp_c >= self.interlock_temp:
            self.reason = "temp"
            return True
        elapsed = now - self._drying_since
        if elapsed < self.warmup_s:
            self.reason = "warmup"
            return False
        if rh is not None:  # humidity latch with its own hysteresis band
            if rh >= self.rh_on:
                self._humid = True
            elif rh <= self.rh_off:
                self._humid = False
            if self._humid:
                self.reason = "humidity"
                return True
        if (elapsed - self.warmup_s) % self.pulse_period_s < self.pulse_on_s:
            self.reason = "pulse"
            return True
        self.reason = "hold"
        return False

    def _switch(self, on: bool, now: float) -> None:
        if on != self.fans_on:
            self.fans_on = on
            self._last_change = now
