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
import json
import os
import signal
import sys
import time
from datetime import datetime

from hardware import db, pins, sensors
from hardware.batch import BatchController, Commands, Inputs, apply_command
from hardware.health import ComponentHealthRegistry
from hardware.motion import CarriageDriver, SimulatedCarriage, SimulatedConveyor, StepperConveyor
from hardware.sorting import SortingFlow
from hardware.drying import absolute_humidity
from hardware.states import BatchState, ConveyorCommand

_LIVE = (BatchState.INTAKE, BatchState.DRYING, BatchState.COOLDOWN, BatchState.SORTING)
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
        self.hx_bins = {
            "great": HX711(pins.HX711_BIN_GREAT.dout, pins.HX711_BIN_GREAT.sck, scale=args.scale),
            "good":  HX711(pins.HX711_BIN_GOOD.dout,  pins.HX711_BIN_GOOD.sck,  scale=args.scale),
            "bad":   HX711(pins.HX711_BIN_BAD.dout,   pins.HX711_BIN_BAD.sck,   scale=args.scale),
        }

    def read_hopper_kg(self) -> float | None:
        # the cell is mounted on the gate, so its zero shifts with gate angle
        return self.hx.read_kg(self.offset_open if self._gate_open else self.offset_closed)

    def read_bin_kg(self, target_bin: str | None = None) -> float | None:
        """Read a bin scale by bin name ('great', 'good', 'bad')."""
        if target_bin is None:
            return None
        hx = self.hx_bins.get(target_bin)
        if hx is None:
            return None
        try:
            return hx.read_kg()
        except Exception:
            return None

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
        for hx in self.hx_bins.values():
            hx.close()


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
    ap.add_argument("--intake-advance-s", type=float, default=4.0, help="simulated conveyor move duration")

    ap.add_argument("--db", default=None, help=f"database path (default {db.DEFAULT_PATH})")
    ap.add_argument("--no-db", action="store_true", help="run without a database, for bench work")
    ap.add_argument("--live-interval", type=float, default=1.0, help="seconds of real time between heartbeats")
    ap.add_argument("--save-settings", action="store_true", help="write the flags passed here into the settings table")

    # these default to None on purpose: unset means "use the settings table"
    cal = ap.add_argument_group("calibration (overrides the settings table for this run)")
    cal.add_argument("--target-kg", type=float, default=None)
    cal.add_argument("--scale", type=float, default=None, help="HX711 raw counts per kg, from run_hopper --calibrate")
    cal.add_argument("--offset-closed", type=int, default=None)
    cal.add_argument("--offset-open", type=int, default=None, help="separate value; the cell sits on the gate")
    cal.add_argument("--closed-angle", type=float, default=None)
    cal.add_argument("--open-angle", type=float, default=None)
    cal.add_argument("--pwm-chip", type=int, default=None)
    cal.add_argument("--pwm-channel", type=int, default=None)
    cal.add_argument("--sample-interval", type=float, default=None, help="seconds between sensor_log rows")

    gcal = ap.add_argument_group("vision grading configuration (unmeasured placeholders)")
    gcal.add_argument("--sample-mode", choices=["jog", "static"], default=None, help="jog (production) or static (dev-only)")
    gcal.add_argument("--n-samples", type=int, default=None, help="target distinct-position valid frames")
    gcal.add_argument("--min-valid", type=int, default=None, help="minimum valid frames for decision")
    gcal.add_argument("--max-attempts", type=int, default=None, help="max total camera attempts")
    gcal.add_argument("--retry-max", type=int, default=None, help="retries at same position")
    gcal.add_argument("--steps-per-sample", type=int, default=None, help="conveyor steps between captures")
    gcal.add_argument("--sample-hz", type=float, default=None, help="jog frequency")
    gcal.add_argument("--settle-s", type=float, default=None, help="settle delay after jog")
    gcal.add_argument("--tau-bad", type=float, default=None, help="visual contamination guard threshold")
    gcal.add_argument("--min-confidence", type=float, default=None, help="min top confidence")
    gcal.add_argument("--blur-threshold", type=float, default=None, help="min Laplacian blur variance")
    gcal.add_argument("--max-rejected-ratio", type=float, default=None, help="max rejected ratio before review")
    gcal.add_argument("--chamber-to-camera-steps", type=int, default=None, help="steps from chamber mid to camera point")
    gcal.add_argument("--camera-to-discharge-steps", type=int, default=None, help="steps from camera to roller edge")
    gcal.add_argument("--conveyor-p00", type=int, default=None, help="conveyor driver pulses per revolution (P00)")
    gcal.add_argument("--conveyor-hz", type=float, default=None, help="conveyor step frequency (Hz)")
    gcal.add_argument("--discharge-total-steps", type=int, default=None, help="steps to completely discharge batch")
    gcal.add_argument("--safety-margin-steps", type=int, default=None, help="buffer before discharge edge")
    gcal.add_argument("--discharge-timeout-s", type=float, default=None, help="discharge timeout in seconds")
    gcal.add_argument("--carriage-move-timeout-s", type=float, default=None, help="carriage reposition timeout in seconds")
    gcal.add_argument("--hopper-to-chamber-steps", type=int, default=None,
                      help="steps from hopper drop-off to chamber inlet (0 = uncalibrated; real driver will refuse)")
    gcal.add_argument("--intake-advance-timeout-s", type=float, default=None,
                      help="hard timeout for the intake conveyor advance (seconds); must be > conveyor_ack_s")

    fcal = ap.add_argument_group("fan airflow configuration")
    fcal.add_argument("--fan-warmup-s", type=float, default=None, help="fans off duration in DRYING before pulses (s)")
    fcal.add_argument("--fan-pulse-on-s", type=float, default=None, help="fan pulse on duration (s)")
    fcal.add_argument("--fan-pulse-period-s", type=float, default=None, help="fan pulse period (s)")
    fcal.add_argument("--fan-rh-on", type=float, default=None, help="exhaust RH high threshold to force fans on")
    fcal.add_argument("--fan-rh-off", type=float, default=None, help="exhaust RH low threshold to allow fans off")

    args = ap.parse_args(argv)

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


