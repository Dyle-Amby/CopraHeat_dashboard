"""Menu-driven bench test for the assembled prototype, motors excluded.

Run on the Pi from inside CopraHeat_dashboard, with run_cords stopped (it owns
the same pins):

    python -m hardware.prototype_test
    python -m hardware.prototype_test --fan-active-low        # relay board turns on when IN is LOW
    python -m hardware.prototype_test --pwm-chip 0 --pwm-channel 2

Covers fans, heaters, the hopper servo, the limit switches (pressed by hand,
shows which bin each one calls), all four load cells and the chamber sensors.
The steppers are deliberately not touched. Fans and heaters are switched OFF
on every exit path; the servo keeps holding its last angle, as it does in the
real machine.
"""

import argparse
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

from hardware import carriage, pins

CELLS = {
    "hopper": pins.HX711_HOPPER,
    "great": pins.HX711_BIN_GREAT,
    "good": pins.HX711_BIN_GOOD,
    "bad": pins.HX711_BIN_BAD,
}

# the Pi 5's RP1 PWM block, the one wired to the header (GPIO18 = its channel 2)
RP1_PWM_ADDRESS = "1f00098000.pwm"
SERVO_PWM_CHANNEL = 2


def stamp() -> str:
    return f"{datetime.now():%H:%M:%S}"


def find_rp1_pwm_chip(root: Path = Path("/sys/class/pwm")) -> int | None:
    """Number of the pwmchip that drives the header pins, from its device path."""
    if not root.is_dir():
        return None
    for entry in sorted(root.glob("pwmchip*")):
        if RP1_PWM_ADDRESS in os.path.realpath(entry):
            return int(entry.name.removeprefix("pwmchip"))
    return None


def switch_bins(order=carriage.ORDER, bins=carriage.BINS) -> dict[str, str]:
    """limit switch name -> grade of the bin at that stop."""
    return {switch: grade for grade, switch in carriage.stops(order, bins).items()}


def describe_switches(state: dict[str, bool], bins: dict[str, str]) -> str:
    pressed = [name for name, hit in state.items() if hit]
    if not pressed:
        return "no switch pressed (carriage between stops)"
    if len(pressed) == len(state):
        return "ALL pressed: more likely a missing GND/common wire than three hands"
    if len(pressed) > 1:
        return f"{' + '.join(pressed)} pressed together: wiring fault"
    return f"{pressed[0]} pressed -> {bins[pressed[0]].upper()} bin"


class Outputs:
    """Fan relay and heater SSR, held for the whole session, always left OFF."""

    def __init__(self, fan_active_low: bool):
        from gpiozero import OutputDevice

        self.devices = {
            "fans": (OutputDevice(pins.FAN_RELAY, active_high=not fan_active_low, initial_value=False),
                     pins.FAN_RELAY),
            "heaters": (OutputDevice(pins.HEATER_SSR, initial_value=False), pins.HEATER_SSR),
        }
        # what we commanded, kept here rather than read back from the pin, so a
        # wiring fault dragging the line down cannot make the menu lie about it
        self.on = {name: False for name in self.devices}

    def set(self, name: str, on: bool) -> None:
        device, gpio = self.devices[name]
        device.value = on
        self.on[name] = on
        level = pin_level(gpio)
        print(f"{stamp()}  {name} {'ON' if on else 'off'}  (GPIO{gpio} reads {level})")

    def status(self) -> str:
        return "  ".join(f"{name}={'ON' if on else 'off'}" for name, on in self.on.items())

    def close(self) -> None:
        for name, (device, _) in self.devices.items():
            device.off()
            device.close()
            self.on[name] = False


def pin_level(gpio: int) -> str:
    """The pin's actual level as the Pi sees it, via pinctrl; '?' if unavailable."""
    import subprocess

    try:
        out = subprocess.run(["pinctrl", "lev", str(gpio)], capture_output=True, text=True, timeout=2)
        return {"1": "HIGH", "0": "low"}.get(out.stdout.strip(), "?")
    except (OSError, subprocess.SubprocessError):
        return "?"


def toggle(outputs: Outputs, name: str) -> None:
    outputs.set(name, not outputs.on[name])


def heater_toggle(outputs: Outputs) -> None:
    if not outputs.on["heaters"]:
        print("HEATERS: 220 V bulbs. Keep hands clear of the chamber.")
        if input("type yes to switch them on: ").strip().lower() not in ("y", "yes"):
            print("  left off")
            return
    toggle(outputs, "heaters")


def servo_menu(args: argparse.Namespace, state: dict) -> None:
    if state.get("servo") is None:
        chip = args.pwm_chip if args.pwm_chip is not None else find_rp1_pwm_chip()
        if chip is None:
            print("no RP1 pwmchip found. Is the pwm-2chan overlay in /boot/firmware/config.txt?\n"
                  "  dtoverlay=pwm-2chan,pin=18,func=7,pin2=19,func2=7   (then reboot)\n"
                  "  or pass --pwm-chip N (see `ls -l /sys/class/pwm/`)")
            return
        from hardware.servo import Servo

        try:
            state["servo"] = Servo(channel=args.pwm_channel, chip=chip)
        except Exception as exc:
            print(f"could not open pwmchip{chip} channel {args.pwm_channel}: {exc}")
            return
        print(f"servo on pwmchip{chip} channel {args.pwm_channel} (GPIO18, physical pin 12)")
    servo = state["servo"]
    print("type an angle 0-180, c = closed (180), o = open (0), Enter = back to menu")
    while True:
        line = input(f"servo [{'--' if servo.angle is None else f'{servo.angle:g}'}]> ").strip().lower()
        if not line:
            return
        angle = {"c": 180.0, "o": 0.0}.get(line)
        if angle is None:
            try:
                angle = float(line)
            except ValueError:
                print("  a number 0-180, c, o, or Enter")
                continue
        servo.set_angle(angle)
        print(f"{stamp()}  servo -> {servo.angle:g} deg")


