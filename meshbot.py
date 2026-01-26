#!/usr/bin/env python3
"""
MeshCore Bot
Author: Marcin SP4IM
License: MIT
Description:
 A robust auto-responder bot for MeshCore/Meshtastic networks.
 Features:
 - Auto-replies to specific keywords.
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
import json
from urllib.parse import urlencode
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
# CONFIGURATION
# ==========================================
CONFIG = {
    # Serial port. Using a stable symlink (via UDEV) is recommended for Proxmox/VMs.
    "serial_port": "/dev/mesh_radio",
    # MeshCore channel index to listen on (e.g., 1 for a private/test channel)
    "channel_index": 1,
    # Minimum seconds between replies to the same sender (Rate Limiting)
    "reply_interval": 2.0,
    # Interval (seconds) to ping the radio hardware.
    # If ping fails, the script exits to trigger a systemd restart.
    "keep_alive_interval": 60,
    # Geographic location (Latitude/Longitude) used for solar Day/Night calculation and weather forecast.
    # Default: Białystok, PL
    "location": {
        "lat": 53.1325,
        "lon": 23.1688
    },
    # Language of outgoing messages: "pl" or "en"
    "lang": "pl",

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
    # NOTE: unified scheme:
    "triggers": {
        "basic": {"test", "ping"},
        "weather_now": {"pogoda", "weather"},
        "weather_tomorrow": {"pogoda_jutro", "weather_tomorrow"},
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


# --- Localization (PL/EN) ---
LANG_MAP = {
    "pl": {
        "tomorrow": "Jutro",
        "wx_err": "WX ERR",
        "help_text": "ACK CMDs: test/ping, pogoda (pomiar), pogoda_jutro (prognoza), solar/warunki, infobot (repo)",
        "solar_day": "Dzień",
        "solar_night": "Noc",
        "solar_err": "ERR: Błąd połączenia z serwisem HAMQSL",
        "w_file_missing": "ERR: Brak pliku pogody.",
        "w_data_err": "ERR: Błąd danych pogodowych."
    },
    "en": {
        "tomorrow": "Tomorrow",
        "wx_err": "WX ERR",
        "help_text": "ACK CMDs: test/ping, weather (measurement), weather_tomorrow (forecast), solar, infobot (repo)",
        "solar_day": "Day",
        "solar_night": "Night",
        "solar_err": "ERR: Service HAMQSL Connection Fail",
        "w_file_missing": "ERR: Weather file missing.",
        "w_data_err": "ERR: Data error."
    },
}
def L(key: str) -> str:
    """Return localized string for key based on CONFIG['lang']."""
    lang = CONFIG.get("lang", "pl")
    return LANG_MAP.get(lang, LANG_MAP["pl"]).get(key, key)


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
        except Exception:
            # Fallback in case of math domain error (e.g. polar regions)
            h = datetime.now().hour
            return 6 <= h < 18


class SolarModule:
    """Fetches solar indices (SFI, K, A) from HamQSL."""
    @staticmethod
    def get_info() -> str:
        url = CONFIG["solar"]["url"]
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=10.0) as response:
                xml_data = response.read()
            root = ET.fromstring(xml_data)
            data = root.find('solardata')
            if data is None and root.tag == 'solardata':
                data = root
            if data is None:
                return "ERR: XML structure invalid"
            sfi = data.findtext('solarflux') or data.findtext('flux')
            k_idx = data.findtext('kindex') or data.findtext('k')

            if not sfi:
                sfi = "?"
            if not k_idx:
                k_idx = "?"

            lat = CONFIG["location"]["lat"]
            lon = CONFIG["location"]["lon"]
            is_day = SunCalc.is_daytime(lat, lon)
            mode_str = L("solar_day") if is_day else L("solar_night")
            xml_tag = "day" if is_day else "night"

            # Parse band conditions
            conds_str = ""
            calc = data.find('calculatedconditions')
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
                        short_name = name.replace('m', '')
                        found_bands.append(f"{short_name}:{short_val}")
                conds_str = " ".join(found_bands)

            return f"SFI={sfi} K={k_idx} [{mode_str}] {conds_str}"
        except Exception as e:
            logger.error(f"Solar fetch error: {e}")
            return L("solar_err")


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
            return L("w_file_missing")
        data = {}
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    data[k.strip()] = v.strip()
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
            return L("w_data_err")


class WXForecast:
    # Emoji + short EN description for WMO weather codes (Open-Meteo daily.weathercode)
    # Source: Open-Meteo docs (WMO codes)
    WMO_EMOJI = {
        0:  ("☀️", "Clear"),
        1:  ("🌤️", "Mostly Clear"),
        2:  ("⛅",  "Partly Cloudy"),
        3:  ("☁️",  "Cloudy"),
        45: ("🌫️", "Fog"), 48: ("🌫️", "Fog"),
        51: ("🌦️", "Drizzle"), 53: ("🌦️", "Drizzle"), 55: ("🌦️", "Drizzle"),
        56: ("🌧️", "Freezing Drizzle"), 57: ("🌧️", "Freezing Drizzle"),
        61: ("🌧️", "Rain"), 63: ("🌧️", "Rain"), 65: ("🌧️", "Heavy Rain"),
        66: ("🌨️", "Freezing Rain"), 67: ("🌨️", "Freezing Rain"),
        71: ("🌨️", "Snow"), 73: ("🌨️", "Snow"), 75: ("❄️", "Heavy Snow"),
        77: ("❄️", "Snow Grains"),
        80: ("🌧️", "Showers"), 81: ("🌧️", "Showers"), 82: ("🌧️", "Heavy Showers"),
        85: ("🌨️", "Snow Showers"), 86: ("🌨️", "Snow Showers"),
        95: ("⛈️", "Thunderstorm"), 96: ("⛈️", "Thundersnow"), 99: ("⛈️", "Thundersnow"),
    }

    WMO_DESC_PL = {
        0:"Bezchmurnie", 1:"Przew. słonecznie", 2:"Częściowe chmury", 3:"Pochmurnie",
        45:"Mgła", 48:"Mgła",
        51:"Mżawka", 53:"Mżawka", 55:"Mżawka",
        56:"Marznąca mżawka", 57:"Marznąca mżawka",
        61:"Deszcz", 63:"Deszcz", 65:"Ulew. deszcz",
        66:"Marznący deszcz", 67:"Marznący deszcz",
        71:"Śnieg", 73:"Śnieg", 75:"Obfity śnieg",
        77:"Ziarnisty śnieg",
        80:"Przel. deszcz", 81:"Przel. deszcz", 82:"Ulewy przel.",
        85:"Przel. śnieg", 86:"Przel. śnieg",
        95:"Burza", 96:"Burza/śnieg", 99:"Burza/śnieg",
    }

    @staticmethod
    def _dir_to_compass(deg: float) -> str:
        dirs = ["N","NNE","NE","ENE","E","ESE","SE","SSE",
                "S","SSW","SW","WSW","W","WNW","NW","NNW"]
        i = int((deg % 360) / 22.5 + 0.5) % 16
        return dirs[i]

    @staticmethod
    def fetch_tomorrow(lat: float, lon: float, timeout: float = 5.0) -> dict | None:
        """
        Fetch next-day daily forecast from Open-Meteo.
        Daily variables: weathercode, t2m min/max, precipitation_sum, windspeed_10m_max, winddirection_10m_dominant
        """
        base = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": lat, "longitude": lon,
            "timezone": "auto",
            "forecast_days": 2,
            "daily": ",".join([
                "weathercode","temperature_2m_max","temperature_2m_min",
                "precipitation_sum","windspeed_10m_max","winddirection_10m_dominant"
            ]),
        }
        url = f"{base}?{urlencode(params)}"
        req = urllib.request.Request(url, headers={'User-Agent': 'MeshWX/1.0'})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
            d = data.get("daily") or {}
            def get_at(key):
                arr = d.get(key) or []
                return arr[1] if len(arr) > 1 else None  # index 1 -> tomorrow
            return {
                "wmo":  get_at("weathercode"),
                "tmax": get_at("temperature_2m_max"),
                "tmin": get_at("temperature_2m_min"),
                "prcp": get_at("precipitation_sum"),
                "wspd": get_at("windspeed_10m_max"),
                "wdir": get_at("winddirection_10m_dominant")
            }
        except Exception as e:
            logger.error(f"WX fetch error: {e}")
            return None

    @classmethod
    def _desc(cls, wmo: int, lang: str) -> tuple[str, str]:
        emo, en = cls.WMO_EMOJI.get(wmo, ("🌡️", "Weather"))
        if lang == "pl":
            return emo, cls.WMO_DESC_PL.get(wmo, "Pogoda")
        return emo, en

    @classmethod
    def format_135_chars(cls, lat: float, lon: float, lang: str) -> str:

        fx = cls.fetch_tomorrow(lat, lon)
        if not fx or fx.get("wmo") is None:
            return L("wx_err")
        wmo = int(fx["wmo"])
        emo, desc = cls._desc(wmo, lang)
        tmin, tmax = fx["tmin"], fx["tmax"]
        wspd, wdir, prcp = fx["wspd"], fx["wdir"], fx["prcp"]

        parts = [f"{emo} {L('tomorrow')}: {desc}"]
        if (tmin is not None) and (tmax is not None):
            parts.append(f"{int(round(tmin))}–{int(round(tmax))}°C")
        if (wdir is not None) and (wspd is not None):
            parts.append(f"{cls._dir_to_compass(float(wdir))}{int(round(wspd))}km/h")
        if prcp is not None:
            mm = f"{prcp:.1f}".rstrip("0").rstrip(".")
            parts.append(f"{mm}mm")

        txt = " ".join(parts)
        if len(txt) <= 135:
            return txt

        short = " ".join(p for p in parts if "km/h" not in p)
        if len(short) <= 135:
            return short

        reduced = " ".join([f"{emo} {L('tomorrow')}:", f"{int(round(tmin))}–{int(round(tmax))}°C"]
                           + ([f"{mm}mm"] if prcp is not None else []))
        if len(reduced) <= 135:
            return reduced

        return f"{emo} {L('tomorrow')}: {int(round(tmin))}–{int(round(tmax))}°C"


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

        # Ignore own messages (very simple heuristic to avoid loops)
        if "ack" in clean_text.lower() and "bot" in clean_text.lower():
            return

        # Rate limiting logic
        if not self.logic.should_process(sender, text):
            return

        logger.info(f"*** TRIGGER [{sender}] ({matched_trigger}): {clean_text} ***")

        # Generate Reply
        if matched_trigger == "weather_now":
            info = WeatherModule.get_info(CONFIG["weather"]["file_path"])
            reply_text = f"ACK {info}"

        elif matched_trigger == "weather_tomorrow":
            lat = CONFIG["location"]["lat"]
            lon = CONFIG["location"]["lon"]
            loop = asyncio.get_running_loop()
            reply = await loop.run_in_executor(None, WXForecast.format_135_chars, lat, lon, CONFIG.get("lang", "pl"))
            reply_text = f"ACK {reply}"

        elif matched_trigger == "solar":
            info = SolarModule.get_info()
            reply_text = f"ACK {info}"

        elif matched_trigger == "info":
            reply_text = "ACK Repo: https://github.com/Arthua1/meshcore-bot"

        elif matched_trigger == "help":
            reply_text = L("help_text")

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