# CLI flag -> settings key and its type
SETTING_FLAGS = {
    "target_kg": ("target_kg", float),
    "scale": ("hx711_scale", float),
    "offset_closed": ("hx711_offset_closed", int),
    "offset_open": ("hx711_offset_open", int),
    "closed_angle": ("servo_closed_angle", float),
    "open_angle": ("servo_open_angle", float),
    "pwm_chip": ("pwm_chip", int),
    "pwm_channel": ("pwm_channel", int),
    "sample_interval": ("sample_interval_s", float),
    # Vision grading
    "sample_mode": ("sample_mode", str),
    "n_samples": ("n_samples", int),
    "min_valid": ("min_valid", int),
    "max_attempts": ("max_attempts", int),
    "retry_max": ("retry_max", int),
    "steps_per_sample": ("steps_per_sample", int),
    "sample_hz": ("sample_hz", float),
    "settle_s": ("settle_s", float),
    "tau_bad": ("tau_bad", float),
    "min_confidence": ("min_confidence", float),
    "blur_threshold": ("blur_threshold", float),
    "max_rejected_ratio": ("max_rejected_ratio", float),
    "camera_to_discharge_steps": ("camera_to_discharge_steps", int),
    "discharge_total_steps": ("discharge_total_steps", int),
    "safety_margin_steps": ("safety_margin_steps", int),
    "discharge_timeout_s": ("discharge_timeout_s", float),
    "carriage_move_timeout_s": ("carriage_move_timeout_s", float),
    "hopper_to_chamber_steps": ("hopper_to_chamber_steps", int),
    "intake_advance_timeout_s": ("intake_advance_timeout_s", float),
    "chamber_to_camera_steps": ("chamber_to_camera_steps", int),
    "conveyor_p00": ("conveyor_p00", int),
    "conveyor_hz": ("conveyor_hz", float),
    # Fan airflow
    "fan_warmup_s": ("fan_warmup_s", float),
    "fan_pulse_on_s": ("fan_pulse_on_s", float),
    "fan_pulse_period_s": ("fan_pulse_period_s", float),
    "fan_rh_on": ("fan_rh_on", float),
    "fan_rh_off": ("fan_rh_off", float),
}


