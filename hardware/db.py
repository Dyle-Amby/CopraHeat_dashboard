"""SQLite data layer. No Flask, no GPIO: both sides of the machine talk through here.

The database is also the IPC. The supervisor (`run_cords`) writes `machine_state`,
`sensor_log` and `batches` and consumes the `commands` queue; Flask writes
`commands` and `settings` and reads everything else. Neither process reaches past
this module, and Flask never touches a pin.

WAL mode so a reader never blocks the writer, `synchronous=NORMAL` because this
lives on an SD card, and a busy timeout because there are two writers. Keep every
transaction short.

Connections are not shared between threads: open one per process, or per request
under Flask -- opening a SQLite connection is cheap.

PRAGMA user_version guards the schema with additive migrations.
"""

from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3

from hardware.batch import COMMAND_NAMES, BatchRecord

SCHEMA_VERSION = 3

# app.py must run from inside CopraHeat_dashboard, but the supervisor may not,
# so anchor the default to this file rather than the working directory
DEFAULT_PATH = Path(__file__).resolve().parent.parent / "cords.db"

PENDING = "pending"
DONE = "done"
REJECTED = "rejected"

# Recognized maintenance and process commands
VALID_COMMAND_NAMES = COMMAND_NAMES | frozenset({
    "acquire_calibration_lock",
    "release_calibration_lock",
    "jog_conveyor",
    "stop_conveyor",
    "save_conveyor_leg",
    "set_conveyor_p00",
    "mark_conveyor_stale",
    "goto_carriage_bin",
    "home_carriage",
    "tare_hopper",
    "calibrate_hopper_scale",
    "calibrate_bin_scale",
    "set_hopper_servo",
    "capture_camera_preview",
    "test_component",
})

DEFAULT_SETTINGS = {
    "target_kg": "2.0",
    "hx711_scale": "1.0",
    "hx711_offset_closed": "0",
    "hx711_offset_open": "0",
    "servo_closed_angle": "180.0",
    "servo_open_angle": "0.0",
    "pwm_chip": "0",
    "pwm_channel": "2",
    "sample_interval_s": "10.0",
    # Unmeasured placeholder vision grading configuration
    "sample_mode": "jog",
    "n_samples": "20",
    "min_valid": "10",
    "max_attempts": "40",
    "retry_max": "3",
    "steps_per_sample": "200",
    "sample_hz": "200.0",
    "settle_s": "0.3",
    "tau_bad": "0.20",
    "min_confidence": "0.55",
    "blur_threshold": "100.0",
    "max_rejected_ratio": "0.40",
    "camera_to_discharge_steps": "5000",
    "discharge_total_steps": "3000",
    "safety_margin_steps": "500",
    "discharge_timeout_s": "60.0",
    "carriage_move_timeout_s": "30.0",
    # UNCALIBRATED: 0 means the real stepper driver must refuse to run.
    # Measure with: python -m hardware.bench jog conveyor --steps 500 --hz 400
    # then save with: python -m hardware.run_cords --save-settings --hopper-to-chamber-steps <N>
    "hopper_to_chamber_steps": "0",
    "chamber_to_camera_steps": "0",
    "conveyor_p00": "10000",
    "conveyor_hz": "2000.0",
    "conveyor_cal_stale": "0",
    "leg_hopper_to_chamber_meta": "{}",
    "leg_chamber_to_camera_meta": "{}",
    "leg_camera_to_discharge_meta": "{}",
    "intake_advance_timeout_s": "30.0",
    # Fan pulse & warm-up airflow configuration
    "fan_warmup_s": "1200.0",
    "fan_pulse_on_s": "60.0",
    "fan_pulse_period_s": "300.0",
    "fan_rh_on": "60.0",
    "fan_rh_off": "45.0",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at              TEXT    NOT NULL,
    finished_at             TEXT,
    state                   TEXT    NOT NULL,
    duration_s              REAL,
    drying_s                REAL,
    target_kg               REAL,
    weight_in_kg            REAL,
    weight_out_kg           REAL,
    weight_loss_pct         REAL,
    aborted                 INTEGER NOT NULL DEFAULT 0,
    abort_reason            TEXT,
    alarms                  TEXT,
    -- vision output; legacy per-piece count columns preserved untouched
    great                   INTEGER,
    good                    INTEGER,
    bad                     INTEGER,
    final_moisture_pct      REAL,
    -- v2 additive batch-level ensemble vision grading columns
    batch_grade             TEXT,
    operator_grade          TEXT,
    override_used           INTEGER NOT NULL DEFAULT 0,
    needs_review            INTEGER NOT NULL DEFAULT 0,
    sample_vectors          TEXT,
    sample_valid_n          INTEGER NOT NULL DEFAULT 0,
    sample_total_n          INTEGER NOT NULL DEFAULT 0,
    sorting_config_snapshot TEXT,
    -- v3 additive batch column for non-aborting 8 h ceiling
    drying_timed_out        INTEGER NOT NULL DEFAULT 0,
    drying_timer_duration_s REAL,
    drying_timer_deadline_s REAL
);

