# CopraHeat Dashboard · CORDS

> **CORDS** — Copra drying & sorting Dashboard System  
> A Raspberry Pi–hosted web dashboard for monitoring and controlling an automated copra drying and sorting machine.

---

## Overview

CopraHeat Dashboard is a Flask-based web application that serves as the operator interface for an automated copra (dried coconut) processing system. It provides real-time monitoring of drying chamber conditions, sensor data visualization, batch history records, and manual override controls — all accessible from any device on the local network via a Raspberry Pi host.

The system tracks the full copra processing pipeline, while also recording milestone events for each batch and surfacing operator prompts when a run needs recovery or confirmation.

```
Intake → Drying (closed-loop, static tray) → Sorting (vision-based grading) → Review / recovery
```

---

## Features

| Page | Description |
|---|---|
| **Dashboard** | Live overview polled every 2 s — phase track, heater/fan state, chamber and exhaust readings, exhaust absolute humidity (the drying endpoint signal), hopper weight vs. target, batch vision grade consensus & sample frame counts, active alarms, and any blocking prompt from the machine |
| **Sensor Logs** | Per-batch time-series charts for chamber temperature, exhaust temperature, exhaust RH, exhaust absolute humidity and hopper weight, with peak / average / lowest; a running batch refreshes itself |
| **Batch History** | Every batch with start time, total and drying time, weight in / out, post-sort weight loss, unified batch grade (`great`, `good`, `bad`, legacy, or review flag) with full sample frame breakdown tooltips; CSV export |
| **Control Panel** | Operator commands — start, end drying (manual endpoint), manual bin assignment / review override (`great`, `good`, `bad`), finish sorting, reset, abort with a reason, target batch weight, and prompt responses such as recover vs. abort after an unclean shutdown |
| **Components** | Maintenance and calibration page for calibration locks, conveyor jogs, carriage homing, hopper tare / scale calibration, servo checks, and component-level troubleshooting |

