"""CORDS dashboard. Reads and writes the SQLite database, and nothing else.

The supervisor (`python -m hardware.run_cords`) owns every pin; this process
queues operator commands into the database and reads back what the machine
did with them. See hardware/db.py for the tables.
"""

import json
import math
import os
from datetime import datetime

from flask import Flask, g, jsonify, render_template, request

from hardware import db
from hardware.service import get_supervisor_service, CONFIRMATION_PHRASE
from hardware.batch import COMMAND_NAMES

ABORT_REASON_MAX = 200

# Maintenance commands for /components page (calibrations, tests, jogs)
MAINTENANCE_COMMANDS = frozenset({
    "acquire_calibration_lock",
    "release_calibration_lock",
    "jog_conveyor",
    "stop_conveyor",
    "save_conveyor_leg",
    "set_conveyor_p00",
    "mark_conveyor_stale",
    "goto_carriage_bin",
    "home_carriage",
    "test_carriage_limits",
    "tare_hopper",
    "calibrate_hopper_scale",
    "set_hopper_servo",
    "test_component",
})

ALL_COMMANDS = COMMAND_NAMES | MAINTENANCE_COMMANDS


def supervisor_status(live: dict | None) -> str:
    """One word for the header pill. 'stopped' and 'not_responding' are kept
    apart on purpose: one was a clean shutdown, the other a process that died
    while it may have had the heaters on."""
    if live is None:
        return "never_run"
    if live["supervisor_pid"] is None:
        return "stopped"
    if live["stale"]:
        return "not_responding"
    return "running"


def validate_command(name, payload):
    """Returns (name, payload) ready to queue, or raises ValueError with the
    message to show the operator. Accepts string, numeric, or JSON-object (dict) payloads.
    The machine re-validates everything; this only catches what is wrong regardless of machine state."""
    if name not in ALL_COMMANDS:
        raise ValueError(f"unknown command {name!r}")

    # Process commands that take no payload drop any provided payload
    if name in ("start", "skip_intake", "end_drying", "finish_sorting", "reset"):
        return name, None

    # Handle dictionary / JSON-object payload
    if isinstance(payload, dict):
        # Strip sensitive pin from payload before storing/queuing
        payload = {k: v for k, v in payload.items() if k != "pin"}
        if name == "acquire_calibration_lock":
            dur = payload.get("duration_s", 300.0)
            try:
                dur_f = float(dur)
            except (TypeError, ValueError):
                raise ValueError("duration_s must be a number") from None
            if not math.isfinite(dur_f):
                raise ValueError("duration_s must be a finite number")
            payload["duration_s"] = max(10.0, min(1800.0, dur_f))
            return name, json.dumps(payload)
        if name == "set_target_kg":
            val = payload.get("target_kg")
            if val is None:
                raise ValueError("set_target_kg needs a target weight")
            try:
                f_val = float(val)
            except (TypeError, ValueError):
                raise ValueError("target weight must be a number of kilograms") from None
            if not math.isfinite(f_val) or f_val <= 0:
                raise ValueError("target weight must be greater than zero")
            return name, repr(f_val)
        if name == "override_grade":
            grade = payload.get("grade")
            if grade not in ("great", "good", "bad"):
                raise ValueError(f"invalid grade {grade!r}; expected 'great', 'good', or 'bad'")
            return name, grade
        if name == "abort":
            reason = (payload.get("reason") or "").strip() or "operator abort"
            return name, reason[:ABORT_REASON_MAX]
        if name == "set_drying_timer":
            dur = payload.get("duration_s") or float(payload.get("duration_min", 60)) * 60
            try:
                dur = float(dur)
            except (TypeError, ValueError):
                raise ValueError("set_drying_timer needs duration_s or duration_min") from None
            if dur <= 0:
                raise ValueError("drying timer duration must be greater than zero")
            return name, json.dumps({"duration_s": dur})
        if name in ("answer_prompt", "clear_prompt"):
            return name, json.dumps(payload)
        return name, json.dumps(payload)

    # String / scalar payload
    if name == "set_target_kg":
        if payload is None:
            raise ValueError("set_target_kg needs a target weight")
        try:
            value = float(payload)
        except (TypeError, ValueError):
            raise ValueError("target weight must be a number of kilograms") from None
        if not math.isfinite(value) or value <= 0:
            raise ValueError("target weight must be greater than zero")
        return name, repr(value)
    if name == "override_grade":
        if payload not in ("great", "good", "bad"):
            raise ValueError(f"invalid grade {payload!r}; expected 'great', 'good', or 'bad'")
        return name, payload
    if name == "abort":
        reason = (payload or "").strip() or "operator abort"
        return name, reason[:ABORT_REASON_MAX]
    if name == "set_drying_timer":
        if payload is None:
            raise ValueError("set_drying_timer needs duration_s or duration_min")
        try:
            dur = float(payload)
        except (TypeError, ValueError):
            raise ValueError("set_drying_timer needs duration_s or duration_min") from None
        if dur <= 0:
            raise ValueError("drying timer duration must be greater than zero")
        return name, json.dumps({"duration_s": dur})
    if name in ("answer_prompt", "clear_prompt"):
        if payload is None:
            return name, None
        return name, str(payload) if not isinstance(payload, str) else payload
    if payload is not None:
        return name, str(payload)
    return name, None


