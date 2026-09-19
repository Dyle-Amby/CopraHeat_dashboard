"""GPIO pin map for the CORDS machine (Raspberry Pi 5, BCM numbering).

Single source of truth: every control script imports from here.
Physical 40-pin header numbers are in the comments for wiring reference.
"""

from typing import NamedTuple


class HX711Pins(NamedTuple):
    dout: int
    sck: int


class StepperPins(NamedTuple):
    pul: int
    dir: int


# --- Sensors -----------------------------------------------------------------
DS18B20_DATA = 4    # phys 7  - 1-Wire; needs dtoverlay=w1-gpio,gpiopin=4; 4.7k pull-up to 3V3 on board
DHT22_DATA = 26     # phys 37 - 10k pull-up (check whether the breakout already has one)

# --- Load cells --------------------------------------------------------------
# GPIO7-11 are the SPI0 block. SPI must stay DISABLED in raspi-config,
# otherwise the kernel claims these lines and the load cells go dead silently.
HX711_HOPPER = HX711Pins(dout=7, sck=8)       # phys 26 / 24
HX711_BIN_GREAT = HX711Pins(dout=9, sck=10)   # phys 21 / 19
HX711_BIN_GOOD = HX711Pins(dout=11, sck=12)   # phys 23 / 32
HX711_BIN_BAD = HX711Pins(dout=13, sck=16)    # phys 33 / 36

# --- Motor drivers -----------------------------------------------------------
TB6600_CARRIAGE = StepperPins(pul=17, dir=27)  # phys 11 / 13 - 57BYGH420, bin carriage
HBS57H_CONVEYOR = StepperPins(pul=22, dir=23)  # phys 15 / 16 - 57HS82, conveyor belt

# --- Servo -------------------------------------------------------------------
# Hardware PWM: the slanted hopper puts the copra load on the closed gate, so the
# signal is never detached. Needs a pwm dtoverlay in /boot/firmware/config.txt;
# confirm with `pinctrl get 18` after boot. GPIO19 is deliberately left
# unassigned so an overlay that claims the 18/19 pair breaks nothing.
SERVO_HOPPER_GATE = 18  # phys 12

# --- Limit switches (bin carriage position stops) ----------------------------
# Wired NC to GND with internal pull-ups: an open circuit or broken wire reads
# as "stop", never as silence.
LIMIT_SWITCH_1 = 24  # phys 18
LIMIT_SWITCH_2 = 20  # phys 38
LIMIT_SWITCH_3 = 21  # phys 40

# --- Relay / SSR triggers ----------------------------------------------------
# Both lines have a 10k pull-down on the interface board so they cannot fire
# while the GPIO is floating during boot, reset, or a crashed control process.
HEATER_SSR = 5  # phys 29 - Fotek SSR, 3x 220V IR bulbs; pull-down GPIO -> GND
FAN_RELAY = 6   # phys 31 - Songle relay via 2N2222; pull-down on transistor base

# GPIO25 (phys 22) is the only free GPIO left.
# GPIO2/3 (I2C), GPIO14/15 (UART) reserved. GPIO0/1 are HAT EEPROM, never use.


_ASSIGNED = [
    DS18B20_DATA, DHT22_DATA,
    *HX711_HOPPER, *HX711_BIN_GREAT, *HX711_BIN_GOOD, *HX711_BIN_BAD,
    *TB6600_CARRIAGE, *HBS57H_CONVEYOR,
    SERVO_HOPPER_GATE,
    LIMIT_SWITCH_1, LIMIT_SWITCH_2, LIMIT_SWITCH_3,
    HEATER_SSR, FAN_RELAY,
]
assert len(_ASSIGNED) == len(set(_ASSIGNED)), "duplicate GPIO assignment in pins.py"
assert 19 not in _ASSIGNED, "GPIO19 is reserved for the servo's hardware PWM pair"
