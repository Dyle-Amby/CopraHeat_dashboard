"""Chamber sensor readers backed by the kernel's sysfs drivers.

Requires in /boot/firmware/config.txt:
    dtoverlay=w1-gpio,gpiopin=4      # DS18B20 (w1_therm)
    dtoverlay=dht11,gpiopin=26       # DHT22 (the dht11 IIO driver handles both)

Every reader returns None on any failure so callers fall into their fault paths.
"""

from pathlib import Path

W1_DEVICES = Path("/sys/bus/w1/devices")
IIO_DEVICES = Path("/sys/bus/iio/devices")


def parse_w1_slave(text: str) -> float | None:
    lines = text.strip().splitlines()
    if len(lines) < 2 or not lines[0].rstrip().endswith("YES"):
        return None
    _, sep, raw = lines[1].partition("t=")
    if not sep:
        return None
    try:
        return int(raw) / 1000.0
    except ValueError:
        return None


def parse_milli(text: str) -> float | None:
    try:
        return int(text.strip()) / 1000.0
    except ValueError:
        return None


def read_ds18b20() -> float | None:
    try:
        device = next(W1_DEVICES.glob("28-*"))
        return parse_w1_slave((device / "w1_slave").read_text())
    except (StopIteration, OSError):
        return None


def _find_dht() -> Path | None:
    for device in IIO_DEVICES.glob("iio:device*"):
        try:
            if (device / "name").read_text().strip() == "dht11":
                return device
        except OSError:
            continue
    return None


def read_dht22() -> tuple[float | None, float | None]:
    device = _find_dht()
    if device is None:
        return None, None
    try:
        temp = parse_milli((device / "in_temp_input").read_text())
        humidity = parse_milli((device / "in_humidityrelative_input").read_text())
    except OSError:
        # the driver returns EIO on checksum failure and EAGAIN if polled under 2 s apart
        return None, None
    return temp, humidity
