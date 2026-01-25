# MeshCore Bot - VM/RPi Edition

A robust, Python-based auto-responder and information bot for MeshCore networks (Meshtastic companion radios). Designed for stability in virtualized environments (Proxmox, VMware) and reliable 24/7 operation.

MeshCore-Bot works in public channel number 1 - #testy, you can change it in config.

## Key Features

*   Solar & Propagation Data:** Fetches real-time solar indices (SFI, A, K) from HamQSL. Uses a built-in **astronomical algorithm** (NOAA-based) to automatically switch between Day/Night propagation predictions based on your precise GPS location – no internet time services or heavy libraries required.
*   Weather Integration:** Reads local weather data from a file (compatible with Netatmo-API fetchers).
*   VM Stability:** Includes a dedicated "Watchdog" loop that aggressively monitors the USB serial connection. If the radio hangs or USB disconnects (common in VM pass-through), the bot self-terminates to trigger a clean systemd restart.
*   Anti-Spam & Deduplication:** Filters out old buffered messages after a reboot and prevents reply loops.
*   Zero-Dependency Logic:** Solar calculations and watchdog logic use standard Python libraries.

## Commands

Users on the mesh network can send messages to the channel where the bot is listening:

*   `test`, `ping` - Returns a simple ACK with timestamp.
*   `weather`, `pogoda` - Returns current local weather (Temp, Humidity, Pressure, Wind).
*   `solar`, `propa` - Returns solar indices and band conditions (Day/Night auto-detected).
    *   *Example reply:* `ACK SFI=168 K=2 [Day] 80-40:P 20:G 15:G 10:F`

## Installation

1.  **Clone the repository:**
    ```bash
    git clone https://github.com/Arthua1/meshcore-bot.git
    cd meshcore-bot
    ```

2.  **Install dependencies:**
    ```bash
    pip3 install meshcore
    ```

3.  **Configure:**
    Edit the `CONFIG` dictionary in `meshbot.py`:
    *   Set your serial port (e.g., `/dev/ttyACM0` or `/dev/mesh_radio`).
    *   Set your GPS coordinates for accurate Day/Night solar calculations.
    *   Adjust triggers and file paths.

4.  **Run as a Systemd Service (Recommended):**
    See the `meshbot.service` example file for ensuring 24/7 uptime with auto-restart.

## Proxmox / USB Stability Tip

For virtual machines, it is highly recommended to use a UDEV rule to assign a persistent symlink to your radio, preventing path changes (e.g., `ttyACM0` -> `ttyACM1`) after disconnects.

Create `/etc/udev/rules.d/99-mesh.rules`:
```bash
SUBSYSTEM=="tty", ATTRS{idVendor}=="1a86", ATTRS{idProduct}=="7523", SYMLINK+="mesh_radio"
