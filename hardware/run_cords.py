"""Supervisor loop for the whole CORDS machine.

One process owns every pin. `BatchController` decides what the machine should be
doing; this applies those decisions to the SSR, the fan relay, the hopper servo
and the conveyor, and reports the hardware's answers back. Flask must never
touch GPIO -- it will reach this process through the database instead.

    python -m hardware.run_cords --simulate --speed 60 --interval 0.02 --start --exit-on-finish
    python -m hardware.run_cords --dry-run --start
    python -m hardware.run_cords --start --scale 21500 --offset-closed 84210 \
        --offset-open 83950 --closed-angle 0 --open-angle 90

Calibration values are CLI flags until the settings table exists. Replaces the
two bench loops (run_climate, run_hopper), which stay for single-subsystem work.
"""

import argparse
import signal
import sys
import time
from datetime import datetime

from hardware import pins, sensors
from hardware.batch import BatchController, Commands, Inputs
from hardware.conveyor import SimulatedConveyor
from hardware.states import BatchState

_FINISHED = (BatchState.COMPLETE, BatchState.ABORTED)

# the shortest timer the state machine runs on; a simulated tick coarser than
# this trips alarms that say nothing about the logic
TIGHTEST_TIMER_S = 5.0


class SensorCache:
    """Each sensor refreshes on its own schedule so the loop can keep ticking
    fast for the hopper: a DS18B20 conversion blocks for ~750 ms and the DHT22
    returns EAGAIN if polled under 2 s apart. Both reads still happen inline, so
    a tick is only as short as the slowest refresh that lands on it.
    """

    def __init__(self, chamber_s: float = 2.0, exhaust_s: float = 2.5):
        self.chamber_s = chamber_s
        self.exhaust_s = exhaust_s
        self.chamber_temp: float | None = None
        self.exhaust_temp: float | None = None
        self.exhaust_rh: float | None = None
        self._chamber_at = -1e9
        self._exhaust_at = -1e9

    def poll(self, now: float) -> None:
        if now - self._chamber_at >= self.chamber_s:
            self.chamber_temp = sensors.read_ds18b20()
            self._chamber_at = now
        if now - self._exhaust_at >= self.exhaust_s:
            self.exhaust_temp, self.exhaust_rh = sensors.read_dht22()
            self._exhaust_at = now


class Actuators:
    """The only object in the process that touches a pin."""

    def __init__(self, args: argparse.Namespace):
        from gpiozero import OutputDevice

        from hardware.hx711 import HX711
        from hardware.servo import Servo

        self.closed_angle = args.closed_angle
        self.open_angle = args.open_angle
        self.offset_closed = args.offset_closed
        self.offset_open = args.offset_open
        self._gate_open = False

        self.heater = OutputDevice(pins.HEATER_SSR, initial_value=False)
        self.fans = OutputDevice(pins.FAN_RELAY, initial_value=False)
        self.servo = Servo(channel=args.pwm_channel, chip=args.pwm_chip)
        self.servo.set_angle(self.closed_angle)
        self.hx = HX711(pins.HX711_HOPPER.dout, pins.HX711_HOPPER.sck, scale=args.scale)

    def read_hopper_kg(self) -> float | None:
        # the cell is mounted on the gate, so its zero shifts with gate angle
        return self.hx.read_kg(self.offset_open if self._gate_open else self.offset_closed)

    def apply(self, c: Commands) -> None:
        self.heater.value = c.heater_on
        self.fans.value = c.fans_on
        if c.gate_open != self._gate_open:
            self.servo.set_angle(self.open_angle if c.gate_open else self.closed_angle)
            self._gate_open = c.gate_open

    def close(self) -> None:
        self.heater.off()
        self.fans.off()
        # move to closed but leave the PWM running: the gate bears the copra load
        self.servo.set_angle(self.closed_angle)
        self.heater.close()
        self.fans.close()
        self.hx.close()


def fmt(value: float | None, unit: str = "C") -> str:
    return "--" if value is None else f"{value:.1f}{unit}"


