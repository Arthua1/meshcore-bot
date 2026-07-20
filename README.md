# MeshCore Bot

A stable information and auto-responder bot for MeshCore networks, designed for continuous operation on Raspberry Pi, virtual machines, and Linux servers.

The bot listens on a configurable MeshCore group channel and provides local weather data, next-day forecasts, HF propagation conditions, and locally calculated International Space Station position and pass predictions.

## Features

- MeshCore group channel support
- Configurable channel index and default region scope
- Default `pl-podlasie` scope for the `testy` channel
- UTF-8 byte-aware message limit for scoped channels
- Local weather readings from a file
- Next-day weather forecast from Open-Meteo
- Solar indices and band conditions from HamQSL
- Local ISS position and pass calculations using SGP4
- Periodic TLE updates from CelesTrak with local fallback cache
- Message queue and serialized replies
- Per-sender rate limiting and message deduplication
- Serial connection watchdog
- Graceful shutdown on `SIGINT` and `SIGTERM`
- Suitable for `systemd`, Proxmox USB passthrough, and persistent UDEV device links

## Commands

- `test`, `ping` — reply with an ACK, sender name, and local time
- `pogoda`, `weather` — current local weather reading
- `prognoza`, `weather_tomorrow` — next-day weather forecast
- `solar`, `warunki`, `propa`, `dx` — solar indices and band conditions
- `infobot` — repository URL
- `help`, `pomoc` — short command list
- `iss`, `iss_gdzie` — current ISS reverse geoposition
- `iss_przelot`, `iss_pass`, `iss_lacznosc` — next ISS pass over the configured location

Example replies:

```text
ACK - SP4ABC - 18:42
ACK SFI=145 K=2 [Dzien] 80-40:P 20:G 15:G 10:F
ACK ISS 48.2N 21.7E h=421km
ACK ISS 21.07 18:32-18:39 PL max=42deg SW>NE TCA=18:35
```

The time range returned by `iss_przelot`, `iss_pass`, or `iss_lacznosc` is the period during which the ISS remains above the configured minimum elevation, which is 10 degrees by default. A predicted pass does not guarantee that amateur radio equipment aboard the ISS is active.

## Requirements

- Linux
- Python 3.10 or newer
- MeshCore companion radio with firmware supporting the required commands
- Access to the radio serial port
- Internet access for HamQSL, Open-Meteo, and periodic TLE updates

Python dependencies are pinned in `requirements.txt`:

```text
meshcore==2.3.7
skyfield==1.54
sgp4==2.27
```

## Installation

```bash
git clone https://github.com/Arthua1/meshcore-bot.git
cd meshcore-bot

python3 -m venv venv
venv/bin/pip install --upgrade pip
venv/bin/pip install -r requirements.txt
```

Create a directory for the TLE cache:

```bash
sudo install -d -o meshcore -g meshcore -m 0750 /var/lib/meshcore-bot
```

Change the owner and group if the service uses a different account.

Check the script and run it manually:

```bash
venv/bin/python -m py_compile meshbot.py
venv/bin/python meshbot.py
```

## Configuration

The main settings are stored in the `CONFIG` dictionary in `meshbot.py`.

### Serial port and channel

```python
"serial_port": "/dev/mesh_radio",
"baudrate": 115200,
"channel": {
    "index": 1,
    "name": "testy",
    "scope": "pl-podlasie",
    "set_default_scope": True,
    "max_payload_bytes": 120,
},
```

`set_default_scope` configures the companion's default flood scope when supported by the installed MeshCore library and firmware. The group channel scope should also be configured in the MeshCore app or companion settings.

Scoped channel messages have a smaller payload budget, so replies are limited by UTF-8 byte length rather than Python character count.

### Location

```python
"location": {
    "lat": 53.1325,
    "lon": 23.1688,
    "elevation_m": 150.0,
    "timezone": "Europe/Warsaw",
},
```

The location is used for weather forecasts, day/night propagation selection, and ISS pass predictions.

### Language

```python
"lang": "pl",
```

Supported values are `pl` and `en`. Command aliases for both languages remain available regardless of the selected reply language.

### Local weather file

```python
"weather": {
    "enabled": True,
    "file_path": "/tmp/netatmo_data.py",
    "max_age_seconds": 900,
},
```

The file is read as plain `key=value` data and is not executed as Python code:

```text
outside_temp=21.4
outside_Humidity=58
outside_Pressure=1015.2
wind_speed=7
wind_dir=245
wind_gust=14
```

Data older than `max_age_seconds` is reported as stale.

### ISS

```python
"iss": {
    "enabled": True,
    "tle_file": "/var/lib/meshcore-bot/iss_25544.tle",
    "tle_refresh_seconds": 21600,
    "tle_warning_age_hours": 48,
    "tle_max_age_days": 10,
    "min_elevation_deg": 10.0,
    "prediction_hours": 72,
    "pass_cache_seconds": 900,
},
```

ISS orbital elements are downloaded periodically and stored locally. Current position and pass predictions are calculated on the host using Skyfield and SGP4, without calling an external pass prediction API for each request.

If CelesTrak is temporarily unavailable, the bot continues using the last valid TLE file. It marks data older than 48 hours and rejects orbital data older than 10 days.

## Running with systemd

An example unit is included as `meshcore-bot.service`:

```ini
[Unit]
Description=MeshCore Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=meshcore
Group=meshcore
WorkingDirectory=/opt/meshcore-bot
ExecStart=/opt/meshcore-bot/venv/bin/python /opt/meshcore-bot/meshbot.py
Restart=on-failure
RestartSec=5
TimeoutStopSec=20

[Install]
WantedBy=multi-user.target
```

Install and start the service:

```bash
sudo cp meshcore-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now meshcore-bot.service
sudo systemctl status meshcore-bot.service
```

Follow the logs:

```bash
journalctl -u meshcore-bot.service -f
```

Adjust `User`, `Group`, `WorkingDirectory`, and `ExecStart` before installing the unit if your deployment uses different values.

## Persistent serial device name

A persistent UDEV symlink is recommended instead of relying on `/dev/ttyUSB0` or `/dev/ttyACM0`, especially with USB passthrough in virtual machines.

Inspect the device attributes:

```bash
udevadm info --attribute-walk --name=/dev/ttyUSB0
```

Example `/etc/udev/rules.d/99-mesh-radio.rules`:

```udev
SUBSYSTEM=="tty", ATTRS{idVendor}=="1a86", ATTRS{idProduct}=="7523", SYMLINK+="mesh_radio"
```

Replace the vendor and product IDs with values matching your hardware, then reload the rules:

```bash
sudo udevadm control --reload-rules
sudo udevadm trigger
```

## Updating

```bash
cd /opt/meshcore-bot
git pull
venv/bin/pip install -r requirements.txt
venv/bin/python -m py_compile meshbot.py
sudo systemctl restart meshcore-bot.service
```

## License

MIT. See `LICENSE` for details.
