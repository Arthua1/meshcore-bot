#!/usr/bin/env python3
"""
MeshCore Bot
Author: Marcin SP4IM
License: MIT

Description:
    A robust auto-responder bot for MeshCore/Meshtastic networks.
    Features:
    - Auto-replies to specific keywords (weather, solar, ping, infobot, help).
    - Fetches solar propagation data from HamQSL with robust XML parsing.
    - Reads local weather data from a file.
    - Includes a hardware Watchdog to handle USB disconnects in VM environments.
"""

import asyncio
import logging
import time
import os
import sys
import math
import urllib.request
import xml.etree.ElementTree as ET
from collections import deque
from datetime import datetime, timezone
from typing import Optional, Tuple

# Ensure meshcore library is present
try:
    from meshcore import MeshCore, EventType
except ImportError:
    print("CRITICAL: 'meshcore' library missing. Install with: pip3 install meshcore")
    sys.exit(1)

# ==========================================
#               CONFIGURATION
# ==========================================
CONFIG = {
    # Serial port. Using a stable symlink (via UDEV) is recommended for Proxmox/VMs.
    "serial_port": "/dev/mesh_radio",
    
    # MeshCore channel index to listen on (e.g., 1 for a private/test channel)
    "channel_index": 1,
    
    # Minimum seconds between replies to the same sender (Rate Limiting)
    "reply_interval": 10.0,
    
    # Interval (seconds) to ping the radio hardware.
    # If ping fails, the script exits to trigger a systemd restart.
    "keep_alive_interval": 60,
    
    # Geographic location (Latitude/Longitude) for astronomical Day/Night calculation.
    # Default: Białystok, PL
    "location": {
        "lat": 53.1325,
        "lon": 23.1688
    },

    # Modules configuration
    "weather": {
        "enabled": True,
        "file_path": "/tmp/netatmo_data.py"
    },
    "solar": {
        "enabled": True,
        "url": "https://www.hamqsl.com/solarxml.php"
    },
    
    # Keywords that trigger the bot
    "triggers": {
        "basic": {"test", "ping"},
        "weather": {"pogoda", "weather"},
        "solar": {"solar", "warunki", "propa", "dx"},
        "info": {"infobot"},
        "help": {"help", "pomoc"}
    }
}

# Logging configuration
logging.basicConfig(
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S',
    level=logging.INFO
)
logger = logging.getLogger("MeshBot")


class SunCalc:
    """
    Calculates Sunrise and Sunset times based on NOAA simplified algorithm.
    Used to determine 'Day' or 'Night' propagation conditions without external APIs.
    """
    
    @staticmethod
    def is_daytime(lat: float, lon: float) -> bool:
        """Returns True if the sun is currently above the horizon at given coords."""
        try:
            now = datetime.now(timezone.utc)
            day_of_year = now.timetuple().tm_yday
            # Current time in decimal hours (UTC)
            hour_utc = now.hour + now.minute/60.0
            
            # Solar declination
            declination = 23.45 * math.sin(math.radians(360/365 * (day_of_year - 81)))
            
            # Hour Angle at Sunrise/Sunset
            # cos(omega) = -tan(phi) * tan(delta)
            has_rad = math.acos(-math.tan(math.radians(lat)) * math.tan(math.radians(declination)))
            has_deg = math.degrees(has_rad)
            
            # Solar Noon (UTC)
            noon_utc = 12.0 - (lon / 15.0)
            
            # Sunrise and Sunset times (UTC)
            sunrise_utc = noon_utc - (has_deg / 15.0)
            sunset_utc = noon_utc + (has_deg / 15.0)
            
            return sunrise_utc <= hour_utc <= sunset_utc
        except Exception as e:
            # Fallback in case of math domain error (e.g. polar regions)
            h = datetime.now().hour
            return 6 <= h < 18


