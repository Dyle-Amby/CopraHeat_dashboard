"""MG996R hopper gate servo on hardware PWM via rpi-hardware-pwm.

Requires in /boot/firmware/config.txt:  dtoverlay=pwm-2chan
Then verify with `pinctrl get 18` (should show a PWM function) and
`ls /sys/class/pwm/` for the chip number. On the Pi 5 GPIO18 is expected
to be chip 2, channel 2 -- confirm on the bench before trusting the defaults.

The signal is held continuously: the gate bears the copra load, so the servo
must never go limp. Hardware PWM keeps running after the process exits, which
is what we want.
"""


class Servo:
    def __init__(self, channel: int = 2, chip: int = 2, hz: float = 50.0, min_us: float = 500.0, max_us: float = 2500.0):
        from rpi_hardware_pwm import HardwarePWM

        self._pwm = HardwarePWM(pwm_channel=channel, hz=hz, chip=chip)
        self._period_us = 1_000_000 / hz
        self.min_us = min_us
        self.max_us = max_us
        self.angle: float | None = None
        self._pwm.start(0)

    def set_angle(self, degrees: float) -> None:
        degrees = max(0.0, min(180.0, degrees))
        pulse_us = self.min_us + (self.max_us - self.min_us) * degrees / 180.0
        self._pwm.change_duty_cycle(pulse_us / self._period_us * 100.0)
        self.angle = degrees

    def release(self) -> None:
        """Drop the signal. Only for bench work: a released servo will not hold the gate."""
        self._pwm.stop()
        self.angle = None