-- No ambient column and no live moisture or weight-loss column: there is no
-- ambient sensor, moisture is not measured, and weight loss is a post-sort
-- figure that belongs on `batches`.
CREATE TABLE IF NOT EXISTS sensor_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id   INTEGER REFERENCES batches(id),
    ts         TEXT    NOT NULL,
    elapsed_s  REAL,
    state      TEXT    NOT NULL,
    chamber_c  REAL,
    exhaust_c  REAL,
    exhaust_rh REAL,
    exhaust_ah REAL,
    hopper_kg  REAL,
    heater_on  INTEGER NOT NULL DEFAULT 0,
    fans_on    INTEGER NOT NULL DEFAULT 0,
    fan_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_sensor_log_batch ON sensor_log(batch_id, id);

CREATE TABLE IF NOT EXISTS commands (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    name        TEXT NOT NULL,
    payload     TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',
    result      TEXT,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_commands_pending ON commands(status, id);

CREATE TABLE IF NOT EXISTS batch_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id    INTEGER REFERENCES batches(id),
    ts          TEXT    NOT NULL,
    event_type  TEXT    NOT NULL,
    details     TEXT
);

CREATE INDEX IF NOT EXISTS idx_batch_events_batch ON batch_events(batch_id, id);

CREATE TABLE IF NOT EXISTS machine_state (
    id             INTEGER PRIMARY KEY CHECK (id = 1),
    updated_at     TEXT    NOT NULL,
    supervisor_pid INTEGER,
    batch_id       INTEGER,
    state          TEXT    NOT NULL,
    elapsed_s      REAL,
    chamber_c      REAL,
    exhaust_c      REAL,
    exhaust_rh     REAL,
    exhaust_ah     REAL,
    hopper_kg      REAL,
    target_kg      REAL,
    heater_on      INTEGER NOT NULL DEFAULT 0,
    fans_on        INTEGER NOT NULL DEFAULT 0,
    fan_reason     TEXT,
    gate_open      INTEGER NOT NULL DEFAULT 0,
    conveyor       TEXT,
    alarms         TEXT,
    components     TEXT,
    active_prompt  TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS calibration_lock (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    acquired_at TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    session_id  TEXT,
    component   TEXT
);
"""


def now_iso() -> str:
    """Local wall clock. The state machine runs on a monotonic clock; turning
    that into a timestamp is this layer's job, which is why it happens here."""
    return datetime.now().isoformat(timespec="seconds")


def connect(path: str | Path | None = None, *, timeout: float = 5.0) -> sqlite3.Connection:
    path = Path(DEFAULT_PATH if path is None else path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=timeout, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
    return conn


def backup_db(db_path: Path | str | None = None) -> Path:
    """Create a timestamped backup copy of cords.db."""
    src = Path(DEFAULT_PATH if db_path is None else db_path)
    if not src.exists():
        return src
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = src.parent / f"{src.name}.bak.{timestamp}"
    import shutil
    shutil.copy2(src, backup_path)
    return backup_path


def migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """Migrate schema from version 1 to 2 by adding batch-level vision grading columns."""
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='batches'")
    if cur.fetchone() is not None:
        cur.execute("PRAGMA table_info(batches)")
        columns = [row[1] for row in cur.fetchall()]
        new_cols = [
            ("batch_grade", "TEXT"),
            ("operator_grade", "TEXT"),
            ("override_used", "INTEGER NOT NULL DEFAULT 0"),
            ("needs_review", "INTEGER NOT NULL DEFAULT 0"),
            ("sample_vectors", "TEXT"),
            ("sample_valid_n", "INTEGER NOT NULL DEFAULT 0"),
            ("sample_total_n", "INTEGER NOT NULL DEFAULT 0"),
            ("sorting_config_snapshot", "TEXT"),
        ]
        for col_name, col_type in new_cols:
            if col_name not in columns:
                cur.execute(f"ALTER TABLE batches ADD COLUMN {col_name} {col_type}")
    conn.execute("PRAGMA user_version = 2")


def migrate_v2_to_v3(conn: sqlite3.Connection) -> None:
    """Migrate schema from version 2 to 3 by adding components, drying_timed_out, and calibration_lock."""
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='batches'")
    if cur.fetchone() is not None:
        cur.execute("PRAGMA table_info(batches)")
        b_cols = [row[1] for row in cur.fetchall()]
        if "drying_timed_out" not in b_cols:
            cur.execute("ALTER TABLE batches ADD COLUMN drying_timed_out INTEGER NOT NULL DEFAULT 0")

    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='machine_state'")
    if cur.fetchone() is not None:
        cur.execute("PRAGMA table_info(machine_state)")
        m_cols = [row[1] for row in cur.fetchall()]
        if "components" not in m_cols:
            cur.execute("ALTER TABLE machine_state ADD COLUMN components TEXT")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS calibration_lock (
        id          INTEGER PRIMARY KEY CHECK (id = 1),
        acquired_at TEXT NOT NULL,
        expires_at  TEXT NOT NULL,
        session_id  TEXT,
        component   TEXT
    );
    """)

    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='settings'")
    if cur.fetchone() is not None:
        stamp = now_iso()
        cur.executemany(
            "INSERT OR IGNORE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
            [(k, v, stamp) for k, v in DEFAULT_SETTINGS.items()],
        )
    conn.execute("PRAGMA user_version = 3")


def _ensure_fan_reason_columns(conn: sqlite3.Connection) -> None:
    """Add fan_reason column to sensor_log and machine_state if missing."""
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='sensor_log'")
    if cur.fetchone() is not None:
        cur.execute("PRAGMA table_info(sensor_log)")
        s_cols = [row[1] for row in cur.fetchall()]
        if s_cols and "fan_reason" not in s_cols:
            cur.execute("ALTER TABLE sensor_log ADD COLUMN fan_reason TEXT")

    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='machine_state'")
    if cur.fetchone() is not None:
        cur.execute("PRAGMA table_info(machine_state)")
        m_cols = [row[1] for row in cur.fetchall()]
        if m_cols and "fan_reason" not in m_cols:
            cur.execute("ALTER TABLE machine_state ADD COLUMN fan_reason TEXT")


def _ensure_prompt_and_timer_columns(conn: sqlite3.Connection) -> None:
    """Add drying_timer columns to batches and active_prompt to machine_state if missing."""
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='batches'")
    if cur.fetchone() is not None:
        cur.execute("PRAGMA table_info(batches)")
        b_cols = [row[1] for row in cur.fetchall()]
        if "drying_timer_duration_s" not in b_cols:
            cur.execute("ALTER TABLE batches ADD COLUMN drying_timer_duration_s REAL")
        if "drying_timer_deadline_s" not in b_cols:
            cur.execute("ALTER TABLE batches ADD COLUMN drying_timer_deadline_s REAL")

    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='machine_state'")
    if cur.fetchone() is not None:
        cur.execute("PRAGMA table_info(machine_state)")
        m_cols = [row[1] for row in cur.fetchall()]
        if "active_prompt" not in m_cols:
            cur.execute("ALTER TABLE machine_state ADD COLUMN active_prompt TEXT")


def init(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == 1:
        migrate_v1_to_v2(conn)
        migrate_v2_to_v3(conn)
    elif version == 2:
        migrate_v2_to_v3(conn)
    elif version not in (0, SCHEMA_VERSION):
        raise RuntimeError(
            f"database schema is version {version}, this build expects {SCHEMA_VERSION}. "
            "There are no migrations for this version -- move the file aside or run backup."
        )
    conn.executescript(SCHEMA)
    _ensure_fan_reason_columns(conn)
    _ensure_prompt_and_timer_columns(conn)
    _ensure_batch_events_table(conn)
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    stamp = now_iso()
    conn.executemany(
        "INSERT OR IGNORE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
        [(k, v, stamp) for k, v in DEFAULT_SETTINGS.items()],
    )
    legacy_servo_defaults = conn.execute(
        """SELECT COUNT(*) FROM settings WHERE
               (key = 'servo_closed_angle' AND value = '0.0') OR
               (key = 'servo_open_angle' AND value = '90.0')"""
    ).fetchone()[0]
    legacy_pwm_default = conn.execute(
        "SELECT value FROM settings WHERE key = 'pwm_chip'"
    ).fetchone()
    if legacy_servo_defaults == 2:
        conn.executemany(
            "UPDATE settings SET value = ?, updated_at = ? WHERE key = ?",
            [
                ("180.0", stamp, "servo_closed_angle"),
                ("0.0", stamp, "servo_open_angle"),
            ],
        )
    if legacy_pwm_default is not None and legacy_pwm_default["value"] == "2":
        conn.execute(
            "UPDATE settings SET value = '0', updated_at = ? WHERE key = 'pwm_chip'",
            (stamp,),
        )

def _ensure_batch_events_table(conn: sqlite3.Connection) -> None:
    """Ensure batch_events exists and normalize its event column."""
    conn.execute("""
    CREATE TABLE IF NOT EXISTS batch_events (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id   INTEGER REFERENCES batches(id),
        ts         TEXT NOT NULL,
        event_type TEXT NOT NULL,
        details    TEXT
    );
    """)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(batch_events)")}
    if "event_type" not in columns:
        if "event" not in columns:
            raise RuntimeError("batch_events table has no event or event_type column")
        conn.execute("ALTER TABLE batch_events RENAME COLUMN event TO event_type")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_batch_events ON batch_events(batch_id, id);")


def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    if "alarms" in out:
        out["alarms"] = json.loads(out["alarms"]) if out["alarms"] else []
    if "sample_vectors" in out and out["sample_vectors"]:
        try:
            out["sample_vectors"] = json.loads(out["sample_vectors"])
        except Exception:
            pass
    if "override_used" in out:
        out["override_used"] = bool(out["override_used"])
    if "needs_review" in out:
        out["needs_review"] = bool(out["needs_review"])
    if "drying_timed_out" in out:
        out["drying_timed_out"] = bool(out["drying_timed_out"])
    if "components" in out and out["components"]:
        try:
            out["components"] = json.loads(out["components"])
        except Exception:
            pass
    if "active_prompt" in out and out["active_prompt"]:
        try:
            out["active_prompt"] = json.loads(out["active_prompt"])
        except Exception:
            pass
    return out


# --- supervisor writes -------------------------------------------------------


def start_batch(conn: sqlite3.Connection, *, target_kg: float, state: str = "intake") -> int:
    cur = conn.execute(
        "INSERT INTO batches (started_at, state, target_kg) VALUES (?, ?, ?)",
        (now_iso(), state, target_kg),
    )
    return cur.lastrowid


def set_batch_state(conn: sqlite3.Connection, batch_id: int, state: str) -> None:
    conn.execute(
        "UPDATE batches SET state = ? WHERE id = ? AND finished_at IS NULL", (state, batch_id)
    )


def set_batch_drying_timed_out(conn: sqlite3.Connection, batch_id: int, timed_out: bool = True) -> None:
    conn.execute(
        "UPDATE batches SET drying_timed_out = ? WHERE id = ?",
        (int(timed_out), batch_id),
    )


def set_batch_drying_timer(
    conn: sqlite3.Connection, batch_id: int, duration_s: float | None, deadline_s: float | None
) -> None:
    conn.execute(
        "UPDATE batches SET drying_timer_duration_s = ?, drying_timer_deadline_s = ? WHERE id = ?",
        (duration_s, deadline_s, batch_id),
    )


def mark_interrupted_batches(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        """UPDATE batches
              SET finished_at = ?, state = 'interrupted', aborted = 1,
                  abort_reason = 'supervisor stopped mid-batch'
            WHERE finished_at IS NULL""",
        (now_iso(),),
    )
    return cur.rowcount


def finish_batch(conn: sqlite3.Connection, batch_id: int, record: BatchRecord, state: str) -> None:
    conn.execute(
        """UPDATE batches SET
               finished_at = ?, state = ?, duration_s = ?, drying_s = ?,
               weight_in_kg = ?, weight_out_kg = ?, weight_loss_pct = ?,
               aborted = ?, abort_reason = ?, alarms = ?,
               batch_grade = ?, operator_grade = ?, override_used = ?,
               needs_review = ?, sample_vectors = ?, sample_valid_n = ?,
               sample_total_n = ?, sorting_config_snapshot = ?,
               drying_timed_out = ?, drying_timer_duration_s = ?, drying_timer_deadline_s = ?
           WHERE id = ?""",
        (
            now_iso(),
            state,
            record.duration_s,
            record.drying_s,
            record.weight_in_kg,
            record.weight_out_kg,
            record.weight_loss_pct,
            int(record.aborted),
            record.abort_reason,
            json.dumps(sorted(record.alarms)),
            record.batch_grade,
            record.operator_grade,
            int(record.override_used),
            int(record.needs_review),
            json.dumps(record.sample_vectors) if record.sample_vectors is not None else None,
            record.sample_valid_n,
            record.sample_total_n,
            record.sorting_config_snapshot,
            int(getattr(record, "drying_timed_out", False)),
            getattr(record, "drying_timer_duration_s", None),
            getattr(record, "drying_timer_deadline", None),
            batch_id,
        ),
    )


def log_sample(
    conn: sqlite3.Connection,
    *,
    batch_id: int | None,
    state: str,
    elapsed_s: float | None = None,
    chamber_c: float | None = None,
    exhaust_c: float | None = None,
    exhaust_rh: float | None = None,
    exhaust_ah: float | None = None,
    hopper_kg: float | None = None,
    heater_on: bool = False,
    fans_on: bool = False,
    fan_reason: str | None = None,
) -> None:
    conn.execute(
        """INSERT INTO sensor_log
               (batch_id, ts, elapsed_s, state, chamber_c, exhaust_c, exhaust_rh,
                exhaust_ah, hopper_kg, heater_on, fans_on, fan_reason)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            batch_id, now_iso(), elapsed_s, state, chamber_c, exhaust_c,
            exhaust_rh, exhaust_ah, hopper_kg, int(heater_on), int(fans_on),
            fan_reason,
        ),
    )


