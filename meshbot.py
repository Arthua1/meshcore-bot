#!/usr/bin/env python3
"""
MeshCore Bot
Author: Marcin SP4IM
License: MIT
"""

import asyncio
import hashlib
import json
import logging
import math
import os
import signal
import sys
import tempfile
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

try:
    import reverse_geocode
except ImportError:
    reverse_geocode = None
    print("WARNING: 'reverse_geocode' library missing, country info will be skipped")

try:
    from meshcore import EventType, MeshCore
except ImportError:
    print("CRITICAL: 'meshcore' library missing")
    sys.exit(1)

try:
    from skyfield.api import EarthSatellite, load, wgs84
except ImportError:
    print("CRITICAL: 'skyfield' library missing")
    sys.exit(1)

CONFIG = {
    "serial_port": "/dev/mesh_radio",
    "baudrate": 115200,
    "channel": {
        "index": 1,
        "name": "testy",
        "scope": "pl-podlasie",
        "set_default_scope": True,
        "max_payload_bytes": 120,
    },
    "reply_interval": 2.0,
    "dedup_ttl": 300.0,
    "keep_alive_interval": 60.0,
    "show_signal": True,
    "command_timeout": 12.0,
    "message_queue_size": 100,
    "location": {
        "lat": 53.1325,
        "lon": 23.1688,
        "elevation_m": 150.0,
        "timezone": "Europe/Warsaw",
        "label": "PL",
    },
    "lang": "pl",
    "weather": {
        "enabled": True,
        "file_path": "/tmp/netatmo_data.py",
        "max_age_seconds": 900,
    },
    "solar": {
        "enabled": True,
        "url": "https://www.hamqsl.com/solarxml.php",
        "timeout": 8.0,
        "cache_seconds": 15 * 60,
    },
    "forecast": {
        "enabled": True,
        "url": "https://api.open-meteo.com/v1/forecast",
        "timeout": 8.0,
        "cache_seconds": 30 * 60,
    },
    "iss": {
        "enabled": True,
        "tle_url": (
            "https://celestrak.org/NORAD/elements/gp.php"
            "?CATNR=25544&FORMAT=TLE"
        ),
        "tle_file": "/var/lib/meshcore-bot/iss_25544.tle",
        "tle_refresh_seconds": 6 * 3600,
        "tle_warning_age_hours": 48,
        "tle_max_age_days": 10,
        "http_timeout": 10.0,
        "min_elevation_deg": 10.0,
        "prediction_hours": 72,
        "pass_cache_seconds": 15 * 60,
    },
    "triggers": {
        "basic": {"test", "ping"},
        "weather_now": {"pogoda", "weather"},
        "weather_tomorrow": {"prognoza", "weather_tomorrow"},
        "solar": {"solar", "warunki", "propa", "dx"},
        "iss_now": {"iss", "iss_gdzie"},
        "iss_pass": {"iss_przelot", "iss_pass", "iss_lacznosc"},
        "info": {"infobot"},
        "help": {"help", "pomoc"},
    },
}



def _merge_config(base: dict[str, Any], override: dict[str, Any]) -> None:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge_config(base[key], value)
        else:
            base[key] = value


def load_config_file() -> Optional[Path]:
    path = Path(
        os.environ.get("MESHBOT_CONFIG")
        or Path(__file__).resolve().with_name("config.json")
    )
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as handle:
        override = json.load(handle)
    if not isinstance(override, dict):
        raise ValueError(f"{path}: top-level JSON value must be an object")
    _merge_config(CONFIG, override)
    return path


logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
)
logger = logging.getLogger("MeshBot")

