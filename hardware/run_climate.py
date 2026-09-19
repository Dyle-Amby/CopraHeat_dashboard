"""Bench loop for the drying-chamber climate control.

Reads the chamber sensors, runs the heater and fan controllers, and drives the
SSR and fan relay. Run from inside CopraHeat_dashboard:

    python -m hardware.run_climate --state drying
    python -m hardware.run_climate --state drying --dry-run   # decisions only, no GPIO
"""

import argparse
import signal
import sys
import time
from datetime import datetime

from hardware import pins, sensors
from hardware.climate import BatchState, FanController, HeaterController


def fmt(value: float | None, unit: str = "C") -> str:
    return "--" if value is None else f"{value:.1f}{unit}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", choices=[s.value for s in BatchState], default=BatchState.IDLE.value)
    ap.add_argument("--interval", type=float, default=2.0, help="seconds between readings (DHT22 minimum is 2)")
    ap.add_argument("--dry-run", action="store_true", help="print decisions without touching GPIO")
    args = ap.parse_args()

    state = BatchState(args.state)
    heater = HeaterController()
    fans = FanController()

    # systemd stop sends SIGTERM; turn it into SystemExit so the finally block runs
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    heater_out = fan_out = None
    if not args.dry_run:
        from gpiozero import OutputDevice
        heater_out = OutputDevice(pins.HEATER_SSR, initial_value=False)
        fan_out = OutputDevice(pins.FAN_RELAY, initial_value=False)

    print(f"climate loop: state={state.value} interval={args.interval}s dry_run={args.dry_run}")
    try:
        while True:
            now = time.monotonic()
            chamber = sensors.read_ds18b20()
            exhaust_t, exhaust_rh = sensors.read_dht22()

            heater_on = heater.update(state, chamber)
            fans_on = fans.update(state, chamber, now)

            if heater_out is not None:
                heater_out.value = heater_on
                fan_out.value = fans_on

            flags = (" FAULT" if heater.fault else "") + (" OVERTEMP" if fans.overtemp_active else "")
            print(
                f"{datetime.now():%H:%M:%S}  chamber={fmt(chamber):>7}  "
                f"exhaust={fmt(exhaust_t):>7} {fmt(exhaust_rh, '%'):>6}  "
                f"heater={'ON ' if heater_on else 'off'}  fans={'ON ' if fans_on else 'off'}{flags}"
            )
            time.sleep(args.interval)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        if heater_out is not None:
            heater_out.off()
            fan_out.off()
            heater_out.close()
            fan_out.close()
        print("heater and fans OFF")


if __name__ == "__main__":
    main()