def fmt_duration(seconds) -> str:
    if seconds is None:
        return "—"
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


def fmt_date(iso) -> str:
    if not iso:
        return "—"
    return datetime.fromisoformat(iso).strftime("%b %d, %Y · %H:%M")


def fmt_num(value, digits: int = 1, unit: str = "") -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}{unit}"


def create_app(db_path=None) -> Flask:
    app = Flask(__name__)
    if isinstance(db_path, dict):
        app.config.update(db_path)
        app.config["DB_PATH"] = app.config.get("DB_PATH") or app.config.get("DATABASE") or db.DEFAULT_PATH
    else:
        app.config["DB_PATH"] = db_path or os.environ.get("CORDS_DB") or db.DEFAULT_PATH

    # create the tables up front, so the pages work before the supervisor has
    # ever run instead of erroring on a missing table
    conn = db.connect(app.config["DB_PATH"])
    db.init(conn)
    conn.close()

    app.jinja_env.filters["duration"] = fmt_duration
    app.jinja_env.filters["date"] = fmt_date
    app.jinja_env.filters["num"] = fmt_num

    def get_conn():
        # one connection per request; SQLite connections are cheap and must not
        # cross threads
        if "conn" not in g:
            g.conn = db.connect(app.config["DB_PATH"])
        return g.conn

    @app.teardown_appcontext
    def close_conn(_exc):
        conn = g.pop("conn", None)
        if conn is not None:
            conn.close()

    def json_error(status: int, message: str):
        return jsonify(error=message), status

    # --- pages -------------------------------------------------------------

    @app.route("/")
    def dashboard():
        return render_template("dashboard.html", active_page="dashboard")

    @app.route("/sensor-logs")
    def sensor_logs():
        batches = db.recent_batches(get_conn(), limit=50)
        selected = request.args.get("batch", type=int)
        if selected is None and batches:
            selected = batches[0]["id"]
        return render_template(
            "sensor_logs.html", active_page="logs", batches=batches, selected=selected
        )

    @app.route("/batch-history")
    def batch_history():
        batches = db.recent_batches(get_conn(), limit=200)
        return render_template("batch_history.html", active_page="history", batches=batches)

    @app.route("/control-panel")
    def control_panel():
        return render_template("control_panel.html", active_page="control")

    @app.route("/components")
    def components():
        return render_template("components.html", active_page="components")
    
    # --- read API ----------------------------------------------------------

    @app.get("/api/live")
    def api_live():
        conn = get_conn()
        live = db.live_state(conn)
        out = dict(live) if live else {"state": None, "alarms": []}
        out["supervisor"] = supervisor_status(live)
        batch_id = out.get("batch_id")
        out["batch"] = db.get_batch(conn, batch_id) if batch_id is not None else None
        lock = db.get_calibration_lock(conn)
        out["calibration_locked"] = lock is not None
        out["calibration_lock"] = lock
        return jsonify(out)

    @app.get("/api/batches")
    def api_batches():
        limit = min(request.args.get("limit", default=20, type=int), 500)
        return jsonify(db.recent_batches(get_conn(), limit=limit))

    @app.get("/api/batches/<int:batch_id>")
    def api_batch(batch_id):
        batch = db.get_batch(get_conn(), batch_id)
        if batch is None:
            return json_error(404, f"no batch #{batch_id}")
        return jsonify(batch)

    @app.get("/api/batches/<int:batch_id>/samples")
    def api_batch_samples(batch_id):
        conn = get_conn()
        if db.get_batch(conn, batch_id) is None:
            return json_error(404, f"no batch #{batch_id}")
        after = request.args.get("after", type=int)
        return jsonify(db.batch_samples(conn, batch_id, after_id=after))

    # --- commands ----------------------------------------------------------

    @app.post("/api/commands")
    def api_queue_command():
        body = request.get_json(silent=True) or {}
        raw_name = body.get("name")
        raw_payload = body.get("payload")
        try:
            name, payload = validate_command(raw_name, raw_payload)
        except ValueError as exc:
            return json_error(400, str(exc))

        conn = get_conn()
        live = db.live_state(conn)
        status = supervisor_status(live)
        if status != "running":
            return json_error(409, f"machine is not accepting commands (supervisor {status.replace('_', ' ')})")

        # Maintenance gate: applies to maintenance actions
        if name in MAINTENANCE_COMMANDS:
            # 1. Supervisor must be running (checked above)
            # 2. Machine must be IDLE
            current_state = live.get("state")
            if current_state != "idle":
                return json_error(409, f"maintenance actions require machine to be IDLE (currently {current_state})")

            # 3. Heaters off
            if live.get("heater_on"):
                return json_error(409, "maintenance actions require heaters to be off")

            # 4. Optional PIN check (CORDS_PIN env var)
            required_pin = os.environ.get("CORDS_PIN")
            if required_pin:
                given_pin = request.headers.get("X-CORDS-PIN")
                if not given_pin and isinstance(raw_payload, dict):
                    given_pin = raw_payload.get("pin")
                if given_pin != required_pin:
                    return json_error(403, "invalid or missing PIN")

            # 5. Calibration lock required (except when acquiring the lock itself)
            if name != "acquire_calibration_lock":
                lock = db.get_calibration_lock(conn)
                if lock is None:
                    return json_error(409, "calibration lock required for maintenance actions")

        # Process command: start requires calibration lock NOT held
        if name == "start" and db.is_calibration_locked(conn):
            return json_error(409, "cannot start batch: calibration in progress")

        # Direct database lock acquisition / release
        if name == "acquire_calibration_lock":
            dur = 300.0
            comp = None
            sess = None
            if isinstance(raw_payload, dict):
                try:
                    dur = float(raw_payload.get("duration_s", 300.0))
                except (TypeError, ValueError):
                    return json_error(400, "duration_s must be a number")
                dur = max(10.0, min(1800.0, dur))
                comp = raw_payload.get("component")
                sess = raw_payload.get("session_id")
            granted = db.acquire_calibration_lock(conn, duration_s=dur, component=comp, session_id=sess)
            if not granted:
                return json_error(409, "calibration lock is held by another active session")
        elif name == "release_calibration_lock":
            sess = None
            if isinstance(raw_payload, dict):
                sess = raw_payload.get("session_id")
            db.release_calibration_lock(conn, session_id=sess)

        command_id = db.queue_command(conn, name, payload)
        return jsonify(db.command_status(conn, command_id)), 202

    @app.get("/api/commands/<int:command_id>")
    def api_command_status(command_id):
        row = db.command_status(get_conn(), command_id)
        if row is None:
            return json_error(404, f"no command #{command_id}")
        return jsonify(row)

    # --- supervisor lifecycle API ------------------------------------------

    @app.get("/api/supervisor")
    def api_supervisor_status():
        svc = get_supervisor_service()
        status = svc.get_status(get_conn())
        return jsonify(status)

    @app.post("/api/supervisor/start")
    def api_supervisor_start():
        required_pin = os.environ.get("CORDS_PIN")
        body = request.get_json(silent=True) or {}
        if required_pin:
            given_pin = request.headers.get("X-CORDS-PIN") or body.get("pin")
            if given_pin != required_pin:
                return json_error(403, "invalid or missing PIN")

        svc = get_supervisor_service()
        status = svc.get_status(get_conn())
        if status["active"]:
            return json_error(409, "supervisor is already running")

        ok, msg = svc.start_service()
        if not ok:
            return json_error(500, msg)
        return jsonify({"status": "started", "message": msg, "supervisor": svc.get_status(get_conn())}), 200

    @app.post("/api/supervisor/stop")
    def api_supervisor_stop():
        required_pin = os.environ.get("CORDS_PIN")
        body = request.get_json(silent=True) or {}
        if required_pin:
            given_pin = request.headers.get("X-CORDS-PIN") or body.get("pin")
            if given_pin != required_pin:
                return json_error(403, "invalid or missing PIN")

        svc = get_supervisor_service()
        status = svc.get_status(get_conn())
        if not status["active"]:
            return json_error(409, "supervisor is already stopped")

        if status.get("batch_active"):
            confirm = str(body.get("confirm", "")).strip()
            if confirm != CONFIRMATION_PHRASE:
                return json_error(
                    409,
                    f"Batch is currently running ({status.get('batch_state')}). "
                    f"Stopping will interrupt the batch and shut off heaters and fans while the chamber may be hot. "
                    f"Type '{CONFIRMATION_PHRASE}' to confirm."
                )

        ok, msg = svc.stop_service()
        if not ok:
            return json_error(500, msg)
        return jsonify({"status": "stopped", "message": msg, "supervisor": svc.get_status(get_conn())}), 200

    @app.post("/api/supervisor/restart")
    def api_supervisor_restart():
        required_pin = os.environ.get("CORDS_PIN")
        body = request.get_json(silent=True) or {}
        if required_pin:
            given_pin = request.headers.get("X-CORDS-PIN") or body.get("pin")
            if given_pin != required_pin:
                return json_error(403, "invalid or missing PIN")

        svc = get_supervisor_service()
        status = svc.get_status(get_conn())

        if status.get("batch_active"):
            confirm = str(body.get("confirm", "")).strip()
            if confirm != CONFIRMATION_PHRASE:
                return json_error(
                    409,
                    f"Batch is currently running ({status.get('batch_state')}). "
                    f"Restarting will interrupt the batch and shut off heaters and fans while the chamber may be hot. "
                    f"Type '{CONFIRMATION_PHRASE}' to confirm."
                )

        ok, msg = svc.restart_service()
        if not ok:
            return json_error(500, msg)
        return jsonify({"status": "restarted", "message": msg, "supervisor": svc.get_status(get_conn())}), 200

    @app.errorhandler(404)
    def not_found(exc):
        if request.path.startswith("/api/"):
            return json_error(404, "not found")
        return exc

    return app


if __name__ == "__main__":
    create_app().run(host="0.0.0.0", port=5000, debug=os.environ.get("FLASK_DEBUG") == "1")
