"""CORDS dashboard. Reads and writes the SQLite database, and nothing else.

The supervisor (`python -m hardware.run_cords`) owns every pin; this process
queues operator commands into the database and reads back what the machine
did with them. See hardware/db.py for the tables.
"""

import math
import os
from datetime import datetime

from flask import Flask, g, jsonify, render_template, request

from hardware import db
from hardware.batch import COMMAND_NAMES

ABORT_REASON_MAX = 200


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
    message to show the operator. The machine re-validates everything; this
    only catches what is wrong regardless of machine state."""
    if name not in COMMAND_NAMES:
        raise ValueError(f"unknown command {name!r}")
    if name == "set_target_kg":
        try:
            value = float(payload)
        except (TypeError, ValueError):
            raise ValueError("target weight must be a number of kilograms") from None
        if not math.isfinite(value) or value <= 0:
            raise ValueError("target weight must be greater than zero")
        return name, repr(value)
    if name == "abort":
        reason = (payload or "").strip() or "operator abort"
        return name, reason[:ABORT_REASON_MAX]
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

    # --- read API ----------------------------------------------------------

    @app.get("/api/live")
    def api_live():
        conn = get_conn()
        live = db.live_state(conn)
        out = dict(live) if live else {"state": None, "alarms": []}
        out["supervisor"] = supervisor_status(live)
        batch_id = out.get("batch_id")
        out["batch"] = db.get_batch(conn, batch_id) if batch_id is not None else None
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
        try:
            name, payload = validate_command(body.get("name"), body.get("payload"))
        except ValueError as exc:
            return json_error(400, str(exc))

        conn = get_conn()
        # refuse rather than queue: a command left waiting for a dead
        # supervisor would fire whenever it next came up
        status = supervisor_status(db.live_state(conn))
        if status != "running":
            return json_error(409, f"machine is not accepting commands (supervisor {status.replace('_', ' ')})")

        command_id = db.queue_command(conn, name, payload)
        return jsonify(db.command_status(conn, command_id)), 202

    @app.get("/api/commands/<int:command_id>")
    def api_command_status(command_id):
        row = db.command_status(get_conn(), command_id)
        if row is None:
            return json_error(404, f"no command #{command_id}")
        return jsonify(row)

    @app.errorhandler(404)
    def not_found(exc):
        if request.path.startswith("/api/"):
            return json_error(404, "not found")
        return exc

    return app


if __name__ == "__main__":
    # host="0.0.0.0" so the dashboard is reachable from other devices on the
    # same network as the Raspberry Pi, not just localhost. Debug stays opt-in
    # (FLASK_DEBUG=1): the Werkzeug debugger runs arbitrary code for anyone who
    # can reach the port.
    create_app().run(host="0.0.0.0", port=5000, debug=os.environ.get("FLASK_DEBUG") == "1")
