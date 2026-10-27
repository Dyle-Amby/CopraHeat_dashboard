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
import math
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


def capture_camera_test_frame(webcam, classifier):
    if webcam is None:
        raise RuntimeError("camera test failed: camera is not initialized")
    if classifier is None or not classifier.is_ready:
        raise RuntimeError("camera test failed: vision model is not loaded")
    try:
        ok, frame = webcam.read_frame()
    except Exception as exc:
        raise RuntimeError(f"camera capture failed: {exc}") from exc
    if not ok or frame is None:
        raise RuntimeError("camera test failed: no frame captured")
    return frame


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

    def __init__(self, args: argparse.Namespace, conn=None):
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
        settings = db.get_settings(conn) if conn is not None else {}
        self.hx_bins = {
            name: HX711(
                pin.dout,
                pin.sck,
                offset=int(settings.get(f"hx711_offset_bin_{name}", "0")),
                scale=float(settings.get(f"hx711_scale_bin_{name}", str(args.scale or 1.0))),
            )
            for name, pin in (
                ("great", pins.HX711_BIN_GREAT),
                ("good", pins.HX711_BIN_GOOD),
                ("bad", pins.HX711_BIN_BAD),
            )
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

    def calibrate_bin_scale(
        self, conn, target_bin: str, action: str, ref_kg: float = 1.0
    ) -> str:
        hx = self.hx_bins.get(target_bin)
        if hx is None:
            raise ValueError(f"unknown bin {target_bin!r}")

        if action == "tare":
            offset = hx.tare(30)
            if offset is None:
                raise RuntimeError(f"bin {target_bin} tare failed: no HX711 readings")
            hx.offset = offset
            db.set_setting(conn, f"hx711_offset_bin_{target_bin}", offset)
            return f"Bin {target_bin} tared: {offset}"

        if action == "scale":
            if not math.isfinite(ref_kg) or ref_kg <= 0:
                raise ValueError("reference weight must be a positive finite number")
            raw = hx.read_raw_median(30)
            if raw is None:
                raise RuntimeError(f"bin {target_bin} scale calibration failed: no HX711 readings")
            scale = (raw - hx.offset) / ref_kg
            if scale == 0:
                raise RuntimeError(f"bin {target_bin} scale calibration failed: zero scale factor")
            hx.scale = scale
            db.set_setting(conn, f"hx711_scale_bin_{target_bin}", f"{scale:.1f}")
            return f"Bin {target_bin} scale: {scale:.1f} counts/kg"

        raise ValueError(f"unknown bin calibration action {action!r}")

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
        if db.clear_calibration_lock(conn):
            print("cleared calibration lock from previous supervisor instance")
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
    io = None if args.dry_run else Actuators(args, conn)

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

    unclean_prompt_active = False
    if conn is not None:
        last_b = db.get_latest_batch(conn)
        if last_b and last_b.get("state") in ("intake", "drying", "cooldown", "sorting"):
            unclean_batch_id = last_b["id"]
            unclean_state = last_b["state"].upper()
            prompt_data = {
                "id": "unclean_shutdown",
                "message": f"Unclean shutdown detected during {unclean_state} (Batch #{unclean_batch_id}). Heaters and fans held OFF. Choose action:",
                "options": ["recover", "abort"]
            }
            db.update_live(conn, active_prompt=prompt_data, heater_on=False, fans_on=False)
            unclean_prompt_active = True
            print(f"{stamp()}  *** UNCLEAN SHUTDOWN DETECTED: Batch #{unclean_batch_id} was {unclean_state} ***")

    # Milestone tracking set for active batch
    logged_milestones: set[str] = set()

    try:
        while True:
            real = time.monotonic()
            now = (real - started_real) * args.speed
            camera_last_test = None

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
                            if prompt_id == "unclean_shutdown" or unclean_prompt_active:
                                unclean_prompt_active = False
                                if action == "recover" and last_b:
                                    batch_id = last_b["id"]
                                    rec_state = last_b["state"]
                                    # Resume batch
                                    db.log_batch_event(conn, batch_id, "batch_recovered", f"Operator recovered batch during {rec_state}")
                                    message = f"Batch #{batch_id} recovered"
                                    print(f"{stamp()}  Batch #{batch_id} recovered by operator")
                                else:
                                    if last_b:
                                        db.finish_batch(conn, last_b["id"], controller.record(), "aborted")
                                        db.log_batch_event(conn, last_b["id"], "batch_aborted", "Aborted by operator after unclean shutdown")
                                    batch_id = None
                                    message = "Unclean batch aborted; machine returned to IDLE"
                                    print(f"{stamp()}  Unclean batch aborted")
                                db.update_live(conn, active_prompt=None)
                            elif action == "retry" and getattr(carriage_driver, "state", None) == "fault":
                                carriage_driver.clear_fault()
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
                        elif row["name"] == "tare_hopper":
                            payload_dict = json.loads(row["payload"]) if row["payload"] else {}
                            gate_pos = payload_dict.get("gate", "closed")
                            if io is not None:
                                offset = io.hx.tare(30)
                            else:
                                offset = 84000 if gate_pos == "closed" else 83800
                            if offset is None:
                                raise RuntimeError(f"hopper tare ({gate_pos}) failed: no HX711 readings")
                            key = f"hx711_offset_{gate_pos}"
                            db.set_setting(conn, key, str(offset))
                            if io is not None:
                                if gate_pos == "closed":
                                    io.offset_closed = offset
                                else:
                                    io.offset_open = offset
                            message = f"Hopper tare ({gate_pos}) saved: {offset}"
                        elif row["name"] == "calibrate_hopper_scale":
                            payload_dict = json.loads(row["payload"]) if row["payload"] else {}
                            ref_kg = float(payload_dict.get("ref_weight_kg", 1.0))
                            if not math.isfinite(ref_kg) or ref_kg <= 0:
                                raise ValueError("reference weight must be a positive finite number")
                            offset_closed = int(db.get_setting(conn, "hx711_offset_closed") or "0")
                            if io is not None:
                                raw = io.hx.read_raw_median(30)
                                if raw is None:
                                    raise RuntimeError("hopper scale calibration failed: no HX711 readings")
                                scale = (raw - offset_closed) / ref_kg
                            else:
                                scale = 21500.0
                            db.set_setting(conn, "hx711_scale", f"{scale:.1f}")
                            if io is not None:
                                io.scale = scale
                                io.hx.scale = scale
                            message = f"Scale calibrated: {scale:.1f} counts/kg (ref: {ref_kg} kg)"

                        elif row["name"] == "set_hopper_servo":
                            payload_dict = json.loads(row["payload"]) if row["payload"] else {}
                            angle = payload_dict.get("angle")
                            state = payload_dict.get("state")
                            if state == "open":
                                target_angle = float(db.get_setting(conn, "servo_open_angle") or "0.0")
                            elif state == "closed":
                                target_angle = float(db.get_setting(conn, "servo_closed_angle") or "180.0")
                            elif angle is not None:
                                target_angle = float(angle)
                            else:
                                target_angle = 0.0
                            if io is not None:
                                try:
                                    io.servo.set_angle(target_angle)
                                except Exception as exc:
                                    raise RuntimeError(
                                        f"servo command to {target_angle:.1f}° failed: {exc}"
                                    ) from exc
                            message = f"Servo set to {target_angle:.1f}°"

                        elif row["name"] == "goto_carriage_bin":
                            payload_dict = json.loads(row["payload"]) if row["payload"] else {}
                            target_bin = payload_dict.get("bin")
                            if carriage_driver:
                                if not carriage_driver.goto(target_bin, now=now):
                                    raise RuntimeError(
                                        f"carriage cannot move to {target_bin}: state is {carriage_driver.state}"
                                    )
                                message = f"Carriage moving to {target_bin}"
                            else:
                                message = "No carriage driver active"

                        elif row["name"] == "home_carriage":
                            if carriage_driver:
                                if not carriage_driver.goto("bad", now=now):
                                    raise RuntimeError(
                                        f"carriage cannot home down: state is {carriage_driver.state}"
                                    )
                                message = "Carriage homing down to the bad-bin limit"
                            else:
                                message = "No carriage driver active"

                        elif row["name"] == "calibrate_bin_scale":
                            payload_dict = json.loads(row["payload"]) if row["payload"] else {}
                            target_bin = payload_dict.get("bin", "great")
                            action = payload_dict.get("action", "tare")
                            ref_kg = float(payload_dict.get("ref_kg", 1.0))
                            if io is None:
                                raise RuntimeError("bin scale calibration requires live hardware")
                            message = io.calibrate_bin_scale(conn, target_bin, action, ref_kg)
                        elif row["name"] == "jog_conveyor":
                            payload_dict = json.loads(row["payload"]) if row["payload"] else {}
                            steps = int(payload_dict.get("steps", 100))
                            hz = float(payload_dict.get("hz") or db.get_setting(conn, "conveyor_hz") or 400.0)
                            forward = payload_dict.get("forward")
                            if forward is None:
                                forward = (steps >= 0)
                            steps = abs(steps)
                            if conveyor is not None:
                                if conveyor.busy:
                                    conveyor.stop()
                                conveyor.move(steps, hz=hz, forward=forward, now=now)
                                dir_str = "forward" if forward else "reverse"
                                message = f"Conveyor jogging {steps} steps ({dir_str}) at {hz:.0f} Hz"
                            else:
                                message = "No conveyor driver active"

                        elif row["name"] == "stop_conveyor":
                            if conveyor is not None:
                                stopped_steps = conveyor.stop()
                                message = f"Conveyor stopped (decelerated {stopped_steps} steps)"
                            else:
                                message = "No conveyor driver active"

                        elif row["name"] == "save_conveyor_leg":
                            payload_dict = json.loads(row["payload"]) if row["payload"] else {}
                            leg = payload_dict.get("leg")
                            steps = int(payload_dict.get("steps", 0))
                            meta = payload_dict.get("meta", {})
                            meta_str = json.dumps(meta) if isinstance(meta, dict) else str(meta)

                            if leg == "hopper_to_chamber":
                                db.set_setting(conn, "hopper_to_chamber_steps", str(steps))
                                db.set_setting(conn, "leg_hopper_to_chamber_meta", meta_str)
                                if conveyor is not None:
                                    conveyor.hopper_to_chamber_steps = steps
                            elif leg == "chamber_to_camera":
                                db.set_setting(conn, "chamber_to_camera_steps", str(steps))
                                db.set_setting(conn, "leg_chamber_to_camera_meta", meta_str)
                                if sorting_flow is not None:
                                    sorting_flow.chamber_to_camera_steps = steps
                            elif leg == "camera_to_discharge":
                                db.set_setting(conn, "camera_to_discharge_steps", str(steps))
                                db.set_setting(conn, "leg_camera_to_discharge_meta", meta_str)
                                if sorting_flow is not None:
                                    sorting_flow.camera_to_discharge_steps = steps
                            else:
                                raise ValueError(f"Unknown conveyor leg: {leg}")

                            db.set_setting(conn, "conveyor_cal_stale", "0")
                            message = f"Conveyor leg '{leg}' saved: {steps} steps (calibration marked fresh)"

                        elif row["name"] == "set_conveyor_p00":
                            payload_dict = json.loads(row["payload"]) if row["payload"] else {}
                            p00 = int(payload_dict.get("p00", 10000))
                            hz = payload_dict.get("hz")
                            db.set_setting(conn, "conveyor_p00", str(p00))
                            if hz is not None:
                                hz = float(hz)
                                db.set_setting(conn, "conveyor_hz", str(hz))
                                if conveyor is not None:
                                    conveyor.default_hz = hz
                            db.set_setting(conn, "conveyor_cal_stale", "1")
                            message = f"Conveyor driver updated: P00={p00}, Hz={hz or 'unchanged'} (cal marked stale)"

                        elif row["name"] == "mark_conveyor_stale":
                            payload_dict = json.loads(row["payload"]) if row["payload"] else {}
                            stale_flag = "1" if payload_dict.get("stale", True) else "0"
                            db.set_setting(conn, "conveyor_cal_stale", stale_flag)
                            message = f"Conveyor calibration stale flag set to {stale_flag}"

                        elif row["name"] == "test_component":
                            payload_dict = json.loads(row["payload"]) if row["payload"] else {}
                            comp = payload_dict.get("component")
                            if comp == "camera":
                                t0 = time.monotonic()
                                ok = False
                                frame = None
                                mock_vision = args.simulate or args.dry_run
                                if not mock_vision:
                                    frame = capture_camera_test_frame(webcam, classifier)
                                elif webcam is not None:
                                    try:
                                        ok, frame = webcam.read_frame()
                                    except Exception:
                                        ok, frame = False, None

                                if mock_vision and (not ok or frame is None):
                                    try:
                                        import numpy as np
                                        import cv2
                                        frame = np.zeros((480, 640, 3), dtype=np.uint8)
                                        frame[:] = (35, 40, 45)
                                        cv2.circle(frame, (320, 240), 110, (40, 110, 185), -1)
                                        cv2.circle(frame, (320, 240), 70, (25, 75, 130), -1)
                                        cv2.putText(frame, "SIMULATED COPRA", (190, 245), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
                                    except Exception as exc:
                                        raise RuntimeError(f"could not create simulated camera frame: {exc}") from exc

                                if classifier is not None and frame is not None:
                                    try:
                                        top_grade, top_conf, scores = classifier.predict(frame)
                                    except Exception as exc:
                                        raise RuntimeError(f"vision inference failed: {exc}") from exc
                                else:
                                    top_grade, top_conf, scores = "great", 0.942, {"bad": 0.015, "good": 0.043, "great": 0.942}

                                blur_var = 145.0
                                if frame is not None:
                                    try:
                                        import cv2
                                        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if len(frame.shape) == 3 else frame
                                        blur_var = float(cv2.Laplacian(gray, cv2.CV_64F).var())
                                    except Exception:
                                        pass

                                latency_ms = (time.monotonic() - t0) * 1000.0

                                # Save atomic preview frame
                                if frame is not None:
                                    try:
                                        import cv2
                                        tmp_dir = Path(BASE_DIR) / "tmp"
                                        tmp_dir.mkdir(parents=True, exist_ok=True)
                                        preview_path = tmp_dir / "camera_preview.jpg"
                                        tmp_file = tmp_dir / "camera_preview.jpg.tmp"
                                        cv2.imwrite(str(tmp_file), frame)
                                        tmp_file.replace(preview_path)
                                    except Exception as exc:
                                        print(f"Warning: could not write camera preview: {exc}")

                                test_result = {
                                    "grade": top_grade,
                                    "confidence": round(top_conf, 3),
                                    "scores": {k: round(v, 3) for k, v in scores.items()},
                                    "blur_laplacian": round(blur_var, 1),
                                    "blur_threshold": float(db.get_setting(conn, "blur_threshold") or 100.0),
                                    "latency_ms": round(latency_ms, 1),
                                    "tested_at": datetime.now().isoformat()
                                }
                                health.record_ok("camera", now)
                                camera_last_test = test_result
                                message = f"Camera tested: {top_grade.upper()} ({top_conf*100:.1f}%), blur={blur_var:.1f}, {latency_ms:.0f}ms"
                            else:
                                message = f"Component test for {comp} acknowledged" 
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
            previous_state = last_state
            state_changed = commands.state is not last_state

            if state_changed:
                print(f"{stamp()}  -> {commands.state.value.upper()}  (t+{now / 60:.1f} min)")
                if conn is not None:
                    if commands.state is BatchState.INTAKE and last_state in (None, BatchState.IDLE):
                        batch_id = db.start_batch(conn, target_kg=controller.target_kg)
                        batch_drying_timed_out_saved = False
                        logged_milestones.clear()
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

            # ── Phase 6: Milestone Emission ──────────────────────────────────────────
            if conn is not None and batch_id is not None:
                def emit_milestone(evt_name: str, details=None):
                    if evt_name not in logged_milestones:
                        db.log_batch_event(conn, batch_id, evt_name, details)
                        logged_milestones.add(evt_name)

                # 1. Batch Started
                emit_milestone("batch_started", f"Target: {controller.target_kg} kg")

                # 2. Gate Opened
                if commands.gate_open:
                    emit_milestone("gate_opened")

                # 3. Intake Advance Done (when leaving INTAKE)
                if previous_state is BatchState.INTAKE and commands.state is not BatchState.INTAKE:
                    emit_milestone("intake_advance_done")

                # 4. Heater ON
                if commands.heater_on:
                    emit_milestone("heater_on")

                # 5. Temperature 60°C Reached
                if chamber is not None and chamber >= 60.0:
                    emit_milestone("temp_60c_reached", f"Chamber: {chamber:.1f}°C")

                # 6. Fans Pulsing
                if commands.fans_on and "pulse" in (commands.fan_reason or ""):
                    emit_milestone("fans_pulsing")

                # 7. Drying Endpoint Reached (or timer expired)
                if previous_state is BatchState.DRYING and commands.state is not BatchState.DRYING:
                    reason = "timer" if controller.drying_timed_out else "humidity"
                    emit_milestone("drying_endpoint_reached", f"Endpoint reached via {reason}")

                # 8. Cooldown Started
                if commands.state is BatchState.COOLDOWN:
                    emit_milestone("cooldown_started")

                # 9. Sorting Positioning Done
                if commands.state is BatchState.SORTING:
                    emit_milestone("sorting_positioning_done")

                # 10. Sample Captured (valid camera frame)
                if sample_valid_n and sample_valid_n > 0:
                    emit_milestone("sample_captured", f"Valid samples: {sample_valid_n}/{sample_total_n}")

                # 11. Batch Grade Assigned
                if batch_grade:
                    emit_milestone("batch_grade_assigned", f"Consensus Grade: {batch_grade}")

                # 12. Discharge Completed
                if sorting_flow and sorting_flow.stage.value == "complete":
                    emit_milestone("discharge_completed")

                # 13. Weight Gain Verified
                if sorting_flow and sorting_flow.stage.value in ("verify", "complete"):
                    emit_milestone("weight_gain_verified")

            # Reset milestones when transitioning to a new batch or IDLE
            if commands.state is BatchState.INTAKE and previous_state in (None, BatchState.IDLE):
                logged_milestones.clear()
            elif commands.state is BatchState.IDLE:
                logged_milestones.clear()

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
                    iso_now = datetime.now().isoformat()

                    # 1. Limit switch states (LS1 = Great top, LS2 = Good, LS3 = Bad bottom)
                    switches = {"LS1": False, "LS2": False, "LS3": False}
                    if hasattr(carriage_driver, "_limits") and carriage_driver._limits is not None:
                        try:
                            switches = carriage_driver._limits.read()
                        except Exception:
                            pass
                    elif carriage_driver and carriage_driver.current_bin in ("great", "good", "bad"):
                        # Simulation fallback: mark current bin stop as pressed
                        stop_map = {"great": "LS1", "good": "LS2", "bad": "LS3"}
                        switches[stop_map[carriage_driver.current_bin]] = True

                    # 2. Bin scale weights & status
                    bin_weights = {"great": None, "good": None, "bad": None}
                    bin_statuses = {"great": "unknown", "good": "unknown", "bad": "unknown"}
                    if io is not None:
                        for b_name in ("great", "good", "bad"):
                            val = io.read_bin_kg(b_name)
                            bin_weights[b_name] = val
                            bin_statuses[b_name] = "ok" if val is not None else "fault"
                    elif args.simulate:
                        for b_name in ("great", "good", "bad"):
                            bin_weights[b_name] = 0.0
                            bin_statuses[b_name] = "ok"

                    # 3. Last trigger label
                    trigger_labels = {
                        "great": "LS1 (Great top)",
                        "good": "LS2 (Good)",
                        "bad": "LS3 (Bad bottom)",
                    }
                    last_trigger = trigger_labels.get(carriage_driver.current_bin, "—") if carriage_driver else "—"

                    # 4. Conveyor & Carriage components
                    components_snapshot["conveyor"] = {
                        **components_snapshot.get("conveyor", {}),
                        "busy": conveyor.busy,
                        "position_steps": conveyor.position_steps,
                        "last_result": conveyor.last_result,
                        "last_cmd": commands.conveyor.value if hasattr(commands.conveyor, "value") else str(commands.conveyor),
                        "last_cmd_at": iso_now,
                    }
                    components_snapshot["carriage"] = {
                        **components_snapshot.get("carriage", {}),
                        "state": carriage_driver.state if carriage_driver else "idle",
                        "current_bin": carriage_driver.current_bin if carriage_driver else None,
                        "last_trigger": last_trigger,
                        "last_fault": carriage_driver.last_fault if carriage_driver else None,
                        "last_cmd": f"GOTO {commands.carriage_target.upper()}" if commands.carriage_target else "HOLD",
                        "last_cmd_at": iso_now,
                    }

                    # 5. Output-only devices (Servo, Heater, Fans) & sensor states
                    components_snapshot["switches"] = switches
                    components_snapshot["bin_kg"] = bin_weights
                    components_snapshot["bin_scales_status"] = bin_statuses
                    components_snapshot["servo"] = {
                        "last_cmd": "OPEN" if commands.gate_open else "CLOSED",
                        "last_cmd_at": iso_now,
                    }
                    components_snapshot["heater"] = {
                        "last_cmd": "ON" if commands.heater_on else "OFF",
                        "last_cmd_at": iso_now,
                    }
                    components_snapshot["fans"] = {
                        "last_cmd": f"ON ({commands.fan_reason})" if commands.fans_on else "OFF",
                        "last_cmd_at": iso_now,
                    }
                    components_snapshot["camera"] = {
                        **components_snapshot.get("camera", {}),
                        "last_test": camera_last_test,
                        "model_ready": classifier.is_ready if classifier else False,
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
