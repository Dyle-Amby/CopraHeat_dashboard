"""Open the Pi 5's GPIO controller through lgpio.

Shared by every module that talks to lgpio directly (HX711, steppers, limit
switches). Imports lgpio lazily so the pure logic around it stays testable on
Windows.
"""


def open_rp1_chip() -> int:
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