LANG_MAP = {
    "pl": {
        "tomorrow": "Jutro",
        "wx_err": "WX ERR",
        "help_text": (
            "CMD: test/ping, pogoda, prognoza, solar/warunki, "
            "infobot, iss, iss_przelot"
        ),
        "solar_day": "Dzien",
        "solar_night": "Noc",
        "solar_err": "ERR: HAMQSL niedostepny",
        "w_file_missing": "ERR: Brak pliku pogody",
        "w_file_stale": "ERR: Nieaktualne dane pogody",
        "w_data_err": "ERR: Bledne dane pogody",
        "wind": "Wiatr",
        "gust": "poryw",
        "iss_no_tle": "ISS ERR: brak danych orbitalnych",
        "iss_pos_err": "ISS ERR: obliczenie pozycji",
        "iss_pass_err": "ISS ERR: obliczenie przelotu",
        "iss_no_pass": "ISS: brak przelotu >={threshold:.0f}deg w {hours}h",
    },
    "en": {
        "tomorrow": "Tomorrow",
        "wx_err": "WX ERR",
        "help_text": (
            "CMD: test/ping, weather, weather_tomorrow, solar, "
            "infobot, iss, iss_pass"
        ),
        "solar_day": "Day",
        "solar_night": "Night",
        "solar_err": "ERR: HAMQSL unavailable",
        "w_file_missing": "ERR: Weather file missing",
        "w_file_stale": "ERR: Weather data stale",
        "w_data_err": "ERR: Weather data invalid",
        "wind": "Wind",
        "gust": "gust",
        "iss_no_tle": "ISS ERR: no orbital data",
        "iss_pos_err": "ISS ERR: position calculation",
        "iss_pass_err": "ISS ERR: pass calculation",
        "iss_no_pass": "ISS: no pass >={threshold:.0f}deg in {hours}h",
    },
}


def L(key: str) -> str:
    lang = CONFIG.get("lang", "pl")
    return LANG_MAP.get(lang, LANG_MAP["pl"]).get(key, key)


def truncate_utf8(text: str, max_bytes: int) -> str:
    text = " ".join(text.split())
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    encoded = encoded[:max_bytes]
    while encoded:
        try:
            return encoded.decode("utf-8").rstrip()
        except UnicodeDecodeError:
            encoded = encoded[:-1]
    return ""


def fetch_bytes(url: str, timeout: float, accept: str) -> tuple[bytes, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "MeshCoreBot/6.1 SP4IM",
            "Accept": accept,
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status}")
        return response.read(), response.headers


class SunCalc:
    @staticmethod
    def is_daytime(lat: float, lon: float) -> bool:
        try:
            now = datetime.now(timezone.utc)
            day = now.timetuple().tm_yday
            hour = now.hour + now.minute / 60.0 + now.second / 3600.0
            declination = 23.45 * math.sin(math.radians(360 / 365 * (day - 81)))
            value = -math.tan(math.radians(lat)) * math.tan(
                math.radians(declination)
            )
            if value <= -1:
                return True
            if value >= 1:
                return False
            hour_angle = math.degrees(math.acos(value))
            noon = 12.0 - lon / 15.0
            return noon - hour_angle / 15.0 <= hour <= noon + hour_angle / 15.0
        except Exception:
            logger.exception("Sun calculation failed")
            return False


class SolarModule:
    _cache: Optional[tuple[str, str, dict[str, list[str]]]] = None
    _cache_time = 0.0

    @classmethod
    def _fetch(cls) -> tuple[str, str, dict[str, list[str]]]:
        ttl = float(CONFIG["solar"].get("cache_seconds", 0))
        if cls._cache is not None and time.monotonic() - cls._cache_time < ttl:
            return cls._cache

        xml_data, _ = fetch_bytes(
            CONFIG["solar"]["url"],
            float(CONFIG["solar"]["timeout"]),
            "application/xml,text/xml",
        )
        root = ET.fromstring(xml_data)
        data = root.find("solardata")
        if data is None and root.tag == "solardata":
            data = root
        if data is None:
            raise ValueError("Missing solardata")

        sfi = (data.findtext("solarflux") or data.findtext("flux") or "?").strip()
        k_idx = (data.findtext("kindex") or data.findtext("k") or "?").strip()
        conditions: dict[str, list[str]] = {"day": [], "night": []}
        calculated = data.find("calculatedconditions")
        if calculated is not None:
            short = {"Poor": "P", "Fair": "F", "Good": "G"}
            for band in calculated.findall("band"):
                period = band.get("time")
                if period not in conditions:
                    continue
                name = (band.get("name") or "?").replace("m", "")
                value = (band.text or "?").strip()
                conditions[period].append(f"{name}:{short.get(value, value[:1])}")

        cls._cache = (sfi, k_idx, conditions)
        cls._cache_time = time.monotonic()
        return cls._cache

    @classmethod
    def get_info(cls) -> str:
        try:
            sfi, k_idx, conditions = cls._fetch()
            lat = float(CONFIG["location"]["lat"])
            lon = float(CONFIG["location"]["lon"])
            is_day = SunCalc.is_daytime(lat, lon)
            mode = L("solar_day") if is_day else L("solar_night")
            suffix = " ".join(conditions["day" if is_day else "night"])
            return f"SFI={sfi} K={k_idx} [{mode}] {suffix}".strip()
        except Exception:
            logger.exception("Solar fetch failed")
            return L("solar_err")


