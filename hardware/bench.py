"""Bench checks for the sorting-side wiring: steppers, limit switches, scales.

Meant for testing the harness before it is fixed in place. Run on the Pi from
inside CopraHeat_dashboard, with run_cords stopped (it owns the same pins):

    python -m hardware.bench limits                      # watch the 3 limit switches
    python -m hardware.bench scales                      # raw readings from all 4 HX711s
    python -m hardware.bench scale-cal great             # tare + scale for one bin cell
    python -m hardware.bench jog conveyor --steps 400    # exact step count, then stop
    python -m hardware.bench jog carriage --steps 2000 --reverse
                                                         # stops early on any limit switch

Start slow (the default --hz 400) and raise it once direction and wiring are
confirmed. The hopper cell's calibration lives in run_hopper --calibrate, since
it needs the gate's two tare offsets.
"""

import argparse
import signal
import sys
import time
from collections import deque
from datetime import datetime

from hardware import pins
from hardware.limits import SWITCHES, newly_triggered
from hardware.stepper import steps_done

MOTORS = {
    "conveyor": pins.HBS57H_CONVEYOR,
    "carriage": pins.TB6600_CARRIAGE,
}

CELLS = {
    "hopper": pins.HX711_HOPPER,
    "great": pins.HX711_BIN_GREAT,
    "good": pins.HX711_BIN_GOOD,
    "bad": pins.HX711_BIN_BAD,
}


def stamp() -> str:
    return f"{datetime.now():%H:%M:%S}"


def show_switches(state: dict[str, bool]) -> str:
    return "  ".join(f"{name}={'HIGH (pressed / wire open)' if hit else 'low'}" for name, hit in state.items())


def watch_limits(args: argparse.Namespace) -> None:
    from hardware.limits import LimitSwitches

    ls = LimitSwitches()
    print("Press each switch by hand, then unplug its connector: both must read HIGH.")
    print("Pins: " + ", ".join(f"{name}=GPIO{gpio}" for name, gpio in SWITCHES.items()))
    prev = None
    try:
        while True:
            state = ls.read()
            if state != prev:
                print(f"{stamp()}  {show_switches(state)}")
                prev = state
            time.sleep(0.01)
    finally:
        ls.close()


def jog(args: argparse.Namespace) -> None:
    from hardware.stepper import Stepper

    guarded = args.motor == "carriage" and not args.ignore_limits
    ls = None
    held: list[str] = []
    if guarded:
        from hardware.limits import LimitSwitches

        ls = LimitSwitches()
        start = ls.read()
        held = [name for name, hit in start.items() if hit]
        if len(held) == len(SWITCHES):
            ls.close()
            sys.exit("all three limit switches read HIGH: more likely a missing GND/common wire than "
                     "a carriage on every stop. Check with `bench limits` first.")
        if held and not args.from_switch:
            ls.close()
            sys.exit(f"{', '.join(held)} already pressed. If this move drives the carriage AWAY from it, "
                     "re-run with --from-switch.")

    stepper = Stepper(MOTORS[args.motor], invert_dir=args.invert_dir)
    direction = "reverse" if args.reverse else "forward"
    print(f"{stamp()}  {args.motor}: {args.steps} steps {direction} at {args.hz:g} Hz"
          f"{'' if guarded else ' (no limit-switch guard)'}")
    reason = None
    t0 = time.monotonic()
    try:
        stepper.start(args.steps, args.hz, forward=not args.reverse)
        prev = start if guarded else None
        # also wait out a short grace period in case tx_busy lags the start
        while stepper.busy or time.monotonic() - t0 < 0.1:
            if ls is not None:
                state = ls.read()
                hit = newly_triggered(prev, state)
                if hit:
                    reason = f"limit switch {', '.join(hit)}"
                    break
                sent = steps_done(time.monotonic() - t0, args.hz, args.steps)
                if held and sent >= args.release_within and all(state[name] for name in held):
                    reason = (f"{', '.join(held)} never releasing (driving INTO the stop? "
                              "try --reverse)")
                    break
                prev = state
            time.sleep(0.0005)
    except KeyboardInterrupt:
        reason = "Ctrl+C"
    finally:
        sent = stepper.stop()
        stepper.close()
        if ls is not None:
            ls.close()
    if reason is None:
        print(f"{stamp()}  done: {args.steps} steps sent")
    else:
        print(f"{stamp()}  STOPPED by {reason} after ~{sent} steps (estimated from time)")