def update_live(
    conn: sqlite3.Connection,
    *,
    state: str,
    supervisor_pid: int | None,
    batch_id: int | None = None,
    elapsed_s: float | None = None,
    chamber_c: float | None = None,
    exhaust_c: float | None = None,
    exhaust_rh: float | None = None,
    exhaust_ah: float | None = None,
    hopper_kg: float | None = None,
    target_kg: float | None = None,
    heater_on: bool = False,
    fans_on: bool = False,
    fan_reason: str | None = None,
    gate_open: bool = False,
    conveyor: str | None = None,
    alarms: frozenset[str] | list[str] | None = None,
    components: dict | str | None = None,
    active_prompt: dict | str | None = None,
) -> None:
    comp_json = json.dumps(components) if isinstance(components, dict) else (components or "{}")
    prompt_json = None
    if active_prompt is not None:
        if hasattr(active_prompt, "to_dict"):
            prompt_json = json.dumps(active_prompt.to_dict())
        elif isinstance(active_prompt, dict):
            prompt_json = json.dumps(active_prompt)
        elif isinstance(active_prompt, str):
            prompt_json = active_prompt
    conn.execute(
        """INSERT OR REPLACE INTO machine_state
               (id, updated_at, supervisor_pid, batch_id, state, elapsed_s,
                chamber_c, exhaust_c, exhaust_rh, exhaust_ah, hopper_kg, target_kg,
                heater_on, fans_on, fan_reason, gate_open, conveyor, alarms, components, active_prompt)
           VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            now_iso(), supervisor_pid, batch_id, state, elapsed_s,
            chamber_c, exhaust_c, exhaust_rh, exhaust_ah, hopper_kg, target_kg,
            int(heater_on), int(fans_on), fan_reason, int(gate_open), conveyor,
            json.dumps(sorted(alarms or [])), comp_json, prompt_json,
        ),
    )


def mark_supervisor_stopped(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE machine_state SET supervisor_pid = NULL, updated_at = ? WHERE id = 1", (now_iso(),))


# --- command queue -----------------------------------------------------------


def queue_command(conn: sqlite3.Connection, name: str, payload: str | None = None) -> int:
    """Flask side. Returns the id to poll for the machine's answer."""
    if name not in VALID_COMMAND_NAMES:
        raise ValueError(f"unknown command {name!r}; expected one of {sorted(VALID_COMMAND_NAMES)}")
    cur = conn.execute(
        "INSERT INTO commands (created_at, name, payload) VALUES (?, ?, ?)",
        (now_iso(), name, payload),
    )
    return cur.lastrowid


def pending_commands(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM commands WHERE status = ? ORDER BY id", (PENDING,)
    ).fetchall()
    return [dict(row) for row in rows]


def resolve_command(conn: sqlite3.Connection, command_id: int, status: str, result: str | None = None) -> None:
    if status not in (DONE, REJECTED):
        raise ValueError(f"status must be {DONE!r} or {REJECTED!r}, got {status!r}")
    conn.execute(
        "UPDATE commands SET status = ?, result = ?, resolved_at = ? WHERE id = ?",
        (status, result, now_iso(), command_id),
    )


def expire_pending_commands(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        "UPDATE commands SET status = ?, result = ?, resolved_at = ? WHERE status = ?",
        (REJECTED, "expired: the supervisor was not running when this was sent", now_iso(), PENDING),
    )
    return cur.rowcount


def command_status(conn: sqlite3.Connection, command_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM commands WHERE id = ?", (command_id,)).fetchone()
    return dict(row) if row is not None else None


# --- calibration lock --------------------------------------------------------


def acquire_calibration_lock(
    conn: sqlite3.Connection,
    duration_s: float = 300.0,
    component: str | None = None,
    session_id: str | None = None,
) -> bool:
    now = datetime.now()
    row = conn.execute("SELECT * FROM calibration_lock WHERE id = 1").fetchone()
    if row is not None:
        try:
            exp = datetime.fromisoformat(row["expires_at"])
            if now < exp:
                if session_id is not None and row["session_id"] == session_id:
                    pass
                else:
                    return False
        except Exception:
            pass
    expires_at = (now + timedelta(seconds=duration_s)).isoformat(timespec="seconds")
    conn.execute(
        """INSERT OR REPLACE INTO calibration_lock
               (id, acquired_at, expires_at, session_id, component)
           VALUES (1, ?, ?, ?, ?)""",
        (now.isoformat(timespec="seconds"), expires_at, session_id, component),
    )
    return True


def release_calibration_lock(conn: sqlite3.Connection, session_id: str | None = None) -> bool:
    if session_id is not None:
        cur = conn.execute("DELETE FROM calibration_lock WHERE id = 1 AND session_id = ?", (session_id,))
        return cur.rowcount > 0
    conn.execute("DELETE FROM calibration_lock WHERE id = 1")
    return True


def clear_calibration_lock(conn: sqlite3.Connection) -> bool:
    """Clear a calibration lock when a new supervisor instance takes ownership."""
    cur = conn.execute("DELETE FROM calibration_lock WHERE id = 1")
    return cur.rowcount > 0


def get_calibration_lock(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT * FROM calibration_lock WHERE id = 1").fetchone()
    if row is None:
        return None
    try:
        if datetime.now() >= datetime.fromisoformat(row["expires_at"]):
            conn.execute("DELETE FROM calibration_lock WHERE id = 1")
            return None
    except Exception:
        return None
    return dict(row)


def is_calibration_locked(conn: sqlite3.Connection) -> bool:
    return get_calibration_lock(conn) is not None


# --- dashboard reads ---------------------------------------------------------


def live_state(conn: sqlite3.Connection, *, stale_after_s: float = 10.0) -> dict | None:
    row = conn.execute("SELECT * FROM machine_state WHERE id = 1").fetchone()
    if row is None:
        return None
    out = _row_to_dict(row)
    for key in ("heater_on", "fans_on", "gate_open"):
        out[key] = bool(out[key])
    age = (datetime.now() - datetime.fromisoformat(out["updated_at"])).total_seconds()
    out["age_s"] = age
    out["stale"] = age > stale_after_s
    out["running"] = out["supervisor_pid"] is not None and not out["stale"]
    return out


def recent_batches(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    rows = conn.execute("SELECT * FROM batches ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [_row_to_dict(row) for row in rows]


def get_batch(conn: sqlite3.Connection, batch_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
    return _row_to_dict(row)


def batch_samples(
    conn: sqlite3.Connection, batch_id: int, limit: int | None = None, after_id: int | None = None
) -> list[dict]:
    sql = "SELECT * FROM sensor_log WHERE batch_id = ?"
    params: tuple = (batch_id,)
    if after_id is not None:
        sql += " AND id > ?"
        params += (after_id,)
    sql += " ORDER BY id"
    if limit is not None:
        sql += " LIMIT ?"
        params += (limit,)
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


# --- settings ----------------------------------------------------------------


def get_settings(conn: sqlite3.Connection) -> dict[str, str]:
    return {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM settings")}


def get_setting(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row is not None else None


def set_setting(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute(
        """INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
        (key, str(value), now_iso()),
    )


def main(argv: list[str] | None = None) -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Database maintenance utilities for CORDS")
    sub = ap.add_subparsers(dest="subcommand", required=True)

    b = sub.add_parser("backup", help="Create a backup copy of cords.db")
    b.add_argument("--db", default=None, help=f"Path to database (default: {DEFAULT_PATH})")

    m = sub.add_parser("migrate", help="Run migrations up to current version")
    m.add_argument("--db", default=None, help=f"Path to database (default: {DEFAULT_PATH})")

    args = ap.parse_args(argv)
    if args.subcommand == "backup":
        bk = backup_db(args.db)
        print(f"Database backed up to: {bk}")
    elif args.subcommand == "migrate":
        c = connect(args.db)
        init(c)
        c.close()
        print("Database schema verified and up to date.")

def log_batch_event(conn: sqlite3.Connection, batch_id: int, event: str, details: str | None = None) -> None:
    """Record a process milestone in the batch_events table"""
    details_str = json.dumps(details) if isinstance(details,dict) else (details or "")
    conn.execute(
        "INSERT INTO batch_events (batch_id, ts, event_type, details) VALUES (?, ?, ?, ?)",
        (batch_id, now_iso(), event, details_str),
    )

def get_batch_events(conn: sqlite3.Connection, batch_id: int) -> list[dict]:
    """Retrieve all milestones recorded for a given batch."""
    cur = conn.execute(
        "SELECT id, batch_id, ts, event_type AS event, details "
        "FROM batch_events WHERE batch_id = ? ORDER BY id ASC",
        (batch_id,)
    )
    return [dict(r) for r in cur.fetchall()]


def get_latest_batch(conn: sqlite3.Connection) -> dict | None:
    """Return the most recent batch record."""
    cur = conn.execute("SELECT * FROM batches ORDER BY id DESC LIMIT 1")
    row = cur.fetchone()
    return _row_to_dict(row)

if __name__ == "__main__":
    main()
