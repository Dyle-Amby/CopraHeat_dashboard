"""Interactive end-to-end walkthrough test for the CORDS prototype.

Designed to be run over SSH on the Raspberry Pi while watching the dashboard
on a laptop browser, with full support for manual / mock triggers:
  1. INTAKE: Mock hopper fill (gate servo physically opens) and drain (gate closes).
  2. CONVEYOR: Jog the real conveyor stepper (HBS57H) to carry batch to chamber.
  3. DRYING: Heaters (SSR) & fans (relay) activate with live sensor logging.
             Manual 'end drying' trigger moves to cooldown.
  4. COOLDOWN: Heaters cut off; fans purge heat.
  5. SORTING: Type grades ('great', 'good', 'bad') to physically drive the
              bin carriage stepper (TB6600) to each limit switch stop!
  6. COMPLETE: Saves batch history and records grade metrics to SQLite.

Usage:
    # Full hardware walkthrough on the Raspberry Pi:
    python -m hardware.walkthrough

    # Safe dry-run (no pins driven, works on Windows laptop too):
    python -m hardware.walkthrough --dry-run

    # With hardware options:
    python -m hardware.walkthrough --fan-active-low --conveyor-steps 500
"""

import argparse
import math
import os
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from hardware import carriage, db, pins, sensors
from hardware.batch import BatchController, BatchRecord, Inputs, apply_command
from hardware.climate import FanController, HeaterController
from hardware.drying import absolute_humidity
from hardware.states import BatchState, ConveyorCommand

# -----------------------------------------------------------------------------
# Timing & Constants
# -----------------------------------------------------------------------------
DEFAULT_CONVEYOR_STEPS = 400
DEFAULT_CONVEYOR_HZ = 400.0
DEFAULT_CARRIAGE_HZ = 400.0
DEFAULT_CARRIAGE_MAX_STEPS = 3000
DEFAULT_RELEASE_WITHIN = 400


def stamp() -> str:
    return f"{datetime.now():%H:%M:%S}"


def fmt_val(v: float | None, unit: str = "") -> str:
    return "--" if v is None else f"{v:.1f}{unit}"


def check_stdin_enter(timeout: float = 0.5) -> bool:
    """Non-blocking check if Enter was pressed in the terminal."""
    if sys.platform == "win32":
        import msvcrt

        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            if msvcrt.kbhit():
                ch = msvcrt.getch()
                if ch in (b"\r", b"\n"):
                    return True
            time.sleep(0.05)
        return False
    else:
        import select

        r, _, _ = select.select([sys.stdin], [], [], timeout)
        if r:
            sys.stdin.readline()
            return True
        return False


