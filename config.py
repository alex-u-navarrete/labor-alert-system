"""
Configuration — loads and validates all environment variables.
All other modules import a shared Config instance from here.
"""

import os
import pytz


class Config:
    """Single source of truth for all runtime configuration."""

    BUSINESS_HOURS: dict = {
        0: ("09:30", "19:00"),  # Monday
        2: ("09:30", "19:00"),  # Wednesday
        3: ("09:30", "19:00"),  # Thursday
        4: ("09:30", "19:30"),  # Friday
        5: ("09:30", "19:30"),  # Saturday
        6: ("09:30", "19:30"),  # Sunday
    }

    DAY_NAMES: dict = {
        0: "Monday", 2: "Wednesday", 3: "Thursday",
        4: "Friday",  5: "Saturday",  6: "Sunday",
    }

    def __init__(self) -> None:
        # ── Required ──────────────────────────────────────────────────────────
        self.square_token    = self._require("SQUARE_ACCESS_TOKEN")
        self.square_location = self._require("SQUARE_LOCATION_ID")

        # ── Optional / defaulted ──────────────────────────────────────────────
        self.tz_name          = os.environ.get("TIMEZONE", "America/Los_Angeles")
        self.labor_threshold  = float(os.environ.get("LABOR_THRESHOLD_PCT", "33")) / 100
        self.escalation_hours = float(os.environ.get("ESCALATION_HOURS", "2"))
        self.tz               = pytz.timezone(self.tz_name)

        # ── SendGrid (all three required to enable email) ─────────────────────
        self.sg_key       = self._require("SENDGRID_API_KEY")
        self.email_from   = self._require("ALERT_EMAIL_FROM")
        _email_to_raw     = self._require("ALERT_EMAIL_TO")
        self.alert_emails = [e.strip() for e in _email_to_raw.split(",") if e.strip()]

        # ── Anthropic / Claude AI ─────────────────────────────────────────────
        self.anthropic_api_key  = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        self.claude_enabled     = bool(self.anthropic_api_key)

        # ── External signals ──────────────────────────────────────────────────
        self.ticketmaster_key   = os.environ.get("TICKETMASTER_API_KEY", "").strip()
        self.restaurant_lat     = float(os.environ.get("RESTAURANT_LAT", "34.0522"))
        self.restaurant_lng     = float(os.environ.get("RESTAURANT_LNG", "-118.2437"))

    @staticmethod
    def _require(name: str) -> str:
        val = os.environ.get(name, "").strip()
        if not val:
            raise RuntimeError(f"Required environment variable '{name}' is not set.")
        return val