def watch_scales(args: argparse.Namespace) -> None:
    from hardware.hx711 import HX711

    names = args.cells or list(CELLS)
    cells = {name: HX711(*CELLS[name]) for name in names}
    history = {name: deque(maxlen=10) for name in names}
    print("raw counts (± = spread of last 10). Press on each cell: its number must move.")
    print("'timeout' = power/DOUT/SCK wiring; 'railed' = load-cell side (E+/E-/A+/A-).")
    try:
        while True:
            parts = []
            for name, hx in cells.items():
                raw = hx.read_raw()
                if raw is None:
                    parts.append(f"{name} -- {hx.last_fault}")
                    continue
                history[name].append(raw)
                spread = max(history[name]) - min(history[name])
                parts.append(f"{name} {raw:>9} (±{spread})")
            print(f"{stamp()}  " + "  |  ".join(parts))
    finally:
        for hx in cells.values():
            hx.close()


def calibrate_scale(args: argparse.Namespace) -> None:
    from hardware.hx711 import HX711

    hx = HX711(*CELLS[args.cell])
    try:
        input(f"{args.cell} bin empty. Press Enter to tare... ")
        offset = hx.tare(30)
        if offset is None:
            sys.exit(f"no readings ({hx.last_fault}); run `bench scales --cells {args.cell}` first")
        print(f"  offset = {offset}")

        kg = float(input("Place a known weight in the bin and enter its mass in kg: "))
        raw = hx.read_raw_median(30)
        if raw is None:
            sys.exit(f"no readings ({hx.last_fault})")
        scale = (raw - offset) / kg
        print(f"  raw = {raw}  ->  scale = {scale:.1f} counts/kg")
        if abs(scale) < 1000:
            print("  WARNING: very small scale. Is the weight actually on this cell?")
        if scale < 0:
            print("  note: negative scale means A+/A- are swapped. It still works, but keep the sign.")
        print(f"\n{args.cell}: scale={scale:.1f} offset={offset}")
    finally:
        hx.close()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)

    sub.add_parser("limits", help="watch the three limit switches").set_defaults(func=watch_limits)

    j = sub.add_parser("jog", help="move one stepper an exact number of steps")
    j.add_argument("motor", choices=MOTORS)
    j.add_argument("--steps", type=int, default=200)
    j.add_argument("--hz", type=float, default=400.0, help="steps per second")
    j.add_argument("--reverse", action="store_true")
    j.add_argument("--invert-dir", action="store_true", help="swap what counts as forward")
    j.add_argument("--from-switch", action="store_true",
                   help="allow starting while a limit switch is pressed (move must leave it)")
    j.add_argument("--release-within", type=int, default=400,
                   help="with --from-switch: stop if the start switch is still pressed after this many steps")
    j.add_argument("--ignore-limits", action="store_true", help="carriage only: no limit-switch guard")
    j.set_defaults(func=jog)

    s = sub.add_parser("scales", help="raw readings from the HX711s")
    s.add_argument("--cells", nargs="+", choices=CELLS)
    s.set_defaults(func=watch_scales)

    c = sub.add_parser("scale-cal", help="tare + scale one bin load cell")
    c.add_argument("cell", choices=[name for name in CELLS if name != "hopper"])
    c.set_defaults(func=calibrate_scale)

    args = ap.parse_args(argv)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        args.func(args)
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        if "busy" in str(exc).lower():
            sys.exit(f"{exc}: is run_cords or another bench script holding these pins? Stop it first.")
        raise


if __name__ == "__main__":
    main()
