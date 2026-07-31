from flask import Flask, render_template

app = Flask(__name__)

# Placeholder batch data — replace with real reads from the Raspberry Pi's
# batch log (e.g. a SQLite table or CSV written at the end of each cycle).
BATCHES = [
    {"id": "09", "date": "Aug 20, 2026", "duration": "5h 42m", "moisture": "6.4%", "great": 22, "good": 12, "bad": 4},
    {"id": "08", "date": "Aug 18, 2026", "duration": "6h 05m", "moisture": "6.1%", "great": 18, "good": 15, "bad": 9},
    {"id": "07", "date": "Aug 17, 2026", "duration": "5h 51m", "moisture": "7.0%", "great": 15, "good": 15, "bad": 12},
    {"id": "06", "date": "Aug 15, 2026", "duration": "5h 30m", "moisture": "6.3%", "great": 25, "good": 16, "bad": 3},
]


@app.route("/")
def dashboard():
    return render_template("dashboard.html", active_page="dashboard")


@app.route("/sensor-logs")
def sensor_logs():
    return render_template("sensor_logs.html", active_page="logs")


@app.route("/batch-history")
def batch_history():
    return render_template("batch_history.html", active_page="history", batches=BATCHES)


@app.route("/control-panel")
def control_panel():
    return render_template("control_panel.html", active_page="control")


if __name__ == "__main__":
    # host="0.0.0.0" so the dashboard is reachable from other devices on the
    # same network as the Raspberry Pi, not just localhost.
    app.run(debug=True, host="0.0.0.0", port=5000)