class SolarModule:
    """Fetches solar indices (SFI, K, A) from HamQSL."""
    
    @staticmethod
    def get_info() -> str:
        url = CONFIG["solar"]["url"]
        try:
            # Use a standard browser User-Agent to avoid blocking
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=10.0) as response:
                xml_data = response.read()

            root = ET.fromstring(xml_data)
            
            # HamQSL XML structure handling
            # Usually <solar><solardata>...</solardata></solar>
            data = root.find('solardata')
            
            # Fallback: sometimes the root IS solardata
            if data is None and root.tag == 'solardata':
                data = root
                
            if data is None:
                return "ERR: XML structure invalid"

            # Parse numeric values with CORRECT tag names for HamQSL
            # Primary: 'solarflux', 'kindex'
            # Fallback: 'flux', 'k' (old format)
            sfi = data.findtext('solarflux') or data.findtext('flux')
            k_idx = data.findtext('kindex') or data.findtext('k')
            
            # Fallback to "?" if tag is missing or empty
            if not sfi: sfi = "?"
            if not k_idx: k_idx = "?"

            # Determine Day/Night mode for the user's location
            lat = CONFIG["location"]["lat"]
            lon = CONFIG["location"]["lon"]
            is_day = SunCalc.is_daytime(lat, lon)
            
            mode_str = "Day" if is_day else "Night"
            xml_tag = "day" if is_day else "night"

            # Parse band conditions
            conds_str = ""
            calc = data.find('calculatedconditions')
            
            # Check explicitly if 'calc' exists to avoid DeprecationWarning
            if calc is not None:
                bands = calc.findall('band')
                # Map conditions to short codes: Poor->P, Fair->F, Good->G
                short_map = {"Poor": "P", "Fair": "F", "Good": "G"}
                found_bands = []
                
                for b in bands:
                    if b.get('time') == xml_tag:
                        name = b.get('name') 
                        val = b.text
                        short_val = short_map.get(val, val[0] if val else "?")
                        
                        # Shorten band names (80m-40m -> 80-40)
                        short_name = name.replace('m', '') 
                        found_bands.append(f"{short_name}:{short_val}")
                
                conds_str = " ".join(found_bands)

            return f"SFI={sfi} K={k_idx} [{mode_str}] {conds_str}"

        except Exception as e:
            logger.error(f"Solar fetch error: {e}")
            return "ERR: Solar Connection Fail"


class WeatherModule:
    """Reads local weather data from a Python-formatted file (Netatmo integration)."""
    
    @staticmethod
    def degrees_to_cardinal(d: str) -> str:
        try:
            val = float(d) % 360
            dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW", "N"]
            return dirs[int((val + 22.5) / 45)]
        except (ValueError, TypeError):
            return str(d)

    @staticmethod
    def get_info(filepath: str) -> str:
        if not os.path.exists(filepath):
            return "ERR: Weather file missing."
        
        data = {}
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    data[k.strip()] = v.strip()
            
            # Extract fields safely
            temp = data.get("outside_temp", "?")
            hum = data.get("outside_Humidity", "?")
            pres = data.get("outside_Pressure", "?")
            w_spd = data.get("wind_speed", "?")
            w_dir = WeatherModule.degrees_to_cardinal(data.get("wind_dir", "?"))
            w_gst = data.get("wind_gust", "?")
            
            return (f"Temp={temp}C Hum={hum}% Pres={pres}hPa "
                    f"Wind={w_spd}km/h({w_dir}) Gust={w_gst}km/h")
        except Exception as e:
            logger.error(f"Weather parsing error: {e}")
            return "ERR: Data error."


class BotLogic:
    """Handles rate limiting and deduplication."""
    
    def __init__(self):
        self.last_reply_time = {}
        self.seen_messages = deque(maxlen=50)

    def should_process(self, sender: str, text: str) -> bool:
        now = time.time()
        
        # Deduplicate based on content hash
        sig = hash(f"{sender}:{text}")
        if sig in self.seen_messages:
            return False
        self.seen_messages.append(sig)
        
        # Rate limit per sender
        if sender in self.last_reply_time:
            if now - self.last_reply_time[sender] < CONFIG["reply_interval"]:
                return False
        
        self.last_reply_time[sender] = now
        return True


