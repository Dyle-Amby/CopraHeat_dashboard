# CORDS Supervisor systemd User Service

The hardware supervisor runs as a **systemd user service** under the operator's Linux account on the Raspberry Pi 5.

## Architectural Invariants
1. **Never Enabled at Boot**: The service must **never** be enabled (`systemctl --user enable cords-supervisor` is prohibited). It runs strictly on-demand when the operator clicks **Start Supervisor** on the Control Panel.
2. **Restart=no**: The service does not automatically restart if killed or if it exits.
3. **Graceful SIGTERM Stop**: Stopping issues `SIGTERM` with `TimeoutStopSec=20`. This allows `hardware/run_cords.py`'s `finally:` handler to:
   - Terminate stepper pulse trains safely.
   - De-energize heaters and fans.
   - Mark open batches as `interrupted` in SQLite.
   - Release GPIO chips cleanly.
4. **No CLI Flags Needed**: The supervisor queries all hardware configurations (PWM chip/channel, servo angles, hopper calibration offsets, conveyor step calibrations) dynamically from the SQLite `settings` table on startup.

## Setup on Raspberry Pi 5

1. **Enable user lingering**:
   Ensures the user systemd instance runs independently of an active SSH/GUI session:
   ```bash
   loginctl enable-linger $USER
   ```

2. **Install unit file**:
   ```bash
   mkdir -p ~/.config/systemd/user
   cp systemd/cords-supervisor.service ~/.config/systemd/user/
   systemctl --user daemon-reload
   ```

3. **Verify manual start/stop**:
   ```bash
   systemctl --user start cords-supervisor
   systemctl --user status cords-supervisor
   systemctl --user stop cords-supervisor
   ```

## Web Application (Flask) Requirements

When Flask connects to the user systemd daemon (via `systemctl --user`), it requires the user runtime environment:
- `XDG_RUNTIME_DIR=/run/user/<UID>` (e.g. `/run/user/1000`)
- `DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/<UID>/bus`

If Flask is running under gunicorn, systemd system unit, or supervisor, ensure these environment variables are exported or configured in the service file.