# -----------------------------------------------------------------------------
# Hardware Abstraction Layer (Graceful Fallback for Dry-Run / Non-Pi)
# -----------------------------------------------------------------------------
class HardwareHarness:
    """Encapsulates all physical actuators and sensors with dry-run support."""

    def __init__(self, args: argparse.Namespace):
        self.dry_run = args.dry_run
        self.fan_active_low = args.fan_active_low
        self.conveyor_invert = args.conveyor_invert
        self.up_reverse = args.up_reverse
        self.order = args.order
        self.bins = args.bins
        self.grade_stops = carriage.stops(self.order, self.bins)

        self._heater_dev = None
        self._fan_dev = None
        self._servo = None
        self._conveyor_stepper = None
        self._carriage_stepper = None
        self._limit_switches = None

        self.heater_on = False
        self.fans_on = False
        self.gate_open = False
        self.current_carriage_pos: str | None = None
        self.travel_cache: dict[frozenset, int] = {}

        if not self.dry_run:
            self._init_real_hardware(args)

    def _init_real_hardware(self, args: argparse.Namespace) -> None:
        try:
            from gpiozero import OutputDevice

            self._heater_dev = OutputDevice(pins.HEATER_SSR, initial_value=False)
            self._fan_dev = OutputDevice(
                pins.FAN_RELAY,
                active_high=not self.fan_active_low,
                initial_value=False,
            )
        except Exception as exc:
            print(f"Warning: could not initialize relay OutputDevices ({exc}); fallback to simulated.")

        try:
            from hardware.servo import Servo

            chip = args.pwm_chip
            chan = args.pwm_channel
            self._servo = Servo(channel=chan, chip=chip)
            self._servo.set_angle(args.closed_angle)
        except Exception as exc:
            print(f"Warning: could not initialize hardware PWM servo ({exc}); fallback to simulated.")

        try:
            from hardware.limits import LimitSwitches
            from hardware.stepper import Stepper

            self._conveyor_stepper = Stepper(pins.HBS57H_CONVEYOR, invert_dir=self.conveyor_invert)
            self._carriage_stepper = Stepper(pins.TB6600_CARRIAGE, invert_dir=self.up_reverse)
            self._limit_switches = LimitSwitches()
        except Exception as exc:
            print(f"Warning: could not initialize steppers/limits ({exc}); fallback to simulated.")

    def set_heater(self, on: bool) -> None:
        self.heater_on = on
        if self._heater_dev is not None:
            self._heater_dev.value = on
        print(f"{stamp()}  [HEATER] {'ON (220V SSR active)' if on else 'off'}")

    def set_fans(self, on: bool) -> None:
        self.fans_on = on
        if self._fan_dev is not None:
            self._fan_dev.value = on
        print(f"{stamp()}  [FANS]   {'ON (Relay active)' if on else 'off'}")

    def set_gate(self, open_gate: bool, open_angle: float = 90.0, closed_angle: float = 0.0) -> None:
        self.gate_open = open_gate
        angle = open_angle if open_gate else closed_angle
        if self._servo is not None:
            self._servo.set_angle(angle)
        print(f"{stamp()}  [GATE]   {'OPENED' if open_gate else 'CLOSED'} (Servo angle: {angle}°)")

    def jog_conveyor(self, steps: int, hz: float = DEFAULT_CONVEYOR_HZ) -> None:
        print(f"{stamp()}  [CONVEYOR] Running {steps} steps at {hz:g} Hz...")
        if self._conveyor_stepper is not None:
            self._conveyor_stepper.start(steps, hz, forward=True)
            t0 = time.monotonic()
            while self._conveyor_stepper.busy or time.monotonic() - t0 < 0.1:
                time.sleep(0.01)
            self._conveyor_stepper.stop()
        else:
            time.sleep(steps / hz if hz > 0 else 1.0)
        print(f"{stamp()}  [CONVEYOR] Advance move complete.")

    def move_carriage_to_grade(
        self,
        grade: str,
        hz: float = DEFAULT_CARRIAGE_HZ,
        max_steps: int = DEFAULT_CARRIAGE_MAX_STEPS,
        release_within: int = DEFAULT_RELEASE_WITHIN,
    ) -> str:
        grade = grade.lower()
        if grade not in self.grade_stops:
            raise ValueError(f"Unknown grade '{grade}'. Choose from: {', '.join(self.grade_stops.keys())}")
        target_switch = self.grade_stops[grade]

        if self._carriage_stepper is None or self._limit_switches is None:
            self.current_carriage_pos = target_switch
            print(f"{stamp()}  [CARRIAGE (Simulated)] Moved to {target_switch} for grade '{grade.upper()}'.")
            return target_switch

        # Real hardware carriage move using limits
        from hardware.stepper import steps_done

        here = carriage.located_at(self._limit_switches.read())
        if here is None:
            print(f"{stamp()}  [CARRIAGE] Between stops: homing down to nearest limit switch...")
            here, _ = self._exec_carriage_move(None, up=False, limit=max_steps, hz=hz, release_within=release_within)
        self.current_carriage_pos = here

        if target_switch == here:
            print(f"{stamp()}  [CARRIAGE] Already at {target_switch} ({grade.upper()} bin).")
            return here

        pair = frozenset((here, target_switch))
        limit = max_steps
        if pair in self.travel_cache:
            limit = min(limit, int(self.travel_cache[pair] * 1.5) + release_within)

        up = carriage.moving_up(here, target_switch, self.order)
        print(f"{stamp()}  [CARRIAGE] Moving from {here} -> {target_switch} ({'UP' if up else 'DOWN'})...")
        arrived, sent = self._exec_carriage_move(
            target_switch, up=up, limit=limit, hz=hz, release_within=release_within
        )
        self.travel_cache[pair] = max(self.travel_cache.get(pair, 0), sent)
        self.current_carriage_pos = arrived
        return arrived

    def _exec_carriage_move(
        self,
        target: str | None,
        up: bool,
        limit: int,
        hz: float,
        release_within: int,
    ) -> tuple[str, int]:
        from hardware.stepper import steps_done

        ls = self._limit_switches
        stepper = self._carriage_stepper
        watch = carriage.MoveWatch(ls.read(), target, self.order, release_within)

        t0 = time.monotonic()
        status, detail = carriage.MOVING, None
        stepper.start(limit, hz, forward=up)
        try:
            while stepper.busy or time.monotonic() - t0 < 0.1:
                status, detail = watch.update(ls.read(), steps_done(time.monotonic() - t0, hz, limit))
                if status != carriage.MOVING:
                    break
                time.sleep(0.001)
        finally:
            sent = stepper.stop()

        if status == carriage.ARRIVED:
            print(f"{stamp()}  [CARRIAGE] Arrived at {detail} (~{sent} steps).")
            return detail, sent
        if status == carriage.FAULT:
            raise RuntimeError(f"Carriage fault: {detail}")
        raise RuntimeError(f"Carriage failed to reach stop within {limit} steps.")

    def close(self) -> None:
        self.set_heater(False)
        self.set_fans(False)
        if self._heater_dev is not None:
            self._heater_dev.close()
        if self._fan_dev is not None:
            self._fan_dev.close()
        if self._conveyor_stepper is not None:
            self._conveyor_stepper.close()
        if self._carriage_stepper is not None:
            self._carriage_stepper.close()
        if self._limit_switches is not None:
            self._limit_switches.close()


