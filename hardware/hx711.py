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

SATURATED = {0x7FFFFF, 0x800000}


def decode_24bit(raw: int) -> int | None:
    """Two's-complement decode of a 24-bit sample; None if the ADC is railed."""
    raw &= 0xFFFFFF
    if raw in SATURATED:
        return None
    return raw - (1 << 24) if raw & 0x800000 else raw


def _open_rp1_chip():
    import lgpio

    # The Pi 5 header lives on the RP1 chip: gpiochip4 on older kernels, gpiochip0 on newer ones.
    for chip in (4, 0):
        try:
            handle = lgpio.gpiochip_open(chip)
        except lgpio.error:
            continue
        _, _, label = lgpio.gpio_get_chip_info(handle)
        if "rp1" in label.lower():
            return handle
        lgpio.gpiochip_close(handle)
    raise RuntimeError("no RP1 gpiochip found; is this a Pi 5?")


class HX711:
    def __init__(self, dout: int, sck: int, offset: int = 0, scale: float = 1.0):
        import lgpio

        self._lgpio = lgpio
        self._h = _open_rp1_chip()
        self.dout = dout
        self.sck = sck
        self.offset = offset      # raw counts at zero load
        self.scale = scale        # raw counts per kg
        lgpio.gpio_claim_input(self._h, dout)
        lgpio.gpio_claim_output(self._h, sck, 0)

    def read_raw(self, timeout_s: float = 0.5) -> int | None:
        g = self._lgpio
        deadline = time.monotonic() + timeout_s
        while g.gpio_read(self._h, self.dout):        # DOUT high = conversion not ready
            if time.monotonic() > deadline:
                return None
            time.sleep(0.001)
        value = 0
        for _ in range(24):
            g.gpio_write(self._h, self.sck, 1)
            g.gpio_write(self._h, self.sck, 0)
            value = (value << 1) | g.gpio_read(self._h, self.dout)
        g.gpio_write(self._h, self.sck, 1)             # 25th pulse: channel A, gain 128 next time
        g.gpio_write(self._h, self.sck, 0)
        return decode_24bit(value)

    def read_raw_median(self, samples: int = 15) -> int | None:
        readings = [r for r in (self.read_raw() for _ in range(samples)) if r is not None]
        return int(statistics.median(readings)) if readings else None

    def read_kg(self, offset: int | None = None) -> float | None:
        raw = self.read_raw()
        if raw is None:
            return None
        return (raw - (self.offset if offset is None else offset)) / self.scale

    def tare(self, samples: int = 15) -> int | None:
        """Measure and return the raw zero-load offset without changing self.offset."""
        return self.read_raw_median(samples)

    def close(self) -> None:
        self._lgpio.gpio_write(self._h, self.sck, 0)
        self._lgpio.gpiochip_close(self._h)
