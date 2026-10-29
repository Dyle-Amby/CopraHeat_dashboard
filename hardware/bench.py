"""Bench checks for the machine wiring: relays, sensors, steppers, limit switches, scales.

Meant for testing the harness before it is fixed in place. Run on the Pi from
inside CopraHeat_dashboard, with run_cords stopped (it owns the same pins):

    python -m hardware.bench relay fan                   # type on / off, the fan relay follows
    python -m hardware.bench relay heater                # same for the heater SSR (mains!)
    python -m hardware.bench sensors                     # live DS18B20 + DHT22 readings
    python -m hardware.bench limits                      # watch the 3 limit switches
    python -m hardware.bench scales                      # raw readings from all 4 HX711s
    python -m hardware.bench scale-cal great             # tare + scale for one bin cell
    python -m hardware.bench jog conveyor --steps 400    # exact step count, then stop
    python -m hardware.bench jog carriage --steps 2000 --reverse
                                                         # stops early on any limit switch
    python -m hardware.bench sort                        # type great / good / bad, carriage goes there
    python -m hardware.bench sort great bad good         # or run a fixed sequence

Start slow (the default --hz 400) and raise it once direction and wiring are
confirmed. The hopper cell's calibration lives in run_hopper --calibrate, since
it needs the gate's two tare offsets.
"""
import argparse
import json
import signal
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from hardware import carriage, pins
from hardware.limits import SWITCHES, newly_triggered
from hardware.stepper import steps_done

MOTORS = {
    "conveyor": pins.HBS57H_CONVEYOR,
    "carriage": pins.TB6600_CARRIAGE,
}

RELAYS = {
    "fan": pins.FAN_RELAY,
    "heater": pins.HEATER_SSR,
}

CELLS = {
    "hopper": pins.HX711_HOPPER,
    "great": pins.HX711_BIN_GREAT,
    "good": pins.HX711_BIN_GOOD,
    "bad": pins.HX711_BIN_BAD,
}
STATE_FILE = Path(__file__).with_name(".carriage_state.json")


def load_state() -> dict | None:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return None


def save_state(stop: str, leaving_up: bool | None) -> None:
    """leaving_up: True/False while a move is in progress, None once arrived."""
    STATE_FILE.write_text(json.dumps({"stop": stop, "leaving_up": leaving_up}))

def stamp() -> str:
    return f"{datetime.now():%H:%M:%S}"


def show_switches(state: dict[str, bool]) -> str:
    return "  ".join(f"{name}={'HIGH (pressed / wire open)' if hit else 'low'}" for name, hit in state.items())


def relay(args: argparse.Namespace) -> None:
    from gpiozero import OutputDevice

    gpio = RELAYS[args.name]
    if args.name == "heater":
        print("HEATER: this switches 220 V bulbs. Keep hands off the chamber.")
    # active_high=False: 'on' drives the pin LOW, for low-level-trigger relay boards
    out = OutputDevice(gpio, active_high=not args.active_low, initial_value=False)
    print(f"{args.name} on GPIO{gpio}{' (active-low)' if args.active_low else ''}, starting OFF. "
          "Type on / off (q to quit).")
    print("No click on 'on'? Measure GPIO -> GND (expect ~3.3 V), then the relay/SSR input side.")
    try:
        while True:
            try:
                line = input(f"{args.name}> ").strip().lower()
            except EOFError:
                break
            if line in ("q", "quit", "exit"):
                break
            if line in ("on", "1"):
                out.on()
            elif line in ("off", "0"):
                out.off()
            elif line:
                print("  on / off / q")
                continue
            level = bool(out.value) != args.active_low
            print(f"{stamp()}  {args.name} {'ON' if out.value else 'off'}  (GPIO{gpio} {'HIGH' if level else 'low'})")
    finally:
        out.off()
        out.close()
        print(f"{args.name} OFF")


def watch_sensors(args: argparse.Namespace) -> None:
    from hardware import sensors

    print("chamber = DS18B20 (GPIO4), exhaust = DHT22 (GPIO11, physical pin 23). '--' = no reading.")
    print("DS18B20 '--': w1-gpio overlay / 4.7k pull-up. DHT22 '--': dht11 overlay / wiring.")
    while True:
        chamber = sensors.read_ds18b20()
        t, rh = sensors.read_dht22()
        show = lambda v, unit: "--" if v is None else f"{v:.1f}{unit}"
        print(f"{stamp()}  chamber={show(chamber, 'C'):>7}  exhaust={show(t, 'C'):>7} {show(rh, '%'):>6}")
        time.sleep(args.interval)


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


def carriage_move(stepper, ls, target: str | None, up: bool, limit: int,
                  args: argparse.Namespace) -> tuple[str, int]:
    """Run the carriage until MoveWatch says it arrived; returns (switch it is on, steps sent)."""
    watch = carriage.MoveWatch(ls.read(), target, args.order, args.release_within, up=up)
    t0 = time.monotonic()
    status, detail = carriage.MOVING, None
    stepper.start(limit, args.hz, forward=up)
    try:
        # also wait out a short grace period in case tx_busy lags the start
        while stepper.busy or time.monotonic() - t0 < 0.1:
            status, detail = watch.update(ls.read(), steps_done(time.monotonic() - t0, args.hz, limit))
            if status != carriage.MOVING:
                break
            time.sleep(0.0005)
    finally:
        sent = stepper.stop()
    if status == carriage.ARRIVED:
        print(f"{stamp()}    at {detail} after ~{sent} steps {'up' if up else 'down'}")
        return detail, sent
    if status == carriage.FAULT:
        raise RuntimeError(detail)
    raise RuntimeError(f"no limit switch within {limit} steps: the carriage left an end stop the wrong way "
                       "(--up-reverse), stalled, or a switch is not wired (check with `bench limits`)")