class WeatherModule:
    @staticmethod
    def degrees_to_cardinal(value: str) -> str:
        try:
            degrees = float(value) % 360.0
            directions = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
            return directions[int((degrees + 22.5) // 45) % 8]
        except (TypeError, ValueError):
            return "?"

    @staticmethod
    def get_info(filepath: str) -> str:
        path = Path(filepath)
        if not path.exists():
            return L("w_file_missing")
        try:
            max_age = float(CONFIG["weather"].get("max_age_seconds", 0))
            if max_age > 0 and time.time() - path.stat().st_mtime > max_age:
                return L("w_file_stale")

            data = {}
            with path.open("r", encoding="utf-8") as handle:
                for raw_line in handle:
                    line = raw_line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    data[key.strip()] = value.strip().strip("'\"")

            temp = data.get("outside_temp", "?")
            humidity = data.get("outside_Humidity", "?")
            pressure = data.get("outside_Pressure", "?")
            wind_speed = data.get("wind_speed", "?")
            wind_dir = WeatherModule.degrees_to_cardinal(data.get("wind_dir", "?"))
            wind_gust = data.get("wind_gust", "?")
            return (
                f"T={temp}C RH={humidity}% P={pressure}hPa "
                f"{L('wind')}={wind_speed}km/h {wind_dir} {L('gust')}={wind_gust}km/h"
            )
        except Exception:
            logger.exception("Weather parsing failed")
            return L("w_data_err")


class WXForecast:
    WMO_PL = {
        0: "Bezchmurnie",
        1: "Prawie bezchmurnie",
        2: "Czesciowe chmury",
        3: "Pochmurnie",
        45: "Mgla",
        48: "Mgla",
        51: "Mzawka",
        53: "Mzawka",
        55: "Silna mzawka",
        56: "Marznaca mzawka",
        57: "Marznaca mzawka",
        61: "Deszcz",
        63: "Deszcz",
        65: "Ulewny deszcz",
        66: "Marznacy deszcz",
        67: "Marznacy deszcz",
        71: "Snieg",
        73: "Snieg",
        75: "Obfity snieg",
        77: "Ziarnisty snieg",
        80: "Przelotny deszcz",
        81: "Przelotny deszcz",
        82: "Silne opady",
        85: "Przelotny snieg",
        86: "Przelotny snieg",
        95: "Burza",
        96: "Burza z gradem",
        99: "Burza z gradem",
    }

    WMO_EN = {
        0: "Clear",
        1: "Mostly clear",
        2: "Partly cloudy",
        3: "Cloudy",
        45: "Fog",
        48: "Fog",
        51: "Drizzle",
        53: "Drizzle",
        55: "Heavy drizzle",
        56: "Freezing drizzle",
        57: "Freezing drizzle",
        61: "Rain",
        63: "Rain",
        65: "Heavy rain",
        66: "Freezing rain",
        67: "Freezing rain",
        71: "Snow",
        73: "Snow",
        75: "Heavy snow",
        77: "Snow grains",
        80: "Showers",
        81: "Showers",
        82: "Heavy showers",
        85: "Snow showers",
        86: "Snow showers",
        95: "Thunderstorm",
        96: "Thunderstorm",
        99: "Thunderstorm",
    }

    @staticmethod
    def direction(degrees: float) -> str:
        directions = [
            "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
        ]
        return directions[int((degrees % 360) / 22.5 + 0.5) % 16]

    _cache: dict[tuple[Any, ...], tuple[float, str]] = {}

    @classmethod
    def get_tomorrow(cls, lat: float, lon: float, lang: str) -> str:
        today = datetime.now(ZoneInfo(CONFIG["location"]["timezone"])).date()
        key = (today, lat, lon, lang)
        cached = cls._cache.get(key)
        ttl = float(CONFIG["forecast"].get("cache_seconds", 0))
        if cached is not None and time.monotonic() - cached[0] < ttl:
            return cached[1]

        params = {
            "latitude": lat,
            "longitude": lon,
            "timezone": CONFIG["location"]["timezone"],
            "forecast_days": 2,
            "daily": ",".join(
                [
                    "weather_code",
                    "temperature_2m_max",
                    "temperature_2m_min",
                    "precipitation_sum",
                    "wind_speed_10m_max",
                    "wind_direction_10m_dominant",
                ]
            ),
        }
        url = f"{CONFIG['forecast']['url']}?{urlencode(params)}"
        try:
            body, headers = fetch_bytes(
                url,
                float(CONFIG["forecast"]["timeout"]),
                "application/json",
            )
            charset = headers.get_content_charset() or "utf-8"
            response = json.loads(body.decode(charset))
            daily = response.get("daily") or {}

            def tomorrow(key: str) -> Any:
                values = daily.get(key) or []
                return values[1] if len(values) > 1 else None

            code = tomorrow("weather_code")
            if code is None:
                return L("wx_err")

            descriptions = cls.WMO_PL if lang == "pl" else cls.WMO_EN
            description = descriptions.get(int(code), "Pogoda" if lang == "pl" else "Weather")
            tmin = tomorrow("temperature_2m_min")
            tmax = tomorrow("temperature_2m_max")
            precipitation = tomorrow("precipitation_sum")
            wind_speed = tomorrow("wind_speed_10m_max")
            wind_dir = tomorrow("wind_direction_10m_dominant")

            parts = [f"{L('tomorrow')}: {description}"]
            if tmin is not None and tmax is not None:
                parts.append(f"{round(float(tmin))}-{round(float(tmax))}C")
            if wind_speed is not None and wind_dir is not None:
                parts.append(
                    f"{cls.direction(float(wind_dir))}{round(float(wind_speed))}km/h"
                )
            if precipitation is not None:
                parts.append(f"{float(precipitation):g}mm")
            result = " ".join(parts)
            cls._cache = {key: (time.monotonic(), result)}
            return result
        except Exception:
            logger.exception("Forecast fetch failed")
            return L("wx_err")


class ISSModule:
    def __init__(self, config: dict[str, Any], location: dict[str, Any]):
        self.config = config
        self.location = location
        self.tle_path = Path(config["tle_file"])
        self.local_tz = ZoneInfo(location["timezone"])
        self.ts = load.timescale(builtin=True)
        self._satellite: Optional[EarthSatellite] = None
        self._satellite_mtime: Optional[int] = None
        self._refresh_lock = asyncio.Lock()
        self._calculation_lock = asyncio.Lock()
        self._pass_cache: Optional[str] = None
        self._pass_cache_time = 0.0

    def _file_age(self) -> Optional[float]:
        try:
            return max(0.0, time.time() - self.tle_path.stat().st_mtime)
        except FileNotFoundError:
            return None

    @staticmethod
    def _parse_tle(text: str) -> tuple[str, str, str]:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        line1 = next((line for line in lines if line.startswith("1 25544")), None)
        line2 = next((line for line in lines if line.startswith("2 25544")), None)
        if line1 is None or line2 is None:
            raise ValueError("Invalid ISS TLE")
        if len(line1) < 69 or len(line2) < 69:
            raise ValueError("Truncated ISS TLE")
        name_index = min(lines.index(line1), lines.index(line2)) - 1
        name = lines[name_index] if name_index >= 0 else "ISS (ZARYA)"
        return name, line1, line2

    def _download_tle(self) -> None:
        body, headers = fetch_bytes(
            self.config["tle_url"],
            float(self.config["http_timeout"]),
            "text/plain",
        )
        charset = headers.get_content_charset() or "ascii"
        name, line1, line2 = self._parse_tle(body.decode(charset))
        content = f"{name}\n{line1}\n{line2}\n"
        self.tle_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=".iss-tle-", dir=str(self.tle_path.parent), text=True
        )
        try:
            with os.fdopen(fd, "w", encoding="ascii") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o640)
            os.replace(temporary, self.tle_path)
        except Exception:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    async def refresh_if_needed(self, force: bool = False) -> bool:
        async with self._refresh_lock:
            age = self._file_age()
            if (
                not force
                and age is not None
                and age < float(self.config["tle_refresh_seconds"])
            ):
                return True
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(self._download_tle),
                    timeout=float(self.config["http_timeout"]) + 3.0,
                )
                logger.info("ISS TLE updated")
                return True
            except Exception:
                if self.tle_path.exists():
                    logger.exception("ISS TLE update failed; using cached data")
                    return True
                logger.exception("ISS TLE update failed")
                return False

    def _load_satellite(self) -> EarthSatellite:
        stat = self.tle_path.stat()
        if self._satellite is not None and self._satellite_mtime == stat.st_mtime_ns:
            return self._satellite
        name, line1, line2 = self._parse_tle(
            self.tle_path.read_text(encoding="ascii")
        )
        self._satellite = EarthSatellite(line1, line2, name, self.ts)
        self._satellite_mtime = stat.st_mtime_ns
        self._pass_cache = None
        return self._satellite

    def _check_age(self) -> None:
        age = self._file_age()
        if age is None:
            raise RuntimeError("Missing ISS TLE")
        if age > float(self.config["tle_max_age_days"]) * 86400.0:
            raise RuntimeError("ISS TLE too old")

    def _age_suffix(self) -> str:
        age = self._file_age()
        if age is None or age < float(self.config["tle_warning_age_hours"]) * 3600.0:
            return ""
        return f" TLE={age / 3600.0:.0f}h"

    def _position(self) -> str:
        self._check_age()
        satellite = self._load_satellite()
        subpoint = wgs84.subpoint(satellite.at(self.ts.now()))
        lat = subpoint.latitude.degrees
        lon = subpoint.longitude.degrees
        altitude = subpoint.elevation.km
        ns = "N" if lat >= 0 else "S"
        ew = "E" if lon >= 0 else "W"
        
        country_str = ""
        if reverse_geocode is not None:
            try:
                rg_result = reverse_geocode.search([(lat, lon)])
                if rg_result and isinstance(rg_result, list):
                    country_name = rg_result[0].get("country")
                    if country_name:
                        country_str = f" [{country_name}]"
            except Exception as e:
                logger.warning("Reverse geocode lookup failed: %s", e)

        return (
            f"ISS {abs(lat):.1f}{ns} {abs(lon):.1f}{ew}{country_str} h={altitude:.0f}km"
            f"{self._age_suffix()}"
        )

    async def get_position(self) -> str:
        if not await self.refresh_if_needed():
            return L("iss_no_tle")
        async with self._calculation_lock:
            try:
                return await asyncio.to_thread(self._position)
            except Exception:
                logger.exception("ISS position calculation failed")
                return L("iss_pos_err")

    @staticmethod
    def _compass(azimuth: float) -> str:
        directions = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
        return directions[int((azimuth % 360 + 22.5) // 45) % 8]

    def _next_pass(self) -> str:
        self._check_age()
        satellite = self._load_satellite()
        observer = wgs84.latlon(
            latitude_degrees=float(self.location["lat"]),
            longitude_degrees=float(self.location["lon"]),
            elevation_m=float(self.location.get("elevation_m", 0.0)),
        )
        now = datetime.now(timezone.utc)
        search_start = now - timedelta(minutes=15)
        search_end = now + timedelta(hours=float(self.config["prediction_hours"]))
        threshold = float(self.config["min_elevation_deg"])
        times, events = satellite.find_events(
            observer,
            self.ts.from_datetime(search_start),
            self.ts.from_datetime(search_end),
            altitude_degrees=threshold,
        )

        passes = []
        current: dict[str, Any] = {}
        for event_time, event_code in zip(times, events):
            local_time = event_time.utc_datetime().astimezone(self.local_tz)
            topocentric = (satellite - observer).at(event_time)
            altitude, azimuth, _ = topocentric.altaz()
            if event_code == 0:
                current = {"aos": local_time, "aos_az": azimuth.degrees}
            elif event_code == 1 and current:
                current.update(
                    {
                        "tca": local_time,
                        "max_el": altitude.degrees,
                        "tca_az": azimuth.degrees,
                    }
                )
            elif event_code == 2 and current:
                current.update({"los": local_time, "los_az": azimuth.degrees})
                if {"aos", "tca", "los"}.issubset(current):
                    passes.append(current)
                current = {}

        now_local = now.astimezone(self.local_tz)
        selected = next((item for item in passes if item["los"] > now_local), None)
        if selected is None:
            return L("iss_no_pass").format(
                threshold=threshold, hours=self.config["prediction_hours"]
            )

        aos = selected["aos"]
        tca = selected["tca"]
        los = selected["los"]
        rise = self._compass(selected["aos_az"])
        setting = self._compass(selected["los_az"])
        max_el = round(selected["max_el"])
        label = str(self.location.get("label", "")).strip()
        label = f" {label}" if label else ""
        return (
            f"ISS {aos:%d.%m} {aos:%H:%M}-{los:%H:%M}{label} "
            f"max={max_el}deg {rise}>{setting} TCA={tca:%H:%M}"
            f"{self._age_suffix()}"
        )

    async def get_next_pass(self) -> str:
        now = time.monotonic()
        if (
            self._pass_cache is not None
            and now - self._pass_cache_time < float(self.config["pass_cache_seconds"])
        ):
            return self._pass_cache
        if not await self.refresh_if_needed():
            return L("iss_no_tle")
        async with self._calculation_lock:
            now = time.monotonic()
            if (
                self._pass_cache is not None
                and now - self._pass_cache_time
                < float(self.config["pass_cache_seconds"])
            ):
                return self._pass_cache
            try:
                result = await asyncio.to_thread(self._next_pass)
            except Exception:
                logger.exception("ISS pass calculation failed")
                return L("iss_pass_err")
            self._pass_cache = result
            self._pass_cache_time = time.monotonic()
            return result


class BotLogic:
    def __init__(self) -> None:
        self.last_reply: dict[str, float] = {}
        self.seen: dict[str, float] = {}

    def should_process(self, sender: str, signature: str) -> bool:
        now = time.monotonic()
        ttl = float(CONFIG["dedup_ttl"])
        self.seen = {key: value for key, value in self.seen.items() if now - value < ttl}
        if signature in self.seen:
            return False
        self.seen[signature] = now
        last = self.last_reply.get(sender)
        return last is None or now - last >= float(CONFIG["reply_interval"])

    def mark_replied(self, sender: str) -> None:
        self.last_reply[sender] = time.monotonic()


class MeshBot:
    def __init__(self) -> None:
        self.logic = BotLogic()
        self.meshcore: Optional[MeshCore] = None
        self.iss = ISSModule(CONFIG["iss"], CONFIG["location"])
        self.queue: asyncio.Queue[Any] = asyncio.Queue(
            maxsize=int(CONFIG["message_queue_size"])
        )
        self.send_lock = asyncio.Lock()
        self.stop_event = asyncio.Event()
        self.disconnected = asyncio.Event()
        self.tasks: list[asyncio.Task[Any]] = []
        self.own_name = ""
        self.trigger_map = {
            word.casefold(): category
            for category, words in CONFIG["triggers"].items()
            for word in words
        }

    async def start(self) -> None:
        logger.info("MeshCore Bot v6.1 starting")
        try:
            self.meshcore = await MeshCore.create_serial(
                CONFIG["serial_port"],
                int(CONFIG["baudrate"]),
                debug=False,
                auto_reconnect=False,
            )
            if self.meshcore is None:
                raise RuntimeError("MeshCore connection failed")

            self_info = self.meshcore.self_info or {}
            self.own_name = str(self_info.get("name") or self_info.get("adv_name") or "")
            logger.info("Connected as %s", self.own_name or "?")
            self.meshcore.subscribe(EventType.DISCONNECTED, self._on_disconnect)
            await self._configure_scope()
            await self._discard_pending_messages()
            self.meshcore.subscribe(
                EventType.CHANNEL_MSG_RECV,
                self._on_message,
                attribute_filters={"channel_idx": int(CONFIG["channel"]["index"])},
            )
            await self.meshcore.start_auto_message_fetching()

            self.tasks = [
                asyncio.create_task(self._message_worker(), name="message-worker"),
                asyncio.create_task(self._watchdog(), name="watchdog"),
            ]
            if CONFIG["iss"]["enabled"]:
                self.tasks.append(
                    asyncio.create_task(self._iss_refresh_loop(), name="iss-refresh")
                )

            logger.info(
                "Listening on channel %s (%s), scope=%s",
                CONFIG["channel"]["index"],
                CONFIG["channel"]["name"],
                CONFIG["channel"]["scope"],
            )

            stop_task = asyncio.create_task(self.stop_event.wait(), name="stop-wait")
            done, _ = await asyncio.wait(
                [stop_task, *self.tasks], return_when=asyncio.FIRST_COMPLETED
            )
            if stop_task not in done:
                for task in done:
                    if task.cancelled():
                        continue
                    error = task.exception()
                    if error is not None:
                        raise error
                    raise RuntimeError(f"Task stopped unexpectedly: {task.get_name()}")
        finally:
            await self._shutdown()

    async def _configure_scope(self) -> None:
        if not CONFIG["channel"].get("set_default_scope"):
            return
        scope = str(CONFIG["channel"].get("scope", "")).strip()
        if not scope:
            return
        command = getattr(self.meshcore.commands, "set_flood_scope", None)
        if command is None:
            logger.warning("set_flood_scope not supported by installed meshcore")
            return
        try:
            result = await asyncio.wait_for(
                command(scope), timeout=float(CONFIG["command_timeout"])
            )
            if result.type == EventType.ERROR:
                logger.warning("Default scope failed: %s", result.payload)
            else:
                logger.info("Default flood scope set to %s", scope)
        except Exception:
            logger.exception("Default scope configuration failed")

    async def _discard_pending_messages(self) -> None:
        """Drop messages queued on the companion while the bot was offline.

        Comparing sender timestamps with the boot time is unreliable, because
        many nodes have no GPS or synced clock, so the backlog is drained
        before the message handler is subscribed instead.
        """
        discarded = 0
        for _ in range(500):
            result = await self.meshcore.commands.get_msg(
                timeout=float(CONFIG["command_timeout"])
            )
            if result.type in (EventType.NO_MORE_MSGS, EventType.ERROR):
                break
            discarded += 1
        if discarded:
            logger.info("Discarded %d queued message(s) from before startup", discarded)

    async def _on_disconnect(self, event: Any) -> None:
        logger.error("MeshCore disconnected: %s", event.payload)
        self.disconnected.set()

    async def _shutdown(self) -> None:
        for task in self.tasks:
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.meshcore is not None:
            try:
                await self.meshcore.disconnect()
            except Exception:
                logger.exception("Disconnect failed")
        logger.info("MeshCore Bot stopped")

    def request_stop(self) -> None:
        self.stop_event.set()

    async def _watchdog(self) -> None:
        while True:
            try:
                await asyncio.wait_for(
                    self.disconnected.wait(),
                    timeout=float(CONFIG["keep_alive_interval"]),
                )
            except asyncio.TimeoutError:
                pass
            if (
                self.disconnected.is_set()
                or self.meshcore is None
                or not self.meshcore.is_connected
            ):
                raise RuntimeError("MeshCore disconnected")
            result = await asyncio.wait_for(
                self.meshcore.commands.send_device_query(),
                timeout=float(CONFIG["command_timeout"]),
            )
            if result.type == EventType.ERROR:
                raise RuntimeError(f"Watchdog error: {result.payload}")

    async def _iss_refresh_loop(self) -> None:
        while True:
            await self.iss.refresh_if_needed()
            await asyncio.sleep(15 * 60)

    async def _on_message(self, event: Any) -> None:
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning("Message queue full; event dropped")

    async def _message_worker(self) -> None:
        while True:
            event = await self.queue.get()
            try:
                await self._process_message(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Message processing failed")
            finally:
                self.queue.task_done()

    @staticmethod
    def _parse_sender_and_text(raw_text: str) -> tuple[str, str]:
        text = raw_text.strip()
        if ":" not in text:
            return "Unknown", text
        sender, body = text.split(":", 1)
        sender = sender.strip()
        if not sender or len(sender.encode("utf-8")) > 64:
            return "Unknown", text
        return sender, body.strip()

    @staticmethod
    def _message_signature(msg: dict[str, Any], sender: str, text: str) -> str:
        raw = "\0".join(
            [
                str(msg.get("channel_idx", "")),
                sender,
                text,
                str(msg.get("sender_timestamp") or msg.get("timestamp") or ""),
            ]
        )
        return hashlib.blake2s(raw.encode("utf-8"), digest_size=12).hexdigest()

    def _match_trigger(self, text: str) -> Optional[str]:
        first_word = text.split(maxsplit=1)[0].casefold().lstrip("/")
        first_word = first_word.rstrip(".,?!:;")
        return self.trigger_map.get(first_word)

    @staticmethod
    def _signal_info(msg: dict[str, Any]) -> str:
        parts = []
        path_len = msg.get("path_len")
        if isinstance(path_len, int) and 0 <= path_len < 255:
            parts.append(f"hops={path_len}")
        snr = msg.get("SNR")
        if isinstance(snr, (int, float)):
            parts.append(f"SNR={snr:g}dB")
        return " ".join(parts)

    async def _process_message(self, event: Any) -> None:
        msg = event.payload or {}
        if msg.get("channel_idx") != CONFIG["channel"]["index"]:
            return

        sender, clean_text = self._parse_sender_and_text(str(msg.get("text", "")))
        if not clean_text:
            return
        if self.own_name and sender.casefold() == self.own_name.casefold():
            return

        trigger = self._match_trigger(clean_text)
        if trigger is None:
            return

        signature = self._message_signature(msg, sender, clean_text)
        if not self.logic.should_process(sender, signature):
            return

        logger.info("Trigger %s from %s: %s", trigger, sender, clean_text)
        reply = await self._build_reply(trigger, sender, msg)
        if await self._send_reply(reply):
            self.logic.mark_replied(sender)

    async def _build_reply(self, trigger: str, sender: str, msg: dict[str, Any]) -> str:
        if trigger == "weather_now":
            if not CONFIG["weather"]["enabled"]:
                return "ACK WX disabled"
            info = await asyncio.to_thread(
                WeatherModule.get_info, CONFIG["weather"]["file_path"]
            )
            return f"ACK {info}"

        if trigger == "weather_tomorrow":
            if not CONFIG["forecast"]["enabled"]:
                return "ACK WX forecast disabled"
            info = await asyncio.to_thread(
                WXForecast.get_tomorrow,
                float(CONFIG["location"]["lat"]),
                float(CONFIG["location"]["lon"]),
                CONFIG.get("lang", "pl"),
            )
            return f"ACK {info}"

        if trigger == "solar":
            if not CONFIG["solar"]["enabled"]:
                return "ACK Solar disabled"
            return f"ACK {await asyncio.to_thread(SolarModule.get_info)}"

        if trigger == "iss_now":
            if not CONFIG["iss"]["enabled"]:
                return "ACK ISS disabled"
            return f"ACK {await self.iss.get_position()}"

        if trigger == "iss_pass":
            if not CONFIG["iss"]["enabled"]:
                return "ACK ISS disabled"
            return f"ACK {await self.iss.get_next_pass()}"

        if trigger == "info":
            return "ACK Repo: https://github.com/Arthua1/meshcore-bot"

        if trigger == "help":
            return L("help_text")

        local_time = datetime.now(ZoneInfo(CONFIG["location"]["timezone"]))
        reply = f"ACK - {sender} - {local_time:%H:%M}"
        signal_info = self._signal_info(msg) if CONFIG.get("show_signal") else ""
        return f"{reply} - {signal_info}" if signal_info else reply

    async def _send_reply(self, text: str) -> bool:
        if self.meshcore is None or not self.meshcore.is_connected:
            logger.error("Cannot send reply: MeshCore disconnected")
            return False
        text = truncate_utf8(text, int(CONFIG["channel"]["max_payload_bytes"]))
        async with self.send_lock:
            try:
                result = await asyncio.wait_for(
                    self.meshcore.commands.send_chan_msg(
                        int(CONFIG["channel"]["index"]), text
                    ),
                    timeout=float(CONFIG["command_timeout"]),
                )
            except Exception:
                logger.exception("Reply send failed")
                return False
        if result.type == EventType.ERROR:
            logger.error("MeshCore send error: %s", result.payload)
            return False
        logger.info("Reply sent: %s", text)
        return True


async def async_main() -> None:
    config_path = load_config_file()
    if config_path is not None:
        logger.info("Loaded configuration from %s", config_path)
    bot = MeshBot()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, bot.request_stop)
    await bot.start()


if __name__ == "__main__":
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        pass
    except Exception:
        logger.critical("Fatal error", exc_info=True)
        sys.exit(1)