Every page header shows the supervisor heartbeat (online / stopped / not responding), and readings from a supervisor that is not running are dimmed as last-known values. Batch milestone events and active prompts are surfaced in the live API so the frontend can display recovery flows without polling ad hoc tables.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python 3 · [Flask](https://flask.palletsprojects.com/) ≥ 3.0 |
| Templating | Jinja2 (via Flask) |
| Frontend | HTML5 · Vanilla CSS · Bootstrap 5.3 · Bootstrap Icons 1.11 |
| Charts | [Chart.js](https://www.chartjs.org/) 4.4 |
| Fonts | Space Grotesk · Inter (Google Fonts) |
| Target hardware | Raspberry Pi 5 (GPIO via `gpiozero` / lgpio) |

---

## Project Structure

```
CopraHeat_dashboard/
├── app.py                  # Flask pages + JSON API over hardware/db.py (never touches GPIO)
├── requirements.txt        # Python dependencies
├── cords_model.tflite      # MobileNetV3 TFLite copra grading model
├── labels.txt              # Model class labels: Bad (0), Good (1), Great (2)
├── hardware/
│   ├── __init__.py
│   ├── pins.py             # GPIO pin map — single source of truth (BCM numbering)
│   ├── states.py           # BatchState / ConveyorCommand — shared vocabulary
│   ├── batch.py            # Batch state machine: Intake → Drying → Sorting + active prompt handling
│   ├── grading.py          # Batch-level ensemble sampling, soft-voting, visual contamination guard
│   ├── climate.py          # Heater hysteresis + fan interlock logic (no GPIO; unit-tested)
│   ├── hopper.py           # Hopper gate state machine + stable-weight filter (no GPIO; unit-tested)
│   ├── drying.py           # Drying endpoint from exhaust absolute humidity (no GPIO; unit-tested)
│   ├── vision.py           # USB webcam capture with Laplacian blur & confidence filters
│   ├── sensors.py          # DS18B20 / DHT22 readers via kernel sysfs
│   ├── gpiochip.py         # Opens the Pi 5's RP1 GPIO chip for lgpio users
│   ├── hx711.py            # HX711 load-cell driver, bit-banged through lgpio
│   ├── stepper.py          # TB6600 / HBS57H step+dir driver (lgpio tx_pulse)
│   ├── limits.py           # Bin-carriage limit switches (NC, internal pull-ups)
│   ├── carriage.py         # Bin-carriage positioning logic (MoveWatch)
│   ├── servo.py            # MG996R on hardware PWM (rpi-hardware-pwm)
│   ├── db.py               # SQLite data layer & schema v3 migrations (Flask ↔ supervisor IPC)
│   ├── motion.py           # Non-blocking motion layer (conveyor & carriage drivers)
│   ├── conveyor.py         # Conveyor interface wrappers
│   ├── simulate.py         # Fake machine for --simulate runs (test fixture, not production)
│   ├── run_cords.py        # Supervisor: the one process that owns every pin
│   ├── walkthrough.py      # Guided interactive end-to-end testing with physical hardware/mocking
│   ├── run_climate.py      # Bench loop: sensors → controllers → SSR / fan relay
│   ├── run_hopper.py       # Bench loop + --calibrate: HX711 → gate logic → servo
│   └── bench.py            # Wiring checks: steppers, limit switches, all 4 scales
├── tests/                  # python -m unittest discover -s tests
├── static/
│   ├── css/
│   │   └── style.css       # Custom design system & component styles
│   └── js/
│       ├── common.js       # Shared: /api/live poller, header pill + alarm bell, sendCommand()
│       ├── dashboard.js    # Dashboard page (live overview & grade badge)
│       ├── sensor_logs.js  # Sensor Logs chart
│       ├── batch_history.js# CSV export
│       ├── control_panel.js# Operator commands & manual grade assignment
│       └── components.js   # Maintenance/calibration controls and live status widgets
└── templates/
    ├── base.html           # Shared layout (sidebar nav, header, CDN links)
    ├── dashboard.html      # Live batch overview
    ├── sensor_logs.html    # Sensor time-series charts
    ├── batch_history.html  # Batch records table (with legacy & review indicators)
    ├── control_panel.html  # Operator commands
    └── components.html     # Calibration, bench checks, and machine health tools
```

---

## Getting Started

### Prerequisites

- Python 3.9 or higher
- pip

### Installation

```bash
# 1. Clone the repository
git clone <repo-url>
cd CopraHeat_dashboard

# 2. Create a virtual environment
#    Windows (dashboard + unit tests only)
python -m venv .venv
.venv\Scripts\activate
#    Raspberry Pi OS — required (PEP 668 blocks system pip), and it MUST see the
#    system packages: lgpio and picamera2 come from apt, not pip.
python -m venv --system-site-packages .venv
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt
```

### Running the Server

```bash
python app.py                 # FLASK_DEBUG=1 for the reloader/debugger — never on the Pi
```

`CORDS_DB=<path>` points the dashboard at a different database (default `cords.db` next to `app.py`).

### Demo without hardware

Two terminals, same database:

```bash
python -m hardware.run_cords --simulate --speed 60 --interval 0.02   # the fake machine
python app.py                                                          # the dashboard
```

Open `http://localhost:5000`, press **Start** on the Control Panel, and a whole batch runs in a few minutes.

### Running the Machine

The supervisor owns every pin; the dashboard never touches GPIO. Only one supervisor may run per database — a second one refuses to start while the first's heartbeat is fresh.

```bash
# whole batch against a fake machine, no hardware needed (minutes, not hours)
python -m hardware.run_cords --simulate --speed 60 --interval 0.02 --start --exit-on-finish

# on the Pi, with calibration values from `run_hopper --calibrate`
python -m hardware.run_cords --start --scale 21500 --offset-closed 84210 --offset-open 83950
```

`run_climate.py` and `run_hopper.py` remain for single-subsystem bench work.

### Bench-testing the sorting-side wiring

Stop `run_cords` first: it holds the same pins.

```bash
python -m hardware.bench limits                    # press / unplug each switch: must read HIGH
python -m hardware.bench scales                    # all 4 HX711s, raw counts + noise spread
python -m hardware.bench scale-cal great           # tare + scale one bin cell (great/good/bad)
python -m hardware.bench jog conveyor --steps 400  # exact step count at --hz (default 400)
python -m hardware.bench jog carriage --steps 2000 # stops early on any limit switch
python -m hardware.bench sort                      # type great / good / bad: carriage goes to that bin
python -m hardware.bench sort great bad good       # or run a fixed sequence
```

`sort` finds the carriage first: resting on a switch, or homing down to the nearest one. Defaults are
placeholders until confirmed on the machine: switches bottom → top `--order LS3 LS2 LS1`, bins at
those stops `--bins bad good great`, and forward = up (`--up-reverse` flips it). Set `--max-steps`
to a little over the full carriage travel (measure it with `jog`): it is the only cap on the very
first move if the motor turns out to be wired backwards. After that, each pair of stops gets a cap
learned from its own first trip.

The carriage jog refuses to start on a pressed switch unless `--from-switch` is given, and stops if
that switch never releases (driving into the stop). Run `scales` while a stepper jogs to check
whether motor wiring disturbs the long load-cell leads.

The server binds to `0.0.0.0:5000`, making the dashboard reachable from any device on the same network:

| Device | URL |
|---|---|
| Raspberry Pi (local) | `http://localhost:5000` |
| Other devices on LAN | `http://<raspberry-pi-ip>:5000` |

---

## Pages & Routes

| Route | View Function | Page |
|---|---|---|
| `/` | `dashboard()` | Dashboard |
| `/sensor-logs` | `sensor_logs()` | Sensor Logs |
| `/batch-history` | `batch_history()` | Batch History |
| `/control-panel` | `control_panel()` | Control Panel |
| `/components` | `components()` | Components / maintenance |

**JSON API** (what the pages poll; usable from any client on the LAN)

| Method & route | Returns |
|---|---|
| `GET /api/live` | Machine snapshot + `supervisor` (`running` / `stopped` / `not_responding` / `never_run`), current `batch`, active prompt, and a `batch_events` list for the active batch |
| `GET /api/batches?limit=N` | Recent batches, newest first |
| `GET /api/batches/<id>` | One batch |
| `GET /api/batches/<id>/samples?after=<sample id>` | Its `sensor_log` rows; `after` returns only newer ones |
| `GET /api/batches/<id>/events` | Timestamped milestone events such as start, drying transitions, gate commands, and recovery actions |
| `POST /api/commands` `{"name", "payload"}` | `202` + the queued command. `400` for a bad name or payload, `409` if the supervisor is not running |
| `GET /api/commands/<id>` | `pending`, then `done` / `rejected` with the machine's message |

The active prompt system allows a supervisor to ask the dashboard to respond to blocking decisions (for example, recover vs. abort after an unclean shutdown or confirm a timed-out drying cycle). These prompts are serialized in `machine_state.active_prompt` and exposed through `/api/live` as JSON.

---

## Configuration & Data

All state lives in a SQLite database (`cords.db`, created on first run, git-ignored). **The database is also the IPC**: the supervisor writes machine state and consumes an operator command queue; Flask writes commands and reads everything else. Flask never touches GPIO.

| Table | Written by | Purpose |
|---|---|---|
| `machine_state` | supervisor | Single-row live snapshot, including a heartbeat so the UI can tell live data from a dead process plus the current `active_prompt` payload |
| `sensor_log` | supervisor | Time series behind the Sensor Logs charts |
| `batches` | supervisor | One row per batch — inserted at start, updated with v3 batch-level grade consensus, operator override, review flags, sample vectors, and configuration snapshot |
| `batch_events` | supervisor | Timestamped process milestones (`batch_started`, `drying_started`, `gate_opened`, recovery / abort decisions, etc.) that keep a searchable event trail for each batch |
| `commands` | Flask | Operator queue (`start`, `end_drying`, `override_grade`, `finish_sorting`, `abort`, `reset`, `set_target_kg`, `answer_prompt`, `clear_prompt`) with per-command results |
| `settings` | supervisor | Calibration parameters (HX711 scale/offsets, servo angles, vision grading thresholds, belt travel steps) |
| `calibration_lock` | Flask / supervisor | Exclusivity lock preventing batch starts during maintenance/calibration sessions |

A rejected command keeps the machine's own words (`"cannot start from drying"`), so the dashboard can show why rather than failing silently. Commands are never left waiting for a supervisor that is not there: Flask refuses them with `409`, and anything still pending when a supervisor starts is expired rather than run late.

Schema versioning is guarded by `PRAGMA user_version = 3`. Upgrades from prior versions run automatically or via CLI:
```bash
python -m hardware.db backup   # Creates cords.db.bak.<timestamp>
python -m hardware.db migrate  # Applies additive columns preserving historical data
```

**Not in `sensor_log`, deliberately:** there is no ambient column (the DHT22 sits by the exhaust, not outside), no live moisture column (moisture is not measured), and no live weight-loss column (weight loss is a post-sort figure on `batches`). The pages follow the schema: none of the three is shown as a live reading.

---

## Hardware Context

The dashboard is the operator interface for a custom copra processing machine, controlled entirely by a Raspberry Pi 5:

- **Hopper** with an MG996R servo gate, held closed under load and opened when the hopper's HX711 load cell reads the target batch weight
- **Conveyor** — heat-resistant stainless steel mesh belt, driven by a 57HS82 stepper via an HBS57H driver
- **Drying chamber** — 3× 220 V ceramic IR heater bulbs switched by a Fotek SSR, with a 60–70 °C two-point hysteresis loop on a DS18B20 probe; 24 V intake/exhaust fans switched together via a relay
- **Exhaust sensing** — DHT22 inside the chamber by the exhaust fan; its absolute humidity is the drying endpoint signal (there is no ambient sensor)
- **Computer-vision module** — 1080p USB webcam + MobileNetV3 (`cords_model.tflite`). During the sorting phase, the belt creeps to sample $N$ distinct positions along the batch. Consensus is calculated via soft-voting across valid frames, with a visual contamination guard forcing `Bad` if mould/char exceed threshold $\tau_{\text{bad}}$, and an operator review prompt if low confidence occurs.
- **Bin carriage** — 57BYGH420 stepper via a TB6600 driver, moving vertically between three bin heights; three normally-closed limit switches act as hard position stops
- **Bin load cells** — one HX711 per output bin; target weight loss during drying is ~43–47% (≈50% → ≤6% moisture)

---

## Hardware Pin Map

[`hardware/pins.py`](hardware/pins.py) is the single source of truth for GPIO assignments (BCM numbering). Every control script imports from it — never hardcode a pin number elsewhere.

| Subsystem | Signal | BCM | Physical pin |
|---|---|---|---|
| Sensors | DS18B20 data (1-Wire) | 4 | 7 |
| | DHT22 data | 26 | 37 |
| Load cells | HX711 Hopper — DOUT / SCK | 7 / 8 | 26 / 24 |
| | HX711 Bin-Great — DOUT / SCK | 9 / 10 | 21 / 19 |
| | HX711 Bin-Good — DOUT / SCK | 15 / 14 | 10 / 8 |
| | HX711 Bin-Bad — DOUT / SCK | 13 / 16 | 33 / 36 |
| Motors | TB6600 PUL / DIR (bin carriage) | 17 / 27 | 11 / 13 |
| | HBS57H PUL / DIR (conveyor) | 22 / 23 | 15 / 16 |
| Servo | MG996R hopper gate (hardware PWM) | 18 | 12 |
| Limit switches | Switch 1 / 2 / 3 | 24 / 20 / 21 | 18 / 38 / 40 |
| Triggers | Fotek SSR (heaters) | 5 | 29 |
| | Fan relay (via 2N2222) | 25 | 22 |

Reserved: GPIO0/1 (HAT EEPROM), GPIO2/3 (I²C), GPIO14/15 (UART), GPIO19 (kept clear for the servo's PWM pair).

**Board and OS requirements**

- SPI must stay **disabled** in `raspi-config` — GPIO7–11 are the SPI0 block; enabling it conflicts with HX711 lines on GPIO7–10.
- 10 kΩ pull-downs on the heater SSR trigger (GPIO5 → GND) and on the fan transistor base, so neither can fire while GPIO floats during boot or a crashed control process.
- 4.7 kΩ pull-up on the DS18B20 data line, plus `dtoverlay=w1-gpio,gpiopin=4` in `/boot/firmware/config.txt`.
- DHT22 data is on GPIO26 (physical pin 37). It is read through the kernel IIO driver, not a Python bit-bang library (unreliable on the Pi 5's RP1): add `dtoverlay=dht11,gpiopin=26` to `config.txt` — the `dht11` driver handles DHT22 as well.
- The servo uses hardware PWM: add `dtoverlay=pwm-2chan` to `config.txt`, then confirm with `pinctrl get 18` and `ls /sys/class/pwm/`. On the current Pi 5 setup GPIO18 is pwmchip0 / channel 2; verify on the target board and pass `--pwm-chip` / `--pwm-channel` to `run_hopper` if different. The hopper gate is configured as 0° open and 180° closed.
- HX711 tare offsets and scale are per-machine: run `python -m hardware.run_hopper --calibrate` after fabrication and pass the printed flags. The hopper cell is mounted on the gate, so it is tared in both the closed and open positions.
- Stepper driver inputs (TB6600, HBS57H) use 2N2222A NPN sinking transistors on PUL/DIR, with PUL+ and DIR+ tied to a 5 V rail (common-anode sinking configuration). Relay and SSR triggers use an NPN buffer.
- Limit switches are wired normally-closed with internal pull-ups, so a broken wire reads as "stop".
- Use `gpiozero` (lgpio backend). Legacy `RPi.GPIO` does not work on the Pi 5's RP1 GPIO controller.

---

## Development Notes

- Debug is off unless `FLASK_DEBUG=1`: the Werkzeug debugger executes arbitrary code for anyone who can reach port 5000, and the server binds to the whole LAN.
- The Control Panel has **no authentication yet**. Anyone on the network can start or abort a batch — add a login before the machine is on a shared network.
- The web Abort is a software stop, not an emergency stop. An e-stop must cut heater and motor power in hardware, independent of the Pi.
- The sidebar navigation highlights the active page using Jinja2 `active_page` context variables passed from each route.

---

## License

This project is part of an undergraduate thesis. All rights reserved by the author(s).
