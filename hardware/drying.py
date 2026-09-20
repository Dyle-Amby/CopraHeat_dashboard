"""Drying endpoint detection from exhaust humidity. No GPIO here.

The water leaving the copra leaves through the exhaust. Converting the DHT22's
RH + temperature to absolute humidity removes the temperature dependence, and
the drying curve then has a clear shape: AH rises as the chamber heats, peaks,
and falls back toward the inlet level as the copra approaches equilibrium.
Drying is done when AH has recovered most of that rise AND stopped falling.

Every threshold here is a calibration parameter to be fitted against the
post-sort gravimetric weight loss. Time is only a floor and a ceiling.
"""

import math
from collections import deque


def absolute_humidity(temp_c: float, rh: float) -> float:
    """Water vapour density in g/m^3 (Magnus-Tetens saturation pressure)."""
    saturation_hpa = 6.112 * math.exp(17.67 * temp_c / (temp_c + 243.5))
    vapour_hpa = saturation_hpa * rh / 100.0
    return 216.7 * vapour_hpa / (temp_c + 273.15)


def _slope_per_min(points: deque) -> float:
    n = len(points)
    mean_t = sum(t for t, _ in points) / n
    mean_y = sum(y for _, y in points) / n
    sxx = sum((t - mean_t) ** 2 for t, _ in points)
    if sxx == 0:
        return 0.0
    sxy = sum((t - mean_t) * (y - mean_y) for t, y in points)
    return sxy / sxx * 60.0


class DryingEndpoint:
    def __init__(
        self,
        min_time_s: float = 30 * 60,
        max_time_s: float = 10 * 3600,
        window_s: float = 10 * 60,
        flat_slope: float = 0.05,        # g/m^3 per minute; falling slower than this counts as flat
        recovery_fraction: float = 0.8,  # AH must have given back this much of its rise above the start level
        min_rise: float = 2.0,           # g/m^3; below this the chamber never really dried anything
        fault_after_s: float = 120.0,
    ):
        self.min_time_s = min_time_s
        self.max_time_s = max_time_s
        self.window_s = window_s
        self.flat_slope = flat_slope
        self.recovery_fraction = recovery_fraction
        self.min_rise = min_rise
        self.fault_after_s = fault_after_s
        self._reset(None)

    def _reset(self, now: float | None) -> None:
        self._points: deque[tuple[float, float]] = deque()
        self.started_at = now
        self.last_valid_at = now
        self.start_ah: float | None = None
        self.peak_ah: float | None = None
        self.ah: float | None = None
        self.slope: float | None = None
        self.done = False
        self.timeout = False
        self.fault = False

    def start(self, now: float) -> None:
        self._reset(now)

    def update(self, exhaust_temp: float | None, exhaust_rh: float | None, now: float) -> bool:
        if self.started_at is None:
            raise RuntimeError("call start(now) when DRYING begins")
        if self.done:
            return True

        elapsed = now - self.started_at
        self.timeout = elapsed >= self.max_time_s

        if exhaust_temp is None or exhaust_rh is None:
            self.fault = now - self.last_valid_at >= self.fault_after_s
            return False
        self.fault = False
        self.last_valid_at = now

        self.ah = absolute_humidity(exhaust_temp, exhaust_rh)
        if self.start_ah is None:
            self.start_ah = self.ah
        self.peak_ah = self.ah if self.peak_ah is None else max(self.peak_ah, self.ah)

        self._points.append((now, self.ah))
        while self._points and now - self._points[0][0] > self.window_s:
            self._points.popleft()
        window_full = len(self._points) >= 2 and now - self._points[0][0] >= 0.8 * self.window_s
        self.slope = _slope_per_min(self._points) if window_full else None

        if elapsed < self.min_time_s or not window_full:
            return False
        rise = self.peak_ah - self.start_ah
        if rise < self.min_rise:
            return False
        recovered_to = self.peak_ah - self.recovery_fraction * rise
        self.done = self.ah <= recovered_to and self.slope >= -self.flat_slope
        return self.done