def resolve_settings(args: argparse.Namespace, conn) -> dict:
    """Fill the calibration flags that were left unset from the settings table,
    and return the ones that were passed explicitly (those win for this run)."""
    stored = dict(db.DEFAULT_SETTINGS)
    if conn is not None:
        stored.update(db.get_settings(conn))

    overrides = {}
    for attr, (key, cast) in SETTING_FLAGS.items():
        given = getattr(args, attr)
        if given is None:
            val = stored.get(key)
            setattr(args, attr, cast(val) if val is not None else None)
        else:
            overrides[key] = given

    if conn is not None and args.save_settings:
        for key, value in overrides.items():
            db.set_setting(conn, key, value)
    return overrides


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    conn = None
    if not args.no_db:
        conn = db.connect(args.db)
        db.init(conn)
        # two supervisors would fight over the pins, and this one's startup
        # clean-up would close out the other's live batch as "interrupted"
        other = db.live_state(conn)
        if other is not None and other["running"]:
            sys.exit(
                f"another supervisor (pid {other['supervisor_pid']}) is running against this "
                f"database, last heartbeat {other['age_s']:.0f}s ago. Stop it first; if it "
                "crashed, its heartbeat goes stale within 10 s."
            )
        interrupted = db.mark_interrupted_batches(conn)
        if interrupted:
            print(f"closed out {interrupted} batch row(s) left unfinished by a previous run")
        expired = db.expire_pending_commands(conn)
        if expired:
            print(f"expired {expired} command(s) queued while no supervisor was running")
    overrides = resolve_settings(args, conn)

    from hardware.climate import FanController

    fan_kwargs = {}
    if args.fan_warmup_s is not None:
        fan_kwargs["warmup_s"] = args.fan_warmup_s
    if args.fan_pulse_on_s is not None:
        fan_kwargs["pulse_on_s"] = args.fan_pulse_on_s
    if args.fan_pulse_period_s is not None:
        fan_kwargs["pulse_period_s"] = args.fan_pulse_period_s
    if args.fan_rh_on is not None:
        fan_kwargs["rh_on"] = args.fan_rh_on
    if args.fan_rh_off is not None:
        fan_kwargs["rh_off"] = args.fan_rh_off

    # Item 16: Intake advance timing calculation
    intake_hz = args.conveyor_hz or args.sample_hz or 400.0
    intake_steps = args.hopper_to_chamber_steps or 0
    expected_intake_s = (intake_steps / intake_hz) if (intake_steps > 0 and intake_hz > 0) else 0.0

    intake_timeout = args.intake_advance_timeout_s
    if intake_timeout is None:
        intake_timeout = max(30.0, expected_intake_s * 1.5 + 5.0)
    elif expected_intake_s >= intake_timeout:
        print(f"WARNING: expected intake time ({expected_intake_s:.1f}s) >= timeout ({intake_timeout:.1f}s); adjusting timeout to {expected_intake_s + 10.0:.1f}s")
        intake_timeout = expected_intake_s + 10.0

    if intake_steps > 0:
        print(f"cords intake advance: {intake_steps} steps @ {intake_hz:.0f} Hz -> expected {expected_intake_s:.1f}s (timeout {intake_timeout:.1f}s)")

    controller = BatchController(
        target_kg=args.target_kg,
        intake_advance_timeout_s=intake_timeout,
        fans=FanController(**fan_kwargs),
    )
    if args.simulate or args.dry_run:
        conveyor = SimulatedConveyor(
            advance_s=args.intake_advance_s,
            hopper_to_chamber_steps=args.hopper_to_chamber_steps or 2000,
            default_hz=args.sample_hz or 400.0,
        )
        carriage_driver = SimulatedCarriage()
    else:
        conveyor = StepperConveyor(
            pins.HBS57H_CONVEYOR,
            hopper_to_chamber_steps=args.hopper_to_chamber_steps or 0,
            default_hz=args.sample_hz or 400.0,
        )
        carriage_driver = CarriageDriver(
            pins.TB6600_CARRIAGE,
            default_hz=400.0,
        )
        carriage_driver.check_home()
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
    where = "off" if conn is None else str(args.db or db.DEFAULT_PATH)
    print(f"cords supervisor: mode={mode} speed={args.speed}x tick={args.interval}s target={args.target_kg}kg db={where}")
    if overrides:
        kept = " (saved)" if args.save_settings else " (this run only)"
        print(f"  calibration overrides{kept}: {', '.join(f'{k}={v}' for k, v in sorted(overrides.items()))}")

    from hardware import grading
    # hopper_to_chamber_steps = 0 means "uncalibrated".
    # A real stepper driver must refuse to move with this value:
    #   if args.hopper_to_chamber_steps <= 0:
    #       raise RuntimeError("hopper_to_chamber_steps is not calibrated ...")
    # SimulatedConveyor ignores it; the check lives in the real driver, not here.
    if not args.simulate and not args.dry_run and not args.hopper_to_chamber_steps:
        print("WARNING: hopper_to_chamber_steps is 0 / unset. "
              "The real conveyor driver will refuse the intake advance. "
              "Calibrate with `bench jog conveyor` then --save-settings.")
    grading_cfg = {
        "sample_mode": args.sample_mode,
        "n_samples": args.n_samples,
        "min_valid": args.min_valid,
        "max_attempts": args.max_attempts,
        "retry_max": args.retry_max,
        "steps_per_sample": args.steps_per_sample,
        "sample_hz": args.sample_hz,
        "settle_s": args.settle_s,
        "tau_bad": args.tau_bad,
        "min_confidence": args.min_confidence,
        "blur_threshold": args.blur_threshold,
        "max_rejected_ratio": args.max_rejected_ratio,
        "camera_to_discharge_steps": args.camera_to_discharge_steps,
        "discharge_total_steps": args.discharge_total_steps,
        "safety_margin_steps": args.safety_margin_steps,
        "discharge_timeout_s": args.discharge_timeout_s,
        "carriage_move_timeout_s": args.carriage_move_timeout_s,
    }
    # Startup validation
    grading.validate_grading_config(grading_cfg)

    classifier = None
    webcam = None
    if not args.simulate and not args.dry_run:
        from hardware.vision import CopraClassifier, USBWebcam
        classifier = CopraClassifier()
        webcam = USBWebcam()

    sorting_flow: SortingFlow | None = None
    sorting_config_snapshot: str | None = None
    prev_capture_requested: bool = False

    health = ComponentHealthRegistry()

    pid = os.getpid()
    commands: Commands | None = None
    started_real = time.monotonic()
    last_status = 0.0
    last_live = -1e9
    last_sample = -1e9
    last_state = None
    last_alarms: frozenset[str] = frozenset()
    batch_id: int | None = None
    batch_drying_timed_out_saved: bool = False

    try:
        while True:
            real = time.monotonic()
            now = (real - started_real) * args.speed

            # operator commands arrive through the database, never through a pin
            if conn is not None:
                for row in db.pending_commands(conn):
                    try:
                        if row["name"] == "start" and db.is_calibration_locked(conn):
                            raise RuntimeError("cannot start batch: calibration in progress")
                        if row["name"] in ("acquire_calibration_lock", "release_calibration_lock"):
                            message = f"{row['name']} acknowledged"
                        elif row["name"] == "answer_prompt":
                            payload_dict = json.loads(row["payload"]) if row["payload"] else {}
                            action = payload_dict.get("action") or payload_dict.get("option", "")
                            prompt_id = payload_dict.get("id")
                            if action == "retry" and getattr(carriage_driver, "state", None) == "fault":
                                carriage_driver.clear_fault()
                            if sorting_flow is not None and sorting_flow.active_prompt and (
                                prompt_id is None or sorting_flow.active_prompt.id == prompt_id
                            ):
                                ok = sorting_flow.answer_prompt(action, now=now)
                                controller.active_prompt = None
                                message = f"prompt answered: {action}" if ok else "invalid prompt action"
                                if "aborted" in sorting_flow.alarms:
                                    controller.abort(f"sorting aborted ({action})")
                            else:
                                message = apply_command(controller, row["name"], row["payload"])
                        elif row["name"] == "set_drying_timer":
                            payload_dict = json.loads(row["payload"]) if row["payload"] else {}
                            dur_s = float(
                                payload_dict.get("duration_s")
                                or float(payload_dict.get("duration_min", 60)) * 60
                            )
                            message = apply_command(controller, row["name"], row["payload"])
                            if conn is not None and batch_id is not None:
                                db.set_batch_drying_timer(
                                    conn, batch_id, dur_s, controller.drying_timer_deadline
                                )
                        else:
                            message = apply_command(controller, row["name"], row["payload"])
                        db.resolve_command(conn, row["id"], db.DONE, message)
                        if row["name"] == "set_target_kg":
                            # keep it across restarts, or the next run silently
                            # reverts to whatever the table held before
                            db.set_setting(conn, "target_kg", controller.target_kg)
                        print(f"{stamp()}  command {row['name']}: {message}")
                    except (RuntimeError, ValueError) as exc:
                        db.resolve_command(conn, row["id"], db.REJECTED, str(exc))
                        print(f"{stamp()}  command {row['name']} REFUSED: {exc}")

            if sim is not None:
                sim.target_kg = controller.target_kg
                sim.advance(now, commands)
                chamber, exhaust_t, exhaust_rh = sim.chamber_temp, sim.exhaust_temp, sim.exhaust_rh
                hopper_kg, sorted_kg = sim.hopper_kg, sim.sorted_kg
            else:
                cache.poll(now)
                chamber, exhaust_t, exhaust_rh = cache.chamber_temp, cache.exhaust_temp, cache.exhaust_rh
                hopper_kg = io.read_hopper_kg() if io is not None else None
                sorted_kg = None
            # ── component health recording ─────────────────────────────────
            if chamber is not None:
                health.record_ok("chamber_sensor", now)
            elif not args.simulate:
                health.record_fault("chamber_sensor", "no reading", now)
            if exhaust_t is not None and exhaust_rh is not None:
                health.record_ok("exhaust_sensor", now)
            elif not args.simulate:
                health.record_warn("exhaust_sensor", "no reading", now)
            if hopper_kg is not None:
                health.record_ok("hopper_scale", now)
            elif not args.simulate:
                health.record_fault("hopper_scale", "no reading", now)
            if conveyor.busy:
                health.record_ok("conveyor", now)
            health_alarms = health.evaluate_timeouts(
                now, batch_active=(controller.state in _LIVE)
            )
            if health_alarms:
                combined_alarms = frozenset(commands.alarms | health_alarms) if hasattr(commands, 'alarms') else frozenset(health_alarms)
                commands = Commands(
                    state=commands.state,
                    heater_on=commands.heater_on,
                    fans_on=commands.fans_on,
                    gate_open=commands.gate_open,
                    conveyor=commands.conveyor,
                    alarms=combined_alarms,
                    fan_reason=commands.fan_reason,
                    sorting_stage=commands.sorting_stage,
                    carriage_target=commands.carriage_target,
                )

            # --- SORTING orchestration ---------------------------------------
            grade_ready = False
            batch_grade = None
            needs_review = False
            sample_vectors = None
            sample_valid_n = 0
            sample_total_n = 0

            if controller.state is BatchState.SORTING:
                # ── initialise SortingFlow on first SORTING tick ──────────────
                if sorting_flow is None:
                    grading_cfg_merged = {**grading.DEFAULT_GRADING_CONFIG, **{
                        k: v for k, v in grading_cfg.items() if v is not None
                    }}
                    sorting_flow = SortingFlow(
                        config=grading_cfg_merged,
                        chamber_to_camera_steps=(
                            grading_cfg_merged.get("camera_to_discharge_steps") or 0
                        ),
                        discharge_total_steps=int(
                            grading_cfg_merged.get("discharge_total_steps") or 3000
                        ),
                        sample_hz=float(
                            grading_cfg_merged.get("sample_hz") or 400.0
                        ),
                        settle_s=float(
                            grading_cfg_merged.get("settle_s") or 0.3
                        ),
                        carriage_move_timeout_s=float(
                            grading_cfg_merged.get("carriage_move_timeout_s") or 30.0
                        ),
                        discharge_timeout_s=float(
                            grading_cfg_merged.get("discharge_timeout_s") or 60.0
                        ),
                    )
                    sorting_flow.start(now=now)
                    sorting_config_snapshot = sorting_flow.sampler.snapshot_config()
                    prev_capture_requested = False

                # ── read target-bin scale ─────────────────────────────────────
                target_bin_for_reading = (
                    sorting_flow.carriage_target
                    or controller.operator_grade
                    or sorting_flow.batch_grade
                )
                bin_kg: float | None = None
                if io is not None and target_bin_for_reading:
                    bin_kg = io.read_bin_kg(target_bin_for_reading)
                    if bin_kg is not None:
                        health.record_ok("bin_scale", now)
                    else:
                        health.record_warn("bin_scale", "no reading", now)
                elif args.simulate and sorting_flow.stage.value in ("verify", "discharge"):
                    # Simulated weight rise during verify
                    bin_kg = (sorting_flow.baseline_bin_kg or 0.0) + (
                        0.5 if sorting_flow.stage.value == "verify" else 0.0
                    )

                # ── camera capture ────────────────────────────────────────────
                frame_vector = None
                capture_done = False
                if sorting_flow.capture_requested and not prev_capture_requested:
                    if args.simulate:
                        frame_vector = [0.05, 0.20, 0.75]   # simulated "great"
                        capture_done = True
                    elif webcam is not None and classifier is not None:
                        frame_vector = webcam.capture_softmax(
                            classifier,
                            sorting_flow.sampler.blur_threshold,
                            sorting_flow.sampler.min_confidence,
                        )
                        capture_done = True  # Item 4: Even if None (rejected/blur), capture completed
                    else:
                        frame_vector = [0.05, 0.15, 0.80]   # dry-run mock
                        capture_done = True
                prev_capture_requested = sorting_flow.capture_requested

                # ── drive carriage & conveyor, then tick SortingFlow ──────────
                carriage_driver.update(now)
                conveyor.update(now)

                if sorting_flow.conveyor_move_steps > 0:
                    conveyor.move(
                        sorting_flow.conveyor_move_steps,
                        hz=sorting_flow.conveyor_move_hz,
                        forward=True,
                        now=now,
                    )

                if sorting_flow.carriage_target and (
                    carriage_driver.current_bin != sorting_flow.carriage_target
                    or carriage_driver.state not in ("at_target", "moving")
                ):
                    carriage_driver.goto(sorting_flow.carriage_target, now=now)

                # Item 14: copra_present flag (empty belt reference check is Phase 5)
                copra_present = True

                sorting_flow.update(
                    now=now,
                    conveyor_busy=conveyor.busy,
                    conveyor_last_result=getattr(conveyor, "last_result", "done"),
                    carriage_state=carriage_driver.state,
                    carriage_current_bin=carriage_driver.current_bin,
                    bin_kg=bin_kg,
                    operator_grade=controller.operator_grade,
                    copra_present=copra_present,
                    frame_vector=frame_vector,
                    capture_done=capture_done,
                )

                if "aborted" in sorting_flow.alarms and controller.state in _LIVE:
                    controller.abort("sorting flow aborted")

                # ── bubble SortingFlow prompt up to BatchController ───────────
                if sorting_flow.active_prompt and controller.active_prompt is None:
                    controller.active_prompt = sorting_flow.active_prompt

                # ── extract outputs ───────────────────────────────────────────
                grade_ready = sorting_flow.grade_ready
                batch_grade = sorting_flow.batch_grade
                needs_review = sorting_flow.needs_review
                sorting_complete = sorting_flow.is_complete
                sorted_kg = sorting_flow.weight_out_kg
                sample_vectors = sorting_flow.sampler.valid_vectors
                sample_valid_n = sorting_flow.sampler.sample_valid_n
                sample_total_n = sorting_flow.sampler.total_attempts

            else:
                if sorting_flow is not None:
                    sorting_flow = None
                    prev_capture_requested = False
                    carriage_driver.stop()
                grade_ready = False
                batch_grade = None
                needs_review = False
                sorting_complete = False
                sorted_kg = None
                sample_vectors = None
                sample_valid_n = 0
                sample_total_n = 0

            inputs = Inputs(
                now=now,
                chamber_temp=chamber,
                exhaust_temp=exhaust_t,
                exhaust_rh=exhaust_rh,
                hopper_kg=hopper_kg,
                conveyor_busy=conveyor.busy,
                grade_ready=grade_ready,
                batch_grade=batch_grade,
                needs_review=needs_review,
                sorting_complete=sorting_complete,
                sorted_kg=sorted_kg,
                sample_vectors=sample_vectors,
                sample_valid_n=sample_valid_n,
                sample_total_n=sample_total_n,
                sorting_config_snapshot=sorting_config_snapshot,
            )
            commands = controller.update(inputs)

            if io is not None:
                io.apply(commands)

            if commands.state is not BatchState.SORTING:
                # In SORTING the conveyor is owned by SortingFlow; elsewhere BatchController drives it
                conveyor.apply(commands.conveyor, now)

            if conn is not None and batch_id is not None:
                if controller.drying_timed_out and not batch_drying_timed_out_saved:
                    db.set_batch_drying_timed_out(conn, batch_id, True)
                    batch_drying_timed_out_saved = True

            exhaust_ah = (
                absolute_humidity(exhaust_t, exhaust_rh)
                if exhaust_t is not None and exhaust_rh is not None
                else None
            )
            state_changed = commands.state is not last_state

            if state_changed:
                print(f"{stamp()}  -> {commands.state.value.upper()}  (t+{now / 60:.1f} min)")
                if conn is not None:
                    if commands.state is BatchState.INTAKE and last_state in (None, BatchState.IDLE):
                        batch_id = db.start_batch(conn, target_kg=controller.target_kg)
                        batch_drying_timed_out_saved = False
                        print(f"{stamp()}  batch #{batch_id} opened")
                    elif commands.state in _FINISHED and batch_id is not None:
                        db.finish_batch(conn, batch_id, controller.record(), commands.state.value)
                        batch_drying_timed_out_saved = False
                        print(f"{stamp()}  batch #{batch_id} written")
                    elif commands.state is BatchState.IDLE:
                        batch_id = None
                        batch_drying_timed_out_saved = False
                    elif batch_id is not None:
                        db.set_batch_state(conn, batch_id, commands.state.value)
                last_state = commands.state
            for alarm in sorted(commands.alarms - last_alarms):
                print(f"{stamp()}  *** ALARM {alarm} ***")
            for alarm in sorted(last_alarms - commands.alarms):
                print(f"{stamp()}  --- cleared {alarm} ---")
            last_alarms = commands.alarms

            if conn is not None:
                readings = dict(
                    chamber_c=chamber, exhaust_c=exhaust_t, exhaust_rh=exhaust_rh,
                    exhaust_ah=exhaust_ah, hopper_kg=hopper_kg,
                    heater_on=commands.heater_on, fans_on=commands.fans_on,
                    fan_reason=commands.fan_reason,
                )
                # the heartbeat is wall-clock, so it is throttled on real time
                if state_changed or real - last_live >= args.live_interval:
                    components_snapshot = health.snapshot()
                    components_snapshot["conveyor"] = {
                        **components_snapshot.get("conveyor", {}),
                        "busy": conveyor.busy,
                        "position_steps": conveyor.position_steps,
                        "last_result": conveyor.last_result,
                    }
                    components_snapshot["carriage"] = {
                        **components_snapshot.get("carriage", {}),
                        "state": carriage_driver.state,
                        "current_bin": carriage_driver.current_bin,
                        "last_fault": carriage_driver.last_fault,
                    }
                    active_prompt_for_db = None
                    if sorting_flow is not None and sorting_flow.active_prompt:
                        active_prompt_for_db = sorting_flow.active_prompt.to_dict()
                    elif controller.active_prompt is not None:
                        ap = controller.active_prompt
                        active_prompt_for_db = ap.to_dict() if hasattr(ap, "to_dict") else ap
                    db.update_live(
                        conn,
                        state=commands.state.value,
                        supervisor_pid=pid,
                        batch_id=batch_id,
                        elapsed_s=controller.elapsed_s(now),
                        target_kg=controller.target_kg,
                        gate_open=commands.gate_open,
                        conveyor=commands.conveyor.value,
                        alarms=commands.alarms,
                        components=components_snapshot,
                        active_prompt=active_prompt_for_db,
                        **readings,
                    )
                    last_live = real
                # samples are spaced on the batch clock, so the charts have a
                # sane time axis whatever --speed was used
                if now - last_sample >= args.sample_interval:
                    db.log_sample(
                        conn,
                        batch_id=batch_id,
                        state=commands.state.value,
                        elapsed_s=controller.elapsed_s(now),
                        **readings,
                    )
                    last_sample = now

            if real - last_status >= 2.0:
                print(
                    f"{stamp()}  {commands.state.value:9} chamber={fmt(chamber):>7} "
                    f"exhaust={fmt(exhaust_t):>7} {fmt(exhaust_rh, '%'):>6} "
                    f"hopper={fmt(hopper_kg, 'kg'):>8} "
                    f"heater={'ON ' if commands.heater_on else 'off'} "
                    f"fans={'ON ' if commands.fans_on else 'off'} ({commands.fan_reason}) "
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
        carriage_driver.close()
        if io is not None:
            io.close()
        if conn is not None:
            # a batch still open here was cut short; say so rather than leaving
            # a row the dashboard would read as still running
            if batch_id is not None and controller.state not in _FINISHED:
                db.mark_interrupted_batches(conn)
                print(f"batch #{batch_id} marked interrupted")
            db.mark_supervisor_stopped(conn)
            conn.close()
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
