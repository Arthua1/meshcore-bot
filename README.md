# MeshCore Bot - VM/RPi Edition

A robust, Python-based auto-responder and information bot for MeshCore networks (Meshtastic companion radios). Designed for stability in virtualized environments (Proxmox, VMware) and reliable 24/7 operation.

MeshCore-Bot operates on a configurable channel (default: public channel 1 - `#testy`).

## Key Features

*   **Solar & Propagation Data:** Fetches real-time solar indices (SFI, A, K) from HamQSL. Uses a built-in **astronomical algorithm** (NOAA-based) to automatically switch between Day/Night propagation predictions based on your precise GPS location – no heavy libraries required.
*   **Weather Forecast (New):** Fetches next-day weather forecast from **Open-Meteo** (no API key needed). Smartly formats messages to fit within Meshtastic/LoRa character limits (e.g., dynamically removing units if the message is too long).
*   **Local Weather:** Reads current local weather data from a file.
*   **Multi-language Support:** Easily switch bot responses between Polish (`pl`) and English (`en`) via configuration.
*   **VM Stability:** Includes a dedicated "Watchdog" loop that aggressively monitors the USB serial connection. If the radio hangs or USB disconnects (common in VM pass-through), the bot self-terminates to trigger a clean systemd restart.
*   **Anti-Spam & Deduplication:** Filters out old buffered messages after a reboot and prevents reply loops.

## Commands

Users on the mesh network can send messages to the channel where the bot is listening:

*   `test`, `ping` - Returns a simple ACK with timestamp.
*   `weather`, `pogoda` - Returns current local weather (Temp, Humidity, Pressure, Wind).
*   `weather_tomorrow`, `pogoda_jutro` - Returns tomorrow's forecast (Temp Min/Max, Wind, Rain).
    *   *Example reply:* `ACK 🌤️ Jutro: Przew. słonecznie 12–18°C NW15km/h`
*   `solar`, `propa` - Returns solar indices and band conditions (Day/Night auto-detected).
    *   *Example reply:* `ACK SFI=168 K=2 [Day] 80-40:P 20:G 15:G 10:F`
*   `infobot` - Returns a link to the bot's repository.
*   `help`, `pomoc` - Returns a concise list of available commands.

## Installation

1.  **Clone the repository:**
    ```bash
    git clone https://github.com/Arthua1/meshcore-bot.git
    cd meshcore-bot
    ```

2.  **Install dependencies:**
    The bot logic uses standard Python libraries. You only need the meshcore library:
    ```bash
    pip3 install meshcore
    ```

3.  **Configure:**
    Edit the `CONFIG` dictionary in `meshbot.py`:
    *   Set your serial port (e.g., `/dev/ttyACM0` or `/dev/mesh_radio`).
    *   **Crucial:** Set your GPS coordinates (`lat`, `lon`) for accurate Day/Night solar calculations and weather forecasts.
    *   Choose your language (`"lang": "pl"` or `"en"`).
    *   Adjust triggers and file paths.

4.  **Run as a Systemd Service (Recommended):**
    See the `meshbot.service` example file for ensuring 24/7 uptime with auto-restart.

## Proxmox / USB Stability Tip

For virtual machines, it is highly recommended to use a UDEV rule to assign a persistent symlink to your radio, preventing path changes (e.g., `ttyACM0` -> `ttyACM1`) after disconnects.

Create `/etc/udev/rules.d/99-mesh.rules`:
```bash
SUBSYSTEM=="tty", ATTRS{idVendor}=="1a86", ATTRS{idProduct}=="7523", SYMLINK+="mesh_radio"
```