class MeshBot:
    """Main application class."""
    
    def __init__(self):
        self.logic = BotLogic()
        self.meshcore: Optional[MeshCore] = None
        self.keep_alive_task: Optional[asyncio.Task] = None
        self.boot_time = time.time()
        self.first_msg_processed = False

    async def start(self):
        logger.info(f"--- MeshCore Bot Started (Solar/Weather Edition v5.8) ---")
        
        # Clean stale lock files
        if os.path.exists("/tmp/meshcore.lock"):
            try:
                os.remove("/tmp/meshcore.lock")
                logger.info("Cleaned stale .lock file")
            except Exception as e:
                logger.warning(f"Could not remove lock file: {e}")

        try:
            # Connect to radio. Auto-reconnect is DISABLED to allow Watchdog/Systemd to handle failures.
            self.meshcore = await MeshCore.create_serial(
                CONFIG["serial_port"], 
                debug=False, 
                auto_reconnect=False 
            )
            
            await self.meshcore.start_auto_message_fetching()
            self.meshcore.subscribe(EventType.CHANNEL_MSG_RECV, self._on_message)
            
            # Start Watchdog
            self.keep_alive_task = asyncio.create_task(self._watchdog_loop())
            logger.info("Bot is listening. Watchdog active.")
            
            await asyncio.Future()

        except Exception as e:
            logger.critical(f"Main loop crashed: {e}")
            sys.exit(1)
        finally:
            if self.meshcore:
                await self.meshcore.disconnect()

    async def _watchdog_loop(self):
        """Monitors radio connection. Exits script if radio is unresponsive."""
        logger.info(f"Watchdog started (Interval: {CONFIG['keep_alive_interval']}s)")
        while True:
            await asyncio.sleep(CONFIG['keep_alive_interval'])
            
            if not self.meshcore:
                logger.critical("MeshCore object lost. Exiting.")
                sys.exit(1)

            try:
                # Ping the radio. If timeout -> radio is dead/frozen.
                await asyncio.wait_for(
                    self.meshcore.commands.send_device_query(), 
                    timeout=8.0
                )
            except Exception as e:
                logger.critical(f"WATCHDOG FAILED: {e}. Exiting to trigger restart.")
                sys.exit(1)

    def _parse_sender_and_text(self, raw_sender: str, raw_text: str) -> Tuple[str, str]:
        """Parses 'Nick: Message' format or returns raw sender/text."""
        sender = raw_sender or "Unknown"
        text = raw_text or ""
        
        # Heuristic for "Nick: Message" format
        if ":" in text:
            parts = text.split(":", 1)
            if len(parts) == 2 and 0 < len(parts[0]) < 25:
                sender = parts[0].strip()
                text = parts[1].strip()
        
        return sender, text

    async def _on_message(self, event):
        msg = event.payload
        
        # Anti-Spam: Ignore messages older than boot time (with 5s buffer)
        msg_ts = msg.get("timestamp") or msg.get("sender_timestamp")
        if msg_ts and (msg_ts < self.boot_time - 5):
            if not self.first_msg_processed:
                # Log only once to avoid console spam
                pass
            return
        
        self.first_msg_processed = True

        # Channel filter
        if msg.get("channel_idx") != CONFIG["channel_index"]:
            return

        # Parse content
        sender, text = self._parse_sender_and_text(msg.get("sender"), msg.get("text", ""))
        clean_text = text.strip()
        if not clean_text:
            return

        # Detect trigger keywords
        first_word = clean_text.split()[0].lower().rstrip(".,?!:;")
        matched_trigger = None

        for category, words in CONFIG["triggers"].items():
            for w in words:
                if first_word.startswith(w):
                    matched_trigger = category
                    break
            if matched_trigger:
                break
        
        if not matched_trigger:
            return

        # Ignore own messages (prevent loops)
        if "ack" in clean_text.lower() and "bot" in clean_text.lower():
            return
            
        # Rate limiting logic
        if not self.logic.should_process(sender, text):
            return

        logger.info(f"*** TRIGGER [{sender}] ({matched_trigger}): {clean_text} ***")

        # Generate Reply
        if matched_trigger == "weather":
            info = WeatherModule.get_info(CONFIG["weather"]["file_path"])
            reply_text = f"ACK {info}"
        
        elif matched_trigger == "solar":
            # Run network request in executor to avoid blocking the loop
            loop = asyncio.get_running_loop()
            info = await loop.run_in_executor(None, SolarModule.get_info)
            reply_text = f"ACK {info}"

        elif matched_trigger == "info":
            # Link to GitHub repo
            reply_text = "ACK Repo: https://github.com/Arthua1/meshcore-bot"

        elif matched_trigger == "help":
            # Short help text
            reply_text = "ACK CMDs: test/ping, pogoda/weather, solar/warunki, infobot (repo)"
            
        else:
            # Basic reply (test/ping)
            ts = datetime.now().strftime("%H:%M")
            reply_text = f"ACK - {sender} - {ts}"

        await self._send_reply(reply_text)

    async def _send_reply(self, text: str):
        if not self.meshcore:
            return
        try:
            res = await self.meshcore.commands.send_chan_msg(
                CONFIG["channel_index"], 
                text
            )
            if res.type == EventType.ERROR:
                logger.error(f"API Error sending reply: {res.payload}")
            else:
                logger.info(f"Reply sent: {text}")
        except Exception as e:
            logger.error(f"Exception sending reply: {e}")


if __name__ == "__main__":
    try:
        bot = MeshBot()
        asyncio.run(bot.start())
    except KeyboardInterrupt:
        logger.info("Stopped by user.")
    except SystemExit:
        raise
    except Exception as e:
        logger.critical(f"Unexpected crash: {e}")
        sys.exit(1)
