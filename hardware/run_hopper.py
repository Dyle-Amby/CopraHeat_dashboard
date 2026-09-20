"""Bench loop for the hopper gate.

Reads the hopper HX711, runs the gate state machine, and drives the MG996R.
Run from inside CopraHeat_dashboard:

    python -m hardware.run_hopper --calibrate
    python -m hardware.run_hopper --target-kg 2.0 --scale 21500 \
        --offset-closed 84210 --offset-open 83950 --closed-angle 0 --open-angle 90

Calibration values are per-load-cell and per-machine; pass them as flags for
now, they move to the settings table once the database exists.
"""

import argparse
import signal
import sys
import time
from datetime import datetime

from hardware import pins
from hardware.hopper import HopperGate


def add_hardware_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--closed-angle", type=float, default=0.0)
    ap.add_argument("--open-angle", type=float, default=90.0)
    ap.add_argument("--pwm-chip", type=int, default=2)
    ap.add_argument("--pwm-channel", type=int, default=2)


def calibrate(args: argparse.Namespace) -> None:
    from hardware.hx711 import HX711
    from hardware.servo import Servo

    hx = HX711(pins.HX711_HOPPER.dout, pins.HX711_HOPPER.sck)
    servo = Servo(channel=args.pwm_channel, chip=args.pwm_chip)
    servo.set_angle(args.closed_angle)
    try:
        input("Hopper empty, gate closed. Press Enter to tare... ")
        offset_closed = hx.tare(30)
        print(f"  offset (closed) = {offset_closed}")

        kg = float(input("Place a known weight on the gate and enter its mass in kg: "))
        raw = hx.read_raw_median(30)
        scale = (raw - offset_closed) / kg
        print(f"  raw = {raw}  ->  scale = {scale:.1f} counts/kg")

        input("Remove the weight. Press Enter to open the gate and tare in the open position... ")
        servo.set_angle(args.open_angle)
        time.sleep(1.5)
        offset_open = hx.tare(30)
        print(f"  offset (open) = {offset_open}  (shift vs closed: {offset_open - offset_closed:+d} counts"
              f" = {(offset_open - offset_closed) / scale:+.3f} kg)")
        servo.set_angle(args.closed_angle)

        print("\nUse these flags:")
        print(f"  --scale {scale:.1f} --offset-closed {offset_closed} --offset-open {offset_open}")
    finally:
        servo.set_angle(args.closed_angle)
        hx.close()


def run(args: argparse.Namespace) -> None:
    gate = HopperGate(target_kg=args.target_kg, empty_kg=args.empty_kg, jam_after_s=args.jam_after)

    hx = servo = None
    if not args.dry_run:
        from hardware.hx711 import HX711
        from hardware.servo import Servo

        hx = HX711(pins.HX711_HOPPER.dout, pins.HX711_HOPPER.sck, scale=args.scale)
        servo = Servo(channel=args.pwm_channel, chip=args.pwm_chip)
        servo.set_angle(args.closed_angle)

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    print(f"hopper loop: target={args.target_kg}kg empty={args.empty_kg}kg jam_after={args.jam_after}s dry_run={args.dry_run}")

    jam_reported = False
    last_status = 0.0
    try:
        while True:
            now = time.monotonic()
            offset = args.offset_open if gate.gate_open else args.offset_closed
            kg = hx.read_kg(offset) if hx is not None else None

            was_open = gate.gate_open
            gate.update(kg, now)

            if gate.gate_open != was_open:
                if servo is not None:
                    servo.set_angle(args.open_angle if gate.gate_open else args.closed_angle)
                print(f"{datetime.now():%H:%M:%S}  GATE {'OPEN' if gate.gate_open else 'CLOSED'}  stable={gate.stable_kg:.3f}kg")
                jam_reported = False

            if gate.jammed and not jam_reported:
                print(f"{datetime.now():%H:%M:%S}  *** JAM: gate open {args.jam_after:.0f}s without emptying ***")
                jam_reported = True

            if now - last_status >= 1.0:
                stable = "--" if gate.stable_kg is None else f"{gate.stable_kg:.3f}kg"
                raw = "--" if kg is None else f"{kg:.3f}kg"
                print(f"{datetime.now():%H:%M:%S}  {gate.state.value:16} raw={raw:>9} stable={stable:>9}{'  JAMMED' if gate.jammed else ''}")
                last_status = now

            if hx is None:
                time.sleep(0.1)   # a real read blocks until the HX711 has a sample (~10 Hz)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        if servo is not None:
            servo.set_angle(args.closed_angle)   # signal stays on after exit: gate stays shut
        if hx is not None:
            hx.close()
        print("gate closed, signal held")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--calibrate", action="store_true", help="interactive tare + scale, then exit")
    ap.add_argument("--target-kg", type=float, default=2.0)
    ap.add_argument("--empty-kg", type=float, default=0.05)
    ap.add_argument("--jam-after", type=float, default=30.0)
    ap.add_argument("--scale", type=float, default=1.0, help="raw counts per kg, from --calibrate")
    ap.add_argument("--offset-closed", type=int, default=0)
    ap.add_argument("--offset-open", type=int, default=None, help="defaults to --offset-closed")
    ap.add_argument("--dry-run", action="store_true", help="run the state machine with no hardware")
    add_hardware_args(ap)
    args = ap.parse_args()
    if args.offset_open is None:
        args.offset_open = args.offset_closed

    if args.calibrate:
        calibrate(args)
    else:
        run(args)


if __name__ == "__main__":
    main()