# -----------------------------------------------------------------------------
# Simulated Sensor Values (when real sensors not connected)
# -----------------------------------------------------------------------------
class SensorSimulator:
    def __init__(self, ambient_c: float = 28.0):
        self.ambient_c = ambient_c
        self.chamber_temp = ambient_c
        self.exhaust_rh = 75.0

    def update(self, heater_on: bool, fans_on: bool, dt: float) -> tuple[float, float, float]:
        target = 68.0 if heater_on else self.ambient_c
        rate = 0.08 if heater_on else (0.15 if fans_on else 0.04)
        self.chamber_temp += (target - self.chamber_temp) * min(1.0, dt * rate)
        exhaust_temp = max(self.ambient_c, self.chamber_temp - 4.0)
        if heater_on and self.chamber_temp > 45.0:
            self.exhaust_rh = max(35.0, self.exhaust_rh - dt * 0.05)
        else:
            self.exhaust_rh = min(75.0, self.exhaust_rh + dt * 0.02)
        return self.chamber_temp, exhaust_temp, self.exhaust_rh


# -----------------------------------------------------------------------------
# Interactive Walkthrough Controller
# -----------------------------------------------------------------------------
class WalkthroughSession:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.harness = HardwareHarness(args)
        self.sim_sensors = SensorSimulator()

        self.db_conn = None
        if not args.no_db:
            self.db_conn = db.connect(args.db)
            db.init(self.db_conn)
            # Ensure no conflicting supervisor is running
            other = db.live_state(self.db_conn)
            if other is not None and other["running"]:
                sys.exit(
                    f"Error: supervisor (pid {other['supervisor_pid']}) is already active. "
                    "Stop it before starting the walkthrough."
                )
            db.mark_interrupted_batches(self.db_conn)
            db.expire_pending_commands(self.db_conn)

        self.controller = BatchController(target_kg=args.target_kg)
        self.batch_id: int | None = None
        self.running = True
        self.stop_requested = False

        # Live readings
        self.chamber_c: float | None = 28.0
        self.exhaust_c: float | None = 25.0
        self.exhaust_rh: float | None = 70.0
        self.hopper_kg: float | None = 0.0
        self.conveyor_busy = False
        self.sorting_complete = False
        self.sorted_kg: float | None = None

        # Sorting grade counters
        self.grade_counts = {"great": 0, "good": 0, "bad": 0}

        # Background DB sync thread
        self.sync_thread = threading.Thread(target=self._sync_loop, daemon=True)
        self.sync_thread.start()

    def _sync_loop(self) -> None:
        """Keeps supervisor heartbeat fresh in SQLite and processes Web UI commands."""
        pid = os.getpid()
        last_sample = time.monotonic()
        while self.running:
            try:
                now_mono = time.monotonic()

                # 1. Process any incoming Web UI commands from control panel
                if self.db_conn is not None:
                    for row in db.pending_commands(self.db_conn):
                        try:
                            msg = apply_command(self.controller, row["name"], row["payload"])
                            db.resolve_command(self.db_conn, row["id"], db.DONE, msg)
                            print(f"\n{stamp()}  [WEB UI COMMAND] {row['name']}: {msg}")
                        except Exception as exc:
                            db.resolve_command(self.db_conn, row["id"], db.REJECTED, str(exc))
                            print(f"\n{stamp()}  [WEB UI COMMAND REFUSED] {row['name']}: {exc}")

                # 2. Update real or simulated sensor values
                if not self.args.dry_run:
                    real_c = sensors.read_ds18b20()
                    real_ext_t, real_ext_rh = sensors.read_dht22()
                    if real_c is not None:
                        self.chamber_c = real_c
                    if real_ext_t is not None and real_ext_rh is not None:
                        self.exhaust_c, self.exhaust_rh = real_ext_t, real_ext_rh

                if self.chamber_c is None or self.args.dry_run:
                    self.chamber_c, self.exhaust_c, self.exhaust_rh = self.sim_sensors.update(
                        self.harness.heater_on, self.harness.fans_on, dt=0.5
                    )

                ah = (
                    absolute_humidity(self.exhaust_c, self.exhaust_rh)
                    if self.exhaust_c and self.exhaust_rh
                    else None
                )

                # 3. Update machine_state table (Heartbeat)
                if self.db_conn is not None:
                    db.update_live(
                        self.db_conn,
                        state=self.controller.state.value,
                        supervisor_pid=pid,
                        batch_id=self.batch_id,
                        elapsed_s=self.controller.elapsed_s(now_mono),
                        target_kg=self.controller.target_kg,
                        chamber_c=self.chamber_c,
                        exhaust_c=self.exhaust_c,
                        exhaust_rh=self.exhaust_rh,
                        exhaust_ah=ah,
                        hopper_kg=self.hopper_kg,
                        heater_on=self.harness.heater_on,
                        fans_on=self.harness.fans_on,
                        gate_open=self.harness.gate_open,
                        conveyor=(
                            ConveyorCommand.INTAKE_ADVANCE.value
                            if self.conveyor_busy
                            else ConveyorCommand.STOP.value
                        ),
                        alarms=set(),
                    )

                    # Log sensor samples every 5 seconds for chart
                    if now_mono - last_sample >= 5.0 and self.batch_id is not None:
                        db.log_sample(
                            self.db_conn,
                            batch_id=self.batch_id,
                            state=self.controller.state.value,
                            elapsed_s=self.controller.elapsed_s(now_mono),
                            chamber_c=self.chamber_c,
                            exhaust_c=self.exhaust_c,
                            exhaust_rh=self.exhaust_rh,
                            exhaust_ah=ah,
                            hopper_kg=self.hopper_kg,
                            heater_on=self.harness.heater_on,
                            fans_on=self.harness.fans_on,
                        )
                        last_sample = now_mono

            except Exception:
                pass
            time.sleep(0.5)

    def tick_controller(self) -> None:
        """Feeds current inputs to the state machine and commands hardware."""
        now_mono = time.monotonic()
        commands = self.controller.update(
            Inputs(
                now=now_mono,
                chamber_temp=self.chamber_c,
                exhaust_temp=self.exhaust_c,
                exhaust_rh=self.exhaust_rh,
                hopper_kg=self.hopper_kg,
                conveyor_busy=self.conveyor_busy,
                sorting_complete=self.sorting_complete,
                sorted_kg=self.sorted_kg,
            )
        )
        # Apply relay decisions
        if commands.state is BatchState.DRYING:
            self.harness.set_heater(commands.heater_on)
            self.harness.set_fans(commands.fans_on)
        elif commands.state is BatchState.COOLDOWN:
            self.harness.set_heater(False)
            self.harness.set_fans(True)  # Purge residual heat
        else:
            self.harness.set_heater(False)
            self.harness.set_fans(False)

        if self.db_conn is not None and self.batch_id is not None:
            db.set_batch_state(self.db_conn, self.batch_id, commands.state.value)

    # -------------------------------------------------------------------------
    # Guided Walkthrough Steps
    # -------------------------------------------------------------------------
    def run(self) -> None:
        print("\n" + "=" * 65)
        print("   CORDS INTERACTIVE HARDWARE WALKTHROUGH")
        print("   Mode: " + ("DRY-RUN (Simulated pins)" if self.args.dry_run else "LIVE HARDWARE"))
        print("   Dashboard URL: http://<this-pi-ip>:5000")
        print("=" * 65 + "\n")

        try:
            self._step_intake()
            self._step_drying()
            self._step_cooldown()
            self._step_sorting()
            self._step_complete()
        except (KeyboardInterrupt, SystemExit):
            print("\nWalkthrough interrupted by user.")
        finally:
            self.cleanup()

    def _step_intake(self) -> None:
        print("\n--- PHASE 1: INTAKE & DISPENSING ---")
        prompt = input(f"Press [Enter] to start batch with target {self.controller.target_kg} kg (or type weight): ").strip()
        if prompt:
            try:
                self.controller.target_kg = float(prompt)
            except ValueError:
                pass

        # Start batch in DB
        self.controller.start()
        self.tick_controller()
        if self.db_conn is not None:
            self.batch_id = db.start_batch(self.db_conn, target_kg=self.controller.target_kg)
            print(f"{stamp()}  Batch #{self.batch_id} created in database (State: INTAKE).")

        # Mock hopper filling
        print(f"\nHopper gate is currently CLOSED. Waiting for {self.controller.target_kg} kg copra...")
        input("Press [Enter] to simulate copra loaded into hopper...")
        self.hopper_kg = self.controller.target_kg
        # StableWeight filter requires 5 agreeing samples
        for _ in range(5):
            self.tick_controller()
            time.sleep(0.05)

        # Gate opens physically
        self.harness.set_gate(True, self.args.open_angle, self.args.closed_angle)
        print(f">>> HOPPER GATE OPEN! Copra dropping onto conveyor belt.")

        # Mock hopper emptying
        input("\nPress [Enter] to simulate hopper empty (0.0 kg)...")
        self.hopper_kg = 0.0
        for _ in range(5):
            self.tick_controller()
            time.sleep(0.05)

        # Gate closes physically
        self.harness.set_gate(False, self.args.open_angle, self.args.closed_angle)
        print(f">>> HOPPER GATE CLOSED!")

        # Advance conveyor
        print("\nAdvancing conveyor to carry copra into drying chamber...")
        self.conveyor_busy = True
        self.tick_controller()

        self.harness.jog_conveyor(self.args.conveyor_steps, self.args.conveyor_hz)
        self.conveyor_busy = False
        self.tick_controller()
        print(f"{stamp()}  Conveyor move finished. Transitioned to DRYING!")

    def _step_drying(self) -> None:
        print("\n--- PHASE 2: DRYING & CLIMATE CONTROL ---")
        print("Heater SSR and fan relay are now under closed-loop control (60-70°C).")
        print("Watch live temperature updates on the dashboard.")
        print("Commands: Press [Enter] or click 'End drying' on the Web Control Panel to finish drying.")

        while self.controller.state is BatchState.DRYING:
            self.tick_controller()
            temp_str = fmt_val(self.chamber_c, "°C")
            fan_str = "ON" if self.harness.fans_on else "off"
            heat_str = "ON" if self.harness.heater_on else "off"
            print(f"\r{stamp()}  Chamber: {temp_str} | Heaters: {heat_str} | Fans: {fan_str}  [Press Enter to end drying]", end="", flush=True)

            # Check if user pressed Enter on terminal (non-blocking poll)
            if check_stdin_enter(timeout=0.5):
                print("\nEnding drying by terminal trigger...")
                self.controller.end_drying()
                break

        print("\n")
        self.tick_controller()

    def _step_cooldown(self) -> None:
        print("\n--- PHASE 3: COOLDOWN PURGE ---")
        print("Heaters cut off. Fans purge residual heat until chamber temp < 40°C.")
        while self.controller.state is BatchState.COOLDOWN:
            self.tick_controller()
            print(f"\r{stamp()}  Cooling chamber: {fmt_val(self.chamber_c, '°C')} (Target < 40°C) [Press Enter to skip purge]", end="", flush=True)
            if check_stdin_enter(timeout=0.5):
                print("\nCooldown purge acknowledged.")
                # Force simulated temp below 40°C to allow state transition
                self.chamber_c = 38.0
                self.sim_sensors.chamber_temp = 38.0
                self.tick_controller()
                break
        print("\nChamber safely cooled. Transitioning to SORTING!")

    def _step_sorting(self) -> None:
        print("\n--- PHASE 4: SORTING (SEGREGATION MECHANISM) ---")
        print("Enter grades sequentially to command the bin carriage stepper:")
        print("  - 'great' : moves carriage to GREAT bin (LS1 stop)")
        print("  - 'good'  : moves carriage to GOOD bin (LS2 stop)")
        print("  - 'bad'   : moves carriage to BAD bin (LS3 stop)")
        print("  - 'done'  : finishes sorting and closes the batch")

        while self.controller.state is BatchState.SORTING:
            self.tick_controller()
            try:
                cmd = input("\nEnter copra grade (great / good / bad / done): ").strip().lower()
            except EOFError:
                break

            if cmd in ("done", "finish", "q", "quit"):
                self.controller.finish_sorting()
                self.tick_controller()
                break

            if cmd in ("great", "good", "bad"):
                try:
                    target_stop = self.harness.move_carriage_to_grade(
                        cmd,
                        hz=self.args.carriage_hz,
                        max_steps=self.args.carriage_max_steps,
                    )
                    self.grade_counts[cmd] += 1
                    print(f"Recorded 1x {cmd.upper()}. Total: {self.grade_counts}")
                except Exception as exc:
                    print(f"Carriage motion error: {exc}")
            else:
                print("Invalid input. Type 'great', 'good', 'bad', or 'done'.")

    def _step_complete(self) -> None:
        print("\n--- BATCH COMPLETE ---")
        self.tick_controller()
        rec = self.controller.record()

        if self.db_conn is not None and self.batch_id is not None and rec is not None:
            db.finish_batch(self.db_conn, self.batch_id, rec, "complete")
            # Update final grade counts in database
            self.db_conn.execute(
                "UPDATE batches SET great = ?, good = ?, bad = ? WHERE id = ?",
                (
                    self.grade_counts["great"],
                    self.grade_counts["good"],
                    self.grade_counts["bad"],
                    self.batch_id,
                ),
            )
            print(f"{stamp()}  Batch #{self.batch_id} summary saved to database:")
            print(f"  - Total Duration : {rec.duration_s / 60:.1f} min")
            print(f"  - Drying Time    : {'--' if rec.drying_s is None else f'{rec.drying_s / 60:.1f} min'}")
            print(f"  - Grades Sorted  : {self.grade_counts['great']} Great, {self.grade_counts['good']} Good, {self.grade_counts['bad']} Bad")
            print("Check Batch History and Sensor Logs on your Dashboard!\n")

    def cleanup(self) -> None:
        self.running = False
        self.harness.close()
        if self.db_conn is not None:
            db.mark_supervisor_stopped(self.db_conn)
            self.db_conn.close()
        print(f"{stamp()}  Outputs safely shut off, supervisor marked stopped. Done.")


