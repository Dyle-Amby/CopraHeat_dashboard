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

There is no migration framework. `PRAGMA user_version` guards the schema, and a
mismatch asks you to delete the file; that is honest for a project that has never
shipped a second schema.
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from hardware.batch import COMMAND_NAMES, BatchRecord

SCHEMA_VERSION = 1

# app.py must run from inside CopraHeat_dashboard, but the supervisor may not,
# so anchor the default to this file rather than the working directory
DEFAULT_PATH = Path(__file__).resolve().parent.parent / "cords.db"

PENDING = "pending"
DONE = "done"
REJECTED = "rejected"

DEFAULT_SETTINGS = {
    "target_kg": "2.0",
    "hx711_scale": "1.0",
    "hx711_offset_closed": "0",
    "hx711_offset_open": "0",
    "servo_closed_angle": "0.0",
    "servo_open_angle": "90.0",
    "pwm_chip": "2",
    "pwm_channel": "2",
    "sample_interval_s": "10.0",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at         TEXT    NOT NULL,
    finished_at        TEXT,
    state              TEXT    NOT NULL,
    duration_s         REAL,
    drying_s           REAL,
    target_kg          REAL,
    weight_in_kg       REAL,
    weight_out_kg      REAL,
    weight_loss_pct    REAL,
    aborted            INTEGER NOT NULL DEFAULT 0,
    abort_reason       TEXT,
    alarms             TEXT,
    -- vision output; NULL until the sorter exists. Nothing fabricates a value.
    great              INTEGER,
    good               INTEGER,
    bad                INTEGER,
    final_moisture_pct REAL
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
    fans_on    INTEGER NOT NULL DEFAULT 0
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
    gate_open      INTEGER NOT NULL DEFAULT 0,
    conveyor       TEXT,
    alarms         TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
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


def init(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version not in (0, SCHEMA_VERSION):
        raise RuntimeError(
            f"database schema is version {version}, this build expects {SCHEMA_VERSION}. "
            "There are no migrations yet -- move the file aside and let it be recreated."
        )
    conn.executescript(SCHEMA)
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    stamp = now_iso()
    conn.executemany(
        "INSERT OR IGNORE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
        [(k, v, stamp) for k, v in DEFAULT_SETTINGS.items()],
    )


def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    if "alarms" in out:
        out["alarms"] = json.loads(out["alarms"]) if out["alarms"] else []
    return out


# --- supervisor writes -------------------------------------------------------


def start_batch(conn: sqlite3.Connection, *, target_kg: float, state: str = "intake") -> int:
    """Insert the row at batch start, not at finish: the dashboard needs the
    batch number and elapsed time while the batch is still running."""
    cur = conn.execute(
        "INSERT INTO batches (started_at, state, target_kg) VALUES (?, ?, ?)",
        (now_iso(), state, target_kg),
    )
    return cur.lastrowid


def set_batch_state(conn: sqlite3.Connection, batch_id: int, state: str) -> None:
    """Keep a running batch's row on its current phase, so history and the
    batch picker do not show 'intake' for a batch that is drying."""
    conn.execute(
        "UPDATE batches SET state = ? WHERE id = ? AND finished_at IS NULL", (state, batch_id)
    )


def mark_interrupted_batches(conn: sqlite3.Connection) -> int:
    """Called at supervisor startup. A batch row with no `finished_at` means the
    process died or was stopped mid-run; without this the dashboard would show a
    phantom batch running forever. Returns how many were closed out."""
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
               aborted = ?, abort_reason = ?, alarms = ?
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
) -> None:
    conn.execute(
        """INSERT INTO sensor_log
               (batch_id, ts, elapsed_s, state, chamber_c, exhaust_c, exhaust_rh,
                exhaust_ah, hopper_kg, heater_on, fans_on)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            batch_id, now_iso(), elapsed_s, state, chamber_c, exhaust_c,
            exhaust_rh, exhaust_ah, hopper_kg, int(heater_on), int(fans_on),
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
    gate_open: bool = False,
    conveyor: str | None = None,
    alarms: frozenset[str] | list[str] | None = None,
) -> None:
    """Single-row snapshot the dashboard polls. `updated_at` doubles as the
    supervisor's heartbeat, so the UI can tell live data from a dead process."""
    conn.execute(
        """INSERT OR REPLACE INTO machine_state
               (id, updated_at, supervisor_pid, batch_id, state, elapsed_s,
                chamber_c, exhaust_c, exhaust_rh, exhaust_ah, hopper_kg, target_kg,
                heater_on, fans_on, gate_open, conveyor, alarms)
           VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            now_iso(), supervisor_pid, batch_id, state, elapsed_s,
            chamber_c, exhaust_c, exhaust_rh, exhaust_ah, hopper_kg, target_kg,
            int(heater_on), int(fans_on), int(gate_open), conveyor,
            json.dumps(sorted(alarms or [])),
        ),
    )


def mark_supervisor_stopped(conn: sqlite3.Connection) -> None:
    """Clear the pid on a clean shutdown, so the dashboard can say 'stopped'
    rather than leaving the operator to infer it from a stale heartbeat."""
    conn.execute("UPDATE machine_state SET supervisor_pid = NULL, updated_at = ? WHERE id = 1", (now_iso(),))


# --- command queue -----------------------------------------------------------


def queue_command(conn: sqlite3.Connection, name: str, payload: str | None = None) -> int:
    """Flask side. Returns the id to poll for the machine's answer."""
    if name not in COMMAND_NAMES:
        raise ValueError(f"unknown command {name!r}; expected one of {sorted(COMMAND_NAMES)}")
    cur = conn.execute(
        "INSERT INTO commands (created_at, name, payload) VALUES (?, ?, ?)",
        (now_iso(), name, payload),
    )
    return cur.lastrowid


def pending_commands(conn: sqlite3.Connection) -> list[dict]:
    """Supervisor side, oldest first."""
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
    """Called at supervisor startup. Anything still pending was queued while no
    supervisor was listening; running it now would fire an hours-old Start or
    Abort at whatever the machine happens to be doing. Returns how many."""
    cur = conn.execute(
        "UPDATE commands SET status = ?, result = ?, resolved_at = ? WHERE status = ?",
        (REJECTED, "expired: the supervisor was not running when this was sent", now_iso(), PENDING),
    )
    return cur.rowcount


def command_status(conn: sqlite3.Connection, command_id: int) -> dict | None:
    """Flask polls this so a refused command shows the machine's own words."""
    row = conn.execute("SELECT * FROM commands WHERE id = ?", (command_id,)).fetchone()
    return dict(row) if row is not None else None


# --- dashboard reads ---------------------------------------------------------


def live_state(conn: sqlite3.Connection, *, stale_after_s: float = 10.0) -> dict | None:
    """The snapshot plus `age_s`, `stale` and `running`. None if the supervisor
    has never run against this database."""
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
    """`after_id` lets a live chart fetch only the rows it has not seen yet."""
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


def set_setting(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute(
        """INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
        (key, str(value), now_iso()),
    )