def stamp() -> str:
    return f"{datetime.now():%H:%M:%S}"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--simulate", action="store_true", help="run against a fake machine; implies --dry-run")
    ap.add_argument("--speed", type=float, default=1.0, help="clock multiplier, --simulate only")
    ap.add_argument("--dry-run", action="store_true", help="read real sensors but drive no pins")
    ap.add_argument("--start", action="store_true", help="start a batch immediately instead of waiting")
    ap.add_argument("--exit-on-finish", action="store_true", help="exit once the batch completes or aborts")
    ap.add_argument("--interval", type=float, default=0.2, help="seconds of real time between ticks")
    ap.add_argument("--target-kg", type=float, default=2.0)
    ap.add_argument("--intake-advance-s", type=float, default=4.0, help="simulated conveyor move duration")
    ap.add_argument("--scale", type=float, default=1.0, help="HX711 raw counts per kg, from run_hopper --calibrate")
    ap.add_argument("--offset-closed", type=int, default=0)
    ap.add_argument("--offset-open", type=int, default=None, help="defaults to --offset-closed")
    ap.add_argument("--closed-angle", type=float, default=0.0)
    ap.add_argument("--open-angle", type=float, default=90.0)
    ap.add_argument("--pwm-chip", type=int, default=2)
    ap.add_argument("--pwm-channel", type=int, default=2)
    args = ap.parse_args(argv)

    if args.offset_open is None:
        args.offset_open = args.offset_closed
    if args.simulate:
        args.dry_run = True
        tick = args.interval * args.speed
        if tick > TIGHTEST_TIMER_S:
            print(
                f"warning: each tick advances the clock {tick:.0f} simulated seconds, past the "
                f"shortest timer in the system ({TIGHTEST_TIMER_S:.0f} s, the conveyor ack). "
                f"Timers below that will trip as artifacts -- lower --speed or --interval."
            )
    elif args.speed != 1.0:
        ap.error("--speed only makes sense with --simulate; real sensors run on a real clock")
    return args


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    controller = BatchController(target_kg=args.target_kg)
    conveyor = SimulatedConveyor(args.intake_advance_s)
    cache = None if args.simulate else SensorCache()
    io = None if args.dry_run else Actuators(args)

    sim = None
    if args.simulate:
        from hardware.simulate import SimulatedMachine

        sim = SimulatedMachine(target_kg=args.target_kg)

    # systemd stop sends SIGTERM; turn it into SystemExit so the finally block runs
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    if args.start:
        controller.start()

    mode = "simulate" if args.simulate else ("dry-run" if args.dry_run else "live")
    print(f"cords supervisor: mode={mode} speed={args.speed}x tick={args.interval}s target={args.target_kg}kg")

    commands: Commands | None = None
    started_real = time.monotonic()
    last_status = 0.0
    last_state = None
    last_alarms: frozenset[str] = frozenset()

    try:
        while True:
            real = time.monotonic()
            now = (real - started_real) * args.speed

            if sim is not None:
                sim.advance(now, commands)
                chamber, exhaust_t, exhaust_rh = sim.chamber_temp, sim.exhaust_temp, sim.exhaust_rh
                hopper_kg, sorting_complete, sorted_kg = sim.hopper_kg, sim.sorting_complete, sim.sorted_kg
            else:
                cache.poll(now)
                chamber, exhaust_t, exhaust_rh = cache.chamber_temp, cache.exhaust_temp, cache.exhaust_rh
                hopper_kg = io.read_hopper_kg() if io is not None else None
                # the sorter is not built yet; until then sorting ends by hand
                sorting_complete, sorted_kg = False, None

            commands = controller.update(
                Inputs(
                    now=now,
                    chamber_temp=chamber,
                    exhaust_temp=exhaust_t,
                    exhaust_rh=exhaust_rh,
                    hopper_kg=hopper_kg,
                    conveyor_busy=conveyor.busy,
                    sorting_complete=sorting_complete,
                    sorted_kg=sorted_kg,
                )
            )

            if io is not None:
                io.apply(commands)
            conveyor.apply(commands.conveyor, now)

            if commands.state is not last_state:
                print(f"{stamp()}  -> {commands.state.value.upper()}  (t+{now / 60:.1f} min)")
                last_state = commands.state
            for alarm in sorted(commands.alarms - last_alarms):
                print(f"{stamp()}  *** ALARM {alarm} ***")
            for alarm in sorted(last_alarms - commands.alarms):
                print(f"{stamp()}  --- cleared {alarm} ---")
            last_alarms = commands.alarms

            if real - last_status >= 2.0:
                print(
                    f"{stamp()}  {commands.state.value:9} chamber={fmt(chamber):>7} "
                    f"exhaust={fmt(exhaust_t):>7} {fmt(exhaust_rh, '%'):>6} "
                    f"hopper={fmt(hopper_kg, 'kg'):>8} "
                    f"heater={'ON ' if commands.heater_on else 'off'} "
                    f"fans={'ON ' if commands.fans_on else 'off'} "
                    f"gate={'OPEN  ' if commands.gate_open else 'closed'} "
                    f"belt={commands.conveyor.value}"
                )
                last_status = real

            if args.exit_on_finish and commands.state in _FINISHED:
                break

            time.sleep(args.interval)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        conveyor.close()
        if io is not None:
            io.close()
        record = controller.record()
        if record is not None:
            loss = "--" if record.weight_loss_pct is None else f"{record.weight_loss_pct:.1f}%"
            print(
                f"\nbatch {'ABORTED' if record.aborted else 'complete'}"
                f"{f' ({record.abort_reason})' if record.abort_reason else ''}: "
                f"duration={record.duration_s / 60:.1f} min  "
                f"drying={'--' if record.drying_s is None else f'{record.drying_s / 60:.1f} min'}  "
                f"in={fmt(record.weight_in_kg, 'kg')}  out={fmt(record.weight_out_kg, 'kg')}  loss={loss}"
            )
        print("heaters and fans OFF, gate closed")


if __name__ == "__main__":
    main()