def sort(args: argparse.Namespace) -> None:
    from hardware.limits import LimitSwitches
    from hardware.stepper import Stepper

    grade_stop = carriage.stops(args.order, args.bins)
    print("bins bottom -> top: " + ", ".join(f"{b} @ {s}" for b, s in zip(args.bins, args.order))
          + f"  (up = {'reverse' if args.up_reverse else 'forward'})")

    ls = LimitSwitches()
    stepper = Stepper(pins.TB6600_CARRIAGE, invert_dir=args.up_reverse)
    try:
        # steps between each pair of stops, learned per pair because the bins are
        # asymmetric; until a pair has been travelled once, only --max-steps caps a
        # move that went the wrong way past an end stop
        travel: dict[frozenset, int] = {}
        grades = list(args.grades) if args.grades else None
        first_target = grade_stop[grades[0].lower()] if grades and grades[0].lower() in grade_stop else None

        here = carriage.located_at(ls.read())
        if here is None:
            state = load_state()
            if first_target is not None and state and state["stop"] in args.order:
                last = state["stop"]
                if last != first_target:
                    up = carriage.moving_up(last, first_target, args.order)
                elif state["leaving_up"] is not None:
                    up = not state["leaving_up"]
                else:
                    up = False
                print(f"{stamp()}  carriage between stops (last at {last}): "
                      f"moving {'up' if up else 'down'} to {first_target}")
                here, _ = carriage_move(stepper, ls, first_target, up, args.max_steps, args)
            else:
                print(f"{stamp()}  carriage between stops, position unknown: homing down to nearest switch")
                here, _ = carriage_move(stepper, ls, None, False, args.max_steps, args)
        else:
            print(f"{stamp()}  carriage resting at {here}")
        save_state(here, None)

        grades_iter = iter(grades) if grades else _prompt_grades(grade_stop)
        for grade in grades_iter:
            grade = grade.lower()
            if grade not in grade_stop:
                print(f"  unknown grade {grade!r}; use one of {', '.join(grade_stop)}")
                continue
            target = grade_stop[grade]
            if target == here:
                print(f"{stamp()}  {grade} -> {target}: already there")
            else:
                pair = frozenset((here, target))
                limit = args.max_steps
                if pair in travel:
                    limit = min(limit, int(travel[pair] * 1.5) + args.release_within)
                print(f"{stamp()}  {grade} -> {target}")
                up = carriage.moving_up(here, target, args.order)
                save_state(here, up)
                here, sent = carriage_move(stepper, ls, target, up, limit, args)
                save_state(here, None)
                travel[pair] = max(travel.get(pair, 0), sent)
            if args.grades:
                time.sleep(args.pause)
    except RuntimeError as exc:
        sys.exit(f"{stamp()}  STOPPED: {exc}")
    finally:
        stepper.close()
        ls.close()


def _prompt_grades(grade_stop: dict[str, str]):
    while True:
        try:
            line = input(f"grade ({' / '.join(grade_stop)}, q to quit)> ").strip().lower()
        except EOFError:
            return


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

    r = sub.add_parser("relay", help="switch the fan relay or heater SSR by hand")
    r.add_argument("name", choices=RELAYS)
    r.add_argument("--active-low", action="store_true", help="relay board turns ON when its IN pin is LOW")
    r.set_defaults(func=relay)

    n = sub.add_parser("sensors", help="live chamber and exhaust readings")
    n.add_argument("--interval", type=float, default=2.0, help="seconds between readings (DHT22 minimum is 2)")
    n.set_defaults(func=watch_sensors)

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

    o = sub.add_parser("sort", help="send the carriage to a grade's bin")
    o.add_argument("grades", nargs="*", help="sequence to run; omit to type grades one by one")
    o.add_argument("--hz", type=float, default=400.0, help="steps per second")
    o.add_argument("--max-steps", type=int, default=20000, help="give up if no switch within this many steps")
    o.add_argument("--up-reverse", action=argparse.BooleanOptionalAction, default=True,
                   help="the motor's reverse direction raises the carriage (default: True)")
    o.add_argument("--order", nargs=3, choices=SWITCHES, default=list(carriage.ORDER), metavar="LS",
                   help="limit switches bottom to top (default: LS1 LS2 LS3)")
    o.add_argument("--bins", nargs=3, default=list(carriage.BINS), metavar="GRADE",
                   help="grade of the bin at each of those stops (default: great good bad)")
    o.add_argument("--release-within", type=int, default=400,
                   help="fault if the start switch is still pressed after this many steps")
    o.add_argument("--pause", type=float, default=1.0, help="seconds at each bin in a sequence")
    o.set_defaults(func=sort)

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