def watch_switches(args: argparse.Namespace) -> None:
    from hardware.limits import LimitSwitches, SWITCHES

    bins = switch_bins()
    print("Press each switch by hand. Unplugging a switch must ALSO read pressed (NC wiring).")
    print("Pins: " + ", ".join(f"{name}=GPIO{gpio}" for name, gpio in SWITCHES.items())
          + "   Ctrl+C = back to menu")
    ls = LimitSwitches()
    prev = None
    try:
        while True:
            state = ls.read()
            if state != prev:
                print(f"{stamp()}  {describe_switches(state, bins)}")
                prev = state
            time.sleep(0.02)
    except KeyboardInterrupt:
        print()
    finally:
        ls.close()


def watch_cells(args: argparse.Namespace) -> None:
    from hardware.hx711 import HX711

    cells = {name: HX711(*gpios) for name, gpios in CELLS.items()}
    print("Taring with nothing on the cells...")
    zero = {name: hx.read_raw_median(10) for name, hx in cells.items()}
    print("Press on each cell / bin: its change must move. Ctrl+C = back to menu")
    print("'timeout' = HX711 power/DOUT/SCK wiring; 'railed' = load-cell side (E+/E-/A+/A-)")
    try:
        while True:
            parts = []
            for name, hx in cells.items():
                raw = hx.read_raw()
                if raw is None:
                    parts.append(f"{name} -- {hx.last_fault}")
                elif zero[name] is None:
                    parts.append(f"{name} {raw:>9}")
                else:
                    parts.append(f"{name} {raw - zero[name]:>+8}")
            print(f"{stamp()}  " + "  |  ".join(parts))
    except KeyboardInterrupt:
        print()
    finally:
        for hx in cells.values():
            hx.close()


def watch_sensors(args: argparse.Namespace) -> None:
    from hardware import sensors

    print("chamber = DS18B20 (GPIO4, pin 7), exhaust = DHT22 (GPIO26, pin 37). Ctrl+C = back to menu")
    show = lambda v, unit: "--" if v is None else f"{v:.1f}{unit}"
    try:
        while True:
            chamber = sensors.read_ds18b20()
            t, rh = sensors.read_dht22()
            print(f"{stamp()}  chamber={show(chamber, 'C'):>7}  exhaust={show(t, 'C'):>7} {show(rh, '%'):>6}")
            time.sleep(2.0)
    except KeyboardInterrupt:
        print()


MENU = """
---------------- CORDS prototype test ----------------
  1  fans     on / off      (relay, GPIO{fan} / pin {fan_pin})
  2  heaters  on / off      (SSR,   GPIO{heat} / pin 29)
  3  servo    move hopper gate
  4  limit switches  (press by hand -> which bin)
  5  load cells      (hopper + 3 bins)
  6  sensors         (chamber + exhaust)
  q  quit (fans + heaters OFF)
"""

PHYSICAL = {25: 22, 6: 31}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fan-active-low", action="store_true", help="relay board turns ON when its IN pin is LOW")
    ap.add_argument("--pwm-chip", type=int, default=None, help="default: find the RP1 pwmchip automatically")
    ap.add_argument("--pwm-channel", type=int, default=SERVO_PWM_CHANNEL)
    args = ap.parse_args(argv)

    # systemd / kill sends SIGTERM; make it unwind through the finally below
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    outputs = Outputs(args.fan_active_low)
    state: dict = {}
    actions = {
        "1": lambda: toggle(outputs, "fans"),
        "2": lambda: heater_toggle(outputs),
        "3": lambda: servo_menu(args, state),
        "4": lambda: watch_switches(args),
        "5": lambda: watch_cells(args),
        "6": lambda: watch_sensors(args),
    }
    try:
        while True:
            print(MENU.format(fan=pins.FAN_RELAY, fan_pin=PHYSICAL.get(pins.FAN_RELAY, "?"), heat=pins.HEATER_SSR))
            print(f"  now: {outputs.status()}")
            choice = input("> ").strip().lower()
            if choice in ("q", "quit", "exit"):
                break
            action = actions.get(choice)
            if action is None:
                continue
            try:
                action()
            except Exception as exc:
                if "busy" in str(exc).lower():
                    print(f"{exc}: is run_cords or a bench script holding these pins? Stop it first.")
                else:
                    print(f"error: {exc}")
    except (KeyboardInterrupt, EOFError):
        print()
    finally:
        outputs.close()
        print("fans and heaters OFF" + ("; servo still holding its last angle" if state.get("servo") else ""))


if __name__ == "__main__":
    main()
