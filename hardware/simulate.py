"""A fake machine, so the supervisor can be exercised before fabrication finishes.

This is a test fixture, not production code: nothing in the control path imports
it, and `run_cords --simulate` is the only caller. It models just enough physics
to drive every branch of the state machine end to end -- a hopper that fills and
empties, a chamber with thermal lag, and an exhaust humidity curve that rises,
peaks and recovers so `DryingEndpoint` actually fires.

Run it against a fast clock (`--speed`) to watch a multi-hour batch in minutes.
"""

import math

from hardware.batch import Commands
from hardware.states import BatchState


def _rh_from_ah(ah: float, temp_c: float) -> float:
    """Inverse of drying.absolute_humidity -- the DHT22 reports RH, not AH."""
    saturation_hpa = 6.112 * math.exp(17.67 * temp_c / (temp_c + 243.5))
    vapour_hpa = ah * (temp_c + 273.15) / 216.7
    return max(0.0, min(100.0, vapour_hpa / saturation_hpa * 100.0))


class SimulatedMachine:
    def __init__(
        self,
        target_kg: float = 2.0,
        ambient_c: float = 30.0,
        heater_ceiling_c: float = 95.0,   # where the chamber would settle if the heaters never cut out
        thermal_tau_s: float = 240.0,
        fill_rate_kg_s: float = 0.05,
        drain_rate_kg_s: float = 0.5,
        inlet_ah: float = 15.0,           # g/m^3 of the incoming air
        max_rise_ah: float = 12.0,        # how far the wet load pushes it above inlet
        moisture_tau_s: float = 3600.0,
        sorting_s: float = 300.0,
        final_loss: float = 0.45,         # the 43-47% target weight loss
    ):
        self.target_kg = target_kg
        self.ambient_c = ambient_c
        self.heater_ceiling_c = heater_ceiling_c
        self.thermal_tau_s = thermal_tau_s
        self.fill_rate_kg_s = fill_rate_kg_s
        self.drain_rate_kg_s = drain_rate_kg_s
        self.inlet_ah = inlet_ah
        self.max_rise_ah = max_rise_ah
        self.moisture_tau_s = moisture_tau_s
        self.sorting_s = sorting_s
        self.final_loss = final_loss

        self.chamber_temp = ambient_c
        self.hopper_kg = 0.0
        self.sorting_complete = False
        self.sorted_kg: float | None = None
        self._moisture = 1.0
        self._dispensed = False
        self._batch_kg: float | None = None
        self._sorting_started: float | None = None
        self._last: float | None = None

    @property
    def exhaust_temp(self) -> float:
        return max(self.ambient_c, self.chamber_temp - 5.0)

    @property
    def exhaust_rh(self) -> float:
        # the load only gives up water once the chamber is actually warm
        heat = max(0.0, min(1.0, (self.chamber_temp - 35.0) / 25.0))
        ah = self.inlet_ah + self.max_rise_ah * self._moisture * heat
        return _rh_from_ah(ah, self.exhaust_temp)

    def advance(self, now: float, commands: Commands | None) -> None:
        dt = 0.0 if self._last is None else now - self._last
        self._last = now
        if dt <= 0.0 or commands is None:
            return

        # chamber: first-order lag toward the heaters' ceiling or toward ambient,
        # with the fans roughly doubling the rate of heat loss
        target = self.heater_ceiling_c if commands.heater_on else self.ambient_c
        tau = self.thermal_tau_s
        if commands.fans_on and not commands.heater_on:
            tau /= 2.0
        self.chamber_temp += (target - self.chamber_temp) * min(1.0, dt / tau)

        if commands.gate_open:
            if self._batch_kg is None:
                self._batch_kg = self.hopper_kg   # what was in the hopper when it opened
            self.hopper_kg = max(0.0, self.hopper_kg - self.drain_rate_kg_s * dt)
            if self.hopper_kg <= 0.0:
                self._dispensed = True
        elif commands.state is BatchState.INTAKE and not self._dispensed:
            self.hopper_kg = min(self.target_kg * 1.05, self.hopper_kg + self.fill_rate_kg_s * dt)

        if commands.state is BatchState.DRYING:
            self._moisture *= math.exp(-dt / self.moisture_tau_s)

        if commands.state is BatchState.SORTING:
            if self._sorting_started is None:
                self._sorting_started = now
            elif now - self._sorting_started >= self.sorting_s:
                self.sorting_complete = True
                self.sorted_kg = (self._batch_kg or self.target_kg) * (1.0 - self.final_loss)
