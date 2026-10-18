"""Minimal HX711 load-cell amplifier driver, bit-banged through lgpio.

Channel A, gain 128. The popular `hx711` pip packages sit on RPi.GPIO, which
does not work on the Pi 5, so this talks to lgpio directly. The same class
serves the hopper cell and the three bin cells.

SCK must never stay high longer than 60 us or the chip powers down and the
next read is garbage; if the OS preempts mid-pulse that can happen, so callers
should treat None as "retry", not "fault", unless it persists.
"""

import statistics
import time

from hardware.gpiochip import open_rp1_chip

SATURATED = {0x7FFFFF, 0x800000}


def decode_24bit(raw: int) -> int | None:
    """Two's-complement decode of a 24-bit sample; None if the ADC is railed."""
    raw &= 0xFFFFFF
    if raw in SATURATED:
        return None
    return raw - (1 << 24) if raw & 0x800000 else raw


DEFAULT_SCALE = 21500.0


def raw_to_kg(raw: int | None, offset: int = 0, scale: float = DEFAULT_SCALE) -> float | None:
    """Convert raw ADC counts to kilograms using calibration offset and scale factor."""
    if raw is None:
        return None
    s = scale if (scale is not None and scale != 0 and scale != 1.0) else DEFAULT_SCALE
    return (raw - offset) / s


class HX711:
    def __init__(self, dout: int, sck: int, offset: int = 0, scale: float = DEFAULT_SCALE):
        import lgpio

        self._lgpio = lgpio
        self._h = open_rp1_chip()
        self.dout = dout
        self.sck = sck
        self.offset = offset      # raw counts at zero load
        self.scale = scale if (scale is not None and scale != 0 and scale != 1.0) else DEFAULT_SCALE  # raw counts per kg
        # why the last read_raw() returned None: "timeout" = DOUT never went low
        # (power, DOUT or SCK wiring); "railed" = ADC pinned at full scale (the
        # load-cell side: E+/E-/A+/A- open or swapped)
        self.last_fault: str | None = None
        lgpio.gpio_claim_input(self._h, dout)
        lgpio.gpio_claim_output(self._h, sck, 0)

    def read_raw(self, timeout_s: float = 0.5) -> int | None:
        g = self._lgpio
        deadline = time.monotonic() + timeout_s
        while g.gpio_read(self._h, self.dout):        # DOUT high = conversion not ready
            if time.monotonic() > deadline:
                self.last_fault = "timeout"
                return None
            time.sleep(0.001)
        value = 0
        for _ in range(24):
            g.gpio_write(self._h, self.sck, 1)
            g.gpio_write(self._h, self.sck, 0)
            value = (value << 1) | g.gpio_read(self._h, self.dout)
        g.gpio_write(self._h, self.sck, 1)             # 25th pulse: channel A, gain 128 next time
        g.gpio_write(self._h, self.sck, 0)
        decoded = decode_24bit(value)
        self.last_fault = "railed" if decoded is None else None
        return decoded

    def read_raw_median(self, samples: int = 15) -> int | None:
        readings = [r for r in (self.read_raw() for _ in range(samples)) if r is not None]
        return int(statistics.median(readings)) if readings else None

    def read_kg(self, offset: int | None = None) -> float | None:
        raw = self.read_raw()
        if raw is None:
            return None
        target_offset = self.offset if offset is None else offset
        return raw_to_kg(raw, offset=target_offset, scale=self.scale)

    def tare(self, samples: int = 15) -> int | None:
        """Measure and return the raw zero-load offset without changing self.offset."""
        return self.read_raw_median(samples)

    def close(self) -> None:
        self._lgpio.gpio_write(self._h, self.sck, 0)
        self._lgpio.gpiochip_close(self._h)
