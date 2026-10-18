"""Step/direction driver for the TB6600 (bin carriage) and HBS57H (conveyor).

Pulses come from lgpio's tx_pulse, which counts them off on lgpio's own timing
thread, so a Python loop never has to keep step timing and a move of N steps is
exactly N pulses. That thread is still software-timed: keep the rate at a few
kHz or below, above that jitter shows up as rough motion and lost steps.

Both drivers sit behind the 74HCT245 (non-inverting), PUL-/DIR- to GND, so a
high GPIO lights the driver's input LED. No acceleration ramp yet: bench rates
are low enough to start from standstill.
"""

import time

from hardware.gpiochip import open_rp1_chip
from hardware.pins import StepperPins

MAX_HZ = 5000
DIR_SETUP_S = 0.001   # both drivers want DIR settled >= 5 us before the first PUL edge


def pulse_timing(hz: float) -> tuple[int, int]:
    """(on_us, off_us) for a 50% duty pulse train at `hz` steps per second."""
    if not 0 < hz <= MAX_HZ:
        raise ValueError(f"step rate must be in (0, {MAX_HZ}] Hz, got {hz}")
    period = round(1_000_000 / hz)
    on = period // 2
    return on, period - on


def steps_done(elapsed_s: float, hz: float, requested: int) -> int:
    """Estimated steps sent after `elapsed_s` of a move. lgpio does not report a
    count for a stopped train, so an early stop is timed, not counted."""
    return min(requested, max(0, int(elapsed_s * hz)))


class Stepper:
    def __init__(self, pins: StepperPins, invert_dir: bool = False):
        import lgpio

        self._lgpio = lgpio
        self._h = open_rp1_chip()
        self.pins = pins
        self.invert_dir = invert_dir   # flip if "forward" turns the wrong way once mounted
        self._started: float | None = None
        self._hz = 0.0
        self._steps = 0
        lgpio.gpio_claim_output(self._h, pins.pul, 0)
        lgpio.gpio_claim_output(self._h, pins.dir, 0)

    def start(self, steps: int, hz: float, forward: bool = True) -> None:
        if steps <= 0:
            raise ValueError("steps must be positive")
        on_us, off_us = pulse_timing(hz)
        g = self._lgpio
        g.gpio_write(self._h, self.pins.dir, int(forward != self.invert_dir))
        time.sleep(DIR_SETUP_S)
        g.tx_pulse(self._h, self.pins.pul, on_us, off_us, 0, steps)
        self._started, self._hz, self._steps = time.monotonic(), hz, steps

    @property
    def busy(self) -> bool:
        return bool(self._lgpio.tx_busy(self._h, self.pins.pul, self._lgpio.TX_PWM))

    def stop(self) -> int:
        """Cut the pulse train; returns the estimated steps sent."""
        self._lgpio.tx_pulse(self._h, self.pins.pul, 0, 0)
        self._lgpio.gpio_write(self._h, self.pins.pul, 0)
        if self._started is None:
            return 0
        sent = steps_done(time.monotonic() - self._started, self._hz, self._steps)
        self._started = None
        return sent

    def stop(self):
    # If you started with tx_pwm (continuous):
    self._lgpio.tx_pwm(self._h, self.pins.pul, 0, 0)   # frequency 0 = stop
    # Make sure the line idles low:
    self._lgpio.gpio_write(self._h, self.pins.pul, 0)