# -----------------------------------------------------------------------------
# CLI Entry Point
# -----------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true", help="simulate all GPIO pins (safe for laptop)")
    p.add_argument("--db", default=None, help=f"database path (default {db.DEFAULT_PATH})")
    p.add_argument("--no-db", action="store_true", help="run without database updates")
    p.add_argument("--fan-active-low", action="store_true", help="relay turns ON on LOW level")
    p.add_argument("--target-kg", type=float, default=2.0, help="target batch weight in kg")

    # Conveyor stepper options
    p.add_argument("--conveyor-steps", type=int, default=DEFAULT_CONVEYOR_STEPS, help="intake step count")
    p.add_argument("--conveyor-hz", type=float, default=DEFAULT_CONVEYOR_HZ, help="conveyor step frequency")
    p.add_argument("--conveyor-invert", action="store_true", help="invert conveyor direction")

    # Carriage stepper & limit switch options
    p.add_argument("--carriage-hz", type=float, default=DEFAULT_CARRIAGE_HZ, help="carriage step frequency")
    p.add_argument("--carriage-max-steps", type=int, default=DEFAULT_CARRIAGE_MAX_STEPS, help="carriage max move cap")
    p.add_argument("--up-reverse", action="store_true", help="invert carriage direction (up is reverse)")
    p.add_argument("--order", nargs="+", default=list(carriage.ORDER), help="switch names bottom to top")
    p.add_argument("--bins", nargs="+", default=list(carriage.BINS), help="bins bottom to top")

    # Hopper servo options
    p.add_argument("--pwm-chip", type=int, default=2, help="RP1 PWM chip for servo")
    p.add_argument("--pwm-channel", type=int, default=2, help="PWM channel for GPIO18")
    p.add_argument("--open-angle", type=float, default=90.0, help="servo gate open angle")
    p.add_argument("--closed-angle", type=float, default=0.0, help="servo gate closed angle")

    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    session = WalkthroughSession(args)
    session.run()


if __name__ == "__main__":
    main()
