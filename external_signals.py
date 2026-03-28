"""
ExternalSignals — fetches weather, Salvadoran holidays, Latino LA events,
and computes the payday/rent cycle index.
All results are in-memory cached for 4 hours to avoid hammering free APIs.
"""

import logging
import time
from datetime import datetime, timedelta

import requests

from config import Config

log = logging.getLogger(__name__)

_CACHE: dict = {}
_CACHE_TTL = 4 * 3600  # 4 hours


def _cached(key: str, fn):
    now = time.monotonic()
    if key in _CACHE and now - _CACHE[key]["ts"] < _CACHE_TTL:
        return _CACHE[key]["val"]
    val = fn()
    _CACHE[key] = {"ts": now, "val": val}
    return val


# ── El Salvador public holidays ───────────────────────────────────────────────
# iCal feed — no API key needed
_ICAL_URL = (
    "https://calendar.google.com/calendar/ical/"
    "en.sv%23holiday%40group.v.calendar.google.com/public/basic.ics"
)


def _parse_ical_holidays(raw: str) -> list[dict]:
    holidays = []
    lines = raw.replace("\r\n", "\n").split("\n")
    event = {}
    for line in lines:
        if line == "BEGIN:VEVENT":
            event = {}
        elif line == "END:VEVENT":
            if "name" in event and "date" in event:
                holidays.append(event)
            event = {}
        elif line.startswith("SUMMARY:"):
            event["name"] = line[8:].strip()
        elif line.startswith("DTSTART;VALUE=DATE:"):
            raw_date = line.split(":")[1].strip()
            try:
                event["date"] = datetime.strptime(raw_date, "%Y%m%d").date()
            except ValueError:
                pass
    return holidays


def get_upcoming_holidays(config: Config, days: int = 10) -> list[dict]:
    """Returns Salvadoran holidays in the next N days."""
    def fetch():
        try:
            resp = requests.get(_ICAL_URL, timeout=10)
            resp.raise_for_status()
            return _parse_ical_holidays(resp.text)
        except Exception as exc:
            log.warning("Could not fetch holidays: %s", exc)
            return []

    all_holidays = _cached("holidays", fetch)
    today = datetime.now(config.tz).date()
    cutoff = today + timedelta(days=days)
    return [h for h in all_holidays if today <= h["date"] <= cutoff]


# ── Ticketmaster — Latino/Latin events near restaurant ────────────────────────

def get_upcoming_latino_events(config: Config, days: int = 7) -> list[dict]:
    """Returns Latino/Latin-classified events near the restaurant in the next N days."""
    if not config.ticketmaster_key:
        return []

    def fetch():
        try:
            today     = datetime.now(config.tz)
            end_date  = today + timedelta(days=days)
            params    = {
                "apikey":              config.ticketmaster_key,
                "latlong":             f"{config.restaurant_lat},{config.restaurant_lng}",
                "radius":              "10",
                "unit":                "miles",
                "classificationName":  "Latin",
                "startDateTime":       today.strftime("%Y-%m-%dT00:00:00Z"),
                "endDateTime":         end_date.strftime("%Y-%m-%dT23:59:59Z"),
                "size":                "5",
                "sort":                "date,asc",
            }
            resp = requests.get(
                "https://app.ticketmaster.com/discovery/v2/events.json",
                params=params, timeout=10,
            )
            resp.raise_for_status()
            data   = resp.json()
            events = data.get("_embedded", {}).get("events", [])
            return [
                {
                    "name":  e.get("name", ""),
                    "date":  e.get("dates", {}).get("start", {}).get("localDate", ""),
                    "venue": e.get("_embedded", {}).get("venues", [{}])[0].get("name", ""),
                }
                for e in events
            ]
        except Exception as exc:
            log.warning("Ticketmaster API error: %s", exc)
            return []

    return _cached("latino_events", fetch)


# ── Open-Meteo — today's weather forecast ─────────────────────────────────────
# Free, no API key required.

def get_weather_today(config: Config) -> dict | None:
    """Returns today's weather forecast for the restaurant location."""
    def fetch():
        try:
            params = {
                "latitude":       config.restaurant_lat,
                "longitude":      config.restaurant_lng,
                "daily":          "temperature_2m_max,temperature_2m_min,precipitation_sum",
                "temperature_unit": "fahrenheit",
                "timezone":       config.tz_name,
                "forecast_days":  2,
            }
            resp = requests.get("https://api.open-meteo.com/v1/forecast", params=params, timeout=10)
            resp.raise_for_status()
            data  = resp.json().get("daily", {})
            if not data or not data.get("time"):
                return None
            idx = 0  # today is index 0
            return {
                "high_f":  data["temperature_2m_max"][idx],
                "low_f":   data["temperature_2m_min"][idx],
                "rain_in": data["precipitation_sum"][idx],
            }
        except Exception as exc:
            log.warning("Open-Meteo error: %s", exc)
            return None

    return _cached("weather_today", fetch)


def get_weather_historical(config: Config, date_str: str) -> dict | None:
    """Returns actual weather for a past date (YYYY-MM-DD). No cache — called once per day."""
    try:
        params = {
            "latitude":         config.restaurant_lat,
            "longitude":        config.restaurant_lng,
            "daily":            "temperature_2m_max,temperature_2m_min,precipitation_sum",
            "temperature_unit": "fahrenheit",
            "timezone":         config.tz_name,
            "start_date":       date_str,
            "end_date":         date_str,
        }
        resp = requests.get("https://api.open-meteo.com/v1/archive", params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json().get("daily", {})
        if not data or not data.get("time"):
            return None
        return {
            "high_f":  data["temperature_2m_max"][0],
            "low_f":   data["temperature_2m_min"][0],
            "rain_in": data["precipitation_sum"][0],
        }
    except Exception as exc:
        log.warning("Open-Meteo historical error for %s: %s", date_str, exc)
        return None


# ── Payday / rent cycle index ─────────────────────────────────────────────────

def get_payday_context(config: Config) -> dict:
    """
    Returns the payday/rent cycle position for today.
    Based on common LA service-worker pay patterns (bi-weekly, semi-monthly).
    No external API — pure date math.
    """
    today    = datetime.now(config.tz)
    dom      = today.day  # day of month
    dow      = today.weekday()  # 0=Mon

    if dom <= 5:
        phase = "post_rent"
        label = "Start of month — rent just paid, customers feel flush. Historically your strongest spending window."
        index = 1.10  # ~10% above baseline
    elif 13 <= dom <= 17:
        phase = "mid_payday"
        label = "Mid-month payday window — bi-weekly pay lands here for many LA workers. Solid spending days."
        index = 1.05
    elif 25 <= dom <= 31:
        phase = "pre_rent"
        label = "Pre-rent squeeze (days 25–31) — discretionary spending tightens before the 1st. Typically your softest sales window."
        index = 0.88  # ~12% below baseline
    else:
        phase = "mid_month"
        label = "Mid-month baseline — no strong payday or rent effect today."
        index = 1.00

    return {"phase": phase, "label": label, "index": index, "day_of_month": dom}
